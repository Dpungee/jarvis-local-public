from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from jarvis.memory import Memory


class MemoryTransitionContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="jarvis-memory-transitions-"
        )
        self.database = Path(self.temporary.name) / "jarvis.db"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _digest(character: str) -> str:
        return character * 64

    def test_project_listing_counts_and_presence_job_filters_are_durable(self) -> None:
        queued_job = "a" * 32
        completed_job = "b" * 32
        with Memory(self.database) as memory:
            project_id = memory.add_project("Security Lab", "@projects/security-lab")
            project_conversation = memory.new_conversation(
                "Project conversation", project_id=project_id
            )
            memory.add_task("Inspect the project", project_id=project_id)
            queued_conversation = memory.new_conversation("Queued Presence")
            completed_conversation = memory.new_conversation("Completed Presence")
            memory.create_presence_job(
                queued_job,
                conversation_id=queued_conversation,
                project_id=1,
                prompt="Remain queued",
                model_override="auto",
            )
            memory.create_presence_job(
                completed_job,
                conversation_id=completed_conversation,
                project_id=1,
                prompt="Finish deterministically",
                model_override="auto",
            )
            self.assertTrue(memory.claim_presence_job(completed_job, "presence:test"))
            self.assertTrue(
                memory.finish_presence_job(
                    completed_job,
                    "completed",
                    runtime_id="presence:test",
                )
            )

            projects = memory.list_projects()
            self.assertEqual(projects[0]["id"], 1)
            project = next(row for row in projects if row["id"] == project_id)
            self.assertEqual(project["conversation_count"], 1)
            self.assertEqual(project["task_count"], 1)
            self.assertEqual(project_conversation > 0, True)
            self.assertEqual(
                [row["job_id"] for row in memory.list_presence_jobs()],
                [queued_job],
            )
            self.assertEqual(
                [
                    row["job_id"]
                    for row in memory.list_presence_jobs(
                        statuses=("completed",), limit=1
                    )
                ],
                [completed_job],
            )
            for statuses in ((), ("unknown",)):
                with self.subTest(statuses=statuses), self.assertRaises(ValueError):
                    memory.list_presence_jobs(statuses=statuses)

        with Memory(self.database) as reopened:
            project = next(
                row for row in reopened.list_projects() if row["id"] == project_id
            )
            self.assertEqual(
                (project["conversation_count"], project["task_count"]),
                (1, 1),
            )
            self.assertEqual(
                reopened.list_presence_jobs(statuses=("completed",))[0]["job_id"],
                completed_job,
            )

    def test_revoke_all_presence_sessions_is_idempotent_and_durable(self) -> None:
        with Memory(self.database) as memory:
            sessions = []
            for label in ("phone", "tablet"):
                pairing = memory.create_presence_pairing_code(label)
                session = memory.consume_presence_pairing_code(pairing["code"])
                self.assertIsNotNone(session)
                sessions.append(session)
            self.assertEqual(memory.revoke_all_presence_sessions(), 2)
            self.assertEqual(memory.revoke_all_presence_sessions(), 0)
            for session in sessions:
                self.assertFalse(memory.authenticate_presence_session(session["token"]))

        with Memory(self.database) as reopened:
            listed = reopened.list_presence_sessions()
            self.assertEqual(len(listed), 2)
            self.assertTrue(all(row["revoked_at"] for row in listed))

    def test_screen_companion_rule_and_feedback_transitions_are_durable(self) -> None:
        action_job_id = "companion-action-1"
        with Memory(self.database) as memory:
            rule_id = memory.add_screen_companion_rule(
                trigger_app="notes.exe",
                action_prompt="Offer one concise outline suggestion.",
            )
            self.assertTrue(memory.set_screen_companion_rule_enabled(rule_id, False))
            self.assertTrue(memory.set_screen_companion_rule_enabled(rule_id, False))
            self.assertFalse(memory.list_screen_companion_rules()[0]["enabled"])
            with self.assertRaises(TypeError):
                memory.set_screen_companion_rule_enabled(rule_id, 1)
            for invalid_id in (None, True, "1", 0):
                with self.subTest(rule_id=invalid_id), self.assertRaises(ValueError):
                    memory.set_screen_companion_rule_enabled(invalid_id, True)
            self.assertFalse(memory.set_screen_companion_rule_enabled(999_999, True))
            self.assertTrue(memory.set_screen_companion_rule_enabled(rule_id, True))
            receipt_id = memory.claim_screen_companion_rule(
                rule_id,
                application="notes.exe",
                context_sha256=self._digest("c"),
            )
            self.assertIsNotNone(receipt_id)

            feedback_id = memory.record_screen_companion_feedback(
                suggestion_sha256=self._digest("a"),
                context_sha256=self._digest("b"),
                application_sha256=self._digest("d"),
                decision="accepted",
                action_job_id=action_job_id,
            )
            self.assertGreater(feedback_id, 0)
            self.assertIsNotNone(
                memory.screen_companion_feedback_for_action_job(action_job_id)
            )
            self.assertTrue(
                memory.discard_screen_companion_feedback_for_action_job(action_job_id)
            )
            self.assertFalse(
                memory.discard_screen_companion_feedback_for_action_job(action_job_id)
            )
            self.assertIsNone(
                memory.screen_companion_feedback_for_action_job(action_job_id)
            )
            for invalid_job in (1, "bad job id"):
                with self.subTest(action_job_id=invalid_job), self.assertRaises(
                    (TypeError, ValueError)
                ):
                    memory.discard_screen_companion_feedback_for_action_job(invalid_job)

            self.assertTrue(memory.delete_screen_companion_rule(rule_id))
            self.assertFalse(memory.delete_screen_companion_rule(rule_id))
            for invalid_id in (None, True, "1", 0):
                with self.subTest(delete_rule_id=invalid_id), self.assertRaises(
                    ValueError
                ):
                    memory.delete_screen_companion_rule(invalid_id)
            self.assertEqual(memory.list_screen_companion_rules(), [])
            self.assertEqual(
                memory.db.execute(
                    "SELECT COUNT(*) FROM screen_companion_receipts"
                ).fetchone()[0],
                0,
            )

        with Memory(self.database) as reopened:
            self.assertEqual(reopened.list_screen_companion_rules(), [])
            self.assertIsNone(
                reopened.screen_companion_feedback_for_action_job(action_job_id)
            )

    def test_scheduled_job_transitions_validate_scope_type_and_persist(self) -> None:
        start = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
        resumed = start + timedelta(hours=1)
        with Memory(self.database) as memory:
            job = memory.add_scheduled_job(
                "Status brief",
                "Write one verified local status brief.",
                60,
                project_id=1,
                now=start,
            )
            job_id = job["id"]
            self.assertTrue(
                memory.set_scheduled_job_enabled(
                    job_id, False, project_id=1, now=start
                )
            )
            self.assertTrue(
                memory.set_scheduled_job_enabled(
                    job_id, False, project_id=1, now=start
                )
            )
            self.assertEqual(memory.list_scheduled_jobs()[0]["enabled"], 0)
            with self.assertRaises(TypeError):
                memory.set_scheduled_job_enabled(job_id, "false", project_id=1)
            for invalid_id in (None, True, "1", 0):
                with self.subTest(job_id=invalid_id), self.assertRaises(ValueError):
                    memory.set_scheduled_job_enabled(invalid_id, True, project_id=1)
            self.assertFalse(
                memory.set_scheduled_job_enabled(
                    job_id, True, project_id=999_999, now=resumed
                )
            )
            self.assertTrue(
                memory.set_scheduled_job_enabled(
                    job_id, True, project_id=1, now=resumed
                )
            )
            enabled = memory.list_scheduled_jobs()[0]
            self.assertEqual(enabled["enabled"], 1)
            self.assertEqual(
                datetime.fromisoformat(enabled["next_run_at"]),
                resumed + timedelta(minutes=1),
            )
            self.assertTrue(memory.delete_scheduled_job(job_id, project_id=1))
            self.assertFalse(memory.delete_scheduled_job(job_id, project_id=1))
            for invalid_id in (None, True, "1", 0):
                with self.subTest(delete_id=invalid_id), self.assertRaises(ValueError):
                    memory.delete_scheduled_job(invalid_id, project_id=1)

        with Memory(self.database) as reopened:
            self.assertEqual(reopened.list_scheduled_jobs(), [])

    def test_goal_subject_and_backlog_transitions_validate_and_persist(self) -> None:
        with Memory(self.database) as memory:
            goal_id = memory.add_goal("Build a verified lab")
            self.assertTrue(memory.update_goal_status(goal_id, "paused"))
            self.assertTrue(memory.update_goal_status(goal_id, "paused"))
            self.assertEqual(memory.list_goals()[0]["status"], "paused")
            with self.assertRaises(ValueError):
                memory.update_goal_status(goal_id, "unknown")
            for invalid_id in (None, True, "1", 0):
                with self.subTest(goal_id=invalid_id), self.assertRaises(ValueError):
                    memory.update_goal_status(invalid_id, "active")
            self.assertFalse(memory.update_goal_status(999_999, "active"))

            subject_id = memory.approve_subject(
                "Local network reliability", "Operator-approved scope"
            )
            self.assertEqual(
                memory.approve_subject(
                    "Local network reliability", "Updated operator scope"
                ),
                subject_id,
            )
            subjects = memory.list_subjects()
            self.assertEqual(len(subjects), 1)
            self.assertEqual(subjects[0]["notes"], "Updated operator scope")
            backlog_id = memory.add_backlog_item("research", subject_id)
            self.assertTrue(memory.set_backlog_enabled(backlog_id, False))
            self.assertTrue(memory.set_backlog_enabled(backlog_id, False))
            self.assertEqual(memory.list_backlog()[0]["enabled"], 0)
            with self.assertRaises(TypeError):
                memory.set_backlog_enabled(backlog_id, "false")
            for invalid_id in (None, True, "1", 0):
                with self.subTest(backlog_id=invalid_id), self.assertRaises(ValueError):
                    memory.set_backlog_enabled(invalid_id, True)
            self.assertFalse(memory.set_backlog_enabled(999_999, True))

        with Memory(self.database) as reopened:
            self.assertEqual(reopened.list_goals()[0]["status"], "paused")
            self.assertEqual(reopened.list_subjects()[0]["id"], subject_id)
            self.assertEqual(reopened.list_backlog()[0]["enabled"], 0)

    def test_work_domain_revoke_is_bounded_idempotent_and_durable(self) -> None:
        with Memory(self.database) as memory:
            domain_id = memory.approve_work_domain(
                "Local maintenance",
                kind="maintenance",
                project_id=1,
                max_tasks_per_day=2,
            )
            self.assertEqual(
                memory.approve_work_domain(
                    "Local maintenance",
                    kind="maintenance",
                    project_id=1,
                    max_tasks_per_day=2,
                ),
                domain_id,
            )
            self.assertTrue(memory.revoke_work_domain(domain_id))
            self.assertTrue(memory.revoke_work_domain(domain_id))
            domain = memory.list_work_domains()[0]
            self.assertEqual(domain["project_name"], "Default workspace")
            self.assertEqual(domain["enabled"], 0)
            self.assertEqual(domain["standing_authorization"], 0)
            for invalid_id in (None, True, "1", 0):
                with self.subTest(domain_id=invalid_id), self.assertRaises(ValueError):
                    memory.revoke_work_domain(invalid_id)
            self.assertFalse(memory.revoke_work_domain(999_999))

        with Memory(self.database) as reopened:
            domain = reopened.list_work_domains()[0]
            self.assertEqual(domain["id"], domain_id)
            self.assertEqual(
                (domain["enabled"], domain["standing_authorization"]),
                (0, 0),
            )


if __name__ == "__main__":
    unittest.main()
