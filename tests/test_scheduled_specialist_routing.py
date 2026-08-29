from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from jarvis.memory import Memory
from jarvis.specialists import (
    scheduled_prompt_required_tool_surfaces,
    specialist_for_prompt,
    specialist_for_scheduled_prompt,
)


class ScheduledSpecialistRoutingTests(unittest.TestCase):
    def test_incompatible_private_and_external_jobs_stay_with_jarvis(self) -> None:
        cases = (
            "Organize the files in my Downloads folder on my computer.",
            "Organize old files in my Google Drive account.",
        )
        for prompt in cases:
            with self.subTest(prompt=prompt):
                topical = specialist_for_prompt(prompt)
                self.assertIsNotNone(topical)
                self.assertTrue(scheduled_prompt_required_tool_surfaces(prompt))
                self.assertIsNone(specialist_for_scheduled_prompt(prompt))

    def test_due_queue_preserves_main_ownership_for_incompatible_surfaces(self) -> None:
        start = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
        prompts = (
            "Search my private files in C:\\Users\\operator\\Documents for invoices.",
            "Organize and rename files in my Google Drive account.",
        )
        with Memory(Path(":memory:")) as memory:
            for index, prompt in enumerate(prompts, start=1):
                memory.add_scheduled_job(
                    f"Main-owned job {index}",
                    prompt,
                    5,
                    project_id=1,
                    now=start,
                )

            self.assertEqual(
                memory.queue_due_scheduled_jobs(now=start + timedelta(minutes=5)),
                2,
            )
            tasks = memory.list_tasks(limit=10)
            self.assertEqual(len(tasks), 2)
            for task in tasks:
                self.assertIsNone(task["specialist_key"])
                self.assertIsNone(task["requested_model"])
                self.assertEqual(task["delegated_by"], "schedule")

    def test_external_job_remains_main_owned_across_approval_resume(self) -> None:
        start = datetime.now(UTC) - timedelta(minutes=10)
        due = start + timedelta(minutes=5)
        with Memory(Path(":memory:")) as memory:
            memory.add_scheduled_job(
                "Drive cleanup",
                "Organize old files in my Google Drive account.",
                5,
                project_id=1,
                now=start,
            )
            self.assertEqual(memory.queue_due_scheduled_jobs(now=due), 1)
            queued = memory.list_tasks(limit=1)[0]
            self.assertIsNone(queued["specialist_key"])

            task_id = int(queued["id"])
            claimed = memory.claim_task("scheduled-approval-worker")
            self.assertIsNotNone(claimed)
            self.assertEqual(claimed["id"], task_id)
            allowed, approval_id = memory.authorize_or_request(
                "communicate_external",
                '{"account":"google-drive","operation":"organize"}',
                "Changes the exact approved Google Drive items.",
                approval_scope=f"task:{task_id}",
                task_id=task_id,
            )
            self.assertFalse(allowed)
            self.assertEqual(
                memory.await_task_approval(
                    task_id,
                    approval_id,
                    worker_id="scheduled-approval-worker",
                ),
                "awaiting_approval",
            )
            self.assertTrue(memory.decide_approval(approval_id, True, ttl_hours=2))

            reclaimed = memory.claim_task("scheduled-approval-worker")
            self.assertIsNotNone(reclaimed)
            self.assertEqual(reclaimed["id"], task_id)
            self.assertIsNone(reclaimed["specialist_key"])
            allowed, consumed_id = memory.authorize_or_request(
                "communicate_external",
                '{"account":"google-drive","operation":"organize"}',
                "Changes the exact approved Google Drive items.",
                approval_scope=f"task:{task_id}",
                task_id=task_id,
            )
            self.assertTrue(allowed)
            self.assertEqual(consumed_id, approval_id)
            self.assertTrue(
                memory.finish_task(
                    task_id,
                    "Google Drive operation completed.",
                    worker_id="scheduled-approval-worker",
                )
            )

    def test_compatible_research_and_coding_jobs_still_delegate(self) -> None:
        start = datetime(2026, 8, 28, 13, 0, tzinfo=UTC)
        expected = {
            "Research current battery-recycling standards and cite public sources.": (
                "research",
                "reasoning",
            ),
            "Build and test a Python parser in the assigned workspace.": (
                "coding",
                "coding",
            ),
            "Organize files into topic folders inside the assigned workspace.": (
                "operations",
                "reasoning",
            ),
        }
        with Memory(Path(":memory:")) as memory:
            for index, prompt in enumerate(expected, start=1):
                memory.add_scheduled_job(
                    f"Compatible job {index}",
                    prompt,
                    10,
                    project_id=1,
                    now=start,
                )

            self.assertEqual(
                memory.queue_due_scheduled_jobs(now=start + timedelta(minutes=10)),
                3,
            )
            tasks = memory.list_tasks(limit=10)
            self.assertEqual(len(tasks), 3)
            for task in tasks:
                original = next(
                    prompt for prompt in expected if prompt in str(task["prompt"])
                )
                specialist_key, model_profile = expected[original]
                self.assertEqual(task["specialist_key"], specialist_key)
                self.assertEqual(task["requested_model"], model_profile)
                self.assertEqual(task["delegated_by"], "schedule")

    def test_subject_mentions_do_not_imply_private_device_control(self) -> None:
        prompt = "Research current Bluetooth security guidance and cite public sources."
        self.assertEqual(scheduled_prompt_required_tool_surfaces(prompt), ())
        selected = specialist_for_scheduled_prompt(prompt)
        self.assertIsNotNone(selected)
        self.assertEqual(selected.key, "research")


if __name__ == "__main__":
    unittest.main()
