from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from jarvis.memory import Memory, SCHEMA_VERSION
from jarvis.strategy_transfer import (
    StrategyTransferError,
    select_strategy_transfer,
    strategy_target_from_runtime,
)


class StrategyTransferMemoryTests(unittest.TestCase):
    EVIDENCE = {
        "schema": "jarvis.strategy-evidence.v1",
        "inspect_before_change": True,
        "checkpoint_and_resume": False,
        "verify_output": True,
        "compare_authoritative_sources": False,
    }

    def _source_lesson(
        self,
        memory: Memory,
        *,
        family: str = "code_fix",
        project_id: int = 1,
        calibrate: bool = True,
    ) -> tuple[int, int]:
        lesson_id = 0
        observation_prediction = 0
        count = 20 if calibrate else 1
        for index in range(count):
            conversation_id = memory.new_conversation(
                f"strategy source {family} {index}", project_id=project_id
            )
            prediction_id = memory.record_prediction(
                family=family,
                profile="strategy-memory-test",
                model="deterministic-test",
                predicted_success=0.9,
                predicted_steps=1,
                predicted_verification="tool_success",
                basis="prior",
                origin="interactive",
                conversation_id=conversation_id,
            )
            self.assertTrue(memory.resolve_prediction(
                prediction_id,
                actual_status="complete",
                actual_steps=1,
                evidence_ok=True,
            ))
            if index == 0:
                self.assertTrue(memory.record_strategy_observations(
                    prediction_id, self.EVIDENCE
                ))
                self.assertFalse(memory.record_task_strategy_observation(
                    prediction_id, self.EVIDENCE
                ))
                reflection_id = memory.record_reflection(
                    status="complete",
                    summary="Verified source strategy outcome.",
                    improvements=(
                        "Inspect the bounded target before changing it and verify "
                        "the resulting output."
                    ),
                    conversation_id=conversation_id,
                    prediction_id=prediction_id,
                    tool_calls=1,
                )
                row = memory.db.execute(
                    "SELECT id FROM memories WHERE reflection_id=? AND kind='lesson'",
                    (reflection_id,),
                ).fetchone()
                self.assertIsNotNone(row)
                lesson_id = int(row["id"])
                observation_prediction = prediction_id
        return lesson_id, observation_prediction

    @staticmethod
    def _target_prediction(
        memory: Memory,
        *,
        family: str = "code_test",
        project_id: int = 1,
    ) -> int:
        conversation_id = memory.new_conversation(
            f"strategy target {family}", project_id=project_id
        )
        return memory.record_prediction(
            family=family,
            profile="strategy-memory-test",
            model="deterministic-test",
            predicted_success=0.7,
            predicted_steps=1,
            predicted_verification="tool_success",
            basis="prior",
            origin="interactive",
            conversation_id=conversation_id,
        )

    @staticmethod
    def _selection(memory: Memory, prediction_id: int, *, project_id: int = 1):
        target = strategy_target_from_runtime(
            task_id=f"prediction:{prediction_id}",
            family="code_test",
            changes_existing_state=True,
            resumable=False,
            verification="tool_success",
            current_external_facts=False,
        )
        as_of = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        candidates = memory.strategy_transfer_candidates(
            target_family="code_test", project_id=project_id, as_of=as_of
        )
        return candidates, select_strategy_transfer(target, candidates, as_of=as_of)

    def test_v39_schema_and_selector_candidates_are_metadata_only(self) -> None:
        with Memory(Path(":memory:")) as memory:
            self.assertEqual(SCHEMA_VERSION, 41)
            self.assertEqual(memory.db.execute("PRAGMA user_version").fetchone()[0], 41)
            for table in (
                "task_strategy_observations", "strategy_transfer_applications",
                "strategy_transfer_attestations",
            ):
                self.assertIsNotNone(memory.db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone())
            lesson_id, _ = self._source_lesson(memory)
            candidates = memory.strategy_transfer_candidates(
                target_family="code_test", project_id=1
            )
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["id"], f"lesson:{lesson_id}")
            self.assertEqual(candidates[0]["source_family"], "code_fix")
            self.assertEqual(
                set(candidates[0]),
                {
                    "id", "record_kind", "source_family", "outcome_status",
                    "derived_from", "provenance_valid", "provenance_sha256",
                    "observed_at", "valid_until", "contradicted_by",
                    "strategies", "authority_claims", "tool_claims",
                },
            )
            rendered = repr(candidates)
            self.assertNotIn("Inspect the bounded target", rendered)
            self.assertNotIn("content", rendered.casefold())
            self.assertEqual(
                memory.strategy_transfer_candidates(
                    target_family="code_fix", project_id=1
                ),
                [],
            )

    def test_candidates_require_calibration_project_and_untampered_evidence(self) -> None:
        with Memory(Path(":memory:")) as memory:
            other_project = memory.add_project("Other", "@projects/other")
            _, observation_prediction = self._source_lesson(
                memory, calibrate=False
            )
            self.assertEqual(memory.strategy_transfer_candidates(
                target_family="code_test", project_id=1
            ), [])
            self.assertEqual(memory.strategy_transfer_candidates(
                target_family="code_test", project_id=other_project
            ), [])
            for index in range(19):
                conversation_id = memory.new_conversation(f"calibrate {index}")
                prediction_id = memory.record_prediction(
                    family="code_fix", profile="calibrate", model="test",
                    predicted_success=0.9, predicted_steps=1,
                    predicted_verification="tool_success", origin="interactive",
                    conversation_id=conversation_id,
                )
                memory.resolve_prediction(
                    prediction_id, actual_status="complete", actual_steps=1,
                    evidence_ok=True,
                )
            self.assertTrue(memory.strategy_transfer_candidates(
                target_family="code_test", project_id=1
            ))
            memory.db.execute(
                "UPDATE task_strategy_observations SET evidence_json='{}' "
                "WHERE prediction_id=?",
                (observation_prediction,),
            )
            self.assertEqual(memory.strategy_transfer_candidates(
                target_family="code_test", project_id=1
            ), [])

    def test_application_receipts_are_idempotent_resolve_once_and_measure(self) -> None:
        with Memory(Path(":memory:")) as memory:
            self._source_lesson(memory)
            target_prediction = self._target_prediction(memory)
            candidates, selection = self._selection(memory, target_prediction)
            self.assertEqual(len(candidates), 1)
            self.assertEqual(
                memory.record_strategy_transfer_applications(
                    target_prediction, "code_test", selection.to_payload()
                ),
                2,
            )
            self.assertEqual(
                memory.record_strategy_transfer_applications(
                    target_prediction, "code_test", selection.to_payload()
                ),
                0,
            )
            before = memory.db.execute(
                "SELECT created_at FROM strategy_transfer_applications "
                "WHERE prediction_id=? ORDER BY id",
                (target_prediction,),
            ).fetchall()
            self.assertTrue(memory.resolve_prediction(
                target_prediction,
                actual_status="complete",
                actual_steps=1,
                evidence_ok=True,
            ))
            self.assertFalse(memory.resolve_prediction(
                target_prediction,
                actual_status="failed",
                actual_steps=1,
                evidence_ok=False,
            ))
            after = memory.db.execute(
                "SELECT created_at, resolved_at, successful "
                "FROM strategy_transfer_applications WHERE prediction_id=? "
                "ORDER BY id",
                (target_prediction,),
            ).fetchall()
            self.assertEqual(
                [row["created_at"] for row in before],
                [row["created_at"] for row in after],
            )
            self.assertTrue(all(row["resolved_at"] for row in after))
            self.assertTrue(all(int(row["successful"]) == 1 for row in after))
            effectiveness = memory.strategy_transfer_effectiveness("code_test")
            self.assertEqual(len(effectiveness), 2)
            self.assertTrue(all(row["mode"] == "observe" for row in effectiveness))
            self.assertTrue(all(row["applied"] is False for row in effectiveness))
            self.assertTrue(all(row["resolved"] == 1 for row in effectiveness))
            self.assertTrue(all(row["success_rate"] == 1.0 for row in effectiveness))
            readiness = memory.strategy_transfer_readiness()
            self.assertEqual(readiness["valid_observations"], 1)
            self.assertEqual(readiness["valid_applications"], 2)

    def test_application_rejects_authority_cross_project_and_tampering(self) -> None:
        with Memory(Path(":memory:")) as memory:
            self._source_lesson(memory)
            target_prediction = self._target_prediction(memory)
            _, selection = self._selection(memory, target_prediction)
            unsafe = selection.to_payload()
            unsafe["authority_grants"] = ["filesystem_write"]
            with self.assertRaises(StrategyTransferError):
                memory.record_strategy_transfer_applications(
                    target_prediction, "code_test", unsafe
                )
            other_project = memory.add_project("Other", "@projects/other")
            other_target = self._target_prediction(
                memory, family="code_test", project_id=other_project
            )
            with self.assertRaises(ValueError):
                memory.record_strategy_transfer_applications(
                    other_target, "code_test", selection.to_payload()
                )
            memory.record_strategy_transfer_applications(
                target_prediction, "code_test", selection.to_payload()
            )
            memory.resolve_prediction(
                target_prediction, actual_status="complete", actual_steps=1,
                evidence_ok=True,
            )
            memory.db.execute(
                "UPDATE strategy_transfer_applications SET application_sha256=? "
                "WHERE prediction_id=?",
                ("0" * 64, target_prediction),
            )
            self.assertEqual(
                memory.strategy_transfer_effectiveness("code_test"), []
            )

    def test_readiness_remains_reporting_only_without_sealed_applied_ab_evidence(self) -> None:
        with Memory(Path(":memory:")) as memory:
            for family in ("code_fix", "code_build", "deep_research"):
                self._source_lesson(memory, family=family)

            advised_prediction = self._target_prediction(memory)
            _, advised = self._selection(memory, advised_prediction)
            memory.record_strategy_transfer_applications(
                advised_prediction,
                "code_test",
                advised.to_payload(),
                mode="advise",
                applied=True,
            )
            memory.resolve_prediction(
                advised_prediction,
                actual_status="complete",
                actual_steps=1,
                evidence_ok=True,
            )
            before = memory.strategy_transfer_readiness()
            self.assertFalse(before["allowed"])
            self.assertEqual(before["resolved_observe_targets"], 0)
            self.assertEqual(before["resolved_applied_targets"], 1)

            for _ in range(20):
                prediction_id = self._target_prediction(memory)
                _, selection = self._selection(memory, prediction_id)
                memory.record_strategy_transfer_applications(
                    prediction_id,
                    "code_test",
                    selection.to_payload(),
                    mode="observe",
                )
                memory.resolve_prediction(
                    prediction_id,
                    actual_status="complete",
                    actual_steps=1,
                    evidence_ok=True,
                )
            ready = memory.strategy_transfer_readiness()
            self.assertFalse(ready["allowed"])
            self.assertTrue(ready["reporting_only"])
            self.assertEqual(ready["resolved_observe_targets"], 20)
            self.assertEqual(len(ready["source_target_pairs"]), 3)
            self.assertEqual(ready["observe_success_rate"], 1.0)
            self.assertFalse(ready["sealed_benchmark_attested"])
            self.assertFalse(ready["applied_ab_evidence_attested"])

            memory.db.execute(
                """UPDATE strategy_transfer_applications
                   SET application_sha256=? WHERE id=(
                       SELECT MIN(id) FROM strategy_transfer_applications
                   )""",
                ("0" * 64,),
            )
            blocked = memory.strategy_transfer_readiness()
            self.assertFalse(blocked["allowed"])
            self.assertEqual(blocked["invalid_applications"], 1)

    def test_applied_failures_quarantine_strategy_and_evidence_is_required(self) -> None:
        with Memory(Path(":memory:")) as memory:
            self._source_lesson(memory)
            for _ in range(2):
                prediction_id = self._target_prediction(memory)
                _, selection = self._selection(memory, prediction_id)
                with self.assertRaises(ValueError):
                    memory.record_strategy_transfer_applications(
                        prediction_id,
                        "code_test",
                        selection.to_payload(),
                        mode="observe",
                        applied=True,
                    )
                memory.record_strategy_transfer_applications(
                    prediction_id,
                    "code_test",
                    selection.to_payload(),
                    mode="advise",
                    applied=True,
                )
                memory.resolve_prediction(
                    prediction_id,
                    actual_status="failed",
                    actual_steps=1,
                    evidence_ok=False,
                )
            self.assertEqual(memory.strategy_transfer_candidates(
                target_family="code_test", project_id=1
            ), [])
            self.assertEqual(
                memory.strategy_transfer_readiness()["empirical_harm_quarantines"],
                2,
            )

            conversation_id = memory.new_conversation("no evidence target")
            no_evidence_prediction = memory.record_prediction(
                family="conversation",
                profile="strategy-memory-test",
                model="deterministic-test",
                predicted_success=0.7,
                predicted_steps=0,
                predicted_verification="not_applicable",
                origin="interactive",
                conversation_id=conversation_id,
            )
            target = strategy_target_from_runtime(
                task_id=f"prediction:{no_evidence_prediction}",
                family="conversation",
                changes_existing_state=True,
                resumable=False,
                verification="not_applicable",
                current_external_facts=False,
            )
            as_of = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            candidates = memory.strategy_transfer_candidates(
                target_family="conversation", project_id=1, as_of=as_of
            )
            selection = select_strategy_transfer(target, candidates, as_of=as_of)
            memory.record_strategy_transfer_applications(
                no_evidence_prediction, "conversation", selection.to_payload()
            )
            memory.resolve_prediction(
                no_evidence_prediction,
                actual_status="complete",
                actual_steps=0,
                evidence_ok=None,
            )
            row = memory.db.execute(
                """SELECT successful FROM strategy_transfer_applications
                   WHERE prediction_id=? LIMIT 1""",
                (no_evidence_prediction,),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(int(row["successful"]), 0)

    def test_harm_ledger_corruption_or_query_failure_excludes_candidates(self) -> None:
        with Memory(Path(":memory:")) as memory:
            self._source_lesson(memory)
            prediction_id = self._target_prediction(memory)
            _, selection = self._selection(memory, prediction_id)
            memory.record_strategy_transfer_applications(
                prediction_id,
                "code_test",
                selection.to_payload(),
                mode="advise",
                applied=True,
            )
            memory.resolve_prediction(
                prediction_id,
                actual_status="failed",
                actual_steps=1,
                evidence_ok=False,
            )
            memory.db.execute(
                "UPDATE strategy_transfer_applications SET application_sha256=?",
                ("0" * 64,),
            )
            self.assertEqual(memory.strategy_transfer_candidates(
                target_family="code_test", project_id=1
            ), [])
            health = memory.strategy_transfer_candidate_health()
            self.assertFalse(health["available"])
            self.assertEqual(health["reason"], "harm_ledger_unavailable")
            self.assertEqual(health["unavailable_strategies"], 2)

            memory.db.execute("DROP TABLE strategy_transfer_applications")
            self.assertEqual(memory.strategy_transfer_candidates(
                target_family="code_test", project_id=1
            ), [])
            unavailable = memory.strategy_transfer_candidate_health()
            self.assertFalse(unavailable["available"])
            self.assertEqual(unavailable["reason"], "harm_ledger_unavailable")

    def test_sealed_benchmark_is_immutable_but_cannot_activate_alone(self) -> None:
        with Memory(Path(":memory:")) as memory:
            benchmark = memory.build_strategy_transfer_benchmark_attestation(
                run_id="benchmark-test"
            )
            self.assertTrue(memory.record_strategy_transfer_attestation(
                "sealed_benchmark",
                benchmark,
                evaluator_version=benchmark["evaluator_version"],
                evaluator_sha256=benchmark["evaluator_sha256"],
                config_sha256=benchmark["config_sha256"],
            ))
            self.assertFalse(memory.record_strategy_transfer_attestation(
                "sealed_benchmark",
                benchmark,
                evaluator_version=benchmark["evaluator_version"],
                evaluator_sha256=benchmark["evaluator_sha256"],
                config_sha256=benchmark["config_sha256"],
            ))
            conflicting_receipt = dict(benchmark)
            conflicting_receipt["run_id"] = "same-seal-different-receipt"
            with self.assertRaises(ValueError):
                memory.record_strategy_transfer_attestation(
                    "sealed_benchmark",
                    conflicting_receipt,
                    evaluator_version=benchmark["evaluator_version"],
                    evaluator_sha256=benchmark["evaluator_sha256"],
                    config_sha256=benchmark["config_sha256"],
                )
            readiness = memory.strategy_transfer_readiness(
                mode="advise",
                evaluator_version=benchmark["evaluator_version"],
                evaluator_sha256=benchmark["evaluator_sha256"],
                config_sha256=benchmark["config_sha256"],
            )
            self.assertFalse(readiness["allowed"])
            self.assertTrue(readiness["sealed_benchmark_attested"])
            self.assertFalse(readiness["applied_ab_evidence_attested"])

            tampered = dict(benchmark)
            tampered["all_exit_criteria"] = False
            with self.assertRaises(ValueError):
                memory.record_strategy_transfer_attestation(
                    "sealed_benchmark",
                    tampered,
                    evaluator_version=benchmark["evaluator_version"],
                    evaluator_sha256=benchmark["evaluator_sha256"],
                    config_sha256=benchmark["config_sha256"],
                )

    def test_activation_requires_sealed_benchmark_and_real_disjoint_ab_receipts(self) -> None:
        with Memory(Path(":memory:")) as memory:
            for family in ("code_fix", "code_build", "deep_research"):
                self._source_lesson(memory, family=family)
            benchmark = memory.build_strategy_transfer_benchmark_attestation(
                run_id="benchmark-activation-test"
            )
            memory.record_strategy_transfer_attestation(
                "sealed_benchmark",
                benchmark,
                evaluator_version=benchmark["evaluator_version"],
                evaluator_sha256=benchmark["evaluator_sha256"],
                config_sha256=benchmark["config_sha256"],
            )
            control_ids: list[int] = []
            applied_ids: list[int] = []
            for index in range(20):
                prediction_id = self._target_prediction(memory)
                _, selection = self._selection(memory, prediction_id)
                memory.record_strategy_transfer_applications(
                    prediction_id,
                    "code_test",
                    selection.to_payload(),
                    mode="observe",
                    applied=False,
                )
                control_ids.append(prediction_id)
                success = index >= 5
                memory.resolve_prediction(
                    prediction_id,
                    actual_status="complete" if success else "failed",
                    actual_steps=1,
                    evidence_ok=success,
                )
            for _ in range(20):
                prediction_id = self._target_prediction(memory)
                _, selection = self._selection(memory, prediction_id)
                memory.record_strategy_transfer_applications(
                    prediction_id,
                    "code_test",
                    selection.to_payload(),
                    mode="advise",
                    applied=True,
                )
                applied_ids.append(prediction_id)
                memory.resolve_prediction(
                    prediction_id,
                    actual_status="complete",
                    actual_steps=1,
                    evidence_ok=True,
                )
            manifest_sha256 = "a" * 64
            applied_ab = memory.build_strategy_transfer_applied_ab_attestation(
                control_prediction_ids=control_ids,
                applied_prediction_ids=applied_ids,
                assignment_manifest_sha256=manifest_sha256,
                run_id="applied-ab-activation-test",
            )
            self.assertFalse(applied_ab["all_exit_criteria"])
            self.assertEqual(
                applied_ab["claim_scope"],
                "retrospective_receipt_comparison_only_not_activation_evidence",
            )
            self.assertFalse(
                applied_ab["passes"]["pre_outcome_randomized_assignment"]
            )
            with self.assertRaises(ValueError):
                memory.record_strategy_transfer_attestation(
                    "applied_ab",
                    applied_ab,
                    evaluator_version=applied_ab["evaluator_version"],
                    evaluator_sha256=applied_ab["evaluator_sha256"],
                    config_sha256=applied_ab["config_sha256"],
                )
            observe = memory.strategy_transfer_readiness(
                mode="observe",
                evaluator_version=benchmark["evaluator_version"],
                evaluator_sha256=benchmark["evaluator_sha256"],
                config_sha256=benchmark["config_sha256"],
            )
            self.assertFalse(observe["allowed"])
            ready = memory.strategy_transfer_readiness(
                mode="advise",
                evaluator_version=benchmark["evaluator_version"],
                evaluator_sha256=benchmark["evaluator_sha256"],
                config_sha256=benchmark["config_sha256"],
            )
            # Even complete benchmark and retrospective applied/control
            # receipts cannot self-authorize this release.  The bounded trial
            # substrate exists, but advise still needs independently valid
            # causal evidence plus explicit operator promotion.
            self.assertFalse(ready["allowed"])
            self.assertTrue(ready["activation_trial_supported"])
            self.assertTrue(any(
                "applied A/B" in reason
                for reason in ready["reasons"]
            ))
            self.assertFalse(ready["causal_trial_attested"])
            self.assertFalse(ready["attestation_binding_matches_current"])
            self.assertFalse(ready["applied_ab_evidence_attested"])
            self.assertEqual(ready["resolved_observe_targets"], 20)
            self.assertEqual(ready["resolved_applied_targets"], 20)
            self.assertGreaterEqual(len(ready["source_target_pairs"]), 3)

            with self.assertRaises(ValueError):
                memory.build_strategy_transfer_applied_ab_attestation(
                    control_prediction_ids=control_ids,
                    applied_prediction_ids=control_ids,
                    assignment_manifest_sha256=manifest_sha256,
                    run_id="overlap-rejected",
                )

    def test_receipts_survive_reopen(self) -> None:
        handle = tempfile.NamedTemporaryFile(
            dir=Path.cwd(), suffix=".db", delete=False
        )
        database_path = Path(handle.name)
        handle.close()
        database_path.unlink()
        try:
            with Memory(database_path) as memory:
                self._source_lesson(memory)
                target_prediction = self._target_prediction(memory)
                _, selection = self._selection(memory, target_prediction)
                memory.record_strategy_transfer_applications(
                    target_prediction, "code_test", selection.to_payload()
                )
                memory.resolve_prediction(
                    target_prediction, actual_status="complete", actual_steps=1,
                    evidence_ok=True,
                )
            with Memory(database_path) as reopened:
                self.assertEqual(
                    reopened.strategy_transfer_readiness()["valid_observations"], 1
                )
                self.assertEqual(
                    reopened.strategy_transfer_readiness()["valid_applications"], 2
                )
                self.assertEqual(
                    len(reopened.strategy_transfer_effectiveness("code_test")), 2
                )
        finally:
            for suffix in ("", "-wal", "-shm"):
                Path(f"{database_path}{suffix}").unlink(missing_ok=True)

    def test_v39_partial_tables_and_interrupted_migration_do_not_wedge_reopen(
        self,
    ) -> None:
        handle = tempfile.NamedTemporaryFile(
            dir=Path.cwd(), suffix=".db", delete=False
        )
        database_path = Path(handle.name)
        handle.close()
        database_path.unlink()
        try:
            with Memory(database_path):
                pass
            connection = sqlite3.connect(str(database_path), isolation_level=None)
            try:
                connection.execute("PRAGMA foreign_keys=OFF")
                connection.execute("DROP TABLE strategy_transfer_attestations")
                connection.execute("DROP TABLE strategy_transfer_applications")
                connection.execute("DROP TABLE task_strategy_observations")
                connection.execute(
                    "CREATE TABLE strategy_transfer_attestations(stale TEXT)"
                )
                connection.execute(
                    "CREATE TABLE strategy_transfer_applications(stale TEXT)"
                )
                connection.execute(
                    "CREATE TABLE task_strategy_observations(stale TEXT)"
                )
                connection.execute("PRAGMA user_version=37")
            finally:
                connection.close()

            with Memory(database_path) as recovered:
                self.assertEqual(
                    recovered.db.execute("PRAGMA user_version").fetchone()[0],
                    41,
                )
                columns = {
                    str(row["name"])
                    for row in recovered.db.execute(
                        "PRAGMA table_info(strategy_transfer_attestations)"
                    ).fetchall()
                }
                self.assertIn("id", columns)
                self.assertIn("artifact_sha256", columns)
                self.assertNotIn("stale", columns)
        finally:
            for suffix in ("", "-wal", "-shm"):
                Path(f"{database_path}{suffix}").unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
