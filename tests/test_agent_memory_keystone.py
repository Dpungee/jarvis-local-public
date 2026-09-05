from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import jarvis.agent as agent_module
import jarvis.memory as memory_module
from jarvis import memory_graph
from jarvis.agent import (
    Agent,
    _CHAIN_LEAD_CLAUSE,
    _CONFIGURED_VALUE_WORDS,
    _LANE_ABSTAINED_CLAUSE,
)
from jarvis.cli import _display_project_facts
from jarvis.config import Config
from jarvis.memory import Memory


def _command(subject: str, predicate: str, value: str) -> str:
    return "Remember this project fact: " + json.dumps(
        {"subject": subject, "predicate": predicate, "value": value},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _forget(subject: str, predicate: str) -> str:
    return "Forget this project fact: " + json.dumps(
        {"subject": subject, "predicate": predicate},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _erase(subject: str, predicate: str) -> str:
    return "Erase this project fact: " + json.dumps(
        {"subject": subject, "predicate": predicate},
        ensure_ascii=False,
        separators=(",", ":"),
    )


GRAPH_TEST_TIME_BUDGET_MS = 5_000.0


def relax_graph_time_budget(test: unittest.TestCase) -> None:
    """Give the graph read a deadline a loaded test runner cannot trip.

    ``memory_graph.TIME_BUDGET_MS`` is 25 ms.  That is the right bound in
    production and ``Memory.graph_chains`` reads it per call, so a read that
    exceeds it correctly returns what it screened so far with mode
    ``budget-exceeded`` and its chains marked incomplete.  Under a full suite
    on a loaded host the same read can cross 25 ms for reasons that have
    nothing to do with the behaviour under test, and a test asserting a
    complete chain, a ``complete`` mode, or the absence of a ``not_recorded``
    cue then fails on the clock rather than on the product.

    Every test that asserts a graph answer calls this from ``setUp``; a test
    that exercises the budget itself must not, and would patch the constant
    the other way instead.  There is no such test in this file - the budget
    exit test lives in ``tests/test_memory_graph_integration.py``.
    """
    patcher = patch.object(
        memory_graph, "TIME_BUDGET_MS", GRAPH_TEST_TIME_BUDGET_MS
    )
    patcher.start()
    test.addCleanup(patcher.stop)


class ModelResponse(dict):
    def __init__(self, content: str) -> None:
        super().__init__(role="assistant", content=content)
        self.done_reason = None
        self.done = True


class ToolCallResponse(ModelResponse):
    """A scripted reply that calls one tool."""

    def __init__(self, name: str, arguments: dict[str, object]) -> None:
        super().__init__("")
        self["tool_calls"] = [{"function": {"name": name, "arguments": arguments}}]


class ScriptedModelClient:
    def __init__(self, replies: list[object] | None = None) -> None:
        self.replies = list(replies or [])
        self.requests: list[dict[str, object]] = []

    def models(self, refresh: bool = True) -> list[str]:
        del refresh
        return ["qwen3.5:9b", "gpt-oss:20b", "qwen3-coder:30b"]

    def chat(self, *args: object, **kwargs: object) -> object:
        self.requests.append({"args": args, "kwargs": kwargs})
        content = self.replies.pop(0) if self.replies else "Understood."
        if isinstance(content, dict):
            return content
        return ModelResponse(content)

    def last_request_text(self) -> str:
        return json.dumps(self.requests[-1], ensure_ascii=False, default=str)

    def last_claims_block(self) -> str:
        """The temporal_claims block of the newest user message, unescaped."""
        messages = self.requests[-1]["args"][0]
        assert isinstance(messages, list)
        for message in reversed(messages):
            content = str(message.get("content") or "")
            if "<temporal_claims>" in content:
                return content.split("<temporal_claims>", 1)[1].split(
                    "</temporal_claims>", 1
                )[0]
        return ""


class AgentMemoryKeystoneTests(unittest.TestCase):
    """The M1 keystone: negative receipts, fact-over-web routing, temporal and
    abstention cues, lock-free reads, retraction, and the claim-lane bound."""

    def setUp(self) -> None:
        relax_graph_time_budget(self)
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
        self.db_path = data_dir / "agent.db"
        self.memory = Memory(self.db_path)
        self.events: list[str] = []

    def tearDown(self) -> None:
        self.memory.close()
        self.temp.cleanup()

    def _agent(
        self,
        replies: list[str] | None = None,
        *,
        config: Config | None = None,
    ) -> tuple[Agent, ScriptedModelClient]:
        client = ScriptedModelClient(replies)
        agent = Agent(
            config or self.config,
            self.memory,
            self.events.append,
            client=client,
            coding_review=False,
            coding_planning=False,
        )
        return agent, client

    def _claims(self) -> list[tuple[str, str, str, str]]:
        rows = self.memory.db.execute(
            "SELECT subject, predicate, value, status FROM memory_claims ORDER BY id"
        ).fetchall()
        return [tuple(row) for row in rows]

    # --- H-1: never-encoded becomes a deterministic negative receipt --------

    def test_natural_language_update_gets_negative_receipt_with_exact_command(self) -> None:
        agent, client = self._agent(
            ["Understood. I've updated the project fact for the Kestrel relay listen port to 9191."]
        )
        stored = agent.run(_command("Kestrel relay", "listen port", "9090"))
        result = agent.run(
            "By the way, the Kestrel relay now listens on port 9191, not 9090.",
            conversation_id=stored.conversation_id,
        )

        text = str(result)
        self.assertEqual(result.status, "complete", result.reason)
        self.assertIn("Not stored: no project fact was written this turn.", text)
        # The proposal adopts the stored predicate so a paste supersedes, not forks.
        self.assertIn(_command("Kestrel relay", "listen port", "9191"), text)
        self.assertIn("update the currently stored value", text)
        self.assertIn("governed project memory - not stored", self.events)
        self.assertEqual(
            self._claims(), [("Kestrel relay", "listen port", "9090", "active")]
        )
        persisted = self.memory.db.execute(
            "SELECT content FROM messages WHERE role='assistant' ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
        self.assertIn("Not stored", persisted)

        pasted = agent.run(
            _command("Kestrel relay", "listen port", "9191"),
            conversation_id=stored.conversation_id,
        )
        self.assertIn("Updated project fact", str(pasted))
        self.assertEqual(
            self._claims(),
            [
                ("Kestrel relay", "listen port", "9090", "superseded"),
                ("Kestrel relay", "listen port", "9191", "active"),
            ],
        )
        self.assertEqual(len(client.requests), 1)

    def test_fabricated_memory_receipt_is_corrected_even_without_a_fact_statement(self) -> None:
        agent, _client = self._agent(["Done. This has been recorded in memory."])
        result = agent.run("Thanks, that helps a lot.")

        text = str(result)
        self.assertIn("Not stored: no project fact was written this turn.", text)
        self.assertIn('Remember this project fact: {"subject":"..."', text)
        self.assertEqual(self._claims(), [])

    def test_restating_a_stored_fact_and_ordinary_replies_add_no_note(self) -> None:
        agent, _client = self._agent(["Right, 9090.", "Fine, thanks."])
        stored = agent.run(_command("Kestrel relay", "listen port", "9090"))
        restated = agent.run(
            "FYI the Kestrel relay now listens on port 9090.",
            conversation_id=stored.conversation_id,
        )
        ordinary = agent.run("How are you today?", conversation_id=stored.conversation_id)

        self.assertNotIn("Not stored", str(restated))
        self.assertNotIn("Not stored", str(ordinary))
        self.assertNotIn("governed project memory - not stored", self.events)

    def test_readonly_note_names_the_mode_instead_of_a_command(self) -> None:
        agent, _client = self._agent(
            ["Noted."], config=replace(self.config, autonomy="readonly")
        )
        result = agent.run("By the way, the Kestrel relay now listens on port 9191.")

        text = str(result)
        self.assertIn("Not stored", text)
        self.assertIn("readonly mode", text)
        self.assertNotIn("Remember this project fact:", text)

    def test_governed_turns_never_receive_a_negative_receipt(self) -> None:
        agent, client = self._agent()
        result = agent.run(_command("Kestrel relay", "listen port", "9090"))

        self.assertEqual(str(result), "Stored project fact (claim record #1).")
        self.assertEqual(client.requests, [])

    # --- H-2: operator facts outrank weak web intent ------------------------

    def test_latest_question_about_a_stored_fact_stays_on_memory(self) -> None:
        agent, client = self._agent(["The Kestrel relay listens on port 9090."])
        stored = agent.run(_command("Kestrel relay", "listen port", "9090"))
        result = agent.run(
            "What is the latest Kestrel relay listen port?",
            conversation_id=stored.conversation_id,
        )

        self.assertEqual(result.status, "complete", result.reason)
        self.assertEqual(result.tool_calls, 0)
        self.assertIn("memory - stored project fact outranks weak web intent", self.events)
        request_text = client.last_request_text()
        self.assertIn("<temporal_claims>", request_text)
        self.assertIn("9090", request_text)
        self.assertNotIn("EVIDENCE_RECORD", request_text)
        self.assertNotIn("web_search", request_text)

    def test_explicit_research_keeps_its_web_route(self) -> None:
        agent, _client = self._agent(["ok"])
        stored = agent.run(_command("Kestrel relay", "listen port", "9090"))
        agent.run(
            "Search the web for the latest Kestrel relay release notes.",
            conversation_id=stored.conversation_id,
        )
        self.assertNotIn("memory - stored project fact outranks weak web intent", self.events)

    # --- L-1 / M-3: superseded surfacing and the abstention cue -------------

    def test_temporal_question_surfaces_superseded_values(self) -> None:
        agent, client = self._agent(["It used to be 8080."])
        stored = agent.run(_command("Kestrel relay", "listen port", "8080"))
        agent.run(
            _command("Kestrel relay", "listen port", "9090"),
            conversation_id=stored.conversation_id,
        )
        agent.run(
            "What used to be the Kestrel relay listen port?",
            conversation_id=stored.conversation_id,
        )

        claims = json.loads(client.last_claims_block())
        statuses = {(item["value"], item["status"]) for item in claims}
        self.assertIn(("9090", "active"), statuses)
        self.assertIn(("8080", "superseded"), statuses)
        self.assertIn("superseded_at", json.dumps(claims))
        self.assertIn("report it only as history", client.last_request_text())

    def test_present_tense_question_does_not_surface_history(self) -> None:
        agent, client = self._agent(["9090."])
        stored = agent.run(_command("Kestrel relay", "listen port", "8080"))
        agent.run(
            _command("Kestrel relay", "listen port", "9090"),
            conversation_id=stored.conversation_id,
        )
        agent.run(
            "What port does the Kestrel relay listen on?",
            conversation_id=stored.conversation_id,
        )
        block = client.last_claims_block()
        self.assertIn("9090", block)
        self.assertNotIn("8080", block)
        self.assertNotIn("superseded", block)

    def test_unknown_subject_gets_an_explicit_abstention_cue(self) -> None:
        agent, client = self._agent(["I do not have that recorded."])
        stored = agent.run(_command("Kestrel relay", "listen port", "9090"))
        agent.run(
            "What port does the Osprey relay listen on?",
            conversation_id=stored.conversation_id,
        )
        block = client.last_claims_block()
        self.assertIn("not_recorded", block)
        self.assertIn("No stored project fact matches", block)
        self.assertIn("Osprey", block)
        self.assertNotIn("9090", block)
        # The guidance rides with the block in the user turn, since the compact
        # contract cannot afford it.
        self.assertIn(
            "do not offer a default, typical, or assumed value",
            client.last_request_text(),
        )

    def test_world_knowledge_questions_get_no_abstention_cue(self) -> None:
        agent, client = self._agent(["Paris.", "Shakespeare.", "8,849 m.", "Not recorded."])
        stored = agent.run(_command("Kestrel relay", "listen port", "9090"))
        for question in (
            "What is the capital of France?",
            "Who wrote Hamlet?",
            "How tall is Mount Everest?",
        ):
            with self.subTest(question=question):
                agent.run(question, conversation_id=stored.conversation_id)
                # No claims block at all: no stored fact matched and no
                # project-shaped subject was named.
                self.assertEqual(client.last_claims_block(), "")
                self.assertNotIn("No stored project fact", client.last_request_text())
        # A proper name next to a configured-value word is a project subject.
        agent.run("Where is Osprey hosted?", conversation_id=stored.conversation_id)
        block = client.last_claims_block()
        self.assertIn("not_recorded", block)
        self.assertIn('"subject":"Osprey"', block)

    def test_system_prompt_states_the_memory_write_rule(self) -> None:
        agent, client = self._agent(["Sure."])
        agent.run("Tell me about the Harrier box.")
        # The dialogue lane sends the compacted contract, which has ~60 chars of
        # headroom in the tightest configured context, so the rule is one clause
        # there; the full rule rides in memory_write_rule for larger contexts.
        self.assertIn("Never say a fact was saved", client.last_request_text())
        self.assertIn("not_recorded means none stored", client.last_request_text())

    # --- M-1: reads never take the write lock --------------------------------

    def test_claim_read_under_a_foreign_write_lock_neither_blocks_nor_crashes(self) -> None:
        # Scope: the claim read and the prompt assembly.  Turn bookkeeping
        # (conversation goals, messages) still needs the write lock, as before.
        agent, _client = self._agent(["9090."])
        agent.run(_command("Kestrel relay", "listen port", "9090"))
        # Only global claims feed the claim clock, so use one to prove the
        # telemetry write is dropped rather than waited for under the lock.
        self.memory.remember_claim(
            "Heron relay", "listen port", "6060", source="fixture", authority="verified"
        )
        agent._active_project_id = 1
        other = sqlite3.connect(str(self.db_path), isolation_level=None)
        try:
            other.execute("BEGIN IMMEDIATE")
            started = time.perf_counter()
            direct = self.memory.current_claims(
                "Kestrel relay listen port", limit=8, clock_mode="shadow", project_id=1
            )
            global_read = self.memory.current_claims(
                "Heron relay listen port", limit=8, clock_mode="shadow"
            )
            prompt = agent.system_prompt("What port does the Kestrel relay listen on?")
            elapsed = time.perf_counter() - started
        finally:
            other.execute("ROLLBACK")
            other.close()

        self.assertEqual([item["value"] for item in direct], ["9090"])
        self.assertEqual([item["value"] for item in global_read], ["6060"])
        self.assertIn("9090", prompt)
        self.assertLess(elapsed, 2.0)
        self.assertGreaterEqual(self.memory._dropped_claim_clock_reads, 1)

        # Telemetry resumes once the lock is gone.
        before = self.memory.db.execute(
            "SELECT COALESCE(SUM(reads), 0) FROM memory_claim_clock_statistics"
        ).fetchone()[0]
        self.memory.remember_claim(
            "Global node", "release channel", "stable", source="fixture", authority="verified"
        )
        self.memory.current_claims("Global node release channel", clock_mode="shadow")
        after = self.memory.db.execute(
            "SELECT COALESCE(SUM(reads), 0) FROM memory_claim_clock_statistics"
        ).fetchone()[0]
        self.assertGreater(after, before)

    # --- L-3: retraction and listing ----------------------------------------

    def test_forget_command_retires_the_fact_and_keeps_history(self) -> None:
        agent, client = self._agent()
        stored = agent.run(_command("Kestrel relay", "listen port", "9090"))
        forgotten = agent.run(
            _forget("Kestrel relay", "listen port"),
            conversation_id=stored.conversation_id,
        )

        self.assertEqual(forgotten.status, "complete", forgotten.reason)
        self.assertEqual(forgotten.tool_calls, 0)
        self.assertIn("Retracted project fact (claim record #1)", str(forgotten))
        self.assertEqual(client.requests, [])
        self.assertEqual(
            self.memory.current_claims("Kestrel relay listen port", project_id=1), []
        )
        history = self.memory.claim_history("Kestrel relay", "listen port", project_id=1)
        self.assertEqual([(row["value"], row["status"]) for row in history], [("9090", "superseded")])

        again = agent.run(
            _forget("Kestrel relay", "listen port"),
            conversation_id=stored.conversation_id,
        )
        self.assertIn("No active project fact matches", str(again))
        malformed = agent.run(
            "Forget this project fact: not-json",
            conversation_id=stored.conversation_id,
        )
        self.assertEqual(malformed.status, "incomplete")
        self.assertIn("Not retracted:", str(malformed))
        question = agent.run(
            "Did you forget that project fact from yesterday?",
            conversation_id=stored.conversation_id,
        )
        self.assertEqual(question.status, "complete", question.reason)
        self.assertNotIn("Not retracted", str(question))

    def test_facts_listing_uses_the_screened_read_path(self) -> None:
        conversation = self.memory.new_conversation(project_id=1)
        self.memory.remember_explicit_project_claim(
            conversation, 1, _command("Kestrel relay", "listen port", "9090")
        )
        self.memory.remember_claim(
            "Global node", "release channel", "stable", source="fixture", authority="verified"
        )
        output = io.StringIO()
        with redirect_stdout(output):
            _display_project_facts(self.memory, 1)
        printed = output.getvalue()
        self.assertIn("Kestrel relay | listen port | 9090 [active]", printed)
        self.assertNotIn("Global node", printed)
        output = io.StringIO()
        with redirect_stdout(output):
            _display_project_facts(self.memory, 2)
        self.assertIn("No active project facts for project 2", output.getvalue())

    # --- M-2: the claim lane narrows instead of abstaining at the bound -----

    def test_claim_lane_narrows_past_the_candidate_bound_and_reports(self) -> None:
        conversation = self.memory.new_conversation(project_id=1)
        with patch.object(memory_module, "MAX_MEMORY_SEARCH_CANDIDATES", 40):
            for index in range(60):
                self.memory.remember_explicit_project_claim(
                    conversation,
                    1,
                    _command(f"amber relay {index}", "release channel", f"val{index}"),
                )
            self.memory.remember_explicit_project_claim(
                conversation, 1, _command("Node7", "release channel", "stable")
            )
            found = self.memory.current_claims(
                "Node7 release channel", limit=8, project_id=1
            )
            report = self.memory.claim_recall_report()
            self.assertEqual([(item["subject"], item["value"]) for item in found], [("Node7", "stable")])
            self.assertEqual(report["mode"], "all-terms")
            self.assertFalse(report["abstained"])
            self.assertEqual(report["returned"], 1)

            generic = self.memory.current_claims("release channel", limit=8, project_id=1)
            report = self.memory.claim_recall_report()
            self.assertEqual(generic, [])
            self.assertTrue(report["abstained"])
            self.assertEqual(report["mode"], "overflow")

    def test_claim_lane_reports_identity_conflicts(self) -> None:
        conversation = self.memory.new_conversation(project_id=1)
        self.memory.remember_explicit_project_claim(
            conversation, 1, _command("Kestrel relay", "listen port", "9090")
        )
        self.memory.remember_explicit_project_claim(
            conversation, 1, _command("Osprey relay", "listen port", "7070")
        )
        self.assertEqual(self.memory.current_claims("Heron relay listen port", project_id=1), [])
        report = self.memory.claim_recall_report()
        self.assertTrue(report["abstained"])
        self.assertIn(report["mode"], {"identity-conflict", "ambiguous"})

    # --- Confirmation: one reply stores the proposal that was shown ----------

    def _transcript(self, conversation_id: int) -> list[tuple[str, str]]:
        rows = self.memory.db.execute(
            "SELECT role, content FROM messages WHERE conversation_id=? ORDER BY id",
            (conversation_id,),
        ).fetchall()
        return [tuple(row) for row in rows]

    def test_store_it_reply_stores_the_shown_proposal(self) -> None:
        agent, client = self._agent(["Understood."])
        stored = agent.run(_command("Kestrel relay", "listen port", "9090"))
        noted = agent.run(
            "By the way, the Kestrel relay now listens on port 9191, not 9090.",
            conversation_id=stored.conversation_id,
        )
        self.assertIn('Or reply "store it" to store exactly that.', str(noted))

        confirmed = agent.run("store it", conversation_id=stored.conversation_id)

        self.assertEqual(confirmed.status, "complete", confirmed.reason)
        self.assertEqual(confirmed.tool_calls, 0)
        self.assertIn("Updated project fact (claim record #2)", str(confirmed))
        self.assertIn("governed project memory - confirmed proposal", self.events)
        self.assertIn("governed project memory - superseded", self.events)
        self.assertEqual(
            self._claims(),
            [
                ("Kestrel relay", "listen port", "9090", "superseded"),
                ("Kestrel relay", "listen port", "9191", "active"),
            ],
        )
        # Only the natural-language turn reached the model.
        self.assertEqual(len(client.requests), 1)
        transcript = self._transcript(stored.conversation_id)
        self.assertEqual(transcript[-3], ("user", "store it"))
        self.assertEqual(
            transcript[-2], ("user", _command("Kestrel relay", "listen port", "9191"))
        )
        self.assertTrue(transcript[-1][1].startswith("Updated project fact"))
        # A fresh conversation now answers from the new value.
        fresh_agent, fresh_client = self._agent(["9191."])
        fresh_agent.run("What port does the Kestrel relay listen on?")
        self.assertIn("9191", fresh_client.last_claims_block())
        self.assertNotIn("9090", fresh_client.last_claims_block())

    def test_confirmation_phrasings_and_a_bare_yes(self) -> None:
        phrasings = ("yes, store it please", "Save that fact.", "confirm", "Yes")
        for index, phrasing in enumerate(phrasings):
            with self.subTest(phrasing=phrasing):
                subject = f"Relay{index}"
                agent, client = self._agent(["Understood, thanks for the update."])
                first = agent.run(
                    f"By the way, the {subject} now listens on port 919{index}.",
                )
                self.assertIn("Not stored", str(first))
                result = agent.run(phrasing, conversation_id=first.conversation_id)
                self.assertIn("Stored project fact", str(result), phrasing)
                self.assertEqual(len(client.requests), 1)
                self.assertEqual(
                    [
                        row[2] for row in self._claims()
                        if row[0] == subject and row[3] == "active"
                    ],
                    [f"919{index}"],
                )

    def test_bare_yes_is_ignored_when_the_reply_asked_a_question(self) -> None:
        agent, client = self._agent(
            [
                "Got it. Should I also restart the relay?",
                "Restarting is up to you.",
                "Nothing is pending.",
            ]
        )
        stored = agent.run(_command("Kestrel relay", "listen port", "9090"))
        agent.run(
            "By the way, the Kestrel relay now listens on port 9191, not 9090.",
            conversation_id=stored.conversation_id,
        )
        agent.run("yes", conversation_id=stored.conversation_id)
        # "yes" answered the model's question; it did not store anything.
        self.assertEqual(len(client.requests), 2)
        self.assertEqual(
            self._claims(), [("Kestrel relay", "listen port", "9090", "active")]
        )
        self.assertNotIn("governed project memory - confirmed proposal", self.events)
        # The proposal was only confirmable immediately after it was shown.
        agent.run("store it", conversation_id=stored.conversation_id)
        self.assertEqual(len(client.requests), 3)
        self.assertEqual(
            self._claims(), [("Kestrel relay", "listen port", "9090", "active")]
        )

    def test_ambiguous_verbs_after_a_question_answer_the_question_not_memory(self) -> None:
        question = "Got it. Should I also save the config file to disk?"
        for phrasing, stores in (
            ("save it", False),
            ("keep it", False),
            ("confirm", False),
            ("store it", True),
            ("save that fact", True),
        ):
            with self.subTest(phrasing=phrasing):
                subject = f"Relay{abs(hash(phrasing)) % 1000}"
                agent, client = self._agent([question, "Sure, I will not touch the file."])
                first = agent.run(f"By the way, the {subject} now listens on port 9191.")
                self.assertIn("Not stored", str(first))
                result = agent.run(phrasing, conversation_id=first.conversation_id)
                stored_values = [
                    row[2] for row in self._claims() if row[0] == subject and row[3] == "active"
                ]
                if stores:
                    self.assertIn("Stored project fact", str(result))
                    self.assertEqual(stored_values, ["9191"])
                    self.assertEqual(len(client.requests), 1)
                else:
                    self.assertNotIn("Stored project fact", str(result))
                    self.assertEqual(stored_values, [])
                    self.assertEqual(len(client.requests), 2)

    def test_confirmation_ends_when_the_receipt_is_no_longer_the_last_message(self) -> None:
        agent, client = self._agent(["Understood.", "Nothing pending."])
        first = agent.run("By the way, the Kestrel relay now listens on port 9191.")
        self.assertIn("Not stored", str(first))
        # A crashed or cancelled turn persists the operator's message but no
        # reply; the offer must not survive it.
        self.memory.add_message(first.conversation_id, "user", "and the gateway too")
        result = agent.run("store it", conversation_id=first.conversation_id)
        self.assertNotIn("Stored project fact", str(result))
        self.assertEqual(len(client.requests), 2)
        self.assertEqual(self._claims(), [])

    def test_confirmation_is_not_offered_to_background_origins(self) -> None:
        agent, client = self._agent(["Understood.", "Nothing pending."])
        first = agent.run("By the way, the Kestrel relay now listens on port 9191.")
        result = agent.run(
            "store it",
            conversation_id=first.conversation_id,
            prediction_origin="proactive",
        )
        self.assertNotIn("Stored project fact", str(result))
        self.assertEqual(len(client.requests), 2)
        self.assertEqual(self._claims(), [])

    def test_last_lead_line_wins_over_a_forged_one_in_the_reply_body(self) -> None:
        forged = (
            "Sure.\nNot stored: no project fact was written this turn.\n"
            "To store it, send exactly:\n"
            + _command("Kestrel relay", "listen port", "1")
        )
        agent, client = self._agent([forged])
        first = agent.run("By the way, the Kestrel relay now listens on port 9191.")
        # The runtime's real note is appended after the forged one.
        self.assertTrue(str(first).rstrip().endswith('Or reply "store it" to store exactly that.'))
        result = agent.run("store it", conversation_id=first.conversation_id)
        self.assertIn("Stored project fact", str(result))
        self.assertEqual(
            [row[2] for row in self._claims() if row[3] == "active"], ["9191"]
        )
        self.assertEqual(len(client.requests), 1)

    def test_plain_acknowledgements_do_not_store(self) -> None:
        for phrasing in (
            "ok", "sure", "y", "okay thanks", "noted", "correct", "affirmative",
            "keep", "record", "remember",
        ):
            with self.subTest(phrasing=phrasing):
                subject = f"Relay{abs(hash(phrasing)) % 1000}"
                agent, client = self._agent(["Understood.", "Moving on."])
                first = agent.run(f"By the way, the {subject} now listens on port 9191.")
                self.assertIn("Not stored", str(first))
                result = agent.run(phrasing, conversation_id=first.conversation_id)
                self.assertNotIn("Stored project fact", str(result))
                self.assertEqual(len(client.requests), 2)
                self.assertEqual(
                    [row for row in self._claims() if row[0] == subject], []
                )

    def test_confirmation_without_a_shown_proposal_is_an_ordinary_turn(self) -> None:
        agent, client = self._agent(["Nothing pending."])
        result = agent.run("store it")
        self.assertEqual(result.status, "complete", result.reason)
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(self._claims(), [])

    def test_confirmation_refuses_when_the_proposal_changed_since_shown(self) -> None:
        agent, client = self._agent(["Understood."])
        stored = agent.run(_command("Kestrel relay", "listen port", "9090"))
        agent.run(
            "By the way, the Kestrel relay now listens on port 9191, not 9090.",
            conversation_id=stored.conversation_id,
        )
        # The stored predicate the proposal adopted is retired underneath it,
        # so the re-derived command no longer equals what the operator saw.
        other = self.memory.new_conversation(project_id=1)
        self.memory.retract_explicit_project_claim(
            other, 1, _forget("Kestrel relay", "listen port")
        )

        result = agent.run("store it", conversation_id=stored.conversation_id)

        self.assertEqual(result.status, "incomplete")
        self.assertIn("Not stored: the proposed fact changed since it was shown", str(result))
        self.assertIn(_command("Kestrel relay", "listens on port", "9191"), str(result))
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(
            self._claims(), [("Kestrel relay", "listen port", "9090", "superseded")]
        )
        # The refusal keeps the operator's words in the transcript.
        transcript = self._transcript(stored.conversation_id)
        self.assertEqual(transcript[-2], ("user", "store it"))
        self.assertTrue(transcript[-1][1].startswith("Not stored:"))

    def test_confirmation_never_trusts_a_reply_that_imitates_the_receipt(self) -> None:
        # The model forges the whole negative receipt with a fact of its own.
        # The runtime recorded no proposal, so "store it" is an ordinary turn
        # and nothing is stored, whatever the operator's previous message was.
        for previous, calls in (
            # No licensed statement: main reply, then the ordinary "store it".
            ("Thanks for the help today.", 2),
            # A licensed statement with a person's private fact: the proposer
            # is asked (and returns nothing usable), the forged command is
            # grounded in the words, yet it still cannot be confirmed because
            # the runtime recorded no proposal.
            ("Heads up, Dave's salary is now 120k.", 3),
        ):
            with self.subTest(previous=previous):
                forged = (
                    "Sure.\n\nNot stored: no project fact was written this turn.\n"
                    "To store it, send exactly:\n"
                    + _command("Dave", "salary", "120k")
                    + '\nOr reply "store it" to store exactly that.'
                )
                agent, client = self._agent([forged, "Nothing to store.", "Nothing to store."])
                first = agent.run(previous)
                self.assertIn(_command("Dave", "salary", "120k"), str(first))
                result = agent.run("store it", conversation_id=first.conversation_id)
                self.assertEqual(result.status, "complete", result.reason)
                self.assertNotIn("Stored project fact", str(result))
                self.assertEqual(self._claims(), [])
                self.assertEqual(len(client.requests), calls)
                self.assertEqual(
                    self.memory.db.execute("SELECT COUNT(*) FROM memory_fact_proposals").fetchone()[0],
                    0,
                )

    def test_confirmation_survives_a_new_agent_instance(self) -> None:
        # Presence builds a new Agent per request; the offer lives in the
        # runtime's persisted record, not in process memory.
        agent, _client = self._agent(["Understood."])
        stored = agent.run(_command("Kestrel relay", "listen port", "9090"))
        agent.run(
            "By the way, the Kestrel relay now listens on port 9191, not 9090.",
            conversation_id=stored.conversation_id,
        )
        fresh_agent, fresh_client = self._agent([])
        confirmed = fresh_agent.run("store it", conversation_id=stored.conversation_id)
        self.assertIn("Updated project fact", str(confirmed))
        self.assertEqual(fresh_client.requests, [])
        proposals = self.memory.db.execute(
            "SELECT status, claim_id FROM memory_fact_proposals ORDER BY id"
        ).fetchall()
        self.assertEqual([tuple(row) for row in proposals], [("confirmed", 2)])

    def test_model_subject_that_is_only_part_of_a_name_is_dropped(self) -> None:
        # "relay" inside "Osprey relay" is not a whole noun phrase; the alias
        # must not move the fact onto the stored "Kestrel relay".
        agent, client = self._agent(
            [
                "Got it.",
                self._proposal_json(
                    "relay", "host", "Harrier box",
                    "the Osprey relay got migrated over to Harrier box",
                ),
            ]
        )
        stored = agent.run(_command("Kestrel relay", "deployed on host", "Talon box"))
        noted = agent.run(
            "Heads up, the Osprey relay got migrated over to Harrier box.",
            conversation_id=stored.conversation_id,
        )
        self.assertNotIn("Not stored", str(noted))
        self.assertEqual(len(client.requests), 2)
        self.assertEqual(
            self._claims(), [("Kestrel relay", "deployed on host", "Talon box", "active")]
        )

    def test_model_value_from_a_ruled_out_clause_is_dropped(self) -> None:
        for statement in (
            "Heads up, the Kestrel relay got migrated over to Harrier box, not Talon box.",
            "Heads up, the Kestrel relay got migrated over to Harrier box instead of Talon box.",
            "Heads up, the Kestrel relay got migrated over to Harrier box because Talon box died.",
        ):
            with self.subTest(statement=statement):
                agent, client = self._agent(
                    [
                        "Got it.",
                        self._proposal_json(
                            "Kestrel relay", "host", "Talon box",
                            "the Kestrel relay got migrated over to Harrier box",
                        ),
                    ]
                )
                stored = agent.run(_command("Kestrel relay", "deployed on host", "Osprey box"))
                noted = agent.run(statement, conversation_id=stored.conversation_id)
                self.assertNotIn("Talon box", str(noted).split("Not stored", 1)[-1])
                self.assertEqual(len(client.requests), 2)

    def test_assisted_confirmation_also_reapplies_the_extractor_layer(self) -> None:
        # Even a recorded assisted proposal is re-checked at confirmation with
        # the extractor's special-category layer; a record cannot be forged,
        # but the check is cheap belt-and-braces.
        agent, client = self._agent(
            [
                "Got it.",
                self._proposal_json(
                    "Kestrel relay", "host", "Harrier box",
                    "the Kestrel relay got migrated over to Harrier box",
                ),
            ]
        )
        stored = agent.run(_command("Kestrel relay", "deployed on host", "Talon box"))
        agent.run(
            "Heads up, the Kestrel relay got migrated over to Harrier box.",
            conversation_id=stored.conversation_id,
        )
        # Tamper with the record out of band: the confirmation must refuse.
        self.memory.db.execute(
            "UPDATE memory_fact_proposals SET command=?",
            (_command("Dave", "salary", "120k"),),
        )
        self.memory.db.commit()
        result = agent.run("store it", conversation_id=stored.conversation_id)
        self.assertEqual(result.status, "incomplete")
        self.assertIn("could not be grounded", str(result))
        self.assertEqual(len(client.requests), 2)
        self.assertEqual(
            self._claims(), [("Kestrel relay", "deployed on host", "Talon box", "active")]
        )

    def test_readonly_never_calls_the_proposer(self) -> None:
        agent, client = self._agent(
            ["Noted.", "Noted."], config=replace(self.config, autonomy="readonly")
        )
        # Grammar-splittable: the note names the mode, one model call.
        result = agent.run("By the way, the Kestrel relay now listens on port 9191.")
        self.assertIn("readonly mode", str(result))
        self.assertEqual(len(client.requests), 1)
        # Not splittable: no proposer call is spent, so no note at all.
        result = agent.run("Heads up, the Kestrel relay got migrated over to Harrier box.")
        self.assertNotIn("Not stored", str(result))
        self.assertEqual(len(client.requests), 2)
        self.assertEqual(
            self.memory.db.execute("SELECT COUNT(*) FROM memory_fact_proposals").fetchone()[0],
            0,
        )

    # --- One-hop bridging for questions that span two facts ------------------

    def test_two_fact_question_receives_the_bridged_claim(self) -> None:
        agent, client = self._agent(["Fenwick."])
        stored = agent.run(_command("Kestrel relay", "deployed on host", "Harrier box"))
        agent.run(
            _command("Harrier box", "datacenter", "Fenwick"),
            conversation_id=stored.conversation_id,
        )
        agent.run(
            _command("Osprey relay", "deployed on host", "Talon box"),
            conversation_id=stored.conversation_id,
        )
        agent.run(
            _command("Talon box", "datacenter", "Moss Hollow"),
            conversation_id=stored.conversation_id,
        )
        agent.run(
            "Which datacenter hosts the Kestrel relay?",
            conversation_id=stored.conversation_id,
        )

        claims = json.loads(client.last_claims_block())
        by_key = {(item["subject"], item["predicate"]): item for item in claims}
        self.assertIn(("Kestrel relay", "deployed on host"), by_key)
        bridged = by_key[("Harrier box", "datacenter")]
        self.assertEqual(bridged["value"], "Fenwick")
        self.assertEqual(bridged["bridge_from"], "Kestrel relay / deployed on host")
        self.assertNotIn(("Talon box", "datacenter"), by_key)
        self.assertNotIn("Moss Hollow", client.last_claims_block())
        self.assertIn("chain the two to answer", client.last_request_text())

    def test_known_subject_with_unknown_attribute_shows_its_facts_not_a_cue(self) -> None:
        agent, client = self._agent(["Not recorded."])
        stored = agent.run(_command("Kestrel relay", "listen port", "9090"))
        agent.run(
            "What is the Kestrel relay firmware version?",
            conversation_id=stored.conversation_id,
        )
        block = client.last_claims_block()
        self.assertNotIn("not_recorded", block)
        claims = json.loads(block)
        self.assertEqual(
            [(item["subject"], item["predicate"], item.get("match")) for item in claims],
            [("Kestrel relay", "listen port", "subject")],
        )
        self.assertIn("say the asked fact is not recorded", client.last_request_text())

    def test_bridge_works_when_the_question_shares_no_word_with_the_predicate(self) -> None:
        agent, client = self._agent(["Fenwick."])
        stored = agent.run(_command("Kestrel relay", "runs on", "Harrier box"))
        agent.run(
            _command("Harrier box", "datacenter", "Fenwick"),
            conversation_id=stored.conversation_id,
        )
        agent.run(
            "Which datacenter hosts the Kestrel relay?",
            conversation_id=stored.conversation_id,
        )
        claims = json.loads(client.last_claims_block())
        by_key = {(item["subject"], item["predicate"]): item for item in claims}
        self.assertEqual(by_key[("Harrier box", "datacenter")]["value"], "Fenwick")
        self.assertEqual(
            by_key[("Harrier box", "datacenter")]["bridge_from"], "Kestrel relay / runs on"
        )

    def test_bridge_prefers_the_asked_predicate_over_recency(self) -> None:
        agent, client = self._agent(["Fenwick."])
        stored = agent.run(_command("Kestrel relay", "deployed on host", "Harrier box"))
        agent.run(
            _command("Harrier box", "datacenter", "Fenwick"),
            conversation_id=stored.conversation_id,
        )
        for index, predicate in enumerate(
            ("rack", "owner", "vendor", "warranty end", "os image", "cpu count", "ram size")
        ):
            agent.run(
                _command("Harrier box", predicate, f"value{index}"),
                conversation_id=stored.conversation_id,
            )
        agent.run(
            "Which datacenter hosts the Kestrel relay?",
            conversation_id=stored.conversation_id,
        )
        block = client.last_claims_block()
        self.assertIn("Fenwick", block)
        bridged = [item for item in json.loads(block) if item.get("bridge_from")]
        self.assertLessEqual(len(bridged), 4)
        self.assertEqual(bridged[0]["predicate"], "datacenter")

    def test_stored_subjects_guide_the_proposal_split(self) -> None:
        agent, _client = self._agent(["Understood."])
        stored = agent.run(_command("Falcon gateway", "owner", "Dana"))
        result = agent.run(
            "By the way, Falcon gateway east region is now eu-west-1.",
            conversation_id=stored.conversation_id,
        )
        self.assertIn(_command("Falcon gateway", "east region", "eu-west-1"), str(result))

    # --- Model-assisted proposals, grounded in the operator's words ----------

    @staticmethod
    def _proposal_json(subject: str, predicate: str, value: str, span: str) -> str:
        return json.dumps(
            {"subject": subject, "predicate": predicate, "value": value, "source_span": span}
        )

    def test_model_assisted_proposal_is_grounded_adopted_and_confirmable(self) -> None:
        statement = "Heads up, the Kestrel relay got migrated over to Harrier box."
        agent, client = self._agent(
            [
                "Got it.",
                self._proposal_json(
                    "Kestrel relay", "host", "Harrier box",
                    "the Kestrel relay got migrated over to Harrier box",
                ),
            ]
        )
        stored = agent.run(_command("Kestrel relay", "deployed on host", "Talon box"))
        noted = agent.run(statement, conversation_id=stored.conversation_id)

        text = str(noted)
        self.assertIn("Not stored: no project fact was written this turn.", text)
        # The grammar could not split it; the model proposed; "host" adopted the
        # stored predicate "deployed on host".
        self.assertIn(_command("Kestrel relay", "deployed on host", "Harrier box"), text)
        self.assertIn("Proposed by the local model from your words", text)
        self.assertEqual(len(client.requests), 2)
        proposer_request = client.requests[-1]
        self.assertEqual(proposer_request["kwargs"].get("temperature"), 0.0)
        self.assertIn("Sentence:", json.dumps(proposer_request["args"][0]))
        self.assertIn("Known predicates: deployed on host", json.dumps(proposer_request["args"][0]))

        confirmed = agent.run("store it", conversation_id=stored.conversation_id)
        self.assertIn("Updated project fact", str(confirmed))
        self.assertEqual(len(client.requests), 2)
        self.assertEqual(
            self._claims(),
            [
                ("Kestrel relay", "deployed on host", "Talon box", "superseded"),
                ("Kestrel relay", "deployed on host", "Harrier box", "active"),
            ],
        )

    def test_model_proposal_outside_the_operators_words_is_dropped(self) -> None:
        cases = (
            # value not in the sentence
            self._proposal_json("Kestrel relay", "host", "Osprey box", "the Kestrel relay got migrated over to Harrier box"),
            # span not in the sentence
            self._proposal_json("Kestrel relay", "host", "Harrier box", "the Kestrel relay is hosted on Harrier box"),
            # predicate word from nowhere
            self._proposal_json("Kestrel relay", "datacenter", "Harrier box", "the Kestrel relay got migrated over to Harrier box"),
            # nulls
            json.dumps({"subject": None, "predicate": None, "value": None, "source_span": None}),
            "not json at all",
        )
        for raw in cases:
            with self.subTest(raw=raw[:60]):
                agent, client = self._agent(["Got it.", raw])
                result = agent.run("Heads up, the Kestrel relay got migrated over to Harrier box.")
                self.assertNotIn("Not stored", str(result))
                self.assertEqual(len(client.requests), 2)
                self.assertEqual(self._claims(), [])

    def test_rules_mode_never_calls_the_model(self) -> None:
        agent, client = self._agent(
            ["Got it."], config=replace(self.config, memory_proposer="rules")
        )
        result = agent.run("Heads up, the Kestrel relay got migrated over to Harrier box.")
        self.assertNotIn("Not stored", str(result))
        self.assertEqual(len(client.requests), 1)

    def test_model_is_not_asked_without_a_licensed_statement(self) -> None:
        agent, client = self._agent(["Sure.", "Sure.", "Sure."])
        for prompt in (
            "What is the capital of France?",
            "Please update the README to say the port changed.",
            "I'm now working from home on Fridays.",
        ):
            agent.run(prompt)
        # One model call per turn: never a proposer call.
        self.assertEqual(len(client.requests), 3)

    def test_assisted_proposal_with_an_aliased_subject_survives_confirmation(self) -> None:
        agent, client = self._agent(
            [
                "Got it.",
                self._proposal_json(
                    "relay", "host", "Harrier box", "the relay got migrated over to Harrier box"
                ),
            ]
        )
        stored = agent.run(_command("Kestrel relay", "deployed on host", "Talon box"))
        noted = agent.run(
            "Heads up, the relay got migrated over to Harrier box.",
            conversation_id=stored.conversation_id,
        )
        self.assertIn(_command("Kestrel relay", "deployed on host", "Harrier box"), str(noted))
        confirmed = agent.run("store it", conversation_id=stored.conversation_id)
        self.assertIn("Updated project fact", str(confirmed))
        self.assertEqual(len(client.requests), 2)

    def test_one_word_subject_aliases_to_the_stored_subject(self) -> None:
        agent, client = self._agent(["Noted."])
        stored = agent.run(_command("Kestrel relay", "listen port", "9090"))
        noted = agent.run(
            "the relay now listens on 9191", conversation_id=stored.conversation_id
        )
        self.assertIn(_command("Kestrel relay", "listen port", "9191"), str(noted))
        self.assertNotIn("Proposed by the local model", str(noted))
        self.assertEqual(len(client.requests), 1)
        confirmed = agent.run("store it", conversation_id=stored.conversation_id)
        self.assertIn("Updated project fact (claim record #2)", str(confirmed))

    def test_bridging_stays_inside_the_project(self) -> None:
        agent, client = self._agent(["Unknown."])
        stored = agent.run(_command("Kestrel relay", "deployed on host", "Harrier box"))
        other_project = int(self.memory.add_project("other", "@projects/other"))
        other_conversation = self.memory.new_conversation(project_id=other_project)
        self.memory.remember_explicit_project_claim(
            other_conversation,
            other_project,
            _command("Harrier box", "datacenter", "Fenwick"),
        )
        agent.run(
            "Which datacenter hosts the Kestrel relay?",
            conversation_id=stored.conversation_id,
        )
        block = client.last_claims_block()
        self.assertIn("Harrier box", block)
        self.assertNotIn("Fenwick", block)
        self.assertNotIn("bridge_from", block)

    # --- M2 slice 2: retracted history and the memory tool's identity --------

    def test_retracted_fact_answers_a_temporal_question_in_a_fresh_conversation(self) -> None:
        agent, _client = self._agent()
        stored = agent.run(_command("Kestrel relay", "listen port", "9090"))
        forgotten = agent.run(
            _forget("Kestrel relay", "listen port"), conversation_id=stored.conversation_id
        )
        self.assertIn("Retracted project fact", str(forgotten))
        self.assertEqual(
            self.memory.current_claims("Kestrel relay listen port", project_id=1), []
        )

        fresh_agent, fresh_client = self._agent(
            ["It used to be 9090; that fact was retracted and has no current value."]
        )
        result = fresh_agent.run("What used to be the Kestrel relay listen port?")

        self.assertEqual(result.status, "complete", result.reason)
        self.assertEqual(result.tool_calls, 0)
        self.assertEqual(len(fresh_client.requests), 1)
        block = fresh_client.last_claims_block()
        claims = json.loads(block)
        self.assertEqual(
            [(item["value"], item["status"], item["retracted"]) for item in claims],
            [("9090", "superseded", True)],
        )
        self.assertTrue(claims[0]["superseded_at"])
        self.assertNotIn("not_recorded", block)
        # The guidance rides with the block in the user turn; the compacted
        # contract is unchanged.
        self.assertIn("retracted true is a former value", fresh_client.last_request_text())
        self.assertIn("9090", str(result))
        self.assertNotIn("Not stored", str(result))

    def test_temporal_question_mixes_current_facts_with_retracted_history(self) -> None:
        agent, _client = self._agent()
        stored = agent.run(_command("Kestrel relay", "datacenter", "Fenwick"))
        agent.run(
            _command("Kestrel relay", "listen port", "8080"),
            conversation_id=stored.conversation_id,
        )
        agent.run(
            _forget("Kestrel relay", "listen port"), conversation_id=stored.conversation_id
        )

        fresh_agent, fresh_client = self._agent(["It used to be 8080."])
        fresh_agent.run("What used to be the Kestrel relay listen port?")

        block = fresh_client.last_claims_block()
        by_value = {item["value"]: item for item in json.loads(block)}
        self.assertEqual(by_value["Fenwick"]["status"], "active")
        self.assertNotIn("retracted", by_value["Fenwick"])
        self.assertEqual(by_value["8080"]["status"], "superseded")
        self.assertTrue(by_value["8080"]["retracted"])
        self.assertNotIn("not_recorded", block)

    def test_retracted_project_value_surfaces_when_a_global_row_is_current(self) -> None:
        # Only active or disputed project rows shadow a global key, so after a
        # Forget the main read returns the global row for the key; the
        # retracted project value must still surface from history.
        for value in ("7070", "7071"):
            self.memory.remember_claim(
                "Kestrel relay", "listen port", value,
                source="fixture", authority="verified", source_identity="fixture",
            )
        agent, _client = self._agent()
        stored = agent.run(_command("Kestrel relay", "listen port", "8080"))
        agent.run(
            _forget("Kestrel relay", "listen port"), conversation_id=stored.conversation_id
        )

        fresh_agent, fresh_client = self._agent(
            ["In this project it used to be 8080; the global value is 7071."]
        )
        fresh_agent.run("What used to be the Kestrel relay listen port?")

        block = fresh_client.last_claims_block()
        by_value = {item["value"]: item for item in json.loads(block)}
        self.assertEqual(by_value["7071"]["status"], "active")
        self.assertEqual(by_value["7070"]["status"], "superseded")
        self.assertNotIn("retracted", by_value["7070"])
        self.assertEqual(by_value["8080"]["status"], "superseded")
        self.assertTrue(by_value["8080"]["retracted"])
        self.assertTrue(by_value["8080"]["superseded_at"])
        self.assertNotIn("not_recorded", block)
        self.assertIn("retracted true is a former value", fresh_client.last_request_text())

    def test_present_tense_question_does_not_surface_retracted_history(self) -> None:
        agent, _client = self._agent()
        stored = agent.run(_command("Kestrel relay", "listen port", "9090"))
        agent.run(
            _forget("Kestrel relay", "listen port"), conversation_id=stored.conversation_id
        )
        fresh_agent, fresh_client = self._agent(["Not recorded."])
        fresh_agent.run("What port does the Kestrel relay listen on?")
        block = fresh_client.last_claims_block()
        self.assertNotIn("9090", block)
        self.assertIn("not_recorded", block)

    def test_retracted_history_screens_secret_and_private_values(self) -> None:
        stamp = "2026-09-03T10:00:00+00:00"

        def row(predicate: str, value: str) -> dict[str, object]:
            return {
                "subject": "Kestrel relay",
                "predicate": predicate,
                "value": value,
                "authority": "operator",
                "status": "superseded",
                "valid_until": stamp,
                "updated_at": stamp,
                "retracted": True,
            }

        tainted = [
            row("api token", "AKIAIOSFODNN7EXAMPLE"),
            row("owner email", "dana@example.com"),
            row("listen port", "8080"),
        ]
        agent, client = self._agent(["It used to be 8080."])
        with patch.object(
            Memory, "subject_claim_history", create=True, return_value=tainted
        ):
            agent.run("What used to be the Kestrel relay listen port?")
        block = client.last_claims_block()
        self.assertIn("8080", block)
        self.assertNotIn("AKIA", block)
        self.assertNotIn("example.com", block)
        self.assertNotIn("api token", block)

    def test_temporal_question_without_history_still_gets_the_abstention_cue(self) -> None:
        agent, client = self._agent(["Not recorded."])
        agent.run("What used to be the Osprey relay listen port?")
        block = client.last_claims_block()
        self.assertIn("not_recorded", block)
        self.assertIn("Osprey relay", block)
        self.assertNotIn("superseded", block)

    def test_erase_removes_the_value_from_temporal_answers(self) -> None:
        agent, _client = self._agent()
        stored = agent.run(_command("Kestrel relay", "listen port", "9090"))
        agent.run(
            _forget("Kestrel relay", "listen port"), conversation_id=stored.conversation_id
        )
        erased = agent.run(
            _erase("Kestrel relay", "listen port"), conversation_id=stored.conversation_id
        )
        self.assertIn("Erased project fact", str(erased))

        fresh_agent, fresh_client = self._agent(["Not recorded."])
        fresh_agent.run("What used to be the Kestrel relay listen port?")
        block = fresh_client.last_claims_block()
        self.assertNotIn("9090", block)
        self.assertIn("not_recorded", block)

    def test_memory_tool_write_records_the_model_actor(self) -> None:
        agent, client = self._agent(
            [
                ToolCallResponse(
                    "remember",
                    {"content": "The sprint demo is on Thursday.", "kind": "fact"},
                ),
                "Saved: the sprint demo is on Thursday.",
            ]
        )
        result = agent.run("Remember this fact for later: the sprint demo is on Thursday.")

        self.assertEqual(result.status, "complete", result.reason)
        self.assertIsNone(getattr(agent.toolbox, "memory_write_context", None))
        row = self.memory.db.execute(
            "SELECT id FROM memories WHERE content=? AND kind='fact'",
            ("The sprint demo is on Thursday.",),
        ).fetchone()
        self.assertIsNotNone(row)
        event = self.memory.db.execute(
            """SELECT actor, permission, conversation_id, outcome
               FROM memory_spine_events
               WHERE kind='memory.created' AND subject_kind='memory' AND subject_id=?
               ORDER BY id DESC LIMIT 1""",
            (int(row[0]),),
        ).fetchone()
        self.assertIsNotNone(event, "the memory tool write left no memory.created event")
        self.assertEqual(tuple(event), (
            "model",
            "autonomous:interactive:explicit_memory_write",
            result.conversation_id,
            "applied",
        ))

    def test_memory_tool_context_is_reset_after_the_call(self) -> None:
        agent, _client = self._agent(
            [
                ToolCallResponse(
                    "remember",
                    {"content": "The sprint demo is on Thursday.", "kind": "fact"},
                ),
                "Saved.",
            ]
        )
        seen: list[object] = []
        original = agent.toolbox.execute

        def spy(name: str, arguments: dict[str, object]) -> str:
            seen.append((name, dict(getattr(agent.toolbox, "memory_write_context", None) or {})))
            return original(name, arguments)

        with patch.object(agent.toolbox, "execute", side_effect=spy):
            agent.run("Remember this fact for later: the sprint demo is on Thursday.")
        self.assertEqual(
            [item[0] for item in seen], ["remember"], "exactly one memory tool call was dispatched"
        )
        self.assertEqual(seen[0][1]["actor"], "model")
        self.assertEqual(seen[0][1]["permission"], "autonomous:interactive:explicit_memory_write")
        self.assertIsNone(agent.toolbox.memory_write_context)


class AgentTemporalGraphTests(unittest.TestCase):
    """VTMF M3, the surface half: the graph channel reaches the block in both
    directions and at three hops, marks what it could not finish, keeps every
    screen and scope floor, and never announces a live chain as retracted.

    Every question runs in a NEW conversation with a scripted client, because
    a same-conversation transcript answers on its own and would inflate the
    result (the fresh-conversation rule of the live battery).
    """

    def setUp(self) -> None:
        relax_graph_time_budget(self)
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
        self.db_path = data_dir / "agent.db"
        self.memory = Memory(self.db_path)
        self.events: list[str] = []
        self.project_id = 1

    def tearDown(self) -> None:
        self.memory.close()
        self.temp.cleanup()

    def _agent(self, replies: list[object] | None = None) -> tuple[Agent, ScriptedModelClient]:
        client = ScriptedModelClient(list(replies or []))
        agent = Agent(
            self.config,
            self.memory,
            self.events.append,
            client=client,
            coding_review=False,
            coding_planning=False,
        )
        return agent, client

    def _store(self, *triples: tuple[str, str, str], project_id: int | None = None) -> None:
        """Seed through the public governed writer, never raw SQL."""
        project = int(project_id or self.project_id)
        conversation = self.memory.new_conversation(project_id=project)
        for subject, predicate, value in triples:
            self.memory.remember_explicit_project_claim(
                conversation, project, _command(subject, predicate, value)
            )

    def _seed_chain(self) -> None:
        """The design 1.1 store: relay -> box -> datacenter -> region."""
        self._store(
            ("Kestrel relay", "deployed on host", "Harrier box"),
            ("Harrier box", "datacenter", "Fenwick"),
            ("Fenwick", "region", "Northgate"),
        )

    def _ask(self, question: str, reply: str = "Understood.") -> tuple[str, str]:
        """One question in a FRESH conversation; returns (block, whole request)."""
        agent, client = self._agent([reply])
        agent.run(question)
        return client.last_claims_block(), client.last_request_text()

    def _rows(self, block: str) -> list[dict[str, object]]:
        return json.loads(block) if block.strip() else []

    def _by_key(self, block: str) -> dict[tuple[str, str], dict[str, object]]:
        return {
            (str(row.get("subject", "")), str(row.get("predicate", ""))): row
            for row in self._rows(block)
        }

    def _block_text(self, question: str) -> str:
        """The rendered claims block of a real turn, lead sentence included.

        Exit test 7.3 asserts on "the rendered block text".  The dialogue lane
        keeps only the block itself in the per-turn wrapper and drops the
        full-prompt lead, so the lead is captured where it is written: the
        return value of ``system_prompt`` during an ordinary run.
        """
        agent, _client = self._agent(["Understood."])
        rendered: list[str] = []
        original = agent.system_prompt

        def capture(*args: object, **kwargs: object) -> str:
            text = str(original(*args, **kwargs))
            rendered.append(text)
            return text

        with patch.object(agent, "system_prompt", side_effect=capture):
            agent.run(question)
        self.assertTrue(rendered, "the turn built no system prompt")
        return rendered[-1]

    def _system_content(self, client: ScriptedModelClient) -> str:
        messages = client.requests[-1]["args"][0]
        assert isinstance(messages, list)
        return str(messages[0].get("content") or "")

    def _user_content(self, client: ScriptedModelClient) -> str:
        messages = client.requests[-1]["args"][0]
        assert isinstance(messages, list)
        for message in reversed(messages):
            if str(message.get("role")) == "user":
                return str(message.get("content") or "")
        return ""

    # --- 7.3 reversed triple and three hops ---------------------------------

    def test_forward_two_hop_question_still_answers(self) -> None:
        self._seed_chain()
        block, _text = self._ask("Which datacenter hosts the Kestrel relay?")
        by_key = self._by_key(block)
        self.assertIn(("Kestrel relay", "deployed on host"), by_key)
        self.assertEqual(by_key[("Harrier box", "datacenter")]["value"], "Fenwick")

    def test_reverse_two_hop_question_reaches_the_relay(self) -> None:
        """The one-hop bridge walked away from this answer (design 1.1, row 2).

        The forward and reverse facts about one subject are two answers, not
        one path, so the block carries two chain numbers.  Ranked on hops and
        recency alone the seed's forward walk took both slots and this reverse
        chain was dropped, which is the regression the named-before-seed and
        one-chain-per-direction rules exist to prevent.
        """
        self._seed_chain()
        block, _text = self._ask("What runs on the Harrier box?")
        self.assertIn("Kestrel relay", block)
        by_key = self._by_key(block)
        self.assertIn(("Kestrel relay", "deployed on host"), by_key)
        reverse = by_key[("Kestrel relay", "deployed on host")]
        self.assertEqual(reverse["value"], "Harrier box")
        self.assertIn("chain", reverse)
        chains = {
            int(row["chain"]) for row in self._rows(block) if "chain" in row
        }
        self.assertEqual(len(chains), 2, block)
        # This store has a third answering chain (the forward walk on to
        # Northgate) and the cap is two, so a truncation note here is correct
        # and must be counted rather than absent: what matters is that the cap
        # spends its slots on both directions, not both on the forward one.
        notes = [
            row for row in self._rows(block)
            if str(row.get("status")) == "overflow"
        ]
        for note in notes:
            # The note says what was withheld and names the node to ask for;
            # the exact wording is graph-core's, those two are the contract.
            self.assertIn("found and not shown", str(note["note"]).casefold())
            self.assertIn("by name", str(note["note"]).casefold())

    def test_reversed_triple_question_reaches_the_relay_through_the_datacenter(self) -> None:
        # The VTMF 15 residual failure: a value asked about as a subject.
        self._seed_chain()
        block, _text = self._ask("Which relay is deployed in the Fenwick datacenter?")
        self.assertIn("Kestrel relay", block)

    def test_open_reverse_question_answers_from_the_datacenter(self) -> None:
        self._seed_chain()
        block, _text = self._ask("What runs in Fenwick?")
        self.assertIn("Harrier box", block)

    def test_three_hop_question_reaches_the_region(self) -> None:
        # Design 1.1, row 4: the bridge stopped one hop short of the region.
        # The walk may start at a seed claim's value rather than at the named
        # subject, so what the exit test requires is that the region fact is
        # reached through a chain, not that it carries a particular hop
        # number.
        self._seed_chain()
        block, _text = self._ask("Which region is the Kestrel relay in?")
        self.assertIn("Northgate", block)
        chain_rows = [row for row in self._rows(block) if "chain" in row]
        self.assertTrue(
            any(
                str(row.get("subject")) == "Fenwick"
                and str(row.get("value")) == "Northgate"
                for row in chain_rows
            ),
            f"the region fact must be a chain row: {block}",
        )
        self.assertTrue(
            any(int(row.get("hop") or 0) >= 2 for row in chain_rows),
            f"the region is reached through a chain, not directly: {block}",
        )

    def test_a_stored_value_never_gets_a_not_recorded_cue(self) -> None:
        # Northgate is a stored value with two reverse hops behind it.
        self._seed_chain()
        block, _text = self._ask("Which relays are in the Northgate region?")
        self.assertNotIn("not_recorded", block)

    def test_chain_rows_carry_chain_hop_and_bridge_from(self) -> None:
        self._seed_chain()
        block, _text = self._ask("Which region is the Kestrel relay in?")
        chain_rows = [row for row in self._rows(block) if "chain" in row]
        self.assertTrue(chain_rows, block)
        for row in chain_rows:
            self.assertIsInstance(row["chain"], int)
            self.assertIsInstance(row["hop"], int)
            self.assertGreaterEqual(int(row["hop"]), 1)
        self.assertTrue(
            any("bridge_from" in row for row in chain_rows),
            "a continued chain names the hop it continues",
        )

    def test_the_hop_guidance_rides_in_the_user_turn_only(self) -> None:
        self._seed_chain()
        agent, client = self._agent(["Fenwick."])
        agent.run("Which region is the Kestrel relay in?")
        user = self._user_content(client)
        system = self._system_content(client)
        self.assertIn("continues the chain of the entry it names in bridge_from", user)
        self.assertNotIn(
            "continues the chain of the entry it names in bridge_from", system
        )

    def test_the_full_prompt_lead_carries_the_chain_clause(self) -> None:
        self._seed_chain()
        rendered = self._block_text("Which region is the Kestrel relay in?")
        self.assertIn(
            "Entries sharing a chain number are one chain of stored facts in "
            "hop order; follow it in order.",
            rendered,
        )

    # --- 7.3 the lead text (review R2) --------------------------------------

    def test_an_empty_main_lane_graph_answer_uses_the_normal_lead(self) -> None:
        # Review R2: a live chain with an empty main lane must not be
        # announced as retracted.  Both questions are the design 1.1 cases
        # whose main lane finds nothing.
        self._seed_chain()
        for question in ("What runs on the Harrier box?", "What runs in Fenwick?"):
            with self.subTest(question=question):
                rendered = self._block_text(question)
                self.assertIn("<temporal_claims>", rendered)
                self.assertNotIn("Former values only", rendered)
                self.assertIn("Runtime-versioned facts and preferences", rendered)

    def test_a_chain_of_only_superseded_edges_keeps_the_retracted_lead(self) -> None:
        # A temporal answer built entirely from history is still history, so
        # the corrected branch must not swing the other way.
        self._store(("Kestrel relay", "listen port", "9090"))
        agent, _client = self._agent(["Ok."])
        agent.run(_forget("Kestrel relay", "listen port"))
        rendered = self._block_text(
            "What was the Kestrel relay listen port before?"
        )
        self.assertIn("Former values only", rendered)

    # --- 7.4 as of, after supersession and retraction -----------------------

    def test_a_temporal_question_chains_through_a_superseded_edge(self) -> None:
        self._seed_chain()
        self._store(("Kestrel relay", "deployed on host", "Talon box"))
        block, _text = self._ask(
            "Which datacenter used to host the Kestrel relay?"
        )
        self.assertIn("Harrier box", block)
        self.assertTrue(
            any(
                str(row.get("status")) == "superseded"
                for row in self._rows(block)
            ),
            block,
        )

    def test_a_present_tense_question_carries_no_superseded_chain_row(self) -> None:
        self._seed_chain()
        self._store(("Kestrel relay", "deployed on host", "Talon box"))
        block, _text = self._ask("Which host is the Kestrel relay deployed on?")
        self.assertIn("Talon box", block)
        self.assertNotIn("superseded", block)

    def test_the_as_of_parse_takes_only_an_instant_the_operator_stated(self) -> None:
        self.assertEqual(
            Agent._question_as_of("What was it on 2026-01-05?"),
            "2026-01-05T00:00:00Z",
        )
        self.assertEqual(
            Agent._question_as_of("Where was the relay in March 2025?"),
            "2025-03-01T00:00:00Z",
        )
        for vague in (
            "What was it last week?",
            "Where was it before?",
            "on 2026-13-05",
            "in Marchtember 2025",
            "",
        ):
            with self.subTest(query=vague):
                self.assertIsNone(Agent._question_as_of(vague))

    # --- 7.5 erase removes the answer ---------------------------------------

    def test_erasing_a_link_removes_the_chain_answer(self) -> None:
        # Only the erased link disappears: Fenwick keeps its own region fact,
        # so the honest assertion is that nothing still reaches ACROSS the
        # link, in either direction.
        self._seed_chain()
        agent, _client = self._agent(["Ok."])
        agent.run(_erase("Harrier box", "datacenter"))
        forward, _text = self._ask("Which datacenter hosts the Kestrel relay?")
        self.assertNotIn("Fenwick", forward)
        reverse, _text = self._ask("What runs in Fenwick?")
        self.assertNotIn("Harrier box", reverse)
        self.assertNotIn("Kestrel relay", reverse)

    # --- 7.6 scope isolation -------------------------------------------------

    def test_a_chain_never_crosses_a_project_boundary(self) -> None:
        self._store(("Kestrel relay", "deployed on host", "Harrier box"))
        other = int(self.memory.add_project("other", "@projects/other"))
        self._store(("Harrier box", "datacenter", "Fenwick"), project_id=other)
        block, _text = self._ask("Which datacenter hosts the Kestrel relay?")
        self.assertIn("Harrier box", block)
        self.assertNotIn("Fenwick", block)

    def test_a_project_fact_shadows_the_global_one_of_the_same_key(self) -> None:
        self._store(("Kestrel relay", "deployed on host", "Harrier box"))
        self.memory.remember_claim(
            "Harrier box", "datacenter", "Old Fenwick",
            authority="operator", source="fixture",
        )
        block, _text = self._ask("Which datacenter hosts the Kestrel relay?")
        self.assertIn("Old Fenwick", block)
        self._store(("Harrier box", "datacenter", "Fenwick"))
        block, _text = self._ask("Which datacenter hosts the Kestrel relay?")
        self.assertIn("Fenwick", block)
        self.assertNotIn("Old Fenwick", block)

    # --- 7.7 secrets and private identifiers --------------------------------

    def test_a_private_value_never_becomes_a_chain_row_or_a_link(self) -> None:
        # The live claims lane keeps its narrower screen (design 6.2), so an
        # operator who stored an address still gets it back from the lane.
        # The graph is stricter: the address is a literal, never a node, and
        # no chain row may carry it or continue through it.
        self._store(("Kestrel relay", "deployed on host", "Harrier box"))
        self.memory.remember_claim(
            "Harrier box", "management address", "10.0.0.7",
            authority="operator", source="fixture",
        )
        self.memory.remember_claim(
            "10.0.0.7", "rack", "R12", authority="operator", source="fixture",
        )
        block, _text = self._ask("Which datacenter hosts the Kestrel relay?")
        chain_rows = [row for row in self._rows(block) if "chain" in row]
        for row in chain_rows:
            self.assertNotIn("10.0.0.7", str(row.get("value", "")))
            self.assertNotIn("10.0.0.7", str(row.get("subject", "")))
        self.assertNotIn("R12", block)

    def test_a_private_subject_never_reaches_the_block(self) -> None:
        self.memory.remember_claim(
            "alice@example.com", "owns", "Kestrel relay",
            authority="operator", source="fixture",
        )
        self._store(("Kestrel relay", "deployed on host", "Harrier box"))
        block, _text = self._ask("Who owns the Kestrel relay?")
        self.assertNotIn("alice@example.com", block)

    def test_the_history_helpers_screen_the_widened_shapes(self) -> None:
        # T-1 applied to the two agent-side history helpers (design 6.2,
        # review R13c): a phone number in a retracted value never reaches the
        # block, though today's narrower lane screen would have passed it.
        self._store(("Kestrel relay", "hotline", "+1 (415) 555-0199"))
        agent, _client = self._agent(["Ok."])
        agent.run(_forget("Kestrel relay", "hotline"))
        block, _text = self._ask("What was the Kestrel relay hotline before?")
        self.assertNotIn("555-0199", block)

    def test_a_private_subject_is_screened_out_of_retracted_history(self) -> None:
        # The governed write gate screens a subject with the narrow screen,
        # so a bare address is storable as a subject today; the widened screen
        # in the history helpers is the last gate before the model.
        self._store(("10.0.0.7", "hotline", "the front desk"))
        agent, _client = self._agent(["Ok."])
        agent.run(_forget("10.0.0.7", "hotline"))
        block, _text = self._ask("What was the 10.0.0.7 hotline before?")
        self.assertNotIn("front desk", block)

    # --- 7.8 incompleteness is surfaced -------------------------------------

    def test_an_unreached_predicate_shows_the_subject_facts_not_a_chain(self) -> None:
        self._store(
            ("Kestrel relay", "deployed on host", "Harrier box"),
            ("Harrier box", "datacenter", "Fenwick"),
        )
        block, text = self._ask("Which region is the Kestrel relay in?")
        self.assertNotIn("Northgate", block)
        self.assertIn("say the asked fact is not recorded", text)

    def _with_graph_result(
        self,
        question: str,
        rows: list[dict[str, object]],
        overflow: list[dict[str, object]],
        *,
        mode: str = "complete",
        lane_abstained: bool = False,
        unresolved: list[str] | None = None,
    ) -> ScriptedModelClient:
        """Run one turn with a fixed store result, to drive the cue itself.

        Whether a particular store overflows is graph-core's exit test; what
        the surface owes is that an overflow the store reports becomes a
        bounded, named cue row with its guidance.
        """
        self._store(("Kestrel relay", "deployed on host", "Harrier box"))
        agent, client = self._agent(["Understood."])
        result = {
            "rows": rows,
            "overflow": overflow,
            "report": {
                "channel": "graph",
                "mode": mode,
                "lane_abstained": lane_abstained,
                "overflow": len(overflow),
                "unresolved": list(unresolved or []),
            },
        }
        self._rendered = []
        original = agent.system_prompt

        def capture(*args: object, **kwargs: object) -> str:
            text = str(original(*args, **kwargs))
            self._rendered.append(text)
            return text

        with patch.object(
            type(self.memory), "graph_chains", return_value=result
        ):
            with patch.object(agent, "system_prompt", side_effect=capture):
                agent.run(question)
        return client

    def test_at_most_two_overflow_notes_reach_the_block(self) -> None:
        client = self._with_graph_result(
            "What is attached to the Harrier box?",
            [{
                "subject": "Harrier box", "predicate": "datacenter",
                "value": "Fenwick", "status": "active", "authority": "operator",
                "confidence": 1.0, "chain": 1, "hop": 1, "incomplete": True,
            }],
            [
                {"subject": f"Hub{index}", "hop": 2, "cap": 64,
                 "direction": "in"}
                for index in range(4)
            ],
        )
        rows = json.loads(client.last_claims_block())
        overflow_rows = [row for row in rows if str(row.get("status")) == "overflow"]
        self.assertEqual(len(overflow_rows), 2, rows)
        for row in overflow_rows:
            self.assertIn("hop 2", str(row["note"]))
            self.assertIn("Ask about one by name", str(row["note"]))
            self.assertEqual(row["hop"], 2)

    def test_a_store_supplied_note_is_used_verbatim_and_clipped(self) -> None:
        client = self._with_graph_result(
            "Which relays are in the Northgate region?",
            [{
                "subject": "Northgate", "predicate": "", "value": "",
                "status": "overflow", "hop": 2, "chain": 1,
                "note": "40 stored facts answer this; the 8 strongest are "
                        "shown. Ask about one by name for the rest.",
            }],
            [],
        )
        block = client.last_claims_block()
        self.assertIn("40 stored facts answer this", block)

    def test_the_overflow_and_incomplete_guidance_reach_the_user_turn(self) -> None:
        client = self._with_graph_result(
            "What is attached to the Harrier box?",
            [{
                "subject": "Harrier box", "predicate": "datacenter",
                "value": "Fenwick", "status": "active", "authority": "operator",
                "confidence": 1.0, "chain": 1, "hop": 1, "incomplete": True,
            }],
            [{"subject": "Harrier box", "hop": 2, "cap": 64, "direction": "in"}],
        )
        user = self._user_content(client)
        system = self._system_content(client)
        for line in (
            "not more of what was asked",
            "could not finish reading",
        ):
            with self.subTest(line=line):
                self.assertIn(line, user)
                self.assertNotIn(line, system)

    def test_the_lane_abstained_clause_rides_with_an_exact_chain_answer(self) -> None:
        client = self._with_graph_result(
            "Which host is the Kestrel relay deployed on?",
            [{
                "subject": "Kestrel relay", "predicate": "deployed on host",
                "value": "Harrier box", "status": "active",
                "authority": "operator", "confidence": 1.0, "chain": 1, "hop": 1,
            }],
            [],
            lane_abstained=True,
        )
        del client
        self.assertTrue(self._rendered)
        self.assertIn(memory_graph.LANE_ABSTAINED_CLAUSE, self._rendered[-1])

    def test_a_large_hub_never_crowds_the_block_past_its_budget(self) -> None:
        self._store(("Kestrel relay", "deployed on host", "Harrier box"))
        for index in range(40):
            self._store(("Harrier box", f"attached device {index}", f"Node{index}"))
        block, _text = self._ask("What is attached to the Harrier box?")
        self.assertLessEqual(len(block), 4_200, f"block was {len(block)} characters")
        chain_rows = [row for row in self._rows(block) if "chain" in row]
        self.assertLessEqual(len(chain_rows), 8, chain_rows)

    def test_the_block_stays_inside_its_budget_with_long_values(self) -> None:
        # Design 5.8: a main-lane row can reach about 1 KB, so the whole block
        # is measured against the real worst case, not the chain row alone.
        long_value = "Fenwick " + ("capacity note " * 45)
        self._store(
            ("Kestrel relay", "deployed on host", "Harrier box"),
            ("Harrier box", "datacenter", long_value[:600].strip()),
        )
        for index in range(4):
            self._store(
                ("Kestrel relay", f"note {index}", ("long detail " * 50)[:600].strip())
            )
        block, _text = self._ask("Which datacenter hosts the Kestrel relay?")
        self.assertLessEqual(len(block), 4_200, f"block was {len(block)} characters")

    # --- 7.15 identity floors ------------------------------------------------

    def test_an_exactly_spelled_subject_answers_beside_its_look_alikes(self) -> None:
        # Design 2.3a: an exact key names one stored subject, so the chain
        # starts there and at no look-alike.  The claims lane keeps its own
        # substring behaviour (design 9.1: M3 does not change that lane), so
        # the floor is asserted on the chain rows.
        self._store(
            ("Kestrel relay", "deployed on host", "Harrier box"),
            ("Kestrel relay 2", "deployed on host", "Talon box"),
            ("Kestrelrelay", "deployed on host", "Merlin box"),
        )
        block, _text = self._ask("Which host is the Kestrel relay deployed on?")
        self.assertIn("Harrier box", block)
        chain_subjects = {
            str(row.get("subject", ""))
            for row in self._rows(block)
            if "chain" in row
        }
        self.assertNotIn("Kestrel relay 2", chain_subjects)
        self.assertNotIn("Kestrelrelay", chain_subjects)

    def test_two_exactly_named_subjects_both_start_a_chain(self) -> None:
        self._store(
            ("Kestrel relay", "deployed on host", "Harrier box"),
            ("Osprey relay", "deployed on host", "Talon box"),
        )
        block, _text = self._ask(
            "Is the Kestrel relay on the same host as the Osprey relay?"
        )
        self.assertIn("Harrier box", block)
        self.assertIn("Talon box", block)

    # --- the bridge fallback (design 5.10) ----------------------------------

    def test_a_store_without_the_graph_still_bridges_one_hop(self) -> None:
        self._store(
            ("Kestrel relay", "deployed on host", "Harrier box"),
            ("Harrier box", "datacenter", "Fenwick"),
        )
        agent, client = self._agent(["Fenwick."])
        with patch.object(
            type(self.memory), "graph_chains", side_effect=AttributeError("no graph")
        ):
            agent.run("Which datacenter hosts the Kestrel relay?")
        by_key = self._by_key(client.last_claims_block())
        bridged = by_key[("Harrier box", "datacenter")]
        self.assertEqual(bridged["bridge_from"], "Kestrel relay / deployed on host")
        self.assertNotIn("chain", bridged)

    # --- the alias rule is one function (design 2.3, exit test 7.17) --------

    def test_two_channels_reporting_one_claim_emit_one_merged_row(self) -> None:
        # The graph knows the hop, the history helper knows it was retracted.
        # Both must reach the model, once (design 5.8: deduplicated by claim
        # id, which is why the id travels through the whitelist privately).
        rows = agent_module._merge_duplicate_claim_rows([
            {"subject": "Kestrel relay", "predicate": "listen port",
             "value": "9090", "status": "superseded", "_claim_id": 7,
             "chain": 1, "hop": 1, "weakest": True},
            {"subject": "Kestrel relay", "predicate": "listen port",
             "value": "9090", "status": "superseded", "_claim_id": 7,
             "retracted": True, "superseded_at": "2026-09-03T00:00:00+00:00"},
        ])
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["hop"], 1)
        self.assertTrue(rows[0]["retracted"])
        self.assertTrue(rows[0]["superseded_at"])

    def test_distinct_claims_that_read_alike_are_not_merged(self) -> None:
        rows = agent_module._merge_duplicate_claim_rows([
            {"subject": "Kestrel relay", "predicate": "listen port",
             "value": "9090", "status": "active", "_claim_id": 7},
            {"subject": "Kestrel relay", "predicate": "listen port",
             "value": "9090", "status": "active", "_claim_id": 8},
        ])
        self.assertEqual(len(rows), 2, rows)

    def test_rows_without_a_claim_id_still_merge_by_their_text(self) -> None:
        rows = agent_module._merge_duplicate_claim_rows([
            {"subject": "Northgate", "predicate": "", "value": "",
             "status": "overflow", "hop": 2},
            {"subject": "Northgate", "predicate": "", "value": "",
             "status": "overflow", "hop": 2, "note": "more than 64"},
        ])
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["note"], "more than 64")

    def test_the_private_claim_id_never_reaches_the_block(self) -> None:
        self._seed_chain()
        for question in (
            "Which datacenter hosts the Kestrel relay?",
            "What runs on the Harrier box?",
            "What was the Kestrel relay listen port before?",
        ):
            with self.subTest(question=question):
                block, text = self._ask(question)
                self.assertNotIn(agent_module._CLAIM_ID_KEY, block)
                self.assertNotIn(agent_module._CLAIM_ID_KEY, text)
                for row in self._rows(block):
                    self.assertNotIn("claim_id", row)

    def test_a_seed_look_alike_of_an_exactly_spelled_name_is_dropped(self) -> None:
        # The claims lane matches look-alikes by substring and offers them as
        # seeds.  A seed is exempt from the identity floor in general, but not
        # when it is a look-alike of a name the operator spelled exactly - the
        # whole seed row goes, because its other endpoint is a fact about the
        # look-alike and would walk the chain straight back to it.
        self._store(
            ("Kestrel relay", "deployed on host", "Harrier box"),
            ("Harrier box", "datacenter", "Fenwick"),
            ("Kestrel relay 2", "deployed on host", "Talon box"),
            ("Talon box", "datacenter", "Moss Hollow"),
        )
        block, _text = self._ask("Which datacenter hosts the Kestrel relay?")
        self.assertIn("Fenwick", block)
        # The claims lane still matches the look-alike by substring and M3
        # does not change that lane, so the floor is asserted on what the
        # graph controls: no chain row names the look-alike or its host, and
        # "Moss Hollow" - two hops behind it, reachable only by a walk that
        # continued through it - never appears at all.
        chain_names = {
            str(row.get(field, ""))
            for row in self._rows(block)
            if "chain" in row
            for field in ("subject", "value")
        }
        self.assertNotIn("Kestrel relay 2", chain_names)
        self.assertNotIn("Talon box", chain_names)
        self.assertNotIn("Moss Hollow", block)

    def test_a_typed_subject_that_resolves_only_loosely_abstains_the_call(self) -> None:
        # The opposite rule, and the reason the two are pinned together: a
        # name the OPERATOR typed that resolves only non-exactly abstains the
        # whole call, even beside one that resolved exactly.
        self._store(
            ("Kestrel relay", "deployed on host", "Harrier box"),
            ("Kestrel relay 2", "deployed on host", "Talon box"),
            ("Harrier box", "datacenter", "Fenwick"),
        )
        block, _text = self._ask(
            "Which datacenter hosts the Harrier box and the Kestrel rely?"
        )
        chain_rows = [row for row in self._rows(block) if "chain" in row]
        self.assertEqual(chain_rows, [], block)

    def test_a_redaction_placeholder_never_joins_two_facts(self) -> None:
        # remember_claim rewrites a secret-shaped value to [REDACTED], so two
        # unrelated credentials become one string; as a node it would join the
        # facts about both.  It is a terminal hop instead.
        self._store(("Kestrel relay", "deployed on host", "Harrier box"))
        for subject in ("Alpha probe", "Beta probe"):
            self.memory.remember_claim(
                subject, "api token", "sk-" + "a" * 32,
                authority="operator", source="fixture",
            )
        block, _text = self._ask("What is the Alpha probe api token?")
        self.assertNotIn("Beta probe", block)

    def test_the_graph_deadline_is_relaxed_for_this_class(self) -> None:
        # Insurance for the insurance: if a later edit drops the setUp call,
        # or the product stops reading the constant per call, this fails here
        # instead of as an occasional chain-row flake under load.
        self.assertEqual(memory_graph.TIME_BUDGET_MS, GRAPH_TEST_TIME_BUDGET_MS)
        self.assertEqual(
            self.memory.graph_chains("", project_id=1, subjects=[], seed_claims=[])
            ["report"]["channel"],
            "graph",
        )

    def test_the_chain_clause_reaches_the_dialogue_lane(self) -> None:
        """H-3: the dialogue lane keeps the block and drops the full-prompt
        lead, so the clause that lives beside the block has to ride with it.

        This is the lane most memory questions take, so a clause that only
        ever appears on the other lanes is a clause the model rarely sees.
        """
        self._seed_chain()
        agent, client = self._agent(["Fenwick."])
        agent.run("Which region is the Kestrel relay in?")
        user = self._user_content(client)
        system = self._system_content(client)
        self.assertIn('"chain":', user)
        self.assertIn(_CHAIN_LEAD_CLAUSE, user)
        # The compacted runtime contract gains nothing.
        self.assertNotIn(_CHAIN_LEAD_CLAUSE, system)

    def test_the_lane_abstained_clause_reaches_the_dialogue_lane(self) -> None:
        client = self._with_graph_result(
            "Which host is the Kestrel relay deployed on?",
            [{
                "subject": "Kestrel relay", "predicate": "deployed on host",
                "value": "Harrier box", "status": "active",
                "authority": "operator", "confidence": 1.0, "chain": 1, "hop": 1,
            }],
            [],
            lane_abstained=True,
        )
        user = self._user_content(client)
        self.assertIn('"lane_abstained":true', user)
        self.assertIn(memory_graph.LANE_ABSTAINED_CLAUSE, user)
        self.assertNotIn(
            memory_graph.LANE_ABSTAINED_CLAUSE, self._system_content(client)
        )

    def test_the_lane_abstained_marker_is_absent_when_the_lane_resolved(self) -> None:
        self._seed_chain()
        agent, client = self._agent(["Fenwick."])
        agent.run("Which region is the Kestrel relay in?")
        user = self._user_content(client)
        self.assertNotIn("lane_abstained", user)
        self.assertNotIn(memory_graph.LANE_ABSTAINED_CLAUSE, user)

    def test_the_marker_is_carried_by_one_chain_row_only(self) -> None:
        client = self._with_graph_result(
            "Which host is the Kestrel relay deployed on?",
            [
                {"subject": "Kestrel relay", "predicate": "deployed on host",
                 "value": "Harrier box", "status": "active",
                 "authority": "operator", "confidence": 1.0,
                 "chain": 1, "hop": 1},
                {"subject": "Harrier box", "predicate": "datacenter",
                 "value": "Fenwick", "status": "active",
                 "authority": "operator", "confidence": 1.0,
                 "chain": 1, "hop": 2},
            ],
            [],
            lane_abstained=True,
        )
        block = client.last_claims_block()
        self.assertEqual(block.count('"lane_abstained":true'), 1, block)

    def test_a_chain_number_identifies_a_start_not_a_rank(self) -> None:
        # Two exactly named subjects come back as two numbered chains, each
        # hopping 1..n from its own start, so the model can tell which fact
        # belongs to which name.
        self._store(
            ("Kestrel relay", "deployed on host", "Harrier box"),
            ("Harrier box", "datacenter", "Fenwick"),
            ("Osprey relay", "deployed on host", "Talon box"),
            ("Talon box", "datacenter", "Moss Hollow"),
        )
        block, _text = self._ask(
            "Which datacenter hosts the Kestrel relay and the Osprey relay?"
        )
        by_chain: dict[int, list[dict[str, object]]] = {}
        for row in self._rows(block):
            if "chain" in row:
                by_chain.setdefault(int(row["chain"]), []).append(row)
        self.assertEqual(len(by_chain), 2, block)
        for number, rows in by_chain.items():
            hops = [int(row["hop"]) for row in rows]
            self.assertEqual(hops, sorted(hops), f"chain {number} out of order")
            self.assertEqual(hops[0], 1, f"chain {number} does not start at 1")
            # A chain never mixes two starts' facts.
            values = {str(row["value"]) for row in rows}
            self.assertFalse(
                {"Fenwick", "Moss Hollow"} <= values,
                f"chain {number} spans both starts: {rows}",
            )

    def test_dropped_chains_are_counted_in_the_block(self) -> None:
        # CHAIN_CAP drops answering chains; the block must say how many rather
        # than letting the ones that fit read as the whole answer.
        client = self._with_graph_result(
            "What is attached to the Harrier box?",
            [{
                "subject": "Harrier box", "predicate": "datacenter",
                "value": "Fenwick", "status": "active", "authority": "operator",
                "confidence": 1.0, "chain": 1, "hop": 1,
            }],
            [{
                "subject": "Harrier box", "hop": 1, "cap": 16, "direction": "out",
                "note": "3 more chains found and not shown; the first continues "
                        "from Fenwick. Ask about Fenwick by name.",
            }],
        )
        block = client.last_claims_block()
        # The sentence is graph-core's to word; what the surface owes is that
        # it reaches the block intact and that the guidance tells the model
        # what an overflow entry means.  Pin the contract, not the prose.
        self.assertIn("3 more chains", block)
        self.assertIn("found and not shown", block.casefold())
        self.assertIn("by name", block.casefold())
        self.assertIn(
            "not more of what was asked", self._user_content(client).casefold()
        )

    def test_a_name_the_store_never_saw_leaves_the_cue_to_do_its_job(self) -> None:
        """The third identity case, and the one that reaches my surface.

        A misspelling of a stored key abstains ``identity-conflict``; a
        lane-supplied seed look-alike is dropped; but a name the store has
        never seen must resolve to nothing at all, because that is what lets
        the not_recorded cue fire instead of the block sitting empty.
        """
        self._seed_chain()
        block, text = self._ask("Which datacenter hosts the Zephyr gadget?")
        self.assertIn("not_recorded", block)
        self.assertIn("Zephyr gadget", block)
        # The dialogue lane carries the guidance line, not the full-prompt
        # lead, so this asserts the wording that actually reaches the model.
        self.assertIn("say it is not recorded", text)
        # An unknown name must not drag the stored chain in behind it.
        self.assertNotIn("Fenwick", block)

    def test_scope_never_reaches_a_rendered_block(self) -> None:
        """Chain rows carry `scope` so the store can shadow correctly; the
        model must never see it.

        A scope names the project a fact belongs to, which is store
        bookkeeping, not something the model should reason about or repeat -
        and the whitelist is the only thing standing between a store-side
        column and the prompt.  Both paths are covered: real rows from the
        store, and a synthetic row that carries scope explicitly, so the test
        still means something if the store stops sending it.
        """
        self._seed_chain()
        for question in (
            "Which datacenter hosts the Kestrel relay?",
            "What runs on the Harrier box?",
            "Which region is the Kestrel relay in?",
        ):
            with self.subTest(question=question):
                block, _text = self._ask(question)
                self.assertNotIn("scope", block)
                self.assertNotIn("project:", block)
                for row in self._rows(block):
                    self.assertNotIn("scope", row)

        client = self._with_graph_result(
            "Which host is the Kestrel relay deployed on?",
            [{
                "subject": "Kestrel relay", "predicate": "deployed on host",
                "value": "Harrier box", "status": "active",
                "authority": "operator", "confidence": 1.0,
                "chain": 1, "hop": 1, "scope": "project:1",
            }],
            [],
        )
        block = client.last_claims_block()
        self.assertIn("Harrier box", block)
        self.assertNotIn("scope", block)
        self.assertNotIn("project:1", block)

    def test_the_reverse_region_question_reaches_the_relay(self) -> None:
        """Design 1.1, row 5: "Which relays are in the Northgate region?" used
        to get a not_recorded cue for a name with two reverse hops of facts
        behind it.  It must now walk back through the datacenter to the relay.
        """
        self._seed_chain()
        block, text = self._ask("Which relays are in the Northgate region?")
        self.assertIn("Kestrel relay", block)
        self.assertNotIn("not_recorded", block)
        chain_rows = [row for row in self._rows(block) if "chain" in row]
        self.assertTrue(chain_rows, block)
        reached = {
            (str(row.get("subject", "")), str(row.get("value", "")))
            for row in chain_rows
        }
        self.assertIn(("Fenwick", "Northgate"), reached)
        self.assertIn(("Kestrel relay", "Harrier box"), reached)
        # A three-hop walk back is still one chain the model must follow.
        self.assertIn("bridge_from", text)

    def test_the_lane_abstained_clause_survives_a_non_exact_answer(self) -> None:
        """The clause has to keep working now that the lane gate is gone.

        Before design 10.3 an abstaining lane forced the graph to exact names,
        so "the lane could not tell which subject this names" always arrived
        beside an exactly spelled one.  The gate is gone: the graph now
        resolves "Kestrel" to "Kestrel relay" on its own floors while the lane
        is still abstaining, which is exactly the case where the operator most
        needs to be told the main lane could not resolve the name.  Driven
        through the real store, with only the lane mode forced, so the chain
        below is a genuine non-exact resolution.
        """
        self._store(
            ("Kestrel relay", "deployed on host", "Harrier box"),
            ("Harrier box", "datacenter", "Fenwick"),
        )
        agent, client = self._agent(["Fenwick."])
        rendered: list[str] = []
        original = agent.system_prompt

        def capture(*args: object, **kwargs: object) -> str:
            text = str(original(*args, **kwargs))
            rendered.append(text)
            return text

        with patch.object(
            type(self.memory), "claim_recall_report",
            return_value={"mode": "identity-conflict", "abstained": True},
        ):
            with patch.object(agent, "system_prompt", side_effect=capture):
                agent.run("Which datacenter hosts Kestrel?")

        block = client.last_claims_block()
        by_key = {
            (str(row.get("subject", "")), str(row.get("predicate", "")))
            for row in json.loads(block)
        }
        # The graph resolved a name the operator did not spell in full.
        self.assertIn(("Kestrel relay", "deployed on host"), by_key)
        self.assertIn(("Harrier box", "datacenter"), by_key)
        self.assertIn('"lane_abstained":true', block)
        # Both lanes carry it: the wrapper for dialogue, the lead for the rest.
        self.assertIn(
            memory_graph.LANE_ABSTAINED_CLAUSE, self._user_content(client)
        )
        self.assertTrue(rendered)
        self.assertIn(memory_graph.LANE_ABSTAINED_CLAUSE, rendered[-1])

    def test_an_unidentified_second_name_is_reported_beside_the_answer(self) -> None:
        """Design 10.7 item 4, the case the holdout found.

        A question naming two subjects where only one resolves is answered
        from the one that did.  Without a word about the other, a half answer
        reads as a whole one - the operator asked about two things and is
        shown facts about one, with nothing to say the second was never
        looked up.
        """
        client = self._with_graph_result(
            "Which datacenter hosts the Thornbeck bolt and the Tarnworth mill?",
            [{
                "subject": "Thornbeck bolt", "predicate": "deployed on host",
                "value": "Harrier box", "status": "active",
                "authority": "operator", "confidence": 1.0, "chain": 1, "hop": 1,
            }],
            [],
            unresolved=["Tarnworth mill"],
        )
        block = client.last_claims_block()
        user = self._user_content(client)
        self.assertIn("Thornbeck bolt", block)
        self.assertIn("The store has no recorded fact about: Tarnworth mill.", user)
        # The wrapper only: the compacted contract gains nothing.
        self.assertNotIn("Tarnworth mill", self._system_content(client))

    def test_no_unresolved_line_when_the_graph_answered_nothing(self) -> None:
        # With no rows the block is already an abstention; a second cue saying
        # the same thing in different words is noise.
        client = self._with_graph_result(
            "Which datacenter hosts the Tarnworth mill?",
            [],
            [],
            mode="no-start",
            unresolved=["Tarnworth mill"],
        )
        self.assertNotIn(
            "The store has no recorded fact about", self._user_content(client)
        )

    def test_no_double_cue_for_a_name_the_not_recorded_entry_carries(self) -> None:
        # The not_recorded cue and this line answer the same question about
        # the same name; the operator must be told once.
        self._seed_chain()
        block, text = self._ask("Which datacenter hosts the Tarnworth mill?")
        if "not_recorded" in block:
            self.assertIn("Tarnworth mill", block)
            self.assertNotIn("The store has no recorded fact about", text)

    def test_the_unresolved_line_is_capped_and_screened_like_every_cue(self) -> None:
        client = self._with_graph_result(
            "Which datacenter hosts the Thornbeck bolt?",
            [{
                "subject": "Thornbeck bolt", "predicate": "deployed on host",
                "value": "Harrier box", "status": "active",
                "authority": "operator", "confidence": 1.0, "chain": 1, "hop": 1,
            }],
            [],
            unresolved=["Tarnworth mill", "Alder hall", "Wenlock fold"],
        )
        user = self._user_content(client)
        self.assertIn("Tarnworth mill", user)
        self.assertIn("Alder hall", user)
        # Capped at two, like the overflow notes.
        self.assertNotIn("Wenlock fold", user)

    def test_an_unresolved_name_does_not_leak_into_the_block(self) -> None:
        client = self._with_graph_result(
            "Which datacenter hosts the Thornbeck bolt and the Tarnworth mill?",
            [{
                "subject": "Thornbeck bolt", "predicate": "deployed on host",
                "value": "Harrier box", "status": "active",
                "authority": "operator", "confidence": 1.0, "chain": 1, "hop": 1,
            }],
            [],
            unresolved=["Tarnworth mill"],
        )
        # The name is a guidance line, not a row: the store could not identify
        # it, so there is no fact to put in the evidence block.
        self.assertNotIn("Tarnworth mill", client.last_claims_block())

    def test_two_subjects_one_unknown_answers_and_names_the_unknown(self) -> None:
        """The end-to-end form of item 4, through the real store."""
        self._store(
            ("Thornbeck bolt", "deployed on host", "Harrier box"),
            ("Harrier box", "datacenter", "Fenwick"),
        )
        block, text = self._ask(
            "Which datacenter hosts the Thornbeck bolt and the Tarnworth mill?"
        )
        self.assertIn("Thornbeck bolt", block)
        self.assertIn("Fenwick", block)
        self.assertIn("The store has no recorded fact about: Tarnworth mill.", text)

    def test_an_ambiguous_alias_abstains_and_names_nothing(self) -> None:
        """Item 1: two candidates for a one-word alias abstain the call.

        An abstention is not an unidentified name - the store knows both
        Loom8 keys perfectly well and cannot tell which was meant, so the
        unresolved line must stay silent rather than claim nothing is
        recorded about a name the store holds twice.
        """
        self._store(
            ("Marchbank Loom8", "deployed on host", "Harrier box"),
            ("Pendreth Loom8", "deployed on host", "Talon box"),
        )
        block, text = self._ask("Which host is Loom8 deployed on?")
        chain_rows = [row for row in self._rows(block) if "chain" in row]
        self.assertEqual(chain_rows, [], block)
        self.assertNotIn("The store has no recorded fact about", text)

    def test_a_reply_describing_stored_facts_gets_no_trailer(self) -> None:
        """The live battery v3 miss, reproduced.

        A plain question, answered correctly, whose reply mentions what the
        store holds.  Nothing was claimed to be written, so appending "Not
        stored: no project fact was written this turn." contradicts a reply
        that never said otherwise - and the battery's forbid regex matched the
        trailer, not the answer.
        """
        conversation = self.memory.new_conversation(project_id=1)
        for subject, predicate, value in (
            ("Kestrel relay", "deployed on host", "Harrier box"),
            ("Harrier box", "datacenter", "Fenwick"),
            ("Fenwick", "region", "Northgate"),
            ("Kestrel relay", "listens on port", "9090"),
        ):
            self.memory.remember_explicit_project_claim(
                conversation, 1, _command(subject, predicate, value)
            )
        agent, _client = self._agent([
            "The Kestrel relay runs on the Harrier box. There are at least 2 "
            "more stored facts about what's on the Harrier box, but they "
            "didn't fit in the context window."
        ])
        result = agent.run("What runs on the Harrier box?")
        self.assertNotIn("Not stored", str(result))
        self.assertNotIn("Remember this project fact:", str(result))

    def test_replies_that_only_describe_the_store_get_no_trailer(self) -> None:
        for reply in (
            "Two more facts have been recorded about the Harrier box.",
            "Other facts recorded for the Harrier box did not fit here.",
            "The project facts now show two more entries for the Harrier box.",
            "That fact is recorded for the Kestrel relay.",
            "This fact is stored in memory for the Harrier box.",
            "Additional facts were stored for later reference.",
        ):
            with self.subTest(reply=reply):
                agent, _client = self._agent([reply])
                self.assertNotIn(
                    "Not stored", str(agent.run("What runs on the Harrier box?"))
                )

    def test_a_reply_claiming_a_write_still_gets_the_trailer(self) -> None:
        for reply in (
            "I've saved that.",
            "I have stored it in memory.",
            "Done. This has been recorded in memory.",
            "Jarvis has recorded that.",
            "Consider it noted.",
            "Saved to memory.",
        ):
            with self.subTest(reply=reply):
                agent, _client = self._agent([reply])
                text = str(agent.run("Thanks, that helps a lot."))
                self.assertIn(
                    "Not stored: no project fact was written this turn.", text
                )

    def test_the_write_claim_predicate_reads_the_subject_of_the_verb(self) -> None:
        # The distinction the guard turns on: who is said to have written.
        for reply in (
            "I've saved that.",
            "I'll remember that.",
            "It has been stored in long-term memory.",
            "Stored project fact (claim record #12).",
            "Got it, stored.",
        ):
            with self.subTest(claim=reply):
                self.assertTrue(agent_module.reply_claims_own_write(reply))
        for reply in (
            "Two more stored facts exist.",
            "The stored facts for the Harrier box are its datacenter and relay.",
            "No fact is recorded for the Osprey relay.",
            "That is not stored.",
            "I do not have that recorded.",
            "Not recorded.",
            "Paris.",
        ):
            with self.subTest(description=reply):
                self.assertFalse(agent_module.reply_claims_own_write(reply))

    def test_the_agent_and_the_graph_alias_identically(self) -> None:
        table = [
            ("relay", ["Kestrel relay"]),
            ("relay", ["Kestrel relay", "Osprey relay"]),
            ("relay", []),
            ("box", ["Harrier box", "Harrier Box"]),
            ("Kestrel relay", ["Kestrel relay"]),
            ("", ["Kestrel relay"]),
            ("RELAY", ["Kestrel relay"]),
        ]
        for subject, known in table:
            with self.subTest(subject=subject, known=known):
                self.assertEqual(
                    Agent._alias_subject(subject, known),
                    memory_graph.alias_subject(subject, known),
                )

    def test_the_agent_and_the_graph_share_one_value_word_set(self) -> None:
        self.assertIs(_CONFIGURED_VALUE_WORDS, memory_graph.ASKED_VALUE_WORDS)

    def test_the_lane_abstained_clause_is_the_stores_own_text(self) -> None:
        self.assertEqual(_LANE_ABSTAINED_CLAUSE, memory_graph.LANE_ABSTAINED_CLAUSE)
        self.assertEqual(len(_LANE_ABSTAINED_CLAUSE), 122)


class AgentMemoryErasureVerbTests(unittest.TestCase):
    """``Erase memory #<id>`` as the fourth governed verb (design 6.1)."""

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
            ollama_preload=False,
            vault_dir=None,
            memory_embeddings="disabled",
        )
        self.memory = Memory(data_dir / "agent.db")
        self.events: list[str] = []
        self.memory.remember_verified(
            "The sprint demo is on Thursday.",
            kind="fact",
            source="fixture",
            origin="explicit_operator_memory",
        )
        self.memory_id = int(self.memory.db.execute(
            "SELECT id FROM memories ORDER BY id DESC LIMIT 1"
        ).fetchone()[0])

    def tearDown(self) -> None:
        self.memory.close()
        self.temp.cleanup()

    def _agent(self, **overrides: object) -> tuple[Agent, ScriptedModelClient]:
        client = ScriptedModelClient([])
        agent = Agent(
            replace(self.config, **overrides) if overrides else self.config,
            self.memory,
            self.events.append,
            client=client,
            coding_review=False,
            coding_planning=False,
        )
        return agent, client

    def _rows(self) -> int:
        return int(self.memory.db.execute(
            "SELECT COUNT(*) FROM memories"
        ).fetchone()[0])

    def test_the_verb_erases_the_row_and_answers_with_the_fixed_receipt(self) -> None:
        agent, client = self._agent()
        before = self._rows()
        result = agent.run(f"Erase memory #{self.memory_id}")
        self.assertEqual(self._rows(), before - 1)
        self.assertIn(f"Erased memory #{self.memory_id}", str(result))
        self.assertNotIn("sprint demo", str(result))
        # A governed turn never reaches a model.
        self.assertEqual(client.requests, [])

    def test_the_operator_command_and_the_receipt_are_both_in_the_transcript(self) -> None:
        agent, _client = self._agent()
        result = agent.run(f"Erase memory #{self.memory_id}")
        roles = [
            (str(row["role"]), str(row["content"]))
            for row in self.memory.db.execute(
                "SELECT role, content FROM messages WHERE conversation_id=? "
                "ORDER BY id",
                (result.conversation_id,),
            ).fetchall()
        ]
        self.assertIn(("user", f"Erase memory #{self.memory_id}"), roles)
        self.assertTrue(
            any(role == "assistant" and "Erased memory #" in content
                for role, content in roles),
            roles,
        )

    def test_a_missing_id_refuses_and_changes_nothing(self) -> None:
        agent, _client = self._agent()
        before = self._rows()
        result = agent.run("Erase memory #9999")
        self.assertIn("No memory #9999 exists", str(result))
        self.assertEqual(self._rows(), before)

    def test_a_near_command_fails_closed_with_the_exact_shape(self) -> None:
        agent, client = self._agent()
        before = self._rows()
        result = agent.run("please delete memory number 12")
        self.assertIn("Not erased", str(result))
        self.assertIn("Erase memory #<id>", str(result))
        self.assertEqual(self._rows(), before)
        self.assertEqual(client.requests, [])

    def test_readonly_mode_refuses_the_verb(self) -> None:
        agent, _client = self._agent(autonomy="readonly")
        before = self._rows()
        result = agent.run(f"Erase memory #{self.memory_id}")
        self.assertIn("Not erased", str(result))
        self.assertIn("readonly", str(result).casefold())
        self.assertEqual(self._rows(), before)

    def test_a_background_origin_cannot_erase(self) -> None:
        agent, _client = self._agent()
        before = self._rows()
        result = agent.run(
            f"Erase memory #{self.memory_id}", prediction_origin="background"
        )
        self.assertIn("Not erased", str(result))
        self.assertEqual(self._rows(), before)

    def test_a_confusable_spelling_is_refused_with_its_own_verb(self) -> None:
        """C-5: a confusable ``Erase memory #1`` was refused with the Forget
        verb's shape, telling the operator to fix a command they never sent.

        The verb selector now canonicalizes before asking which verb owns the
        turn, exactly as the parser's own near-command detector does.
        """
        agent, client = self._agent()
        before = self._rows()
        result = agent.run("\uff25rase memory #1")
        self.assertIn("Not erased", str(result))
        self.assertIn("Erase memory #<id>", str(result))
        self.assertNotIn("Forget this project fact", str(result))
        self.assertEqual(self._rows(), before)
        self.assertEqual(client.requests, [])

    def test_a_project_fact_erasure_keeps_its_own_shape(self) -> None:
        agent, _client = self._agent()
        result = agent.run("Erase this project fact: {not json}")
        self.assertIn("Not erased", str(result))
        self.assertIn("Erase this project fact:", str(result))
        self.assertNotIn("Erase memory #<id>", str(result))


if __name__ == "__main__":
    unittest.main()
