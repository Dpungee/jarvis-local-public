"""The transcript recall channel: prior-conversation excerpts on the read path.

Every transcript here is synthetic.  Nothing from any public benchmark (no
case, question, answer, haystack sentence, entity name, date or paraphrase)
may enter this file, per honest-reporting rule 3 of ``docs/BENCHMARKS.md``.
"""
from __future__ import annotations

import json
import re
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import jarvis.memory as memory_module
from jarvis.agent import Agent, _TRANSCRIPT_EXCERPT_GUIDANCE
from jarvis.config import Config
from jarvis.memory import (
    TRANSCRIPT_RECALL_COLD_TIME_BUDGET_MS,
    TRANSCRIPT_RECALL_EXCERPT_CAP,
    TRANSCRIPT_RECALL_EXCERPT_CHARS,
    TRANSCRIPT_RECALL_TIME_BUDGET_MS,
    Memory,
)

TAG = "prior_conversation_excerpts"


class ModelResponse(dict):
    def __init__(self, content: str) -> None:
        super().__init__(role="assistant", content=content)
        self.done_reason = None
        self.done = True


class ScriptedModelClient:
    def __init__(self, replies: list[str] | None = None) -> None:
        self.replies = list(replies or [])
        self.requests: list[dict[str, object]] = []

    def models(self, refresh: bool = True) -> list[str]:
        del refresh
        return ["qwen3.5:9b", "gpt-oss:20b", "qwen3-coder:30b"]

    def chat(self, *args: object, **kwargs: object) -> object:
        self.requests.append({"args": args, "kwargs": kwargs})
        return ModelResponse(self.replies.pop(0) if self.replies else "Understood.")

    def last_messages(self) -> list[dict[str, str]]:
        messages = self.requests[-1]["args"][0]
        assert isinstance(messages, list)
        return messages

    def last_user_turn(self) -> str:
        for message in reversed(self.last_messages()):
            if message.get("role") == "user":
                return str(message.get("content") or "")
        return ""


def _command(subject: str, predicate: str, value: str) -> str:
    return "Remember this project fact: " + json.dumps(
        {"subject": subject, "predicate": predicate, "value": value},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _seed(memory: Memory, title: str, turns: list[tuple[str, str]]) -> int:
    conversation_id = memory.new_conversation(title)
    for role, content in turns:
        memory.add_message(conversation_id, role, content)
    return conversation_id


class TranscriptRecallStoreTests(unittest.TestCase):
    """``Memory.prior_conversation_excerpts`` and its diagnostic record."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.memory = Memory(Path(self.temp.name) / "store.db")
        # These tests assert WHICH rows recall returns. The whole-call deadline has its own
        # tests below; a slow or contended runner must not turn it into a missing row here.
        for name in ("TRANSCRIPT_RECALL_TIME_BUDGET_MS", "TRANSCRIPT_RECALL_COLD_TIME_BUDGET_MS"):
            patcher = patch.object(memory_module, name, 60_000.0)
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        self.memory.close()
        self.temp.cleanup()

    def test_reads_other_conversations_and_never_the_current_one(self) -> None:
        older = _seed(self.memory, "garden notes", [
            ("user", "I planted a fig tree called Marigold by the back fence."),
            ("assistant", "A fig tree by a fence wants two metres of clearance."),
        ])
        later = _seed(self.memory, "weekend", [
            ("user", "The fig tree Marigold moved to the front yard; the fence shaded it."),
        ])
        current = _seed(self.memory, "today", [
            ("user", "Where is the fig tree Marigold now?"),
        ])

        rows = self.memory.prior_conversation_excerpts(
            "Where is the fig tree Marigold now?", exclude_conversation_id=current
        )

        self.assertEqual([row["conversation_id"] for row in rows], [older, older, later])
        self.assertNotIn(current, {row["conversation_id"] for row in rows})
        self.assertEqual([row["role"] for row in rows], ["user", "assistant", "user"])
        self.assertTrue(all(row["title"] for row in rows))
        report = self.memory.transcript_recall_report()
        self.assertEqual(report["mode"], "or")
        self.assertEqual(report["candidates"], 3)
        self.assertEqual(report["returned"], 3)
        self.assertFalse(report["abstained"])
        # The lexical report carries the same record.
        self.assertEqual(self.memory.recall_report()["transcript"], report)

    def test_rows_of_another_store_or_another_project_never_appear(self) -> None:
        other = Memory(Path(self.temp.name) / "other.db")
        try:
            _seed(other, "elsewhere", [("user", "The Talon box runs in the Moss Hollow room.")])
            _seed(self.memory, "here", [("user", "The Talon box is painted green.")])
            project = self.memory.add_project("side", "@projects/side")
            side = self.memory.new_conversation("side project", project_id=project)
            self.memory.add_message(side, "user", "The Talon box in the side project is blue.")

            rows = self.memory.prior_conversation_excerpts(
                "What colour is the Talon box?", project_id=1
            )
            texts = [row["excerpt"] for row in rows]
            self.assertEqual(texts, ["The Talon box is painted green."])
            self.assertNotIn("Moss Hollow", " ".join(texts))

            rows = self.memory.prior_conversation_excerpts(
                "What colour is the Talon box?", project_id=project
            )
            self.assertEqual([row["excerpt"] for row in rows], ["The Talon box in the side project is blue."])
            # No project scope reads the whole store, never another store.
            rows = self.memory.prior_conversation_excerpts("What colour is the Talon box?")
            self.assertEqual(len(rows), 2)
            self.assertNotIn("Moss Hollow", " ".join(row["excerpt"] for row in rows))
        finally:
            other.close()

    def test_missing_or_disabled_project_abstains(self) -> None:
        _seed(self.memory, "here", [("user", "The Talon box is painted green.")])
        rows = self.memory.prior_conversation_excerpts("Talon box colour", project_id=99)
        self.assertEqual(rows, [])
        report = self.memory.transcript_recall_report()
        self.assertEqual(report["mode"], "project-unavailable")
        self.assertTrue(report["abstained"])

    def test_cap_is_hard_and_results_are_oldest_first(self) -> None:
        for index in range(6):
            _seed(self.memory, f"note {index}", [
                ("user", f"Sprocket batch {index} of the Wren lathe is on shelf {index}."),
                ("assistant", f"Shelf {index} noted for the Wren lathe sprocket batch."),
            ])
        rows = self.memory.prior_conversation_excerpts("Where is the Wren lathe sprocket batch?")
        self.assertEqual(len(rows), TRANSCRIPT_RECALL_EXCERPT_CAP)
        report = self.memory.transcript_recall_report()
        self.assertEqual(report["candidates"], 12)
        self.assertEqual(report["returned"], TRANSCRIPT_RECALL_EXCERPT_CAP)
        keys = [(row["created_at"], row["message_id"]) for row in rows]
        self.assertEqual(keys, sorted(keys))
        # A larger limit is bounded by the cap; a zero limit reads nothing.
        self.assertEqual(
            len(self.memory.prior_conversation_excerpts("Wren lathe sprocket", limit=50)),
            TRANSCRIPT_RECALL_EXCERPT_CAP,
        )
        self.assertEqual(self.memory.prior_conversation_excerpts("Wren lathe sprocket", limit=0), [])
        self.assertEqual(self.memory.transcript_recall_report()["mode"], "empty")

    def test_one_whole_call_deadline_returns_what_it_has(self) -> None:
        _seed(self.memory, "note", [("user", "The Wren lathe sprocket batch is on shelf four.")])
        with (
            patch.object(memory_module, "TRANSCRIPT_RECALL_TIME_BUDGET_MS", 0.0),
            patch.object(memory_module, "TRANSCRIPT_RECALL_COLD_TIME_BUDGET_MS", 0.0),
        ):
            rows = self.memory.prior_conversation_excerpts("Where is the Wren lathe sprocket batch?")
        self.assertEqual(rows, [])
        report = self.memory.transcript_recall_report()
        self.assertEqual(report["mode"], "budget-exceeded")
        self.assertEqual(report["budget"], "time")
        self.assertEqual(report["budget_ms"], 0.0)
        # Exceeding the budget is not an abstention: the call says what it did.
        self.assertFalse(report["abstained"])
        self.assertEqual(report["candidates"], 1)

    def test_first_read_is_cold_then_warm(self) -> None:
        _seed(self.memory, "note", [("user", "The Wren lathe sprocket batch is on shelf four.")])
        # This test is about the shipped budgets themselves, so restore them.
        with (
            patch.object(memory_module, "TRANSCRIPT_RECALL_TIME_BUDGET_MS", TRANSCRIPT_RECALL_TIME_BUDGET_MS),
            patch.object(memory_module, "TRANSCRIPT_RECALL_COLD_TIME_BUDGET_MS",
                         TRANSCRIPT_RECALL_COLD_TIME_BUDGET_MS),
        ):
            self.memory.prior_conversation_excerpts("Wren lathe sprocket batch")
            self.assertEqual(
                self.memory.transcript_recall_report()["budget_ms"],
                TRANSCRIPT_RECALL_COLD_TIME_BUDGET_MS,
            )
            self.memory.prior_conversation_excerpts("Wren lathe sprocket batch")
            self.assertEqual(
                self.memory.transcript_recall_report()["budget_ms"],
                TRANSCRIPT_RECALL_TIME_BUDGET_MS,
            )

    def test_widened_privacy_screen_drops_the_excerpt(self) -> None:
        aws_shaped = "AK" + "IA" + "IOSFODNN7EXAMPLE"
        secret = f"aws_secret_access_key = {aws_shaped}wJalrXUtnFEMI"
        rows = [
            "Call the Wren lathe supplier at +1 (415) 555-0134 about the sprocket.",
            "The Wren lathe controller sits at 10.0.0.7 on the sprocket bench.",
            "The Wren lathe warranty case is 078-05-1120 for the sprocket claim.",
            "Pay the Wren lathe sprocket invoice with card 4111 1111 1111 1111.",
            f"The Wren lathe sprocket upload uses {secret}.",
            "The Wren lathe sprocket batch is on shelf four.",
        ]
        for index, content in enumerate(rows):
            _seed(self.memory, f"note {index}", [("user", content)])

        found = self.memory.prior_conversation_excerpts("Where is the Wren lathe sprocket?")

        # ``add_message`` already redacted the secret at persistence, so that
        # row survives with its placeholder; the other four carry widened kinds
        # the pinned screen does not catch and are dropped whole.
        self.assertEqual(
            [row["excerpt"] for row in found],
            ["The Wren lathe sprocket upload uses [REDACTED]", rows[-1]],
        )
        report = self.memory.transcript_recall_report()
        self.assertEqual(report["candidates"], 6)
        self.assertEqual(report["excluded_by_screen"], 4)
        self.assertEqual(report["returned"], 2)
        rendered = json.dumps(found)
        for leak in ("555-0134", "10.0.0.7", "078-05-1120", "4111", aws_shaped):
            self.assertNotIn(leak, rendered)

    def test_governed_commands_and_receipts_are_never_excerpts(self) -> None:
        _seed(self.memory, "Governed project memory", [
            ("user", _command("Kestrel relay", "listen port", "9090")),
            ("assistant", "Stored project fact (claim record #1). Kestrel relay listen port 9090."),
        ])
        _seed(self.memory, "chat", [
            ("user", "The Kestrel relay firmware was flashed to build 4.2 last night."),
        ])
        rows = self.memory.prior_conversation_excerpts("What do you know about the Kestrel relay?")
        self.assertEqual(
            [row["excerpt"] for row in rows],
            ["The Kestrel relay firmware was flashed to build 4.2 last night."],
        )
        self.assertEqual(self.memory.transcript_recall_report()["excluded_governed"], 2)

    def test_unknown_structured_identifier_abstains(self) -> None:
        _seed(self.memory, "note", [("user", "The port of NODE9 is 7070 on the Osprey relay.")])
        rows = self.memory.prior_conversation_excerpts("What is the port of NODE7 on the Osprey relay?")
        self.assertEqual(rows, [])
        report = self.memory.transcript_recall_report()
        self.assertEqual(report["mode"], "unknown-identity")
        self.assertTrue(report["abstained"])
        self.assertEqual(report["unknown_terms"], ["node7"])
        # The identifier the store has seen answers.
        rows = self.memory.prior_conversation_excerpts("What is the port of NODE9 on the Osprey relay?")
        self.assertEqual(len(rows), 1)

    def test_query_screens_match_the_claims_lane(self) -> None:
        _seed(self.memory, "note", [("user", "The Wren lathe sprocket batch is on shelf four.")])
        self.assertEqual(
            self.memory.prior_conversation_excerpts("Wren lathe sprocket for ops@example.com"),
            [],
        )
        self.assertEqual(self.memory.transcript_recall_report()["mode"], "screened")
        with self.assertRaises(ValueError):
            self.memory.prior_conversation_excerpts("Wren " * 3_000)
        self.assertEqual(self.memory.prior_conversation_excerpts("the of and"), [])
        self.assertEqual(self.memory.transcript_recall_report()["mode"], "empty")

    def test_staged_narrowing_over_the_visible_scope(self) -> None:
        # Eight rows share the everyday word "tree"; three of them name the
        # fence.  With the candidate limit lowered to five, OR overflows, the
        # scoped counts drop "tree" (8 > 5), keep "fence" (3), and the narrowed
        # pool answers.
        for index in range(8):
            fence = " by the fence" if index < 3 else ""
            _seed(self.memory, f"note {index}", [("user", f"Tree {index} was pruned{fence} today.")])
        with patch.object(memory_module, "MAX_MEMORY_SEARCH_CANDIDATES", 5):
            rows = self.memory.prior_conversation_excerpts("Which tree stands by the fence?")
            report = self.memory.transcript_recall_report()
            self.assertEqual(report["mode"], "narrowed")
            self.assertEqual(report["dropped_terms"], ["tree"])
            self.assertEqual(len(rows), 3)
            self.assertTrue(all("fence" in row["excerpt"] for row in rows))

            # Every term is everyday: the intersection is required and, when
            # it still overflows, the channel abstains rather than guessing.
            for index in range(8, 14):
                _seed(self.memory, f"note {index}", [("user", f"Tree {index} by the fence was pruned.")])
            rows = self.memory.prior_conversation_excerpts("tree fence")
            report = self.memory.transcript_recall_report()
            self.assertEqual(rows, [])
            self.assertEqual(report["mode"], "overflow")
            self.assertTrue(report["abstained"])
            self.assertEqual(sorted(report["dropped_terms"]), ["fence", "tree"])

    def test_all_terms_stage_answers_a_bounded_intersection(self) -> None:
        for index in range(8):
            _seed(self.memory, f"note {index}", [("user", f"Tree {index} was pruned today.")])
        # Six rows name the fence (6 > 5, dropped like "tree"), four of which
        # also say tree: the required intersection is those four.
        for index in range(8, 14):
            height = "tree-high" if index < 12 else "tall"
            _seed(self.memory, f"note {index}", [("user", f"Bush {index} by the fence grew {height}.")])
        with patch.object(memory_module, "MAX_MEMORY_SEARCH_CANDIDATES", 5):
            rows = self.memory.prior_conversation_excerpts("tree fence")
            report = self.memory.transcript_recall_report()
        self.assertEqual(report["mode"], "all-terms")
        self.assertEqual(sorted(report["dropped_terms"]), ["fence", "tree"])
        self.assertEqual(len(rows), 4)
        self.assertTrue(all("fence" in row["excerpt"] and "tree" in row["excerpt"] for row in rows))

    def test_excerpt_window_centres_on_the_matched_term(self) -> None:
        filler = " ".join(f"word{index}" for index in range(300))
        content = f"{filler} The Wren lathe sprocket batch is on shelf four. {filler}"
        _seed(self.memory, "long", [("user", content)])
        rows = self.memory.prior_conversation_excerpts("Where is the Wren lathe sprocket batch?")
        self.assertEqual(len(rows), 1)
        excerpt = rows[0]["excerpt"]
        self.assertIn("shelf four", excerpt)
        self.assertLessEqual(len(excerpt), TRANSCRIPT_RECALL_EXCERPT_CHARS + 6)
        self.assertTrue(excerpt.startswith("...") and excerpt.endswith("..."))


class TranscriptRecallAgentTests(unittest.TestCase):
    """The channel on the agent read path: order, cue, flag, lane, screen."""

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
        self.events: list[str] = []

    def tearDown(self) -> None:
        self.memory.close()
        self.temp.cleanup()

    def _agent(
        self, replies: list[str] | None = None, *, config: Config | None = None
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

    @staticmethod
    def _block(text: str, tag: str) -> str:
        match = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.S)
        return match.group(1) if match else ""

    def test_fresh_conversation_sees_excerpts_after_claims_and_no_cue(self) -> None:
        agent, client = self._agent(["The firmware is build 4.2."])
        stored = agent.run(_command("Kestrel relay", "listen port", "9090"))
        _seed(self.memory, "ops chat", [
            ("user", "The Kestrel relay firmware was flashed to build 4.2 last night."),
            ("assistant", "Build 4.2 on the Kestrel relay is the current firmware then."),
        ])

        result = agent.run("What firmware does the Kestrel relay run?")

        self.assertEqual(result.status, "complete", result.reason)
        self.assertNotEqual(result.conversation_id, stored.conversation_id)
        user_turn = client.last_user_turn()
        claims = self._block(user_turn, "temporal_claims")
        excerpts = self._block(user_turn, TAG)
        self.assertIn("9090", claims)
        self.assertIn("build 4.2", excerpts)
        # Governed claims render first; the stored command is not an excerpt.
        self.assertLess(user_turn.index("<temporal_claims>"), user_turn.index(f"<{TAG}>"))
        self.assertNotIn("Remember this project fact", excerpts)
        self.assertNotIn('"status":"not_recorded"', user_turn)
        self.assertEqual(user_turn.count(_TRANSCRIPT_EXCERPT_GUIDANCE), 1)
        self.assertEqual(user_turn.count(f"<{TAG}>"), 1)
        # The dialogue lane moves the block out of the system content.
        self.assertNotIn(TAG, str(client.last_messages()[0]["content"]))
        self.assertIn("memory - prior conversation excerpts: 2", self.events)
        rendered = json.loads(excerpts)
        self.assertEqual([entry["role"] for entry in rendered], ["operator", "jarvis"])
        self.assertTrue(all(entry["conversation"] == "ops chat" for entry in rendered))
        self.assertTrue(all(re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", entry["date"]) for entry in rendered))
        # The excerpt is not a claim, a memory or a receipt.
        self.assertEqual(
            self.memory.db.execute("SELECT COUNT(*) FROM memory_claims").fetchone()[0], 1
        )
        self.assertEqual(
            self.memory.db.execute(
                "SELECT COUNT(*) FROM memories WHERE content LIKE '%build 4.2%'"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.memory.db.execute(
                "SELECT COUNT(*) FROM memory_spine_events WHERE payload_json LIKE '%build 4.2%'"
            ).fetchone()[0],
            0,
        )
        self.assertNotIn("Stored project fact", str(result))

    def test_cue_fires_only_when_every_channel_is_empty(self) -> None:
        # The cue ENTRY, not the standing memory rule that explains it.
        cue = '"status":"not_recorded"'
        agent, _client = self._agent()
        cued = agent.system_prompt("What is the Osprey relay listen port?")
        self.assertIn(cue, cued)
        self.assertNotIn(f"<{TAG}>", cued)

        _seed(self.memory, "ops chat", [
            ("user", "The Osprey relay listens on 8181 since the move."),
        ])
        prompt = agent.system_prompt("What is the Osprey relay listen port?")
        self.assertNotIn(cue, prompt)
        self.assertNotIn("<temporal_claims>", prompt)
        self.assertIn(f"<{TAG}>", prompt)
        self.assertEqual(prompt.count(_TRANSCRIPT_EXCERPT_GUIDANCE), 1)
        self.assertLess(prompt.index("</untrusted_memory_records>"), prompt.index(f"<{TAG}>"))
        # A question the transcripts do not speak to keeps the cue.
        again = agent.system_prompt("What is the Harrier box rack number?")
        self.assertIn(cue, again)
        self.assertNotIn(f"<{TAG}>", again)

    def test_flag_off_restores_the_old_prompt_byte_for_byte(self) -> None:
        _seed(self.memory, "ops chat", [
            ("user", "The Osprey relay listens on 8181 since the move."),
        ])
        question = "What is the Osprey relay listen port?"
        agent_on, _client = self._agent()
        with_channel = agent_on.system_prompt(question)
        self.assertIn(f"<{TAG}>", with_channel)

        agent_off, _client = self._agent(config=replace(self.config, memory_transcript_recall=False))
        without_flag = agent_off.system_prompt(question)
        # The reference is the read path with no channel at all.
        with patch.object(Memory, "prior_conversation_excerpts", new=None):
            reference = agent_on.system_prompt(question)
        self.assertEqual(without_flag, reference)
        self.assertNotIn(TAG, without_flag)
        self.assertIn('"status":"not_recorded"', without_flag)
        self.assertEqual(self.memory.transcript_recall_report()["mode"], "or")

    def test_same_conversation_rows_never_feed_the_channel(self) -> None:
        agent, client = self._agent(["Noted.", "I do not know."])
        first = agent.run("The Talon box runs in the Moss Hollow room these days.")
        agent.run("Which room is the Talon box in?", conversation_id=first.conversation_id)
        user_turn = client.last_user_turn()
        self.assertNotIn(f"<{TAG}>", user_turn)
        self.assertNotIn("Moss Hollow", self._block(user_turn, TAG))
        # The same question from a NEW conversation reaches it.
        fresh_agent, fresh_client = self._agent(["The Moss Hollow room."])
        fresh_agent.run("Which room is the Talon box in?")
        self.assertIn("Moss Hollow", self._block(fresh_client.last_user_turn(), TAG))

    def test_privacy_screen_reaches_the_prompt(self) -> None:
        _seed(self.memory, "supplier", [
            ("user", "Call the Talon box supplier at +1 (415) 555-0134 about the rack."),
        ])
        agent, _client = self._agent()
        prompt = agent.system_prompt("How do I reach the Talon box supplier?")
        self.assertNotIn("555-0134", prompt)
        self.assertNotIn(f"<{TAG}>", prompt)
        report = self.memory.recall_report()["transcript"]
        self.assertEqual(report["excluded_by_screen"], 1)
        self.assertEqual(report["returned"], 0)

    def test_compaction_keeps_the_block_as_an_optional_tag(self) -> None:
        agent, _client = self._agent()
        constitution = "CONSTITUTION_SENTINEL\n" + "safe\n" * 80
        prompt = (
            "verbose preamble\n" + "x" * 9000
            + '<trusted_constitution sha256="abc">\n'
            + constitution
            + "</trusted_constitution>\n"
            + "<untrusted_memory_records>MEMORY_SENTINEL</untrusted_memory_records>\n"
            + "<temporal_claims>CLAIM_SENTINEL</temporal_claims>\n"
            + f"<{TAG}>EXCERPT_SENTINEL</{TAG}>\n"
            + "<persistent_self_context>SELF_SENTINEL</persistent_self_context>\n"
        )
        compacted = agent._compact_messages(
            [
                {"role": "system", "content": prompt},
                {"role": "user", "content": "LATEST_USER_SENTINEL"},
            ],
            4096,
        )
        rendered = compacted[0]["content"]
        for sentinel in ("MEMORY_SENTINEL", "CLAIM_SENTINEL", "EXCERPT_SENTINEL", "SELF_SENTINEL"):
            self.assertIn(sentinel, rendered)
        self.assertEqual(rendered.count(f"<{TAG}"), 1)
        self.assertLess(rendered.index("</temporal_claims>"), rendered.index(f"<{TAG}>"))


if __name__ == "__main__":
    unittest.main()
