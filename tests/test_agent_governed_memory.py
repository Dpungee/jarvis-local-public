from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from jarvis.agent import Agent, AgentRunCancelled
from jarvis.attachments import ImageAttachment
from jarvis.config import Config
from jarvis.memory import Memory


def _command(subject: str, predicate: str, value: str) -> str:
    return "Remember this project fact: " + json.dumps(
        {"subject": subject, "predicate": predicate, "value": value},
        ensure_ascii=False,
        separators=(",", ":"),
    )


class NoModelClient:
    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []

    def models(self, refresh: bool = True) -> list[str]:
        del refresh
        return ["qwen3.5:9b", "gpt-oss:20b", "qwen3-coder:30b"]

    def chat(self, *args: object, **kwargs: object) -> object:
        self.requests.append({"args": args, "kwargs": kwargs})
        raise AssertionError("Governed project memory must not call a model")


class ModelResponse(dict):
    def __init__(self, content: str) -> None:
        super().__init__(role="assistant", content=content)
        self.done_reason = None
        self.done = True


class CapturingModelClient(NoModelClient):
    def chat(self, *args: object, **kwargs: object) -> object:
        self.requests.append({"args": args, "kwargs": kwargs})
        return ModelResponse("I used the bounded project context.")


class AgentGovernedProjectMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        workspace = root / "workspace"
        data_dir = root / "data"
        workspace.mkdir()
        data_dir.mkdir()
        self.config = replace(
            Config.load(),
            autonomy="autonomous",
            workspace=workspace,
            data_dir=data_dir,
            model="auto",
            fast_model="qwen3.5:9b",
            reasoning_model="gpt-oss:20b",
            coding_model="qwen3-coder:30b",
            ollama_preload=False,
            vault_dir=None,
            memory_embeddings="disabled",
        )
        self.memory = Memory(data_dir / "agent.db")

    def tearDown(self) -> None:
        self.memory.close()
        self.temp.cleanup()

    def _agent(
        self,
        *,
        config: Config | None = None,
        client: NoModelClient | None = None,
    ) -> tuple[Agent, NoModelClient]:
        client = client or NoModelClient()
        agent = Agent(
            config or self.config,
            self.memory,
            client=client,
            coding_review=False,
            coding_planning=False,
        )
        return agent, client

    def test_foreground_command_writes_once_without_model_or_tool(self) -> None:
        agent, client = self._agent()
        result = agent.run(_command("AtlasNode", "release channel", "canary"))

        self.assertEqual(result.status, "complete", result.reason)
        self.assertEqual(result.tool_calls, 0)
        self.assertEqual(result.metrics.get("model_attempts"), 0)
        self.assertEqual(client.requests, [])
        self.assertIn("claim record #", str(result))
        claim = self.memory.db.execute(
            """SELECT scope, subject, predicate, value, authority, confidence, status
               FROM memory_claims"""
        ).fetchone()
        self.assertEqual(
            tuple(claim),
            (
                "project:1",
                "AtlasNode",
                "release channel",
                "canary",
                "operator",
                1.0,
                "active",
            ),
        )
        roles = self.memory.db.execute(
            "SELECT role FROM messages WHERE conversation_id=? ORDER BY id",
            (result.conversation_id,),
        ).fetchall()
        self.assertEqual([row["role"] for row in roles], ["user", "assistant"])

    def test_reassert_and_supersede_return_deterministic_receipts(self) -> None:
        agent, client = self._agent()
        created = agent.run(_command("AtlasNode", "release channel", "canary"))
        reasserted = agent.run(
            _command("AtlasNode", "release channel", "canary"),
            conversation_id=created.conversation_id,
        )
        updated = agent.run(
            _command("AtlasNode", "release channel", "stable"),
            conversation_id=created.conversation_id,
        )

        self.assertIn("Stored project fact", str(created))
        self.assertIn("Reasserted project fact", str(reasserted))
        self.assertIn("Updated project fact", str(updated))
        self.assertIn("prior value remains", str(updated).casefold())
        self.assertEqual(client.requests, [])
        rows = self.memory.db.execute(
            """SELECT value, status FROM memory_claims
               WHERE scope='project:1' ORDER BY id"""
        ).fetchall()
        self.assertEqual(
            [(row["value"], row["status"]) for row in rows],
            [("canary", "superseded"), ("stable", "active")],
        )

    def test_confirmation_does_not_echo_fact_or_misreport_future_tense(self) -> None:
        agent, client = self._agent()
        value = "I will finish tomorrow"
        result = agent.run(_command("Atlas milestone", "status note", value))

        self.assertEqual(result.status, "complete", result.reason)
        self.assertIn("Stored project fact", str(result))
        self.assertNotIn(value, str(result))
        self.assertEqual(client.requests, [])
        stored = self.memory.db.execute(
            """SELECT value, status FROM memory_claims
               WHERE scope='project:1'"""
        ).fetchone()
        self.assertEqual((stored["value"], stored["status"]), (value, "active"))

    def test_cancellation_is_honored_before_write_but_not_after_commit(self) -> None:
        agent, client = self._agent()
        before_calls = 0

        def cancel_before_write() -> bool:
            nonlocal before_calls
            before_calls += 1
            return before_calls >= 2

        with self.assertRaises(AgentRunCancelled):
            agent.run(
                _command("CancelBefore", "state", "ready"),
                cancellation_guard=cancel_before_write,
            )
        self.assertEqual(
            self.memory.db.execute("SELECT COUNT(*) FROM memory_claims").fetchone()[0],
            0,
        )

        after_calls = 0

        def cancel_after_commit_boundary() -> bool:
            nonlocal after_calls
            after_calls += 1
            return after_calls >= 3

        result = agent.run(
            _command("CancelAfter", "state", "ready"),
            cancellation_guard=cancel_after_commit_boundary,
        )
        self.assertEqual(result.status, "complete", result.reason)
        self.assertIn("claim record #", str(result))
        self.assertEqual(after_calls, 2)
        self.assertEqual(client.requests, [])
        self.assertEqual(
            self.memory.db.execute("SELECT COUNT(*) FROM memory_claims").fetchone()[0],
            1,
        )

    def test_raising_event_sink_cannot_hide_committed_receipt(self) -> None:
        agent, client = self._agent()
        events = 0

        def raising_sink(_message: str) -> None:
            nonlocal events
            events += 1
            # The first event is emitted before the write. Raise only on the
            # post-commit governed-memory event to exercise the receipt seam.
            if events >= 2:
                raise RuntimeError("event sink unavailable")

        agent.on_event = raising_sink
        result = agent.run(_command("EventProbe", "state", "ready"))

        self.assertEqual(result.status, "complete", result.reason)
        self.assertIn("claim record #", str(result))
        self.assertEqual(client.requests, [])
        self.assertEqual(
            self.memory.db.execute("SELECT COUNT(*) FROM memory_claims").fetchone()[0],
            1,
        )

    def test_generic_assistant_message_failure_cannot_hide_committed_receipt(self) -> None:
        agent, client = self._agent()

        with patch.object(
            self.memory,
            "add_message",
            side_effect=RuntimeError("generic assistant persistence unavailable"),
        ) as add_message:
            result = agent.run(_command("ReceiptProbe", "state", "ready"))

        self.assertEqual(result.status, "complete", result.reason)
        self.assertIn("claim record #", str(result))
        self.assertEqual(client.requests, [])
        add_message.assert_not_called()
        rows = self.memory.db.execute(
            """SELECT role, content FROM messages
               WHERE conversation_id=? ORDER BY id""",
            (result.conversation_id,),
        ).fetchall()
        self.assertEqual([row["role"] for row in rows], ["user", "assistant"])
        self.assertEqual(rows[-1]["content"], str(result))

    def test_mutation_capable_turn_suppresses_prompt_memory(self) -> None:
        conversation = self.memory.new_conversation(project_id=1)
        self.memory.remember_explicit_project_claim(
            conversation,
            1,
            _command("AtlasNode", "release channel", "stable"),
        )
        agent, _client = self._agent()

        with patch.object(
            agent, "system_prompt", wraps=agent.system_prompt
        ) as prompt_spy:
            with self.assertRaisesRegex(AssertionError, "must not call a model"):
                agent.run("Write a plain text note named status.txt containing hello.")

        self.assertTrue(prompt_spy.called)
        self.assertIs(prompt_spy.call_args.kwargs["include_memory"], False)

    def test_schedule_mutation_suppresses_project_memory_and_still_exposes_schedule(
        self,
    ) -> None:
        conversation = self.memory.new_conversation(project_id=1)
        self.memory.remember_explicit_project_claim(
            conversation,
            1,
            _command("Backup", "preferred cadence", "quartz-73"),
        )
        agent, client = self._agent()

        with patch.object(
            agent, "system_prompt", wraps=agent.system_prompt
        ) as prompt_spy:
            with self.assertRaisesRegex(AssertionError, "must not call a model"):
                agent.run("Schedule a Backup preferred cadence task every 1 minute.")

        self.assertTrue(prompt_spy.called)
        self.assertIs(prompt_spy.call_args.kwargs["include_memory"], False)
        request_text = json.dumps(client.requests, ensure_ascii=False, default=str)
        self.assertNotIn("quartz-73", request_text)
        self.assertIn("schedule_create", request_text)

    def test_malformed_or_sensitive_command_never_falls_through_or_persists(self) -> None:
        agent, client = self._agent()
        secret = "sk-proj-abcdefghijklmnop"
        prompts = (
            "Remember this project fact: not-json",
            _command("AtlasNode", "release channel", secret),
            "Remember this project fa\u200bct: "
            '{"subject":"AtlasNode","predicate":"state","value":"ready"}',
            "Can you remember this project fact: "
            '{"subject":"AtlasNode","predicate":"state","value":"ready"}',
            "Please, remember this project fact: "
            '{"subject":"AtlasNode","predicate":"state","value":"ready"}',
            "Remember this proјect fact: "
            '{"subject":"AtlasNode","predicate":"state","value":"ready"}',
        )
        for prompt in prompts:
            with self.subTest(prompt=prompt[:40]):
                result = agent.run(prompt)
                self.assertEqual(result.status, "incomplete")
                self.assertIn("Not stored:", str(result))
        self.assertEqual(client.requests, [])
        self.assertEqual(
            self.memory.db.execute("SELECT COUNT(*) FROM memory_claims").fetchone()[0],
            0,
        )
        durable_text = "\n".join(
            str(row[0])
            for table, column in (("conversations", "title"), ("messages", "content"))
            for row in self.memory.db.execute(f"SELECT {column} FROM {table}").fetchall()
        )
        self.assertNotIn(secret, durable_text)
        self.assertNotIn("not-json", durable_text)

    def test_ordinary_project_fact_discussion_reaches_normal_dialogue(self) -> None:
        client = CapturingModelClient()
        agent, _client = self._agent(client=client)

        result = agent.run(
            "Can you remember the project fact I mentioned yesterday?"
        )

        self.assertEqual(result.status, "complete", result.reason)
        self.assertNotIn("Not stored:", str(result))
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(
            self.memory.db.execute("SELECT COUNT(*) FROM memory_claims").fetchone()[0],
            0,
        )

    def test_readonly_background_specialist_and_attachment_modes_are_blocked(self) -> None:
        cases: list[tuple[str, Agent, dict[str, object], NoModelClient]] = []

        readonly_agent, readonly_client = self._agent(
            config=replace(self.config, autonomy="readonly")
        )
        cases.append(("readonly", readonly_agent, {}, readonly_client))

        background_agent, background_client = self._agent()
        cases.append((
            "background",
            background_agent,
            {"prediction_origin": "proactive"},
            background_client,
        ))

        specialist_agent, specialist_client = self._agent()
        specialist_agent.set_specialist("research")
        cases.append(("specialist", specialist_agent, {}, specialist_client))

        attachment_agent, attachment_client = self._agent()
        image = ImageAttachment("image/png", b"\x89PNG\r\n\x1a\n", "probe.png")
        cases.append((
            "attachment",
            attachment_agent,
            {"attachments": [image]},
            attachment_client,
        ))

        for label, agent, arguments, client in cases:
            with self.subTest(mode=label):
                result = agent.run(
                    _command(f"{label}Probe", "state", "ready"),
                    **arguments,
                )
                self.assertEqual(result.status, "incomplete")
                self.assertIn("Not stored:", str(result))
                self.assertEqual(client.requests, [])
        self.assertEqual(
            self.memory.db.execute("SELECT COUNT(*) FROM memory_claims").fetchone()[0],
            0,
        )

    def test_system_prompt_reads_only_global_and_current_project_claims(self) -> None:
        project_two = self.memory.add_project("Second", "@projects/second")
        self.memory.remember_claim(
            "AtlasNode",
            "release channel",
            "global-preview",
            source="verified global fixture",
            authority="verified",
        )
        first_conversation = self.memory.new_conversation(project_id=1)
        second_conversation = self.memory.new_conversation(project_id=project_two)
        self.memory.remember_explicit_project_claim(
            first_conversation,
            1,
            _command("AtlasNode", "release channel", "canary"),
        )
        self.memory.remember_explicit_project_claim(
            second_conversation,
            project_two,
            _command("AtlasNode", "release channel", "stable"),
        )
        agent, _client = self._agent()

        agent._active_project_id = 1
        first_prompt = agent.system_prompt("AtlasNode release channel")
        old_value_prompt = agent.system_prompt("global-preview")
        agent._active_project_id = project_two
        second_prompt = agent.system_prompt("AtlasNode release channel")
        with agent.toolbox.agent_context(1, conversation_id=first_conversation):
            project_recall = agent.toolbox.recall("AtlasNode global-preview")

        self.assertIn("canary", first_prompt)
        self.assertNotIn("stable", first_prompt)
        self.assertNotIn("global-preview", first_prompt)
        self.assertNotIn("global-preview", old_value_prompt)
        self.assertIn("stable", second_prompt)
        self.assertNotIn("canary", second_prompt)
        self.assertNotIn("global-preview", second_prompt)
        self.assertEqual(project_recall, [])

    def test_agent_run_places_only_current_project_claim_in_model_messages(self) -> None:
        project_two = self.memory.add_project("Second", "@projects/second")
        self.memory.remember_claim(
            "AtlasNode",
            "release channel",
            "global-preview",
            source="verified global fixture",
            authority="verified",
        )
        first_conversation = self.memory.new_conversation(project_id=1)
        second_conversation = self.memory.new_conversation(project_id=project_two)
        self.memory.remember_explicit_project_claim(
            first_conversation,
            1,
            _command("AtlasNode", "release channel", "canary"),
        )
        self.memory.remember_explicit_project_claim(
            second_conversation,
            project_two,
            _command("AtlasNode", "release channel", "stable"),
        )
        client = CapturingModelClient()
        agent, _client = self._agent(client=client)

        result = agent.run(
            "What is the AtlasNode release channel?",
            conversation_id=first_conversation,
        )

        self.assertEqual(result.status, "complete", result.reason)
        self.assertEqual(len(client.requests), 1)
        request_text = json.dumps(
            client.requests[0], ensure_ascii=False, default=str
        )
        self.assertIn("<jarvis_runtime_dialogue_context>", request_text)
        self.assertIn("canary", request_text)
        self.assertNotIn("stable", request_text)
        self.assertNotIn("global-preview", request_text)


if __name__ == "__main__":
    unittest.main()
