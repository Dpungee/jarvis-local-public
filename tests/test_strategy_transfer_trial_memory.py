from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from jarvis.memory import Memory, SCHEMA_VERSION
from jarvis.strategy_transfer import (
    select_strategy_transfer,
    strategy_target_from_runtime,
)
from jarvis.strategy_transfer_trial import (
    StrategyTransferTrialError,
    arm_for_slot,
    strategy_transfer_runtime_sha256,
)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class StrategyTransferTrialMemoryTests(unittest.TestCase):
    EVIDENCE = {
        "schema": "jarvis.strategy-evidence.v1",
        "inspect_before_change": True,
        "checkpoint_and_resume": False,
        "verify_output": True,
        "compare_authoritative_sources": False,
    }

    def _install_benchmark(self, memory: Memory) -> dict:
        artifact = memory.build_strategy_transfer_benchmark_attestation(
            run_id="phase4b-memory-test"
        )
        self.assertTrue(memory.record_strategy_transfer_attestation(
            "sealed_benchmark", artifact,
            evaluator_version=artifact["evaluator_version"],
            evaluator_sha256=artifact["evaluator_sha256"],
            config_sha256=artifact["config_sha256"],
        ))
        return memory.strategy_transfer_trial_pins()

    def _source(self, memory: Memory) -> None:
        self._source_family(memory, "code_fix", "source")

    def _source_family(
        self,
        memory: Memory,
        family: str,
        label: str,
    ) -> None:
        for index in range(20):
            conversation = memory.new_conversation(f"{label} {index}")
            prediction = memory.record_prediction(
                family=family, profile="trial-test", model="test",
                predicted_success=0.9, predicted_steps=1,
                predicted_verification="tool_success", basis="prior",
                origin="interactive", conversation_id=conversation,
            )
            memory.resolve_prediction(
                prediction, actual_status="complete", actual_steps=1,
                evidence_ok=True,
            )
            if index == 0:
                memory.record_strategy_observations(prediction, self.EVIDENCE)
                reflection = memory.record_reflection(
                    status="complete", summary="Verified trial source.",
                    improvements=("Inspect before changing and verify output.",),
                    conversation_id=conversation, prediction_id=prediction,
                    tool_calls=1,
                )
                self.assertIsNotNone(memory.db.execute(
                    "SELECT id FROM memories WHERE reflection_id=?",
                    (reflection,),
                ).fetchone())

    def _complete_trial_target(
        self,
        memory: Memory,
        manifest_id: int,
        *,
        control_success: bool,
    ) -> dict:
        prediction, assignment, _selection = self._prepare_trial_target(
            memory, manifest_id
        )
        memory.record_strategy_transfer_trial_provider_dispatch(prediction)
        successful = assignment["arm"] == "treatment" or control_success
        memory.resolve_prediction(
            prediction,
            actual_status="complete" if successful else "failed",
            actual_steps=2,
            evidence_ok=True if successful else False,
        )
        return assignment

    def _prepare_trial_target(
        self,
        memory: Memory,
        manifest_id: int,
    ) -> tuple[int, dict, dict]:
        prediction, selection = self._target_and_selection(memory)
        assignment = memory.assign_strategy_transfer_trial(
            prediction,
            "code_test",
            selection,
            manifest_id=manifest_id,
            current_runtime_sha256=strategy_transfer_runtime_sha256(),
        )
        base = _digest(f"base prompt {prediction}")
        final = (
            _digest(f"advice prompt {prediction}")
            if assignment["apply_advice"] else base
        )
        memory.record_strategy_transfer_trial_prompt_receipt(
            prediction,
            base_prompt_sha256=base,
            final_prompt_sha256=final,
            advice_applied=assignment["apply_advice"],
        )
        memory.record_strategy_transfer_applications(
            prediction,
            "code_test",
            selection,
            mode="trial",
            applied=assignment["apply_advice"],
        )
        return prediction, assignment, selection

    def _target_and_selection(self, memory: Memory):
        conversation = memory.new_conversation("trial target")
        prediction = memory.record_prediction(
            family="code_test", profile="trial-test", model="test",
            predicted_success=0.7, predicted_steps=1,
            predicted_verification="tool_success", basis="prior",
            origin="interactive", conversation_id=conversation,
        )
        target = strategy_target_from_runtime(
            task_id=f"prediction:{prediction}", family="code_test",
            changes_existing_state=True, resumable=False,
            verification="tool_success", current_external_facts=False,
        )
        as_of = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        candidates = memory.strategy_transfer_candidates(
            target_family="code_test", project_id=1, as_of=as_of,
        )
        selection = select_strategy_transfer(target, candidates, as_of=as_of)
        return prediction, selection.to_payload()

    def _manifest(self, memory: Memory, pins: dict, *, seed: str = "a" * 64):
        return memory.create_strategy_transfer_trial_manifest(
            project_id=1, target_families=["code_test"],
            strategies=["inspect_before_change", "verify_output"],
            sample_cap=40,
            expires_at=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
            seed=seed, operator_confirmed=True, **pins,
        )

    def test_v39_migration_and_deterministic_balanced_blocks(self) -> None:
        self.assertEqual(SCHEMA_VERSION, 39)
        with Memory(Path(":memory:")) as memory:
            self.assertEqual(memory.db.execute("PRAGMA user_version").fetchone()[0], 39)
            for table in (
                "strategy_transfer_trial_manifests",
                "strategy_transfer_trial_assignments",
            ):
                self.assertIsNotNone(memory.db.execute(
                    "SELECT name FROM sqlite_master WHERE name=?", (table,)
                ).fetchone())
        for block in range(20):
            arms = [
                arm_for_slot(
                    seed="b" * 64, target_family="code_test",
                    block_index=block, block_slot=slot,
                )
                for slot in range(4)
            ]
            self.assertEqual(arms.count("control"), 2)
            self.assertEqual(arms.count("treatment"), 2)

    def test_manifest_is_bounded_idempotent_private_and_abortable(self) -> None:
        with Memory(Path(":memory:")) as memory:
            pins = self._install_benchmark(memory)
            status = self._manifest(memory, pins)
            replay = self._manifest(memory, pins)
            self.assertEqual(status["manifest_id"], replay["manifest_id"])
            self.assertNotIn("seed", repr(status).casefold())
            self.assertEqual(status["sample_cap"], 40)
            with self.assertRaises(StrategyTransferTrialError):
                memory.create_strategy_transfer_trial_manifest(
                    project_id=1, target_families=["code_test"],
                    strategies=["verify_output"], sample_cap=44,
                    expires_at=(datetime.now(timezone.utc) + timedelta(days=15)).isoformat(),
                    seed="c" * 64, operator_confirmed=True, **pins,
                )
            self.assertTrue(memory.abort_strategy_transfer_trial(status["manifest_id"]))
            self.assertFalse(memory.abort_strategy_transfer_trial(status["manifest_id"]))
            aborted = memory.strategy_transfer_trial_status(status["manifest_id"])
            self.assertEqual(aborted["status"], "aborted")
            self.assertFalse(aborted["promotion_ready"])

    def test_assignment_prompt_receipt_resolution_and_reopen(self) -> None:
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        database = Path(handle.name)
        handle.close()
        database.unlink()
        try:
            with Memory(database) as memory:
                pins = self._install_benchmark(memory)
                self._source(memory)
                manifest = self._manifest(memory, pins, seed="d" * 64)
                prediction, selection = self._target_and_selection(memory)
                assignment = memory.assign_strategy_transfer_trial(
                    prediction, "code_test", selection,
                    manifest_id=manifest["manifest_id"],
                    current_runtime_sha256=strategy_transfer_runtime_sha256(),
                )
                replay = memory.assign_strategy_transfer_trial(
                    prediction, "code_test", selection,
                    current_runtime_sha256=strategy_transfer_runtime_sha256(),
                )
                self.assertEqual(assignment, replay)
                base = _digest("base prompt")
                final = _digest("advice prompt") if assignment["apply_advice"] else base
                self.assertTrue(memory.record_strategy_transfer_trial_prompt_receipt(
                    prediction, base_prompt_sha256=base,
                    final_prompt_sha256=final,
                    advice_applied=assignment["apply_advice"],
                ))
                self.assertFalse(memory.record_strategy_transfer_trial_prompt_receipt(
                    prediction, base_prompt_sha256=base,
                    final_prompt_sha256=final,
                    advice_applied=assignment["apply_advice"],
                ))
                memory.record_strategy_transfer_applications(
                    prediction, "code_test", selection, mode="trial",
                    applied=assignment["apply_advice"],
                )
                self.assertTrue(
                    memory.record_strategy_transfer_trial_provider_dispatch(
                        prediction
                    )
                )
                self.assertFalse(
                    memory.record_strategy_transfer_trial_provider_dispatch(
                        prediction
                    )
                )
                self.assertTrue(memory.resolve_prediction(
                    prediction, actual_status="complete", actual_steps=2,
                    evidence_ok=True,
                ))
                row = memory.db.execute(
                    "SELECT status, successful FROM strategy_transfer_trial_assignments"
                ).fetchone()
                self.assertEqual((row["status"], row["successful"]), ("resolved", 1))
                manifest_id = manifest["manifest_id"]
            with Memory(database) as reopened:
                status = reopened.strategy_transfer_trial_status(manifest_id)
                self.assertEqual(status["resolved"], 1)
                self.assertEqual(status["contaminated"], 0)
                self.assertEqual(status["control_assigned"] + status["treatment_assigned"], 1)
        finally:
            for suffix in ("", "-wal", "-shm"):
                Path(f"{database}{suffix}").unlink(missing_ok=True)

    def test_missing_prompt_and_tampering_fail_closed(self) -> None:
        with Memory(Path(":memory:")) as memory:
            pins = self._install_benchmark(memory)
            self._source(memory)
            manifest = self._manifest(memory, pins, seed="e" * 64)
            prediction, selection = self._target_and_selection(memory)
            memory.assign_strategy_transfer_trial(
                prediction, "code_test", selection,
                manifest_id=manifest["manifest_id"],
                current_runtime_sha256=strategy_transfer_runtime_sha256(),
            )
            with self.assertRaises(StrategyTransferTrialError):
                memory.record_strategy_transfer_trial_prompt_receipt(
                    prediction, base_prompt_sha256=_digest("same"),
                    final_prompt_sha256=_digest("different"),
                    advice_applied=False,
                )
            # The unresolved assignment cannot be retrofitted into a valid outcome.
            self.assertTrue(memory.resolve_prediction(
                prediction, actual_status="complete", actual_steps=1,
                evidence_ok=True,
            ))
            row = memory.db.execute(
                "SELECT status, status_reason FROM strategy_transfer_trial_assignments"
            ).fetchone()
            self.assertEqual(row["status"], "contaminated")
            self.assertIn(row["status_reason"], {
                "prompt_receipt_missing", "application_receipt_invalid",
            })
            memory.db.execute(
                "UPDATE strategy_transfer_trial_manifests SET runtime_sha256=?",
                ("f" * 64,),
            )
            self.assertIsNone(memory.active_strategy_transfer_trial(
                1, "code_test", strategy_transfer_runtime_sha256()
            ))

    def test_dispatch_rechecks_runtime_expiry_project_and_quarantine(self) -> None:
        cases = ("runtime", "expiry", "project", "quarantine")
        for index, case in enumerate(cases):
            with self.subTest(case=case), Memory(Path(":memory:")) as memory:
                pins = self._install_benchmark(memory)
                self._source(memory)
                manifest = self._manifest(
                    memory, pins, seed=(str(index + 3) * 64)[:64]
                )
                prediction, _assignment, _selection = self._prepare_trial_target(
                    memory, manifest["manifest_id"]
                )
                expected_reason = {
                    "runtime": "dispatch_runtime_drift",
                    "expiry": (
                        "dispatch_manifest_inactive_expired_or_out_of_scope"
                    ),
                    "project": "dispatch_project_disabled",
                    "quarantine": "dispatch_harm_quarantine",
                }[case]
                if case == "runtime":
                    context = patch(
                        "jarvis.memory.strategy_transfer_runtime_sha256",
                        return_value="f" * 64,
                    )
                elif case == "expiry":
                    class _ExpiredClock(datetime):
                        @classmethod
                        def now(cls, tz=None):
                            instant = datetime.now(timezone.utc) + timedelta(days=2)
                            return instant if tz is not None else instant.replace(tzinfo=None)

                    context = patch("jarvis.memory.datetime", _ExpiredClock)
                else:
                    context = patch(
                        "jarvis.memory.strategy_transfer_runtime_sha256",
                        wraps=strategy_transfer_runtime_sha256,
                    )
                if case == "project":
                    memory.db.execute(
                        "UPDATE agent_projects SET enabled=0 WHERE id=1"
                    )
                elif case == "quarantine":
                    for _failure in range(2):
                        failed_prediction, failed_selection = (
                            self._target_and_selection(memory)
                        )
                        memory.record_strategy_transfer_applications(
                            failed_prediction,
                            "code_test",
                            failed_selection,
                            mode="advise",
                            applied=True,
                        )
                        memory.resolve_prediction(
                            failed_prediction,
                            actual_status="failed",
                            actual_steps=1,
                            evidence_ok=False,
                        )
                with context, self.assertRaisesRegex(
                    StrategyTransferTrialError, expected_reason
                ):
                    memory.record_strategy_transfer_trial_provider_dispatch(
                        prediction
                    )
                row = memory.db.execute(
                    """SELECT provider_dispatched_at, provider_dispatch_sha256
                       FROM strategy_transfer_trial_assignments
                       WHERE prediction_id=?""",
                    (prediction,),
                ).fetchone()
                self.assertIsNone(row["provider_dispatched_at"])
                self.assertIsNone(row["provider_dispatch_sha256"])

    def test_concurrent_assignment_is_unique_balanced_and_metadata_only(self) -> None:
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        database = Path(handle.name)
        handle.close()
        database.unlink()
        try:
            with Memory(database) as memory:
                pins = self._install_benchmark(memory)
                self._source(memory)
                manifest = self._manifest(memory, pins, seed="1" * 64)
                prepared = [self._target_and_selection(memory) for _ in range(8)]
                # Rejected diagnostic prose is deliberately accepted by the
                # selector but must never cross into the trial ledger.
                prepared[0][1]["rejected"].append({
                    "lesson_id": "lesson:999", "reason": "private-marker",
                })

            def assign(item):
                prediction, selection = item
                with Memory(database) as worker:
                    return worker.assign_strategy_transfer_trial(
                        prediction, "code_test", selection,
                        manifest_id=manifest["manifest_id"],
                        current_runtime_sha256=strategy_transfer_runtime_sha256(),
                    )

            with ThreadPoolExecutor(max_workers=8) as pool:
                assignments = list(pool.map(assign, prepared))
            self.assertEqual(
                sorted(item["family_sequence"] for item in assignments),
                list(range(8)),
            )
            for block in (0, 1):
                arms = [
                    item["arm"] for item in assignments
                    if item["block_index"] == block
                ]
                self.assertEqual(arms.count("control"), 2)
                self.assertEqual(arms.count("treatment"), 2)
            with Memory(database) as reopened:
                trial_text = " ".join(
                    str(value)
                    for row in reopened.db.execute(
                        "SELECT * FROM strategy_transfer_trial_assignments"
                    ).fetchall()
                    for value in tuple(row)
                )
                self.assertNotIn("private-marker", trial_text)
                status = reopened.strategy_transfer_trial_status(
                    manifest["manifest_id"]
                )
                self.assertEqual(status["assigned"], 8)
        finally:
            for suffix in ("", "-wal", "-shm"):
                Path(f"{database}{suffix}").unlink(missing_ok=True)

    def test_partial_v39_migration_recovers_on_reopen(self) -> None:
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        database = Path(handle.name)
        handle.close()
        database.unlink()
        try:
            with Memory(database):
                pass
            connection = sqlite3.connect(str(database), isolation_level=None)
            try:
                connection.execute("PRAGMA foreign_keys=OFF")
                connection.execute("DROP TABLE strategy_transfer_trial_assignments")
                connection.execute("DROP TABLE strategy_transfer_trial_manifests")
                connection.execute(
                    "CREATE TABLE strategy_transfer_trial_manifests(stale TEXT)"
                )
                connection.execute("PRAGMA user_version=38")
            finally:
                connection.close()
            with Memory(database) as recovered:
                self.assertEqual(
                    recovered.db.execute("PRAGMA user_version").fetchone()[0], 39
                )
                columns = {
                    str(row["name"])
                    for row in recovered.db.execute(
                        "PRAGMA table_info(strategy_transfer_trial_manifests)"
                    ).fetchall()
                }
                self.assertIn("manifest_sha256", columns)
                self.assertNotIn("stale", columns)
                assignment_columns = {
                    str(row["name"])
                    for row in recovered.db.execute(
                        "PRAGMA table_info(strategy_transfer_trial_assignments)"
                    ).fetchall()
                }
                self.assertIn("provider_dispatch_sha256", assignment_columns)
        finally:
            for suffix in ("", "-wal", "-shm"):
                Path(f"{database}{suffix}").unlink(missing_ok=True)

    def test_trial_resolution_is_strictly_after_equal_dispatch_clock(self) -> None:
        with Memory(Path(":memory:")) as memory:
            pins = self._install_benchmark(memory)
            self._source(memory)
            manifest = self._manifest(memory, pins)
            prediction, _assignment, _selection = self._prepare_trial_target(
                memory, manifest["manifest_id"]
            )
            fixed = datetime.now(timezone.utc).isoformat()
            with patch("jarvis.memory.now_iso", return_value=fixed):
                memory.record_strategy_transfer_trial_provider_dispatch(prediction)
                memory.resolve_prediction(
                    prediction,
                    actual_status="complete",
                    actual_steps=2,
                    evidence_ok=True,
                )
            row = memory.db.execute(
                """SELECT a.provider_dispatched_at,
                          a.resolved_at AS assignment_resolved_at,
                          p.resolved_at AS prediction_resolved_at,
                          s.resolved_at AS application_resolved_at
                   FROM strategy_transfer_trial_assignments AS a
                   JOIN task_predictions AS p ON p.id=a.prediction_id
                   JOIN strategy_transfer_applications AS s
                     ON s.prediction_id=a.prediction_id
                   WHERE a.prediction_id=?""",
                (prediction,),
            ).fetchone()
            shared = str(row["assignment_resolved_at"])
            self.assertEqual(str(row["prediction_resolved_at"]), shared)
            self.assertEqual(str(row["application_resolved_at"]), shared)
            self.assertGreater(
                datetime.fromisoformat(shared),
                datetime.fromisoformat(str(row["provider_dispatched_at"])),
            )

    def test_trial_clock_rollback_contaminates_and_fails_validation(self) -> None:
        with Memory(Path(":memory:")) as memory:
            pins = self._install_benchmark(memory)
            self._source(memory)
            manifest = self._manifest(memory, pins)
            prediction, _assignment, _selection = self._prepare_trial_target(
                memory, manifest["manifest_id"]
            )
            dispatch = datetime.now(timezone.utc) + timedelta(seconds=1)
            resolved = dispatch - timedelta(seconds=1)
            with patch("jarvis.memory.now_iso", return_value=dispatch.isoformat()):
                memory.record_strategy_transfer_trial_provider_dispatch(prediction)
            with patch("jarvis.memory.now_iso", return_value=resolved.isoformat()):
                memory.resolve_prediction(
                    prediction,
                    actual_status="complete",
                    actual_steps=2,
                    evidence_ok=True,
                )
            row = memory.db.execute(
                """SELECT * FROM strategy_transfer_trial_assignments
                   WHERE prediction_id=?""",
                (prediction,),
            ).fetchone()
            self.assertEqual(str(row["status"]), "contaminated")
            self.assertEqual(str(row["status_reason"]), "assignment_integrity")
            self.assertEqual(
                memory._strategy_transfer_trial_assignment_validation(row),
                (False, "provider_dispatch_outcome_order_invalid"),
            )

    def test_equal_clock_normalization_does_not_change_nontrial_resolution(self) -> None:
        with Memory(Path(":memory:")) as memory:
            prediction = memory.record_prediction(
                family="conversation",
                profile="nontrial-clock",
                model="test",
                predicted_success=0.8,
                predicted_steps=1,
                predicted_verification="not_applicable",
                basis="prior",
                origin="interactive",
            )
            fixed = datetime.now(timezone.utc).isoformat()
            with patch("jarvis.memory.now_iso", return_value=fixed):
                memory.resolve_prediction(
                    prediction,
                    actual_status="complete",
                    actual_steps=1,
                    evidence_ok=None,
                )
            row = memory.db.execute(
                "SELECT resolved_at FROM task_predictions WHERE id=?",
                (prediction,),
            ).fetchone()
            self.assertEqual(str(row["resolved_at"]), fixed)

    def test_promotion_replays_sealed_evaluator_and_stays_scope_bound(self) -> None:
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        database = Path(handle.name)
        handle.close()
        database.unlink()
        try:
            with Memory(database) as memory:
                pins = self._install_benchmark(memory)
                for family, label in (
                    ("code_fix", "fix-source"),
                    ("code_build", "build-source"),
                    ("file_ops", "file-source"),
                ):
                    self._source_family(memory, family, label)
                manifest = self._manifest(memory, pins, seed="2" * 64)
                control_seen = 0
                for _index in range(40):
                    assignment = self._complete_trial_target(
                        memory,
                        manifest["manifest_id"],
                        control_success=control_seen < 10,
                    )
                    if assignment["arm"] == "control":
                        control_seen += 1
                before = memory.strategy_transfer_trial_status(
                    manifest["manifest_id"]
                )
                diagnostic = None
                if not before["promotion_ready"]:
                    diagnostic = memory.build_strategy_transfer_trial_ab_attestation(
                        manifest["manifest_id"], run_id="diagnostic-promotion"
                    )
                self.assertTrue(
                    before["promotion_ready"],
                    {"status": before, "diagnostic": diagnostic},
                )
                self.assertFalse(before["causal_attestation_valid"])

            def promote(_index):
                with Memory(database) as worker:
                    return worker.promote_strategy_transfer_trial(
                        manifest["manifest_id"], operator_confirmed=True
                    )

            with ThreadPoolExecutor(max_workers=2) as pool:
                promotions = list(pool.map(promote, range(2)))
            self.assertEqual(
                sorted(bool(item["promoted"]) for item in promotions),
                [False, True],
            )
            self.assertTrue(all(item["status"] == "promoted" for item in promotions))
            self.assertTrue(all(
                item["source_target_pairs"] >= 3 for item in promotions
            ))
            with Memory(database) as memory:
                exact = memory.strategy_transfer_readiness(
                    mode="advise",
                    project_id=1,
                    target_family="code_test",
                    strategies=("inspect_before_change", "verify_output"),
                )
                self.assertTrue(exact["allowed"], exact["reasons"])
                for changed in (
                    {"project_id": 2},
                    {"target_family": "deep_research"},
                    {"strategies": ("verify_output",)},
                ):
                    scope = {
                        "project_id": 1,
                        "target_family": "code_test",
                        "strategies": ("inspect_before_change", "verify_output"),
                    }
                    scope.update(changed)
                    rejected = memory.strategy_transfer_readiness(
                        mode="advise", **scope
                    )
                    self.assertFalse(rejected["allowed"])
                    self.assertFalse(
                        rejected["advise_scope_matches_promoted_manifest"]
                    )
        finally:
            for suffix in ("", "-wal", "-shm"):
                Path(f"{database}{suffix}").unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
