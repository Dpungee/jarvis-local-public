from __future__ import annotations

import json
import os
import shutil
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from jarvis.agent import Agent
from jarvis.config import Config
from jarvis.memory import Memory
from jarvis.proactive import record_result_reflection
from tests.test_agent import (
    DelegatingFakeToolBox,
    FakeResponse,
    FakeToolBox,
    ScriptedClient,
    tool_call,
)


TEMP_ROOT = Path(__file__).resolve().parent / ".tmp"
TEMP_ROOT.mkdir(exist_ok=True)


class ScheduleReceiptToolBox(FakeToolBox):
    NAMES = (*FakeToolBox.NAMES, "schedule_create", "schedule_list")

    def __init__(self, memory, *, final_state: str = "active", existing_id=None):
        super().__init__()
        self.memory = memory
        self.final_state = final_state
        self.existing_id = existing_id
        self.receipt_id = None

    def execute(self, name, arguments):
        if name == "schedule_create":
            self.calls.append((name, arguments))
            if self.existing_id is not None:
                rows = self.memory.list_scheduled_jobs(project_id=1, limit=200)
                row = next(
                    item for item in rows
                    if int(item["id"]) == int(self.existing_id)
                )
            else:
                row = self.memory.add_scheduled_job(
                    str(arguments.get("name") or "daily review"),
                    str(arguments.get("task") or "Review the approved backlog."),
                    int(arguments.get("interval_minutes") or 1440),
                    project_id=1,
                )
            self.receipt_id = int(row["id"])
            if self.final_state in {"cancelled", "disabled"}:
                self.memory.set_scheduled_job_enabled(
                    self.receipt_id,
                    False,
                    project_id=1,
                )
            elif self.final_state == "deleted":
                self.memory.delete_scheduled_job(self.receipt_id, project_id=1)
            elif self.final_state == "failed":
                # The current schema has no separate failure status. An empty
                # next run is the fail-closed representation of a schedule
                # that has no pending execution.
                self.memory.db.execute(
                    "UPDATE scheduled_jobs SET next_run_at='' WHERE id=?",
                    (self.receipt_id,),
                )
                self.memory.db.commit()
            return json.dumps({
                "ok": True,
                "result": {
                    **row,
                    "id": self.receipt_id,
                },
            })
        if name == "schedule_list":
            self.calls.append((name, arguments))
            return json.dumps({"ok": True, "result": []})
        return super().execute(name, arguments)


class DelegationAndScheduleToolBox(DelegatingFakeToolBox):
    NAMES = (*DelegatingFakeToolBox.NAMES, "schedule_create", "schedule_list")

    def execute(self, name, arguments):
        if name == "schedule_create":
            self.calls.append((name, arguments))
            return json.dumps({"ok": True, "result": {"id": 73, "enabled": True}})
        if name == "schedule_list":
            self.calls.append((name, arguments))
            return json.dumps({"ok": True, "result": []})
        return super().execute(name, arguments)


class CollidingReceiptToolBox(ScheduleReceiptToolBox):
    NAMES = (*ScheduleReceiptToolBox.NAMES, "delegate_specialist")

    def execute(self, name, arguments):
        if name == "delegate_specialist":
            self.calls.append((name, arguments))
            return json.dumps({
                "ok": True,
                "result": {
                    "task_id": 1,
                    "specialist": "Sentinel",
                    "status": "queued",
                },
            })
        return super().execute(name, arguments)


class AgentCompletionTruthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.test_dir = TEMP_ROOT / (
            f"agent-completion-truth-{os.getpid()}-{self._testMethodName}"
        )
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir)
        self.workspace = self.test_dir / "workspace"
        self.data_dir = self.test_dir / "data"
        self.workspace.mkdir(parents=True)
        self.data_dir.mkdir()
        self.config = replace(
            Config.load(),
            model="auto",
            workspace=self.workspace,
            data_dir=self.data_dir,
            vault_dir=None,
            max_steps=6,
            context_length=4096,
            execution_mode="trusted-host",
            autonomy="autonomous",
            computer_access="disabled",
            ollama_preload=False,
        )
        self.memory = Memory(self.data_dir / "agent.db")

    def tearDown(self) -> None:
        self.memory.close()
        resolved = self.test_dir.resolve()
        self.assertEqual(resolved.parent, TEMP_ROOT.resolve())
        shutil.rmtree(resolved)

    def make_agent(self, responses, *, toolbox=None, events=None):
        selected_toolbox = toolbox or FakeToolBox()
        client = ScriptedClient(responses)
        # Construct against the exact bounded fake rather than initializing
        # unrelated live adapters (network, Bluetooth, Drive, etc.).
        with patch("jarvis.agent.ToolBox", return_value=selected_toolbox):
            agent = Agent(
                self.config,
                self.memory,
                (events if events is not None else []).append,
                client=client,
                coding_review=False,
                coding_planning=False,
            )
        return agent, client

    def test_one_bounded_correction_replaces_promise_with_immediate_answer(self):
        events: list[str] = []
        agent, client = self.make_agent(
            [
                FakeResponse(content="I'll look into that and get back to you."),
                FakeResponse(content=(
                    "DNS caching reduces repeated resolver lookups, which lowers latency "
                    "and upstream DNS traffic until each cached record's TTL expires."
                )),
            ],
            events=events,
        )

        result = agent.run("Explain why DNS caching helps performance in one paragraph.")

        self.assertEqual(result.status, "complete", result.reason)
        self.assertIn("reduces repeated resolver lookups", str(result))
        self.assertEqual(len(client.requests), 2)
        correction = client.requests[1]["messages"][-1]
        self.assertEqual(correction["role"], "user")
        self.assertIn("prior draft promised future or off-turn work", correction["content"])
        self.assertEqual(
            events.count("completion truth - retrying one unreceipted future promise"),
            1,
        )

    def test_repeated_promise_is_incomplete_and_cannot_become_a_lesson(self):
        events: list[str] = []
        agent, _client = self.make_agent(
            [
                FakeResponse(content="I'll research that and report back."),
                FakeResponse(content="I'll keep working on it in the background."),
            ],
            events=events,
        )

        result = agent.run("Explain how DNS caching reduces latency.")

        self.assertEqual(result.status, "incomplete")
        self.assertTrue(result.retryable)
        self.assertFalse(result.lesson_eligible)
        self.assertIn("Nothing will continue in the background", str(result))
        self.assertIn("verified durable task receipt", str(result.reason))
        self.assertEqual(
            self.memory.list_training_examples(verified_only=False),
            [],
        )

        reflection_id = record_result_reflection(self.memory, result)
        reflection = self.memory.db.execute(
            "SELECT improvements FROM reflections WHERE id=?",
            (reflection_id,),
        ).fetchone()
        self.assertIsNotNone(reflection)
        self.assertEqual(reflection["improvements"], "")
        lessons = self.memory.db.execute(
            "SELECT COUNT(*) AS count FROM memories WHERE kind='lesson'"
        ).fetchone()
        self.assertEqual(int(lessons["count"]), 0)
        self.assertIn(
            "completion truth - unreceipted future promise blocked",
            events,
        )

    def test_invented_receipt_id_is_rejected_without_runtime_receipt(self):
        events: list[str] = []
        agent, client = self.make_agent(
            [
                FakeResponse(content=(
                    "I'll report back. Queued task #invented-999 is durable."
                )),
                FakeResponse(content=(
                    "I cannot truthfully claim background work was queued; no task was "
                    "created in this run."
                )),
            ],
            events=events,
        )

        result = agent.run("Explain the difference between recursive and iterative DNS.")

        self.assertEqual(result.status, "complete", result.reason)
        self.assertEqual(len(client.requests), 2)
        self.assertNotIn("invented-999", str(result))
        self.assertIn("no task was created", str(result))
        self.assertIn(
            "completion truth - retrying one unreceipted future promise",
            events,
        )

    def test_automatic_specialist_receipt_cannot_launder_future_promise(self):
        (self.data_dir / "worker.heartbeat").write_text(
            f"{time.time():.6f} 123 worker:test\n",
            encoding="utf-8",
        )
        events: list[str] = []
        agent, client = self.make_agent(
            [
                FakeResponse(content=(
                    "I'll report back later. Queued task #42 remains active."
                )),
                FakeResponse(content=(
                    "The current defensive recommendation is to use default-deny rules "
                    "and verify recovery before expanding access."
                )),
            ],
            toolbox=DelegatingFakeToolBox(),
            events=events,
        )

        result = agent.run(
            "Give me a defensive cybersecurity assessment for my isolated lab."
        )

        self.assertEqual(result.status, "complete", result.reason)
        self.assertIn("default-deny", str(result))
        self.assertNotIn("report back", str(result).casefold())
        self.assertEqual(len(client.requests), 2)
        self.assertIn(
            "completion truth - retrying one unreceipted future promise",
            events,
        )

    def test_specialist_id_cannot_substitute_for_unexecuted_schedule_effect(self):
        delegation = {
            "task": "Review the safety of the requested reminder schedule.",
            "specialist": "security",
        }
        events: list[str] = []
        toolbox = DelegationAndScheduleToolBox()
        agent, client = self.make_agent(
            [
                FakeResponse(tool_calls=[tool_call("delegate_specialist", delegation)]),
                FakeResponse(content=(
                    "I'll report back after the reminder runs. Scheduled task #42 is active."
                )),
                FakeResponse(content=(
                    "No reminder schedule was actually created in this run."
                )),
            ],
            toolbox=toolbox,
            events=events,
        )

        result = agent.run(
            "Create a recurring scheduled task daily named daily review and use a "
            "specialist agent to review it."
        )

        self.assertEqual(result.status, "incomplete")
        self.assertIn("schedule", str(result.reason).casefold())
        self.assertEqual(
            [name for name, _arguments in toolbox.calls],
            ["delegate_specialist"],
        )
        self.assertEqual(len(client.requests), 3)
        self.assertIn(
            "completion truth - retrying one unreceipted future promise",
            events,
        )

    def test_schedule_receipt_is_accepted_only_after_actual_id_is_cited(self):
        arguments = {
            "name": "daily review",
            "task": "Review the approved project backlog.",
            "interval_minutes": 1440,
        }
        toolbox = ScheduleReceiptToolBox(self.memory)
        events: list[str] = []
        agent, client = self.make_agent(
            [
                FakeResponse(tool_calls=[tool_call("schedule_create", arguments)]),
                FakeResponse(content=(
                    "I'll report back after it runs. Scheduled task #999 is active."
                )),
                FakeResponse(content=(
                    "I'll report back after it runs. Scheduled task #1 is active."
                )),
            ],
            toolbox=toolbox,
            events=events,
        )

        result = agent.run(
            "Create a recurring scheduled task daily named daily review."
        )

        self.assertEqual(result.status, "complete", result.reason)
        self.assertIn("Scheduled task #1", str(result))
        self.assertNotIn("#999", str(result))
        self.assertEqual(toolbox.calls, [("schedule_create", arguments)])
        self.assertEqual(len(client.requests), 3)
        self.assertIn(
            "completion truth - retrying one unreceipted future promise",
            events,
        )

    def test_actual_schedule_receipt_passes_without_correction(self):
        arguments = {
            "name": "daily review",
            "task": "Review the approved project backlog.",
            "interval_minutes": 1440,
        }
        toolbox = ScheduleReceiptToolBox(self.memory)
        events: list[str] = []
        agent, client = self.make_agent(
            [
                FakeResponse(tool_calls=[tool_call("schedule_create", arguments)]),
                FakeResponse(content=(
                    "I'll report back after it runs. Scheduled task #1 is active."
                )),
            ],
            toolbox=toolbox,
            events=events,
        )

        result = agent.run(
            "Create a recurring scheduled task daily named daily review."
        )

        self.assertEqual(result.status, "complete", result.reason)
        self.assertIn("Scheduled task #1", str(result))
        self.assertEqual(len(client.requests), 2)
        self.assertNotIn(
            "completion truth - retrying one unreceipted future promise",
            events,
        )

    def test_invented_schedule_receipt_is_corrected_without_future_promise(self):
        arguments = {
            "name": "daily review",
            "task": "Review the approved project backlog.",
            "interval_minutes": 1440,
        }
        toolbox = ScheduleReceiptToolBox(self.memory)
        events: list[str] = []
        agent, client = self.make_agent(
            [
                FakeResponse(tool_calls=[tool_call("schedule_create", arguments)]),
                FakeResponse(content="Done. Scheduled task #999 is active."),
                FakeResponse(content="Done. Scheduled task #1 is active."),
            ],
            toolbox=toolbox,
            events=events,
        )

        result = agent.run(
            "Create a recurring scheduled task daily named daily review."
        )

        self.assertEqual(result.status, "complete", result.reason)
        self.assertIn("Scheduled task #1", str(result))
        self.assertNotIn("#999", str(result))
        self.assertEqual(len(client.requests), 3)
        self.assertIn(
            "completion truth - retrying one unreceipted future promise",
            events,
        )

    def _assert_inactive_schedule_cannot_be_published(self, final_state):
        arguments = {
            "name": "daily review",
            "task": "Review the approved project backlog.",
            "interval_minutes": 1440,
        }
        toolbox = ScheduleReceiptToolBox(
            self.memory,
            final_state=final_state,
        )
        events: list[str] = []
        agent, client = self.make_agent(
            [
                FakeResponse(tool_calls=[tool_call("schedule_create", arguments)]),
                FakeResponse(content=(
                    "I'll report back after it runs. Scheduled task #1 is active."
                )),
                FakeResponse(content=(
                    "The schedule is not active, so no future work is queued."
                )),
            ],
            toolbox=toolbox,
            events=events,
        )

        result = agent.run(
            "Create a recurring scheduled task daily named daily review."
        )

        self.assertEqual(result.status, "incomplete")
        self.assertIn("schedule", str(result.reason).casefold())
        self.assertNotIn("scheduled task #1 is active", str(result).casefold())
        self.assertEqual(len(client.requests), 3)
        self.assertIn(
            "completion truth - retrying one unreceipted future promise",
            events,
        )

    def test_disabled_schedule_after_create_cannot_be_published(self):
        self._assert_inactive_schedule_cannot_be_published("disabled")

    def test_cancelled_schedule_after_create_cannot_be_published(self):
        self._assert_inactive_schedule_cannot_be_published("cancelled")

    def test_deleted_schedule_after_create_cannot_be_published(self):
        self._assert_inactive_schedule_cannot_be_published("deleted")

    def test_failed_schedule_after_create_cannot_be_published(self):
        self._assert_inactive_schedule_cannot_be_published("failed")

    def test_prior_run_other_conversation_schedule_id_is_not_current_receipt(self):
        other_conversation = self.memory.new_conversation("Earlier conversation")
        prior = self.memory.add_scheduled_job(
            "prior job",
            f"Created for conversation {other_conversation}",
            1440,
            project_id=1,
        )
        self.assertEqual(prior["id"], 1)
        arguments = {
            "name": "daily review",
            "task": "Review the approved project backlog.",
            "interval_minutes": 1440,
        }
        toolbox = ScheduleReceiptToolBox(
            self.memory,
            existing_id=prior["id"],
        )
        events: list[str] = []
        agent, client = self.make_agent(
            [
                FakeResponse(tool_calls=[tool_call("schedule_create", arguments)]),
                FakeResponse(content=(
                    "I'll report back after it runs. Scheduled task #1 is active."
                )),
                FakeResponse(content=(
                    "No new schedule was created for this request."
                )),
            ],
            toolbox=toolbox,
            events=events,
        )

        result = agent.run(
            "Create a recurring scheduled task daily named daily review."
        )

        self.assertEqual(result.status, "incomplete")
        self.assertIn("schedule", str(result.reason).casefold())
        self.assertEqual(len(client.requests), 3)
        self.assertIn(
            "completion truth - retrying one unreceipted future promise",
            events,
        )

    def test_specialist_and_schedule_numeric_id_collision_preserves_schedule_kind(self):
        schedule_arguments = {
            "name": "daily review",
            "task": "Review the approved project backlog.",
            "interval_minutes": 1440,
        }
        delegation_arguments = {
            "task": "Review this schedule's safety.",
            "specialist": "security",
        }
        toolbox = CollidingReceiptToolBox(self.memory)
        events: list[str] = []
        agent, client = self.make_agent(
            [
                FakeResponse(tool_calls=[
                    tool_call("delegate_specialist", delegation_arguments),
                    tool_call("schedule_create", schedule_arguments),
                ]),
                FakeResponse(content=(
                    "I'll report back after it runs. Scheduled task #1 is active."
                )),
            ],
            toolbox=toolbox,
            events=events,
        )

        result = agent.run(
            "Create a recurring scheduled task daily named daily review and use a "
            "specialist agent to review it."
        )

        self.assertEqual(result.status, "complete", result.reason)
        self.assertIn("Scheduled task #1", str(result))
        self.assertEqual(
            [name for name, _arguments in toolbox.calls],
            ["delegate_specialist", "schedule_create"],
        )
        self.assertEqual(toolbox.receipt_id, 1)
        self.assertEqual(
            self.memory.list_scheduled_jobs(project_id=1, limit=10)[0]["id"],
            1,
        )
        self.assertEqual(len(client.requests), 2)
        self.assertFalse(any("completion truth" in event for event in events))

    def test_immediate_explanation_remains_complete_without_retry(self):
        events: list[str] = []
        agent, client = self.make_agent(
            [FakeResponse(content=(
                "I'll explain it now: authoritative DNS servers publish records, while "
                "recursive resolvers retrieve and cache those records for clients."
            ))],
            events=events,
        )

        result = agent.run("Explain authoritative and recursive DNS simply.")

        self.assertEqual(result.status, "complete", result.reason)
        self.assertTrue(result.lesson_eligible)
        self.assertEqual(len(client.requests), 1)
        self.assertFalse(any("completion truth" in event for event in events))


if __name__ == "__main__":
    unittest.main()
