from __future__ import annotations

import json
import tempfile
import sqlite3
import threading
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jarvis.config import Config
from jarvis.memory import Memory, SCHEMA_VERSION
from jarvis.proactive import (
    COMPETENCE_MIN_ATTEMPTS,
    RuntimeGuard,
    build_self_model,
    calibrated_meta_gate,
    competence_prediction,
    initiative_cycle,
    initiative_eligibility,
    runtime_identity_contract,
    self_context,
)


class ProactiveSystemTests(unittest.TestCase):
    def _seed_calibrated_family(self, family: str) -> None:
        for index in range(20):
            success = index % 5 != 0
            prediction_id = self.memory.record_prediction(
                family=family, profile="fast", model="m",
                predicted_success=0.8, predicted_steps=3,
                predicted_verification="tool_success",
            )
            self.memory.resolve_prediction(
                prediction_id,
                actual_status="complete" if success else "failed",
                actual_steps=2,
                evidence_ok=success,
                failure_class=None if success else "unknown",
            )

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.workspace = self.root / "workspace"
        self.data = self.root / "data"
        self.workspace.mkdir()
        self.data.mkdir()
        self.memory = Memory(self.data / "jarvis.db", worker_id="test-worker")

    def tearDown(self) -> None:
        self.memory.close()
        self.temporary.cleanup()

    def config(self, **changes):
        base = Config.load()
        return replace(
            base,
            workspace=self.workspace,
            data_dir=self.data,
            vault_dir=None,
            proactive_enabled=True,
            proactive_idle_seconds=5,
            proactive_daily_task_limit=2,
            proactive_max_task_seconds=60,
            daily_tool_limit=100,
            **changes,
        )

    def test_schema_control_goals_journal_preferences_and_self_model_persist(self) -> None:
        self.assertEqual(
            self.memory.db.execute("PRAGMA user_version").fetchone()[0],
            SCHEMA_VERSION,
        )
        goal_id = self.memory.add_goal(
            "Build durable assistant", "Continue across sessions", kind="project", priority=90
        )
        entry_id = self.memory.add_journal_entry(goal_id, "Initial scope accepted")
        preference_id = self.memory.set_preference("answer_style", "concise", source="user")
        self.memory.set_control_state("paused", "maintenance")

        snapshot = build_self_model(
            self.config(), self.memory, ["read_file", "write_file", "run_process"]
        )

        self.assertGreater(entry_id, 0)
        self.assertGreater(preference_id, 0)
        self.assertEqual(snapshot["current_status"]["control"]["state"], "paused")
        self.assertEqual(snapshot["current_status"]["goals"][0]["id"], goal_id)
        self.assertEqual(snapshot["current_status"]["preferences"][0]["value"], "concise")
        self.assertEqual(snapshot["available_tools"], ["read_file", "run_process", "write_file"])
        self.assertEqual(
            self.memory.db.execute("SELECT COUNT(*) FROM self_snapshots").fetchone()[0], 1
        )

    def test_meta_gate_is_strict_and_initiative_needs_recovery_and_three_families(self) -> None:
        from jarvis.self_diagnosis import runtime_manifest_sha256

        config = self.config(initiative="act")
        self.assertFalse(calibrated_meta_gate(self.memory, "code_fix")["allowed"])
        blocked = initiative_eligibility(config, self.memory)
        self.assertFalse(blocked["eligible"])
        self.assertTrue(blocked["tier0_enabled"])
        self.assertFalse(blocked["tier1_eligible"])
        self.assertEqual(blocked["effective_mode"], "observe")
        self.assertIn("no recovery attestation exists", blocked["reasons"])

        for family in ("code_fix", "file_ops", "conversation"):
            self._seed_calibrated_family(family)
        self.assertTrue(calibrated_meta_gate(self.memory, "code_fix")["allowed"])
        self.memory.record_recovery_attestation(
            runtime_sha256=runtime_manifest_sha256(),
            passed=True,
            evidence={"checks": {"restart": True}},
        )
        eligible = initiative_eligibility(config, self.memory)
        self.assertTrue(eligible["eligible"])
        self.assertEqual(len(eligible["calibrated_families"]), 3)

        project_id = self.memory.add_project("Approved project", "@projects/approved-project")
        source_task = self.memory.add_task("Fix parser boundaries", project_id=project_id)
        self.assertEqual(self.memory.claim_task("test-worker")["id"], source_task)
        prediction = self.memory.record_prediction(
            family="code_fix", profile="coding", model="m",
            predicted_success=0.8, predicted_steps=4,
            predicted_verification="process_evidence", task_id=source_task,
            origin="worker",
        )
        self.memory.resolve_prediction(
            prediction, actual_status="failed", actual_steps=2,
            evidence_ok=False, failure_class="verification_absent",
        )
        self.assertEqual(
            self.memory.fail_task(
                source_task, "verification absent", worker_id="test-worker", retry=False
            ),
            "failed",
        )
        self.assertIsNone(initiative_cycle(config, self.memory)["task_id"])

        domain_id = self.memory.approve_work_domain(
            "Parser maintenance", kind="workspace_project",
            project_id=project_id, max_tasks_per_day=1,
        )
        cycle = initiative_cycle(config, self.memory)
        self.assertIsNotNone(cycle["task_id"])
        queued = next(
            item for item in self.memory.list_tasks(100)
            if item["id"] == cycle["task_id"]
        )
        self.assertEqual(queued["project_id"], project_id)
        self.assertEqual(queued["specialist_key"], "coding")
        self.assertEqual(queued["delegated_by"], "jarvis")
        self.assertEqual(queued["requested_model"], "coding")
        self.assertEqual(self.memory.prediction_origin_for_task(queued["id"]), "proactive")
        event = self.memory.list_initiative_events()[0]
        self.assertEqual(event["domain_id"], domain_id)
        self.assertEqual(event["tier"], 1)
        self.assertEqual(event["status"], "queued")

        claimed_recovery = self.memory.claim_task("initiative-worker")
        self.assertEqual(claimed_recovery["id"], queued["id"])
        self.assertEqual(self.memory.list_initiative_events()[0]["status"], "running")
        self.assertTrue(
            self.memory.finish_task(
                queued["id"], "Parser recovery verified.", worker_id="initiative-worker"
            )
        )
        self.memory.record_reflection(
            status="complete",
            summary="Parser recovery verified.",
            task_id=queued["id"],
        )
        completed_event = self.memory.list_initiative_events()[0]
        self.assertEqual(completed_event["status"], "done")
        self.assertEqual(completed_event["result_summary"], "Parser recovery verified.")
        coding = self.memory.get_specialist_agent("coding")
        self.assertEqual(coding["status"], "ready")
        self.assertEqual(coding["completed_tasks"], 1)

    def test_observe_tier_records_drift_while_action_gate_is_closed(self) -> None:
        config = self.config(initiative="act")
        workspace_before = list(self.workspace.iterdir())
        for index in range(40):
            recent = index >= 10
            prediction = self.memory.record_prediction(
                family="code_fix",
                profile="coding",
                model="m",
                predicted_success=0.8,
                predicted_steps=2,
                predicted_verification="process_evidence",
            )
            self.memory.resolve_prediction(
                prediction,
                actual_status="failed" if recent else "complete",
                actual_steps=5 if recent else 2,
                evidence_ok=not recent,
                failure_class="verification_absent" if recent else None,
            )

        eligibility = initiative_eligibility(config, self.memory)
        self.assertFalse(eligibility["tier1_eligible"])
        self.assertEqual(eligibility["effective_mode"], "observe")
        cycle = initiative_cycle(config, self.memory)

        self.assertIsNone(cycle["task_id"])
        self.assertGreaterEqual(cycle["observations_created"], 1)
        events = self.memory.list_initiative_events()
        self.assertTrue(
            any(event["signal_kind"] == "behavioral_drift" for event in events)
        )
        self.assertEqual(list(self.workspace.iterdir()), workspace_before)

    def test_self_model_represents_operational_existence_without_false_sentience(self) -> None:
        snapshot = build_self_model(
            self.config(), self.memory, ["read_file", "system_snapshot"], persist=False
        )
        identity = snapshot["identity"]
        self.assertEqual(identity["name"], "JARVIS")
        self.assertEqual(identity["kind"], "local AI software agent")
        self.assertEqual(identity["awareness"], "operational machine self-model")
        self.assertIn("current executing process", identity["existence_basis"])
        self.assertIn("not established", identity["consciousness"])
        self.assertTrue(
            any(
                "subjective consciousness" in item
                for item in snapshot["limitations"]["structural"]
            )
        )
        self.assertEqual(snapshot["capabilities"]["demonstrated"], [])
        self.assertEqual(snapshot["capabilities"]["developing"], [])
        self.assertEqual(
            len(snapshot["capabilities"]["unknown"]),
            len(self.memory.PREDICTION_FAMILIES),
        )

        self.assertEqual(self_context(self.memory), "")
        self.memory.add_goal("Understand current operating state")
        context = json.loads(self_context(self.memory))
        self.assertEqual(context["identity"]["name"], "JARVIS")
        self.assertEqual(
            context["identity"]["awareness"], "operational machine self-model"
        )
        self.assertIn("task_counts", context)
        self.assertEqual(context["memory_count"], 0)

        contract = runtime_identity_contract()
        self.assertIn("exists operationally as the current process", contract)
        self.assertIn("not proof of consciousness", contract)
        self.assertIn("survival drive, or an agenda", contract)

    def test_self_model_reports_measured_competence_failures_and_calibration(self) -> None:
        for index in range(COMPETENCE_MIN_ATTEMPTS):
            prediction_id = self.memory.record_prediction(
                family="code_fix",
                profile="coding",
                model="test-model",
                predicted_success=0.8,
                predicted_steps=4,
                predicted_verification="process_evidence",
            )
            complete = index < 8
            self.memory.resolve_prediction(
                prediction_id,
                actual_status="complete" if complete else "failed",
                actual_steps=3,
                evidence_ok=complete,
                failure_class=None if complete else "wrong_target_file",
            )

        snapshot = build_self_model(
            self.config(), self.memory, ["read_file"], persist=False
        )
        demonstrated = snapshot["capabilities"]["demonstrated"]
        self.assertEqual([item["family"] for item in demonstrated], ["code_fix"])
        self.assertEqual(demonstrated[0]["attempts"], 10)
        self.assertAlmostEqual(demonstrated[0]["success_rate"], 0.8)
        self.assertEqual(
            demonstrated[0]["top_failure"],
            {"class": "wrong_target_file", "count": 2},
        )
        self.assertEqual(
            snapshot["limitations"]["measured"][0]["family"], "code_fix"
        )
        self.assertAlmostEqual(snapshot["calibration"]["overall_brier"], 0.16)
        code_calibration = next(
            item for item in snapshot["calibration"]["by_family"]
            if item["family"] == "code_fix"
        )
        self.assertTrue(code_calibration["calibrated"])

        predicted, basis = competence_prediction(self.memory, "code_fix", 0.65)
        self.assertAlmostEqual(predicted, 0.8)
        self.assertEqual(basis, "competence")
        cold_prediction, cold_basis = competence_prediction(
            self.memory, "file_ops", 0.85
        )
        self.assertEqual((cold_prediction, cold_basis), (0.85, "prior"))

        context = json.loads(self_context(self.memory, "code_fix"))
        digest = context["current_task_competence"]
        self.assertEqual(digest["family"], "code_fix")
        self.assertEqual(digest["bucket"], "demonstrated")
        self.assertEqual(digest["top_failure"]["class"], "wrong_target_file")
        self.assertLessEqual(len(self_context(self.memory, "code_fix")), 8000)

    def test_idle_scheduler_requires_approved_subject_and_no_active_work(self) -> None:
        goal_id = self.memory.add_goal("Explore agent reliability", kind="project")
        subject_id = self.memory.approve_subject("local AI agent reliability")
        backlog_id = self.memory.add_backlog_item(
            "prototype", subject_id, "Use only the standard library",
            priority=80, interval_hours=24, goal_id=goal_id,
        )

        task_id = self.memory.schedule_idle_activity(daily_limit=2)
        self.assertIsNotNone(task_id)
        task = self.memory.list_tasks()[0]
        self.assertEqual(task["backlog_id"], backlog_id)
        self.assertEqual(task["goal_id"], goal_id)
        self.assertIn("Build and test a small reversible prototype", task["prompt"])
        self.assertIsNone(self.memory.schedule_idle_activity(daily_limit=2))

        claimed = self.memory.claim_task(worker_id="test-worker")
        self.assertIsNotNone(claimed)
        self.assertTrue(self.memory.finish_task(task_id, "Prototype tests passed", worker_id="test-worker"))
        reflection_id = self.memory.record_reflection(
            task_id=task_id,
            status="complete",
            summary="Prototype completed and tests passed.",
            improvements="Reuse the verified prototype harness on similar tasks.",
            tool_calls=5,
        )
        self.assertGreater(reflection_id, 0)
        self.assertEqual(self.memory.list_journal(goal_id)[0]["kind"], "reflection")
        run = self.memory.db.execute(
            "SELECT status FROM proactive_runs WHERE task_id=?", (task_id,)
        ).fetchone()
        self.assertEqual(run["status"], "done")

    def test_idle_scheduler_routes_specialized_subjects_instead_of_hardcoding_agents(self) -> None:
        cases = (
            ("research", "defensive cybersecurity firewall hardening", "cybersecurity"),
            ("ideas", "OSPF and BGP route convergence", "network"),
            ("prototype", "defensive cybersecurity firewall hardening", "cybersecurity"),
            ("research", "local AI inference performance", "research"),
        )
        for kind, subject, expected_specialist in cases:
            with self.subTest(kind=kind, subject=subject):
                with Memory(Path(":memory:")) as memory:
                    subject_id = memory.approve_subject(subject)
                    memory.add_backlog_item(kind, subject_id)
                    task_id = memory.schedule_idle_activity(daily_limit=2)
                    self.assertIsNotNone(task_id)
                    queued = memory.list_tasks()[0]
                    self.assertEqual(queued["specialist_key"], expected_specialist)

    def test_pause_and_emergency_stop_block_background_guard_and_scheduler(self) -> None:
        subject_id = self.memory.approve_subject("approved subject")
        self.memory.add_backlog_item("ideas", subject_id)
        config = self.config()

        self.memory.set_control_state("paused")
        self.assertIsNone(self.memory.schedule_idle_activity(daily_limit=2))
        self.assertTrue(RuntimeGuard(self.memory, config, background=True)())
        self.assertFalse(RuntimeGuard(self.memory, config, background=False)())

        self.memory.set_control_state("stopped", "operator emergency stop")
        foreground = RuntimeGuard(self.memory, config, background=False)
        self.assertTrue(foreground())
        self.assertIn("emergency stop", foreground.reason)

        self.memory.set_control_state("running")
        self.assertFalse(RuntimeGuard(self.memory, config, background=True)())

    def test_background_daily_tool_limit_never_cancels_foreground_chat(self) -> None:
        config = replace(self.config(), daily_tool_limit=10)
        # A large foreground session must not consume the autonomous-worker
        # allowance.
        for index in range(25):
            self.memory.log_activity(
                "tool", f"foreground-{index}", "complete"
            )
        self.assertFalse(RuntimeGuard(self.memory, config, background=True)())

        for index in range(10):
            self.memory.log_activity(
                "tool", f"background-{index}", "complete", task_id=100 + index
            )

        background = RuntimeGuard(self.memory, config, background=True)
        foreground = RuntimeGuard(self.memory, config, background=False)

        self.assertTrue(background())
        self.assertIn("background daily tool budget", background.reason)
        self.assertFalse(foreground())
        self.assertIsNone(foreground.reason)

    def test_runtime_guard_is_safe_for_provider_cancellation_thread(self) -> None:
        guard = RuntimeGuard(self.memory, self.config(), background=False)
        first_read = threading.Event()
        read_again = threading.Event()
        results: list[bool] = []
        errors: list[BaseException] = []

        def probe() -> None:
            try:
                results.append(guard())
                first_read.set()
                self.assertTrue(read_again.wait(2.0))
                results.append(guard())
            except BaseException as exc:
                errors.append(exc)
                first_read.set()

        worker = threading.Thread(target=probe)
        worker.start()
        self.assertTrue(first_read.wait(2.0))
        self.memory.set_control_state("stopped", "test stop")
        read_again.set()
        worker.join(timeout=2.0)

        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results, [False, True])

    def test_approvals_are_exact_one_shot_and_audited(self) -> None:
        allowed, request_id = self.memory.authorize_or_request(
            "publish_external", '{"tool":"github_push","branch":"main"}',
            "Publishes commits to an external service.",
            approval_scope="foreground",
        )
        self.assertFalse(allowed)
        self.assertTrue(self.memory.decide_approval(request_id, True, ttl_hours=2))

        allowed, consumed_id = self.memory.authorize_or_request(
            "publish_external", '{"tool":"github_push","branch":"main"}',
            "Publishes commits to an external service.",
            approval_scope="foreground",
        )
        self.assertTrue(allowed)
        self.assertEqual(consumed_id, request_id)

        allowed, next_id = self.memory.authorize_or_request(
            "publish_external", '{"tool":"github_push","branch":"main"}',
            "Publishes commits to an external service.",
            approval_scope="foreground",
        )
        self.assertFalse(allowed)
        self.assertNotEqual(next_id, request_id)
        statuses = {item["id"]: item["status"] for item in self.memory.list_approvals()}
        self.assertEqual(statuses[request_id], "consumed")
        self.assertEqual(statuses[next_id], "pending")

    def test_approval_is_bound_to_requesting_task_and_exact_resource(self) -> None:
        _, task_a_request = self.memory.authorize_or_request(
            "publish_external",
            '{"tool":"github_push","branch":"main"}',
            "Publishes commits.",
            approval_scope="task:11",
            task_id=11,
        )
        self.assertTrue(self.memory.decide_approval(task_a_request, True, ttl_hours=2))

        task_b_allowed, task_b_request = self.memory.authorize_or_request(
            "publish_external",
            '{"tool":"github_push","branch":"main"}',
            "Publishes commits.",
            approval_scope="task:22",
            task_id=22,
        )
        self.assertFalse(task_b_allowed)
        self.assertNotEqual(task_b_request, task_a_request)

        wrong_resource_allowed, wrong_resource_request = self.memory.authorize_or_request(
            "publish_external",
            '{"tool":"github_push","branch":"release"}',
            "Publishes commits.",
            approval_scope="task:11",
            task_id=11,
        )
        self.assertFalse(wrong_resource_allowed)
        self.assertNotEqual(wrong_resource_request, task_a_request)

        task_a_allowed, consumed_id = self.memory.authorize_or_request(
            "publish_external",
            '{"tool":"github_push","branch":"main"}',
            "Publishes commits.",
            approval_scope="task:11",
            task_id=11,
        )
        self.assertTrue(task_a_allowed)
        self.assertEqual(consumed_id, task_a_request)

        rows = {item["id"]: item for item in self.memory.list_approvals()}
        self.assertEqual(rows[task_a_request]["status"], "consumed")
        self.assertEqual(rows[task_a_request]["scope"], "task:11")
        self.assertEqual(rows[task_b_request]["scope"], "task:22")

    def test_approval_lookup_null_safely_matches_persisted_task_id(self) -> None:
        _, request_id = self.memory.authorize_or_request(
            "publish_external",
            '{"tool":"github_push","branch":"main"}',
            "Publishes commits.",
            approval_scope="task:11",
            task_id=11,
        )
        self.assertTrue(self.memory.decide_approval(request_id, True, ttl_hours=2))
        self.memory.db.execute(
            "UPDATE approvals SET task_id=22 WHERE id=?",
            (request_id,),
        )

        allowed, next_id = self.memory.authorize_or_request(
            "publish_external",
            '{"tool":"github_push","branch":"main"}',
            "Publishes commits.",
            approval_scope="task:11",
            task_id=11,
        )
        self.assertFalse(allowed)
        self.assertNotEqual(next_id, request_id)
        rows = {item["id"]: item for item in self.memory.list_approvals()}
        self.assertEqual(rows[request_id]["status"], "approved")
        self.assertEqual(rows[next_id]["task_id"], 11)

    def test_background_task_parks_and_resumes_on_its_exact_approval(self) -> None:
        task_id = self.memory.add_task("Push branch main", max_attempts=3)
        claimed = self.memory.claim_task("approval-worker")
        self.assertEqual(claimed["id"], task_id)
        _, approval_id = self.memory.authorize_or_request(
            "publish_external",
            "resource-a",
            "Publishes externally.",
            approval_scope=f"task:{task_id}",
            task_id=task_id,
        )

        self.assertEqual(
            self.memory.await_task_approval(
                task_id, approval_id, worker_id="approval-worker"
            ),
            "awaiting_approval",
        )
        waiting = next(item for item in self.memory.list_tasks() if item["id"] == task_id)
        self.assertEqual(waiting["status"], "awaiting_approval")
        self.assertEqual(waiting["awaiting_approval_id"], approval_id)
        self.assertEqual(waiting["attempt_count"], 0)

        self.assertTrue(self.memory.decide_approval(approval_id, True, ttl_hours=2))
        queued = next(item for item in self.memory.list_tasks() if item["id"] == task_id)
        self.assertEqual(queued["status"], "queued")
        self.assertIsNone(queued["awaiting_approval_id"])
        reclaimed = self.memory.claim_task("approval-worker")
        self.assertEqual(reclaimed["id"], task_id)
        allowed, consumed_id = self.memory.authorize_or_request(
            "publish_external",
            "resource-a",
            "Publishes externally.",
            approval_scope=f"task:{task_id}",
            task_id=task_id,
        )
        self.assertTrue(allowed)
        self.assertEqual(consumed_id, approval_id)
        self.assertTrue(
            self.memory.finish_task(task_id, "pushed once", worker_id="approval-worker")
        )

    def test_denied_approval_closes_linked_proactive_and_goal_bookkeeping(self) -> None:
        goal_id = self.memory.add_goal("Publish verified prototype", kind="project")
        subject_id = self.memory.approve_subject("approved deployment prototype")
        backlog_id = self.memory.add_backlog_item(
            "prototype",
            subject_id,
            "Publish only after explicit approval",
            goal_id=goal_id,
        )
        task_id = self.memory.schedule_idle_activity(daily_limit=2)
        self.assertIsNotNone(task_id)
        claimed = self.memory.claim_task("approval-worker")
        self.assertEqual(claimed["id"], task_id)
        self.assertEqual(claimed["backlog_id"], backlog_id)
        self.assertEqual(claimed["goal_id"], goal_id)

        _, approval_id = self.memory.authorize_or_request(
            "publish_external",
            "prototype-resource",
            "Publishes externally.",
            approval_scope=f"task:{task_id}",
            task_id=task_id,
        )
        self.assertEqual(
            self.memory.await_task_approval(
                task_id, approval_id, worker_id="approval-worker"
            ),
            "awaiting_approval",
        )

        self.assertTrue(self.memory.decide_approval(approval_id, False))
        denial = f"Approval #{approval_id} was denied"
        task = next(item for item in self.memory.list_tasks() if item["id"] == task_id)
        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["result"], denial)
        self.assertIsNone(task["awaiting_approval_id"])

        run = self.memory.db.execute(
            """SELECT backlog_id, status, completed_at, result_summary
               FROM proactive_runs WHERE task_id=?""",
            (task_id,),
        ).fetchone()
        self.assertEqual(run["backlog_id"], backlog_id)
        self.assertEqual(run["status"], "failed")
        self.assertIsNotNone(run["completed_at"])
        self.assertEqual(run["result_summary"], denial)

        reflections = [
            item for item in self.memory.list_reflections()
            if item["task_id"] == task_id
        ]
        self.assertEqual(len(reflections), 1)
        self.assertEqual(reflections[0]["status"], "failed")
        self.assertEqual(reflections[0]["summary"], denial)

        journal = self.memory.list_journal(goal_id)
        self.assertEqual(len(journal), 1)
        self.assertEqual(journal[0]["task_id"], task_id)
        self.assertEqual(journal[0]["kind"], "reflection")
        self.assertIn(denial, journal[0]["content"])
        self.assertEqual(
            self.memory.await_task_approval(
                task_id, approval_id, worker_id="approval-worker"
            ),
            "failed",
        )
        self.assertEqual(
            len([
                item for item in self.memory.list_reflections()
                if item["task_id"] == task_id
            ]),
            1,
        )

    def test_denied_approval_terminalizes_task_with_orphan_goal(self) -> None:
        task_id = self.memory.add_task(
            "Publish with a migrated orphan goal",
            goal_id=999,
        )
        self.assertEqual(self.memory.claim_task("approval-worker")["id"], task_id)
        _, approval_id = self.memory.authorize_or_request(
            "publish_external",
            "orphan-goal-resource",
            "Publishes externally.",
            approval_scope=f"task:{task_id}",
            task_id=task_id,
        )
        self.assertEqual(
            self.memory.await_task_approval(
                task_id, approval_id, worker_id="approval-worker"
            ),
            "awaiting_approval",
        )

        self.assertTrue(self.memory.decide_approval(approval_id, False))
        task = next(item for item in self.memory.list_tasks() if item["id"] == task_id)
        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["result"], f"Approval #{approval_id} was denied")
        approval = next(
            item for item in self.memory.list_approvals()
            if item["id"] == approval_id
        )
        self.assertEqual(approval["status"], "denied")
        reflections = [
            item for item in self.memory.list_reflections()
            if item["task_id"] == task_id
        ]
        self.assertEqual(len(reflections), 1)
        self.assertEqual(
            self.memory.db.execute(
                "SELECT COUNT(*) FROM journal_entries WHERE task_id=?", (task_id,)
            ).fetchone()[0],
            0,
        )

    def test_approval_decided_before_task_parks_is_race_safe(self) -> None:
        task_id = self.memory.add_task("Deploy preview", max_attempts=3)
        self.assertEqual(self.memory.claim_task("approval-worker")["id"], task_id)
        _, approval_id = self.memory.authorize_or_request(
            "publish_external",
            "preview-resource",
            "Deploys preview.",
            approval_scope=f"task:{task_id}",
            task_id=task_id,
        )
        self.assertTrue(self.memory.decide_approval(approval_id, True, ttl_hours=2))

        self.assertEqual(
            self.memory.await_task_approval(
                task_id, approval_id, worker_id="approval-worker"
            ),
            "queued",
        )
        task = next(item for item in self.memory.list_tasks() if item["id"] == task_id)
        self.assertEqual(task["status"], "queued")
        self.assertEqual(task["attempt_count"], 0)
        self.assertIsNone(task["awaiting_approval_id"])

    def test_denial_decided_before_task_parks_closes_bookkeeping_once(self) -> None:
        goal_id = self.memory.add_goal("Review release", kind="project")
        subject_id = self.memory.approve_subject("approved release review")
        backlog_id = self.memory.add_backlog_item(
            "prototype", subject_id, goal_id=goal_id
        )
        task_id = self.memory.schedule_idle_activity(daily_limit=2)
        self.assertEqual(self.memory.claim_task("approval-worker")["id"], task_id)
        _, approval_id = self.memory.authorize_or_request(
            "publish_external",
            "race-denial-resource",
            "Publishes externally.",
            approval_scope=f"task:{task_id}",
            task_id=task_id,
        )
        linked = next(item for item in self.memory.list_tasks() if item["id"] == task_id)
        self.assertEqual(linked["awaiting_approval_id"], approval_id)

        self.assertTrue(self.memory.decide_approval(approval_id, False))
        before_park = next(
            item for item in self.memory.list_tasks() if item["id"] == task_id
        )
        self.assertEqual(before_park["status"], "failed")
        self.assertEqual(
            self.memory.db.execute(
                "SELECT status FROM proactive_runs WHERE task_id=?", (task_id,)
            ).fetchone()["status"],
            "failed",
        )

        self.assertEqual(
            self.memory.await_task_approval(
                task_id, approval_id, worker_id="approval-worker"
            ),
            "failed",
        )
        denial = f"Approval #{approval_id} was denied"
        task = next(item for item in self.memory.list_tasks() if item["id"] == task_id)
        self.assertEqual(task["status"], "failed")
        run = self.memory.db.execute(
            """SELECT backlog_id, status, completed_at, result_summary
               FROM proactive_runs WHERE task_id=?""",
            (task_id,),
        ).fetchone()
        self.assertEqual(run["backlog_id"], backlog_id)
        self.assertEqual(run["status"], "failed")
        self.assertIsNotNone(run["completed_at"])
        self.assertEqual(run["result_summary"], denial)
        reflections = [
            item for item in self.memory.list_reflections()
            if item["task_id"] == task_id
        ]
        self.assertEqual(len(reflections), 1)
        self.assertEqual(reflections[0]["summary"], denial)
        journal = self.memory.list_journal(goal_id)
        self.assertEqual(len(journal), 1)
        self.assertEqual(journal[0]["task_id"], task_id)

        self.assertEqual(
            self.memory.await_task_approval(
                task_id, approval_id, worker_id="approval-worker"
            ),
            "failed",
        )
        self.assertEqual(
            len([
                item for item in self.memory.list_reflections()
                if item["task_id"] == task_id
            ]),
            1,
        )

    def test_crashed_task_recovery_preserves_pending_link_for_later_denial(self) -> None:
        base = datetime.now(timezone.utc) + timedelta(seconds=1)
        task_id = self.memory.add_task("Publish after worker recovery", max_attempts=3)
        self.assertEqual(
            self.memory.claim_task(
                "approval-worker", lease_seconds=5, now=base
            )["id"],
            task_id,
        )
        _, approval_id = self.memory.authorize_or_request(
            "publish_external",
            "crash-recovery-resource",
            "Publishes externally.",
            approval_scope=f"task:{task_id}",
            task_id=task_id,
        )
        linked = next(item for item in self.memory.list_tasks() if item["id"] == task_id)
        self.assertEqual(linked["awaiting_approval_id"], approval_id)
        self.memory.db.execute(
            "UPDATE tasks SET awaiting_approval_id=NULL WHERE id=?", (task_id,)
        )
        allowed, returned_id = self.memory.authorize_or_request(
            "publish_external",
            "crash-recovery-resource",
            "Publishes externally.",
            approval_scope=f"task:{task_id}",
            task_id=task_id,
        )
        self.assertFalse(allowed)
        self.assertEqual(returned_id, approval_id)
        relinked = next(item for item in self.memory.list_tasks() if item["id"] == task_id)
        self.assertEqual(relinked["awaiting_approval_id"], approval_id)

        self.assertEqual(
            self.memory.recover_stale_tasks(now=base + timedelta(seconds=6)),
            {"requeued": 1, "failed": 0},
        )
        recovered = next(item for item in self.memory.list_tasks() if item["id"] == task_id)
        self.assertEqual(recovered["status"], "queued")
        self.assertEqual(recovered["awaiting_approval_id"], approval_id)

        self.assertTrue(self.memory.decide_approval(approval_id, False))
        denied = next(item for item in self.memory.list_tasks() if item["id"] == task_id)
        self.assertEqual(denied["status"], "failed")
        self.assertEqual(denied["result"], f"Approval #{approval_id} was denied")
        self.assertEqual(len([
            item for item in self.memory.list_reflections()
            if item["task_id"] == task_id
        ]), 1)

    def test_denial_before_crash_recovery_closes_running_task_once(self) -> None:
        base = datetime.now(timezone.utc) + timedelta(seconds=1)
        task_id = self.memory.add_task("Publish before worker recovery", max_attempts=3)
        self.assertEqual(
            self.memory.claim_task(
                "approval-worker", lease_seconds=5, now=base
            )["id"],
            task_id,
        )
        _, approval_id = self.memory.authorize_or_request(
            "publish_external",
            "crash-denial-resource",
            "Publishes externally.",
            approval_scope=f"task:{task_id}",
            task_id=task_id,
        )

        self.assertTrue(self.memory.decide_approval(approval_id, False))
        self.assertEqual(
            self.memory.recover_stale_tasks(now=base + timedelta(seconds=6)),
            {"requeued": 0, "failed": 0},
        )
        self.assertEqual(
            self.memory.await_task_approval(
                task_id, approval_id, worker_id="approval-worker"
            ),
            "failed",
        )
        self.assertEqual(len([
            item for item in self.memory.list_reflections()
            if item["task_id"] == task_id
        ]), 1)

    def test_deciding_stale_approval_does_not_wake_task_waiting_on_another(self) -> None:
        task_id = self.memory.add_task("Two sensitive resources", max_attempts=3)
        self.assertEqual(self.memory.claim_task("approval-worker")["id"], task_id)
        _, stale_id = self.memory.authorize_or_request(
            "publish_external",
            "stale-resource",
            "Stale request.",
            approval_scope=f"task:{task_id}",
            task_id=task_id,
        )
        _, current_id = self.memory.authorize_or_request(
            "publish_external",
            "current-resource",
            "Current request.",
            approval_scope=f"task:{task_id}",
            task_id=task_id,
        )
        linked = next(item for item in self.memory.list_tasks() if item["id"] == task_id)
        self.assertEqual(linked["awaiting_approval_id"], current_id)
        self.assertIsNone(
            self.memory.await_task_approval(
                task_id, stale_id, worker_id="approval-worker"
            )
        )

        self.assertTrue(self.memory.decide_approval(stale_id, False))
        still_running = next(
            item for item in self.memory.list_tasks() if item["id"] == task_id
        )
        self.assertEqual(still_running["status"], "running")
        self.assertEqual(still_running["awaiting_approval_id"], current_id)
        self.assertEqual(
            self.memory.await_task_approval(
                task_id, current_id, worker_id="approval-worker"
            ),
            "awaiting_approval",
        )

        still_waiting = next(
            item for item in self.memory.list_tasks() if item["id"] == task_id
        )
        self.assertEqual(still_waiting["status"], "awaiting_approval")
        self.assertEqual(still_waiting["awaiting_approval_id"], current_id)
        self.assertTrue(self.memory.decide_approval(current_id, True, ttl_hours=2))
        resumed = next(item for item in self.memory.list_tasks() if item["id"] == task_id)
        self.assertEqual(resumed["status"], "queued")

    def test_one_shot_approval_is_atomic_across_connections(self) -> None:
        _, approval_id = self.memory.authorize_or_request(
            "publish_external",
            "race-resource",
            "Race test.",
            approval_scope="conversation:77",
        )
        self.assertTrue(self.memory.decide_approval(approval_id, True, ttl_hours=2))
        barrier = threading.Barrier(7)
        results: list[tuple[bool, int]] = []
        effects: list[str] = []
        errors: list[BaseException] = []

        def contender(label: str) -> None:
            try:
                with Memory(self.data / "jarvis.db") as connection:
                    barrier.wait(timeout=5)
                    result = connection.authorize_or_request(
                        "publish_external",
                        "race-resource",
                        "Race test.",
                        approval_scope="conversation:77",
                    )
                    results.append(result)
                    if result[0]:
                        effects.append(label)
            except BaseException as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=contender, args=(label,))
            for label in ("a", "b", "c", "d", "e", "f")
        ]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=5)
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(errors, [])
        self.assertEqual(
            sorted(allowed for allowed, _ in results),
            [False, False, False, False, False, True],
        )
        self.assertEqual(len(effects), 1)
        consumed = next(item for item in results if item[0])
        self.assertEqual(consumed[1], approval_id)
        pending_ids = {request_id for allowed, request_id in results if not allowed}
        self.assertEqual(len(pending_ids), 1)
        self.assertNotIn(approval_id, pending_ids)

    def test_migration_expires_live_legacy_approvals_without_scope(self) -> None:
        legacy_path = self.data / "legacy-approvals.db"
        connection = sqlite3.connect(legacy_path)
        connection.row_factory = sqlite3.Row
        # Start from a complete historical v5 database.  A hand-built database
        # containing only the two tables this test reads is not a valid v5
        # schema and must now be rejected by the v30 provenance migration.
        bootstrap = object.__new__(Memory)
        bootstrap.db = connection
        bootstrap._closed = False
        for migration in (
            bootstrap._migrate_v1,
            bootstrap._migrate_v2,
            bootstrap._migrate_v3,
            bootstrap._migrate_v4,
            bootstrap._migrate_v5,
        ):
            migration()
        connection.execute("DROP TABLE approvals")
        connection.execute(
            """CREATE TABLE approvals (
                id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                fingerprint TEXT NOT NULL, action TEXT NOT NULL, resource TEXT NOT NULL,
                reason TEXT NOT NULL, status TEXT NOT NULL,
                expires_at TEXT, decided_at TEXT, task_id INTEGER
            )"""
        )
        connection.execute(
            "CREATE INDEX idx_approvals_fingerprint "
            "ON approvals(fingerprint, status, id)"
        )
        connection.execute(
            """INSERT INTO approvals(
                   created_at, updated_at, fingerprint, action, resource, reason,
                   status, expires_at, task_id
               ) VALUES ('now', 'now', 'old', 'publish_external', 'resource',
                         'reason', 'approved', '2999-01-01', NULL)"""
        )
        connection.execute("PRAGMA user_version=5")
        connection.commit()
        connection.close()
        bootstrap._closed = True

        with Memory(legacy_path) as migrated:
            row = migrated.list_approvals()[0]
            self.assertEqual(row["status"], "expired")
            self.assertEqual(row["scope"], "legacy")
            self.assertEqual(
                migrated.db.execute("PRAGMA user_version").fetchone()[0],
                SCHEMA_VERSION,
            )

    def test_expired_approval_is_not_consumed(self) -> None:
        _, request_id = self.memory.authorize_or_request(
            "publish_external",
            "resource-a",
            "Publishes externally.",
            approval_scope="conversation:9",
        )
        self.assertTrue(self.memory.decide_approval(request_id, True, ttl_hours=2))
        self.memory.db.execute(
            "UPDATE approvals SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
            (request_id,),
        )

        allowed, next_id = self.memory.authorize_or_request(
            "publish_external",
            "resource-a",
            "Publishes externally.",
            approval_scope="conversation:9",
        )
        self.assertFalse(allowed)
        self.assertNotEqual(next_id, request_id)
        rows = {item["id"]: item for item in self.memory.list_approvals()}
        self.assertEqual(rows[request_id]["status"], "expired")
        self.assertEqual(rows[next_id]["status"], "pending")


if __name__ == "__main__":
    unittest.main()
