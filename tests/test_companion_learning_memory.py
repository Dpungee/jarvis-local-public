from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jarvis.memory import Memory, SCHEMA_VERSION


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class CompanionLearningMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.memory = Memory(Path(":memory:"))

    def tearDown(self) -> None:
        self.memory.close()

    def feedback(
        self,
        *,
        suggestion: str = "suggestion-a",
        context: str = "context-a",
        application: str = "application-a",
        decision: str = "accepted",
        category: str = "general",
        action_job_id: str | None = "job-a",
    ) -> int:
        return self.memory.record_screen_companion_feedback(
            suggestion_sha256=digest(suggestion),
            context_sha256=digest(context),
            application_sha256=digest(application),
            decision=decision,
            category=category,
            action_mode="suggest",
            action_job_id=action_job_id,
        )

    def prediction(
        self,
        *,
        origin: str = "companion_action",
        status: str = "complete",
        evidence_ok: bool | None = True,
        verification: str = "tool_success",
        action_job_id: str = "job-a",
    ) -> int:
        prediction_id = self.memory.record_prediction(
            family="conversation",
            profile="quick",
            model="test-model",
            predicted_success=0.5,
            predicted_steps=1,
            predicted_verification=verification,
            origin=origin,
            run_id=action_job_id,
        )
        self.assertTrue(self.memory.resolve_prediction(
            prediction_id,
            actual_status=status,
            actual_steps=1,
            evidence_ok=evidence_ok,
            failure_class=None if status == "complete" else "unknown",
        ))
        return prediction_id

    def test_schema_contains_only_hashes_categories_and_outcome_metadata(self):
        self.assertEqual(
            int(self.memory.db.execute("PRAGMA user_version").fetchone()[0]),
            SCHEMA_VERSION,
        )
        columns = {
            str(row["name"])
            for row in self.memory.db.execute(
                "PRAGMA table_info(screen_companion_feedback)"
            )
        }
        self.assertEqual(columns, {
            "id", "created_at", "suggestion_sha256", "context_sha256",
            "application_sha256", "category", "action_mode", "decision",
            "action_job_sha256",
        })
        forbidden = {
            "text", "content", "prompt", "title", "window_title", "screenshot",
            "image", "pixels", "application", "action_job_id",
        }
        self.assertFalse(columns & forbidden)
        auto_columns = {
            str(row["name"])
            for row in self.memory.db.execute(
                "PRAGMA table_info(screen_companion_auto_receipts)"
            )
        }
        self.assertEqual(
            auto_columns,
            {"id", "created_at", "day_key", "context_sha256"},
        )

    def test_feedback_is_idempotent_and_conflicting_replay_fails_closed(self):
        feedback_id = self.feedback()
        self.assertEqual(self.feedback(), feedback_id)
        with self.assertRaises(ValueError):
            self.feedback(decision="dismissed", action_job_id=None)
        self.assertEqual(
            int(self.memory.db.execute(
                "SELECT COUNT(*) FROM screen_companion_feedback"
            ).fetchone()[0]),
            1,
        )

    def test_feedback_rejects_raw_or_invalid_metadata(self):
        with self.assertRaises(ValueError):
            self.memory.record_screen_companion_feedback(
                suggestion_sha256="Want me to inspect the visible document?",
                context_sha256=digest("context"),
                application_sha256=digest("application"),
                decision="dismissed",
            )
        with self.assertRaises(ValueError):
            self.feedback(category="free-form category")
        with self.assertRaises(ValueError):
            self.feedback(decision="dismissed", action_job_id="job-a")
        with self.assertRaises(ValueError):
            self.feedback(decision="accepted", action_job_id=None)
        with self.assertRaises(sqlite3.IntegrityError):
            self.memory.db.execute(
                """INSERT INTO screen_companion_feedback(
                       created_at, suggestion_sha256, context_sha256,
                       application_sha256, category, action_mode, decision,
                       action_job_sha256
                   ) VALUES ('now', ?, ?, ?, 'arbitrary', 'suggest',
                             'dismissed', NULL)""",
                (digest("s"), digest("c"), digest("a")),
            )

    def test_action_job_lookup_survives_restart_without_storing_raw_identifier(self):
        feedback_id = self.feedback(action_job_id="action-job-123")
        stored = self.memory.db.execute(
            "SELECT action_job_sha256 FROM screen_companion_feedback WHERE id=?",
            (feedback_id,),
        ).fetchone()
        self.assertNotEqual(str(stored["action_job_sha256"]), "action-job-123")
        self.assertEqual(len(str(stored["action_job_sha256"])), 64)
        found = self.memory.screen_companion_feedback_for_action_job(
            "action-job-123"
        )
        self.assertIsNotNone(found)
        self.assertEqual(found["feedback_id"], feedback_id)
        self.assertEqual(found["decision"], "accepted")
        self.assertIsNone(
            self.memory.screen_companion_feedback_for_action_job("different-job")
        )

    def test_only_exact_verified_companion_prediction_becomes_reusable(self):
        self.feedback(action_job_id="verified-job")
        prediction_id = self.prediction(action_job_id="verified-job")
        self.assertTrue(self.memory.bind_screen_companion_outcome(
            action_job_id="verified-job",
            prediction_id=prediction_id,
        ))
        self.assertTrue(self.memory.bind_screen_companion_outcome(
            action_job_id="verified-job",
            prediction_id=prediction_id,
        ))
        found = self.memory.screen_companion_feedback_for_action_job("verified-job")
        self.assertEqual(found["outcome"], "complete")
        self.assertEqual(found["evidence_kind"], "tool_success")
        self.assertTrue(found["reusable"])

    def test_cross_job_prediction_substitution_fails_closed(self):
        self.feedback(
            suggestion="first", context="first", action_job_id="first-job"
        )
        self.feedback(
            suggestion="second", context="second", action_job_id="second-job"
        )
        first_prediction = self.prediction(action_job_id="first-job")
        second_prediction = self.prediction(action_job_id="second-job")
        with self.assertRaisesRegex(ValueError, "exact action job"):
            self.memory.bind_screen_companion_outcome(
                action_job_id="first-job", prediction_id=second_prediction
            )
        with self.assertRaisesRegex(ValueError, "exact action job"):
            self.memory.bind_screen_companion_outcome(
                action_job_id="second-job", prediction_id=first_prediction
            )
        self.assertTrue(self.memory.bind_screen_companion_outcome(
            action_job_id="first-job", prediction_id=first_prediction
        ))

    def test_unverified_success_becomes_negative_and_non_companion_is_rejected(self):
        self.feedback(action_job_id="unverified-job")
        unverified = self.prediction(
            evidence_ok=False, action_job_id="unverified-job"
        )
        self.assertTrue(self.memory.bind_screen_companion_outcome(
            action_job_id="unverified-job",
            prediction_id=unverified,
        ))
        found = self.memory.screen_companion_feedback_for_action_job(
            "unverified-job"
        )
        self.assertEqual(found["outcome"], "incomplete")
        self.assertFalse(found["reusable"])
        ordinary = self.prediction(
            origin="interactive", action_job_id="ordinary-job"
        )
        with self.assertRaises(ValueError):
            self.memory.bind_screen_companion_outcome(
                action_job_id="unverified-job",
                prediction_id=ordinary,
            )

    def test_failed_companion_outcome_is_retained_only_as_negative_signal(self):
        self.feedback(action_job_id="failed-job")
        failed = self.prediction(
            status="failed", evidence_ok=False, action_job_id="failed-job"
        )
        self.assertTrue(self.memory.bind_screen_companion_outcome(
            action_job_id="failed-job",
            prediction_id=failed,
        ))
        found = self.memory.screen_companion_feedback_for_action_job("failed-job")
        self.assertEqual(found["outcome"], "failed")
        self.assertEqual(found["evidence_kind"], "failure_observed")
        self.assertFalse(found["reusable"])

    def test_companion_prediction_cannot_create_a_canonical_lesson(self):
        for origin in ("companion_action", "companion_suggestion"):
            with self.subTest(origin=origin):
                conversation_id = self.memory.new_conversation(origin)
                prediction_id = self.memory.record_prediction(
                    family="conversation",
                    profile="quick",
                    model="test-model",
                    predicted_success=0.5,
                    predicted_steps=1,
                    predicted_verification="tool_success",
                    origin=origin,
                    conversation_id=conversation_id,
                )
                self.memory.resolve_prediction(
                    prediction_id,
                    actual_status="complete",
                    actual_steps=1,
                    evidence_ok=True,
                )
                reflection_id = self.memory.record_reflection(
                    status="complete",
                    summary="The companion run completed.",
                    mistakes="",
                    improvements="Repeat this action in similar contexts.",
                    conversation_id=conversation_id,
                    prediction_id=prediction_id,
                    tool_calls=1,
                )
                self.assertEqual(
                    int(self.memory.db.execute(
                        "SELECT COUNT(*) FROM memories WHERE kind='lesson'"
                    ).fetchone()[0]),
                    0,
                )
                self.assertEqual(
                    int(self.memory.db.execute(
                        "SELECT COUNT(*) FROM lesson_provenance"
                    ).fetchone()[0]),
                    0,
                )
                with self.assertRaises(ValueError):
                    self.memory.remember_verified_lesson(
                        "Task family: conversation.\n"
                        "Observed outcome: complete.\n"
                        "Reusable lesson: Repeat this action in similar contexts.",
                        family="conversation",
                        outcome_status="complete",
                        reflection_id=reflection_id,
                    )

    def test_companion_predictions_are_excluded_from_normal_calibration(self):
        baseline = self.memory.record_prediction(
            family="conversation",
            profile="quick",
            model="test-model",
            predicted_success=0.8,
            predicted_steps=0,
            predicted_verification="not_applicable",
            origin="interactive",
        )
        self.memory.resolve_prediction(
            baseline,
            actual_status="complete",
            actual_steps=0,
            evidence_ok=None,
        )
        expected_competence = self.memory.competence("conversation")
        expected_calibration = self.memory.calibration()
        expected_failures = self.memory.failure_histogram("conversation")
        expected_drift = self.memory.drift_report(
            window=1, baseline=1, minimum_samples=1
        )
        for index in range(20):
            prediction_id = self.memory.record_prediction(
                family="conversation",
                profile="quick",
                model="test-model",
                predicted_success=0.99,
                predicted_steps=1,
                predicted_verification="tool_success",
                origin=(
                    "companion_action" if index % 2 else "companion_suggestion"
                ),
                run_id=f"companion-run-{index}",
            )
            self.memory.resolve_prediction(
                prediction_id,
                actual_status="failed",
                actual_steps=1,
                evidence_ok=False,
                failure_class="unknown",
            )
        self.assertEqual(self.memory.competence("conversation"), expected_competence)
        self.assertEqual(self.memory.calibration(), expected_calibration)
        self.assertEqual(
            self.memory.failure_histogram("conversation"), expected_failures
        )
        self.assertEqual(
            self.memory.drift_report(window=1, baseline=1, minimum_samples=1),
            expected_drift,
        )

    def test_presence_job_origin_survives_and_stale_companion_job_is_not_replayed(self):
        companion_conversation = self.memory.new_conversation("Companion")
        interactive_conversation = self.memory.new_conversation("Interactive")
        companion_job = "a" * 32
        interactive_job = "b" * 32
        created = self.memory.create_presence_job(
            companion_job,
            conversation_id=companion_conversation,
            project_id=1,
            prompt="Generate one suggestion.",
            model_override="fast",
            run_origin="companion_suggestion",
            replayable=False,
        )
        self.assertEqual(created["run_origin"], "companion_suggestion")
        self.assertEqual(int(created["replayable"]), 0)
        self.memory.create_presence_job(
            interactive_job,
            conversation_id=interactive_conversation,
            project_id=1,
            prompt="Continue this request.",
            model_override="auto",
        )
        with self.assertRaises(ValueError):
            self.memory.create_presence_job(
                "c" * 32,
                conversation_id=self.memory.new_conversation("Unsafe replay"),
                project_id=1,
                prompt="Generate one suggestion.",
                model_override="fast",
                run_origin="companion_action",
                replayable=True,
            )

        recovered = self.memory.recover_presence_jobs("test-runtime")
        self.assertEqual(
            [row["job_id"] for row in recovered["queued"]],
            [interactive_job],
        )
        self.assertEqual(
            self.memory.get_presence_job(companion_job)["status"],
            "interrupted",
        )
        self.assertEqual(
            self.memory.get_presence_job(companion_job)["run_origin"],
            "companion_suggestion",
        )

    def test_policy_can_only_suppress_after_repeated_exact_dismissals(self):
        for index in range(3):
            self.feedback(
                suggestion="same-suggestion",
                context=f"context-{index}",
                application="same-application",
                decision="dismissed",
                category="writing",
                action_job_id=None,
            )
        policy = self.memory.screen_companion_learning_policy(
            suggestion_sha256=digest("same-suggestion"),
            application_sha256=digest("same-application"),
            category="writing",
        )
        self.assertEqual(policy["dismissed"], 3)
        self.assertEqual(policy["accepted"], 0)
        self.assertTrue(policy["suppress_auto"])

        self.feedback(
            suggestion="same-suggestion",
            context="accepted-context",
            application="same-application",
            decision="accepted",
            category="writing",
            action_job_id="accepted-job",
        )
        policy = self.memory.screen_companion_learning_policy(
            suggestion_sha256=digest("same-suggestion"),
            application_sha256=digest("same-application"),
            category="writing",
        )
        self.assertEqual(policy["accepted"], 1)
        self.assertFalse(policy["suppress_auto"])
        self.assertNotIn("authorize", policy)
        self.assertNotIn("allow", policy)

    def test_verified_outcomes_rank_categories_but_unverified_acceptance_does_not(self):
        self.feedback(
            suggestion="write", context="write", application="editor",
            category="writing", action_job_id="verified-writing",
        )
        verified = self.prediction(action_job_id="verified-writing")
        self.memory.bind_screen_companion_outcome(
            action_job_id="verified-writing", prediction_id=verified
        )
        self.feedback(
            suggestion="research", context="research", application="editor",
            category="research", action_job_id="unverified-research",
        )
        for index in range(3):
            self.feedback(
                suggestion=f"navigate-{index}", context=f"navigate-{index}",
                application="editor", decision="dismissed",
                category="navigation", action_job_id=None,
            )

        ranking = self.memory.screen_companion_learning_ranking(
            application_sha256=digest("editor")
        )
        self.assertEqual(ranking["preferred"], ["writing"])
        self.assertIn("navigation", ranking["avoided"])
        self.assertNotIn("research", ranking["preferred"])
        self.assertEqual(
            self.memory.screen_companion_learning_ranking(
                application_sha256=digest("other-application")
            )["preferred"],
            [],
        )

    def test_automatic_limits_are_atomic_and_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "jarvis.db"
            stamp = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
            with Memory(database) as memory:
                self.assertIsNotNone(memory.claim_screen_companion_auto(
                    context_sha256=digest("first"), cooldown_seconds=300,
                    daily_limit=6, now=stamp,
                ))
            with Memory(database) as memory:
                self.assertIsNone(memory.claim_screen_companion_auto(
                    context_sha256=digest("second"), cooldown_seconds=300,
                    daily_limit=6, now=stamp + timedelta(seconds=299),
                ))
                self.assertIsNotNone(memory.claim_screen_companion_auto(
                    context_sha256=digest("second"), cooldown_seconds=300,
                    daily_limit=6, now=stamp + timedelta(seconds=300),
                ))
                self.assertIsNotNone(memory.claim_screen_companion_auto(
                    context_sha256=digest("next-day"), cooldown_seconds=300,
                    daily_limit=6, now=stamp + timedelta(days=1),
                ))

            winners: list[int] = []
            barrier = threading.Barrier(6)

            def claim() -> None:
                with Memory(database) as memory:
                    barrier.wait()
                    receipt = memory.claim_screen_companion_auto(
                        context_sha256=digest("concurrent"),
                        cooldown_seconds=300,
                        daily_limit=6,
                        now=stamp + timedelta(days=2),
                    )
                    if receipt is not None:
                        winners.append(receipt)

            threads = [threading.Thread(target=claim) for _ in range(6)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(len(winners), 1)

    def test_stats_are_aggregate_and_forget_removes_all_learning_records(self):
        self.feedback(category="coding", action_job_id="stats-job")
        prediction_id = self.prediction(
            status="incomplete", evidence_ok=False, action_job_id="stats-job"
        )
        self.memory.bind_screen_companion_outcome(
            action_job_id="stats-job", prediction_id=prediction_id
        )
        self.feedback(
            suggestion="dismissed",
            context="dismissed-context",
            decision="dismissed",
            category="coding",
            action_job_id=None,
        )
        rule_id = self.memory.add_screen_companion_rule(
            trigger_app="notes.exe", action_prompt="Offer a concise outline tip."
        )
        receipt_id = self.memory.claim_screen_companion_rule(
            rule_id,
            application="notes.exe",
            context_sha256=digest("receipt-context"),
        )
        self.assertIsNotNone(receipt_id)
        self.assertIsNotNone(self.memory.claim_screen_companion_auto(
            context_sha256=digest("auto-context"),
            cooldown_seconds=300,
        ))

        stats = self.memory.screen_companion_learning_stats()
        self.assertEqual(stats["feedback"], 2)
        self.assertEqual(stats["accepted"], 1)
        self.assertEqual(stats["dismissed"], 1)
        self.assertEqual(stats["verified_outcomes"], 1)
        self.assertEqual(stats["reusable_outcomes"], 0)
        self.assertEqual(stats["non_reusable_outcomes"], 1)
        self.assertEqual(stats["by_category"]["coding"]["total"], 2)

        self.assertEqual(self.memory.forget_screen_companion_receipts(), 6)
        for table in (
            "screen_companion_receipts",
            "screen_companion_auto_receipts",
            "screen_companion_feedback",
            "screen_companion_action_outcomes",
        ):
            self.assertEqual(
                int(self.memory.db.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]),
                0,
            )
        self.assertEqual(
            int(self.memory.db.execute(
                """SELECT COUNT(*) FROM task_predictions
                   WHERE origin IN ('companion_suggestion','companion_action')"""
            ).fetchone()[0]),
            0,
        )


if __name__ == "__main__":
    unittest.main()
