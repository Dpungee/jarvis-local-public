from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jarvis.attachments import ImageAttachment
from jarvis.config import Config
from jarvis.memory import Memory, SCHEMA_VERSION
from jarvis.presence import (
    PresenceJob,
    PresenceRuntime,
    _EphemeralTranscriptMemory,
    companion_action_outcome_message,
    screen_companion_learning_category,
)
from jarvis.screen_companion import (
    COMPANION_SUGGESTION_TTL_SECONDS,
    ScreenObservation,
)
from tests.legacy_store_fixture import strip_spine


class CompanionLearningPresenceTests(unittest.TestCase):
    def _runtime(self, data: Path) -> PresenceRuntime:
        return PresenceRuntime(SimpleNamespace(data_dir=data))

    @staticmethod
    def _dump(database: Path) -> str:
        connection = sqlite3.connect(database)
        try:
            return "\n".join(connection.iterdump())
        finally:
            connection.close()

    def test_ephemeral_suggestion_and_dismissal_never_persist_screen_text(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / "data"
            data.mkdir()
            database = data / "jarvis.db"
            with Memory(database) as memory:
                conversation_id = memory.new_conversation("Operator")

            runtime = self._runtime(data)
            runtime._last_operator_conversation_id = conversation_id
            private_title = "outline-private-title-71ac"
            private_output = "Want me to refine private-visible-copy-92bd?"
            runtime._ephemeral_screen_companion_suggestion = (
                lambda _prompt, _attachments: private_output
            )
            suggestion_id = runtime._screen_companion_action(
                {
                    "action_mode": "suggest",
                    "action_prompt": "Offer one useful editing action",
                    "source": "manual",
                },
                ScreenObservation(
                    application="editor.exe",
                    title=private_title,
                    observed_at=time.time(),
                    context_sha256="1" * 64,
                ),
            )

            self.assertRegex(str(suggestion_id), r"^[0-9a-f]{32}$")
            self.assertNotIn(private_title, self._dump(database))
            self.assertNotIn(private_output, self._dump(database))
            runtime.respond_screen_companion_suggestion(
                str(suggestion_id), accept=False
            )
            dump = self._dump(database)
            self.assertNotIn(private_title, dump)
            self.assertNotIn(private_output, dump)
            with Memory(database) as memory:
                self.assertEqual(memory.screen_companion_learning_stats()["dismissed"], 1)

            def forget() -> int:
                with Memory(database) as memory:
                    return memory.forget_screen_companion_receipts()

            runtime._screen_companion = SimpleNamespace(forget=forget)
            self.assertGreater(runtime.forget_screen_companion(), 0)
            self.assertIsNone(runtime._current_screen_companion_suggestion())
            with self.assertRaises(LookupError):
                runtime.respond_screen_companion_suggestion(
                    str(suggestion_id), accept=False
                )

    def test_accepted_action_is_digest_only_nonreplayable_and_pending_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / "data"
            data.mkdir()
            database = data / "jarvis.db"
            with Memory(database) as memory:
                conversation_id = memory.new_conversation("Operator")

            runtime = self._runtime(data)
            suggestion_text = "Want me to polish visible-private-paragraph-4c2e?"
            record = runtime._publish_screen_companion_suggestion(
                {
                    "application": "editor.exe",
                    "context_sha256": "2" * 64,
                    "source": "manual",
                    "attachments": (),
                    "target_conversation_id": conversation_id,
                },
                suggestion_text,
            )
            self.assertEqual(
                record["expires_at"] - record["created_at"],
                COMPANION_SUGGESTION_TTL_SECONDS,
            )
            result = runtime.respond_screen_companion_suggestion(
                record["id"], accept=True
            )
            self.assertTrue(result["accepted"])
            job_id = str(result["job_id"])
            action_status = runtime.screen_companion_action_status(job_id)
            self.assertEqual(action_status["state"], "queued")
            self.assertFalse(action_status["terminal"])
            with Memory(database) as memory:
                job = memory.get_presence_job(job_id)
                feedback = memory.screen_companion_feedback_for_action_job(job_id)
                stats = memory.screen_companion_learning_stats()
            self.assertEqual(job["prompt"], "[ephemeral Screen Companion request]")
            self.assertEqual(job["attachments_json"], "[]")
            self.assertEqual(job["run_origin"], "companion_action")
            self.assertFalse(job["replayable"])
            self.assertEqual(feedback["decision"], "accepted")
            self.assertIsNone(feedback["outcome"])
            self.assertEqual(stats["accepted"], 1)
            self.assertEqual(stats["verified_outcomes"], 0)
            self.assertNotIn(suggestion_text, self._dump(database))

            runtime._set_screen_companion_action_status(
                job_id,
                state="incomplete",
                message=companion_action_outcome_message(
                    "The active window image was unreadable.",
                    status="incomplete",
                ),
                terminal=True,
            )
            action_status = runtime.screen_companion_action_status(job_id)
            self.assertTrue(action_status["terminal"])
            self.assertIn("couldn't finish", action_status["message"])

            def forget() -> int:
                with Memory(database) as memory:
                    return memory.forget_screen_companion_receipts()

            runtime._screen_companion = SimpleNamespace(forget=forget)
            self.assertGreater(runtime.forget_screen_companion(), 0)
            self.assertNotIn(job_id, runtime._screen_companion_jobs)
            with Memory(database) as memory:
                self.assertEqual(
                    memory.get_presence_job(job_id)["status"], "cancelled"
                )
                self.assertIsNone(
                    memory.screen_companion_feedback_for_action_job(job_id)
                )

    def test_accepted_action_exposes_its_real_terminal_result_to_indicator(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            data = root / "data"
            workspace.mkdir()
            data.mkdir()
            config = Config(
                root=root,
                workspace=workspace,
                data_dir=data,
                soul_path=root / "SOUL.md",
                model="auto",
                fast_model="openai:gpt-test",
                reasoning_model="openai:gpt-test",
                coding_model="openai:gpt-test",
                deep_model="openai:gpt-test",
                ollama_url="http://127.0.0.1:11434",
                ollama_api_key=None,
                max_steps=3,
                context_length=4096,
                command_timeout=30,
                autonomy="autonomous",
            )
            with Memory(data / "jarvis.db") as memory:
                conversation_id = memory.new_conversation("Operator")

            runtime = PresenceRuntime(config)
            record = runtime._publish_screen_companion_suggestion(
                {
                    "application": "editor.exe",
                    "context_sha256": "6" * 64,
                    "source": "manual",
                    "attachments": (),
                    "target_conversation_id": conversation_id,
                },
                "Want me to summarize the visible work?",
            )
            accepted = runtime.respond_screen_companion_suggestion(
                record["id"], accept=True
            )
            job = runtime._jobs.get_nowait()

            class Result(str):
                status = "incomplete"
                reason = "screen unavailable"
                approval_id = None
                prediction_id = None
                model = "openai:gpt-test"
                metrics = {"tool_calls": 0}

            class FakeAgent:
                def __init__(self, *_args, **_kwargs):
                    self.client = SimpleNamespace(provider_status={})

                def run(self, *_args, **_kwargs):
                    return Result("The active window image was unreadable.")

            with patch("jarvis.presence.Agent", FakeAgent):
                runtime._run_job(job)

            action = runtime.screen_companion_action_status(accepted["job_id"])
            self.assertEqual(action["state"], "incomplete")
            self.assertTrue(action["terminal"])
            self.assertIn("active window image was unreadable", action["message"])

    def test_queue_full_acceptance_can_retry_and_forget_revokes_pixels(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / "data"
            data.mkdir()
            database = data / "jarvis.db"
            with Memory(database) as memory:
                conversation_id = memory.new_conversation("Operator")

            runtime = self._runtime(data)
            image = ImageAttachment(
                "image/png", b"\x89PNG\r\n\x1a\nprivate-pixels", "active.png"
            )
            record = runtime._publish_screen_companion_suggestion(
                {
                    "application": "editor.exe",
                    "context_sha256": "7" * 64,
                    "source": "manual",
                    "attachments": (image,),
                    "target_conversation_id": conversation_id,
                },
                "Want me to rewrite this paragraph?",
            )
            for _ in range(runtime._jobs.maxsize):
                runtime._jobs.put_nowait(None)
            with self.assertRaisesRegex(RuntimeError, "too many queued"):
                runtime.respond_screen_companion_suggestion(
                    record["id"], accept=True
                )
            with Memory(database) as memory:
                self.assertEqual(memory.screen_companion_learning_stats()["feedback"], 0)
            self.assertEqual(
                runtime._screen_companion_suggestions[record["id"]]["status"],
                "pending",
            )

            runtime._jobs.get_nowait()
            retried = runtime.respond_screen_companion_suggestion(
                record["id"], accept=True
            )
            job_id = str(retried["job_id"])
            self.assertIn(job_id, runtime._screen_companion_attachment_vault)
            self.assertEqual(
                runtime._screen_companion_suggestions[record["id"]]["attachments"],
                (),
            )

            def forget() -> int:
                with Memory(database) as memory:
                    return memory.forget_screen_companion_receipts()

            runtime._screen_companion = SimpleNamespace(forget=forget)
            self.assertGreater(runtime.forget_screen_companion(), 0)
            self.assertEqual(runtime._screen_companion_attachment_vault, {})
            queued_jobs = []
            while not runtime._jobs.empty():
                item = runtime._jobs.get_nowait()
                if isinstance(item, PresenceJob):
                    queued_jobs.append(item)
            self.assertEqual([job.id for job in queued_jobs], [job_id])
            self.assertEqual(queued_jobs[0].attachments, ())

    def test_pause_and_off_clear_live_observation_immediately(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / "data"
            data.mkdir()
            with Memory(data / "jarvis.db"):
                pass
            runtime = self._runtime(data)
            clears: list[str] = []
            runtime._screen_companion = SimpleNamespace(
                clear_current=lambda: clears.append("clear")
            )
            state = runtime.set_screen_companion(
                mode="suggest", paused=True, auto_suggest=False,
                excluded_apps=[],
            )
            self.assertTrue(state["paused"])
            self.assertIn("learning", state)
            state = runtime.control_screen_companion(action="off")
            self.assertEqual(state["mode"], "disabled")
            self.assertEqual(clears, ["clear", "clear"])

    def test_verified_category_signal_reaches_ephemeral_ranking_only(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / "data"
            data.mkdir()
            database = data / "jarvis.db"
            with Memory(database) as memory:
                memory.record_screen_companion_feedback(
                    suggestion_sha256="1" * 64,
                    context_sha256="2" * 64,
                    application_sha256=hashlib.sha256(b"editor.exe").hexdigest(),
                    decision="accepted",
                    category="writing",
                    action_job_id="verified-job",
                )
                prediction_id = memory.record_prediction(
                    family="conversation", profile="quick", model="test",
                    predicted_success=0.5, predicted_steps=1,
                    predicted_verification="tool_success",
                    origin="companion_action", run_id="verified-job",
                )
                memory.resolve_prediction(
                    prediction_id, actual_status="complete", actual_steps=1,
                    evidence_ok=True,
                )
                memory.bind_screen_companion_outcome(
                    action_job_id="verified-job", prediction_id=prediction_id
                )
            runtime = self._runtime(data)
            guidance = runtime._screen_companion_learning_guidance("editor.exe")
            self.assertIn("verified outcomes: writing", guidance)
            self.assertIn("grants no authority", guidance)
            self.assertEqual(
                runtime._screen_companion_learning_guidance("other.exe"), ""
            )
            self.assertEqual(
                screen_companion_learning_category(
                    "Want me to rewrite this paragraph?"
                ),
                "writing",
            )

    def test_memory_sink_forces_companion_prompt_and_image_descriptors_ephemeral(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "jarvis.db"
            private_prompt = "private-screen-derived-prompt-a91e"
            with Memory(database) as memory:
                conversation_id = memory.new_conversation("Operator")
                row = memory.create_presence_job(
                    "a" * 32,
                    conversation_id=conversation_id,
                    project_id=1,
                    prompt=private_prompt,
                    model_override="auto",
                    attachments_json=(
                        '[{"mime":"image/png","bytes":1,"sha256":"'
                        + "b" * 64
                        + '"}]'
                    ),
                    run_origin="companion_action",
                    replayable=False,
                )
            self.assertEqual(row["prompt"], "[ephemeral Screen Companion request]")
            self.assertEqual(row["attachments_json"], "[]")
            self.assertNotIn(private_prompt, self._dump(database))

    def test_ephemeral_memory_view_cannot_recall_or_write_ordinary_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "jarvis.db"
            with Memory(database) as memory:
                memory.remember_verified(
                    "operator private durable preference",
                    kind="preference",
                    source="explicit user",
                    origin="explicit_operator_memory",
                )
                before = len(memory.list_memories(limit=20))
                ephemeral = _EphemeralTranscriptMemory(memory)
                self.assertEqual(ephemeral.search("preference"), [])
                self.assertEqual(ephemeral.list_memories(limit=20), [])
                self.assertEqual(ephemeral.current_claims("preference"), [])
                self.assertEqual(
                    ephemeral.match_lessons(
                        "private durable lesson", "conversation"
                    ),
                    [],
                )
                self.assertIn(
                    "do not write ordinary long-term memory",
                    ephemeral.remember_verified(
                        "screen-derived private text",
                        kind="fact",
                        source="companion",
                        origin="companion",
                    ),
                )
                self.assertEqual(len(memory.list_memories(limit=20)), before)

    def test_v36_repairs_a_v35_database_missing_internal_marker_table(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "jarvis.db"
            private_text = "legacy-screen-private-4d8f"
            normal_text = "ordinary-same-title-6aa1"
            with Memory(database) as memory:
                normal_id = memory.new_conversation("Screen Companion")
                memory.add_message(normal_id, "user", normal_text)
                internal_id = memory.new_conversation("Screen Companion")
                memory.add_message(internal_id, "user", private_text)
                memory.create_presence_job(
                    "d" * 32,
                    conversation_id=internal_id,
                    project_id=1,
                    prompt="private",
                    model_override="fast",
                    run_origin="companion_suggestion",
                    replayable=False,
                )
            connection = sqlite3.connect(database)
            try:
                connection.execute("DROP TABLE screen_companion_conversations")
                strip_spine(connection)
                connection.execute("PRAGMA user_version=35")
                connection.commit()
            finally:
                connection.close()

            with Memory(database) as memory:
                self.assertEqual(
                    int(memory.db.execute("PRAGMA user_version").fetchone()[0]),
                    SCHEMA_VERSION,
                )
                self.assertFalse(memory.is_screen_companion_conversation(normal_id))
                self.assertTrue(memory.is_screen_companion_conversation(internal_id))
                self.assertEqual(memory.recent_messages(internal_id, limit=5), [])
                self.assertEqual(
                    memory.recent_messages(normal_id, limit=5)[-1]["content"],
                    normal_text,
                )
            dump = self._dump(database)
            self.assertNotIn(private_text, dump)
            self.assertIn(normal_text, dump)

    def test_v36_marks_only_proven_internal_conversation_and_drops_old_outcomes(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "jarvis.db"
            normal_text = "keep-this-normal-conversation-98df"
            private_text = "purge-this-legacy-screen-content-2b7a"
            legacy_job = "b" * 32
            action_job = "c" * 32
            with Memory(database) as memory:
                normal_id = memory.new_conversation("Screen Companion")
                memory.add_message(normal_id, "user", normal_text)
                internal_id = memory.new_conversation("Screen Companion")
                memory.add_message(internal_id, "user", private_text)
                legacy_prediction = memory.record_prediction(
                    family="deep_research",
                    profile="fast",
                    model="openai:test",
                    predicted_success=0.5,
                    predicted_steps=1,
                    predicted_verification="cited_sources",
                    origin="interactive",
                    conversation_id=internal_id,
                )
                memory.create_presence_job(
                    legacy_job,
                    conversation_id=internal_id,
                    project_id=1,
                    prompt=(
                        "Privately analyze this operator-authored Screen Companion routine:\n"
                        "legacy private envelope"
                    ),
                    model_override="auto",
                )
                memory.record_screen_companion_feedback(
                    suggestion_sha256="3" * 64,
                    context_sha256="4" * 64,
                    application_sha256="5" * 64,
                    decision="accepted",
                    action_job_id=action_job,
                )
                prediction_id = memory.record_prediction(
                    family="file_ops",
                    profile="fast",
                    model="openai:test",
                    predicted_success=0.8,
                    predicted_steps=1,
                    predicted_verification="tool_success",
                    basis="prior",
                    origin="companion_action",
                    conversation_id=internal_id,
                    run_id=action_job,
                )
                memory.resolve_prediction(
                    prediction_id,
                    actual_status="complete",
                    actual_steps=1,
                    evidence_ok=True,
                )
                memory.bind_screen_companion_outcome(
                    action_job_id=action_job,
                    prediction_id=prediction_id,
                )

            connection = sqlite3.connect(database)
            try:
                connection.execute("DROP TABLE screen_companion_conversations")
                strip_spine(connection)
                connection.execute("PRAGMA user_version=34")
                connection.commit()
            finally:
                connection.close()

            with Memory(database) as memory:
                self.assertFalse(memory.is_screen_companion_conversation(normal_id))
                self.assertTrue(memory.is_screen_companion_conversation(internal_id))
                self.assertEqual(
                    memory.recent_messages(normal_id, limit=10)[-1]["content"],
                    normal_text,
                )
                self.assertEqual(memory.recent_messages(internal_id, limit=10), [])
                job = memory.get_presence_job(legacy_job)
                stats = memory.screen_companion_learning_stats()
                migrated_origin = str(memory.db.execute(
                    "SELECT origin FROM task_predictions WHERE id=?",
                    (legacy_prediction,),
                ).fetchone()[0])
            self.assertEqual(
                job["prompt"], "[ephemeral Screen Companion prompt removed]"
            )
            self.assertEqual(stats["accepted"], 1)
            self.assertEqual(stats["verified_outcomes"], 0)
            self.assertEqual(migrated_origin, "companion_suggestion")
            self.assertIn(normal_text, self._dump(database))
            self.assertNotIn(private_text, self._dump(database))


if __name__ == "__main__":
    unittest.main()
