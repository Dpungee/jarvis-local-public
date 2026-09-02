from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from jarvis.long_horizon import (
    LongHorizonBudgetError,
    LongHorizonIntegrityError,
    LongHorizonStateError,
    LongHorizonStore,
    LongHorizonValidationError,
    WorkflowBudget,
    WorkflowManifest,
    WorkflowStageSpec,
)
from jarvis.memory import Memory, SCHEMA_VERSION
from tests.sqlite_crash_fixture import (
    create_future_schema_in_hot_wal,
    create_hot_future_database,
    snapshot_directory,
)


def sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def usage(**overrides: int) -> dict[str, int]:
    value = {
        "elapsed_seconds": 1,
        "tool_calls": 1,
        "model_calls": 1,
        "prompt_tokens": 10,
        "completion_tokens": 5,
    }
    value.update(overrides)
    return value


class LongHorizonTests(unittest.TestCase):
    def test_direct_future_schema_in_hot_wal_is_rejected_without_recovery_or_key(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.db"
            with Memory(path):
                pass
            create_future_schema_in_hot_wal(path, user_version=SCHEMA_VERSION + 1)
            before = snapshot_directory(path)
            with self.assertRaisesRegex(LongHorizonStateError, "newer"):
                LongHorizonStore(path, project_id=1)
            self.assertEqual(snapshot_directory(path), before)

    def test_direct_future_hot_journal_is_rejected_without_recovery_or_key_creation(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.db"
            create_hot_future_database(path, user_version=SCHEMA_VERSION + 1)
            before = snapshot_directory(path)
            with self.assertRaisesRegex(LongHorizonStateError, "newer"):
                LongHorizonStore(path, project_id=1)
            self.assertEqual(snapshot_directory(path), before)

    def setUp(self) -> None:
        self.verify_private = Ed25519PrivateKey.generate()
        self.reconcile_private = Ed25519PrivateKey.generate()
        self.verify_public = self.verify_private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.reconcile_public = self.reconcile_private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.path = Path(__file__).parent / f".long-horizon-{uuid4().hex}.db"
        self.memory = Memory(self.path, worker_id="memory-test")
        self.conversation_id = self.memory.new_conversation("long horizon", project_id=1)
        self.task_id = self.memory.add_task("private goal text", project_id=1)
        self.store = LongHorizonStore(
            self.memory, project_id=1, worker_id="worker-a",
            integrity_key=b"phase5-test-integrity-key-00001!",
            authorities={
                "final": {"scope": "final_verification", "verifier_id": "independent",
                          "runtime_sha256": sha("verifier-runtime"), "public_key": self.verify_public},
                "reconciler": {"scope": "mutation_reconciliation", "verifier_id": "reconciler",
                               "runtime_sha256": sha("reconciler-runtime"), "public_key": self.reconcile_public},
            },
            approval_validator=lambda phase, context: {
                "approved": True, "receipt_sha256": sha(f"approval:{phase}:{context['effect_key']}")
            },
        )

    def tearDown(self) -> None:
        if not self.memory.closed:
            self.memory.close()
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(self.path) + suffix)
            if candidate.exists():
                candidate.unlink()

    def budget(self, **overrides: int) -> WorkflowBudget:
        values = {
            "elapsed_seconds": 600,
            "tool_calls": 100,
            "model_calls": 50,
            "prompt_tokens": 20_000,
            "completion_tokens": 10_000,
            "retries": 3,
        }
        values.update(overrides)
        return WorkflowBudget(**values)

    def stages(self, *, mutation: bool = False, stage_budget: WorkflowBudget | None = None):
        bounded = stage_budget or self.budget()
        values = [
            WorkflowStageSpec("inspect", 1, "inspect", "none", bounded),
            WorkflowStageSpec("plan", 2, "plan", "none", bounded),
            WorkflowStageSpec(
                "execute", 3, "mutate" if mutation else "implement",
                "irreversible" if mutation else "none", bounded,
            ),
            WorkflowStageSpec("verify", 4, "verify", "none", bounded),
            WorkflowStageSpec("finalize", 5, "finalize", "none", bounded),
        ]
        return tuple(values)

    def manifest(
        self,
        *,
        mutation: bool = False,
        budget: WorkflowBudget | None = None,
        stages: tuple[WorkflowStageSpec, ...] | None = None,
        project_id: int = 1,
        conversation_id: int | None = None,
        task_id: int | None = None,
    ) -> WorkflowManifest:
        return WorkflowManifest(
            project_id=project_id,
            conversation_id=conversation_id or self.conversation_id,
            task_id=task_id or self.task_id,
            goal_sha256=sha("goal"),
            contract_sha256=sha("contract"),
            constraints_sha256=sha("constraints"),
            approval_scope_sha256=sha("approval"),
            artifact_set_sha256=sha("artifacts"),
            budget=budget or self.budget(),
            stages=stages or self.stages(mutation=mutation),
        )

    def complete_claim(self, plan_id: int, *, executor: str = "executor-a") -> dict:
        claim = self.store.claim_next_stage(plan_id, worker_id="worker-a", lease_seconds=60)
        self.assertIsNotNone(claim)
        assert claim is not None
        self.store.reserve_stage_usage(
            plan_id, claim["stage_id"], worker_id="worker-a",
            lease_token=claim["lease_token"], usage=usage(),
        )
        self.store.record_checkpoint(
            plan_id,
            claim["stage_id"],
            worker_id="worker-a",
            lease_token=claim["lease_token"],
            usage=usage(),
            outcome_sha256=sha(f"outcome-{claim['ordinal']}"),
            artifact_sha256=sha(f"artifact-{claim['ordinal']}"),
            executor_id=executor,
        )
        return claim

    def verify(self, plan_id: int, *, passed: bool, evidence: str) -> dict:
        status = self.store.show_plan(plan_id)
        challenge = {
            "schema": "jarvis.long-horizon.final-verification-challenge.v1",
            "plan_id": plan_id, "project_id": 1,
            "manifest_sha256": status["manifest_sha256"],
            "checkpoint_head_sha256": status["checkpoint_head_sha256"],
            "authority_id": "final", "verifier_id": "independent",
            "verifier_runtime_sha256": sha("verifier-runtime"),
            "evidence_sha256": sha(evidence), "passed": passed,
        }
        return self.store.record_final_verification(
            plan_id, authority_id="final", verifier_runtime_sha256=sha("verifier-runtime"),
            evidence_sha256=sha(evidence),
            signature_sha256=self.verify_private.sign(json.dumps(challenge, sort_keys=True, separators=(",", ":")).encode()).hex(),
            passed=passed,
        )

    def reconcile(self, plan_id: int, stage_id: int, *, outcome: str, evidence: str) -> dict:
        status = self.store.show_plan(plan_id)
        row = self.memory.db.execute(
            "SELECT stage_sha256,effect_key,attempt_count FROM long_horizon_stages WHERE id=?",
            (stage_id,),
        ).fetchone()
        reconciliation_round = self.memory.db.execute(
            "SELECT COALESCE(MAX(reconciliation_round),0)+1 FROM long_horizon_mutation_receipts "
            "WHERE stage_id=? AND generation=? AND event_type='reconciliation'",
            (stage_id, row["attempt_count"]),
        ).fetchone()[0]
        challenge = {
            "schema": "jarvis.long-horizon.mutation-reconciliation-challenge.v1",
            "plan_id": plan_id, "project_id": 1,
            "manifest_sha256": status["manifest_sha256"], "stage_id": stage_id,
            "stage_sha256": row["stage_sha256"], "effect_key": row["effect_key"],
            "generation": row["attempt_count"], "authority_id": "reconciler",
            "reconciliation_round": reconciliation_round,
            "reconciler_id": "reconciler", "reconciler_runtime_sha256": sha("reconciler-runtime"),
            "outcome": outcome, "evidence_sha256": sha(evidence),
        }
        return self.store.reconcile_mutation(
            plan_id, stage_id, authority_id="reconciler",
            reconciler_runtime_sha256=sha("reconciler-runtime"), outcome=outcome,
            evidence_sha256=sha(evidence),
            signature_sha256=self.reconcile_private.sign(json.dumps(challenge, sort_keys=True, separators=(",", ":")).encode()).hex(),
        )

    def reopen_store(self) -> None:
        self.memory.close()
        self.memory = Memory(self.path, worker_id="long-horizon-reopen")
        self.store = LongHorizonStore(
            self.memory, project_id=1, worker_id="worker-a",
            integrity_key=b"phase5-test-integrity-key-00001!",
            authorities={
                "final": {"scope": "final_verification", "verifier_id": "independent",
                          "runtime_sha256": sha("verifier-runtime"), "public_key": self.verify_public},
                "reconciler": {"scope": "mutation_reconciliation", "verifier_id": "reconciler",
                               "runtime_sha256": sha("reconciler-runtime"), "public_key": self.reconcile_public},
            },
            approval_validator=lambda phase, context: {
                "approved": True, "receipt_sha256": sha(f"approval:{phase}:{context['effect_key']}")
            },
        )

    def test_schema_v40_and_normal_five_stage_completion(self) -> None:
        self.assertEqual(SCHEMA_VERSION, 44)
        self.assertEqual(self.memory.db.execute("PRAGMA user_version").fetchone()[0], 44)
        plan_id = self.store.create_plan(self.manifest())
        for _ in range(5):
            self.complete_claim(plan_id)
        status = self.store.show_plan(plan_id)
        self.assertEqual(status["completed_stages"], 5)
        self.assertEqual(status["status"], "active")
        verified = self.verify(plan_id, passed=True, evidence="verified")
        self.assertTrue(verified["plan_complete"])
        evidence = self.store.export_evidence(plan_id)
        self.assertEqual(evidence["plan"]["status"], "complete")
        self.assertEqual(len(evidence["checkpoints"]), 5)
        self.assertNotIn("private goal text", json.dumps(evidence))

    def test_direct_path_rejects_future_schema_before_sidecar_or_ddl(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "future.db"
            db = sqlite3.connect(path)
            try:
                db.execute("CREATE TABLE future_only(value TEXT)")
                db.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
                db.commit()
            finally:
                db.close()
            before_bytes = path.read_bytes()
            before_files = sorted(item.name for item in Path(directory).iterdir())

            with self.assertRaisesRegex(LongHorizonStateError, "newer than supported"):
                LongHorizonStore(path, project_id=1)

            self.assertEqual(path.read_bytes(), before_bytes)
            self.assertEqual(
                sorted(item.name for item in Path(directory).iterdir()),
                before_files,
            )
            self.assertFalse(Path(str(path) + ".long-horizon.key").exists())
            db = sqlite3.connect(path)
            try:
                self.assertEqual(
                    [str(row[0]) for row in db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                    ).fetchall()],
                    ["future_only"],
                )
                self.assertEqual(
                    int(db.execute("PRAGMA user_version").fetchone()[0]),
                    SCHEMA_VERSION + 1,
                )
            finally:
                db.close()

    def test_interrupted_v40_partial_schema_recovers_on_reopen(self) -> None:
        self.memory.close()
        raw = sqlite3.connect(str(self.path), isolation_level=None)
        raw.execute("DROP TABLE IF EXISTS long_horizon_stages")
        raw.execute("DROP TABLE IF EXISTS long_horizon_plans")
        raw.execute("CREATE TABLE long_horizon_plans(id INTEGER PRIMARY KEY, unsafe TEXT)")
        raw.execute("CREATE TABLE long_horizon_stages(id INTEGER PRIMARY KEY, unsafe TEXT)")
        raw.execute("PRAGMA user_version=39")
        raw.close()
        self.memory = Memory(self.path, worker_id="migration-recovery")
        columns = {
            row["name"]
            for row in self.memory.db.execute("PRAGMA table_info(long_horizon_plans)")
        }
        self.assertEqual(self.memory.db.execute("PRAGMA user_version").fetchone()[0], 44)
        self.assertIn("manifest_sha256", columns)
        self.assertNotIn("unsafe", columns)

    def test_manifest_is_strict_and_requires_five_ordered_typed_stages(self) -> None:
        with self.assertRaises(LongHorizonValidationError):
            WorkflowManifest.from_value({**self.manifest().to_payload(), "unexpected": True})
        with self.assertRaises(LongHorizonValidationError):
            WorkflowManifest(
                **{
                    **self.manifest().to_payload(include_stages=False),
                    "stages": self.stages()[:4],
                }
            )
        bad = list(self.stages())
        bad[1] = WorkflowStageSpec("plan", 3, "plan", "none", self.budget())
        with self.assertRaises(LongHorizonValidationError):
            self.manifest(stages=tuple(bad))

    def test_project_conversation_task_binding_fails_closed(self) -> None:
        project_2 = self.memory.add_project("p2", "@projects/p2")
        other_conversation = self.memory.new_conversation("other", project_id=project_2)
        with self.assertRaises(LongHorizonValidationError):
            self.store.create_plan(
                self.manifest(project_id=1, conversation_id=other_conversation)
            )

    def test_restart_preserves_progress_and_hash_chain(self) -> None:
        plan_id = self.store.create_plan(self.manifest())
        self.complete_claim(plan_id)
        self.complete_claim(plan_id)
        head = self.store.show_plan(plan_id)["checkpoint_head_sha256"]
        self.memory.close()
        self.memory = Memory(self.path, worker_id="memory-reopen")
        self.store = LongHorizonStore(
            self.memory, project_id=1, worker_id="worker-a",
            integrity_key=b"phase5-test-integrity-key-00001!",
        )
        status = self.store.show_plan(plan_id)
        self.assertEqual(status["completed_stages"], 2)
        self.assertEqual(status["checkpoint_head_sha256"], head)
        for _ in range(3):
            self.complete_claim(plan_id)
        self.assertEqual(self.store.show_plan(plan_id)["completed_stages"], 5)

    def test_all_usage_budgets_reject_overrun(self) -> None:
        budget_fields = {
            "elapsed_seconds": 1,
            "tool_calls": 1,
            "model_calls": 1,
            "prompt_tokens": 1,
            "completion_tokens": 1,
        }
        for field, maximum in budget_fields.items():
            with self.subTest(field=field):
                plan_budget = self.budget(**{field: maximum})
                stage_budget = self.budget(**{field: maximum})
                plan_id = self.store.create_plan(
                    self.manifest(
                        budget=plan_budget,
                        stages=self.stages(stage_budget=stage_budget),
                    )
                )
                claim = self.store.claim_next_stage(plan_id, worker_id="worker-a")
                self.assertIsNotNone(claim)
                bad_usage = usage(**{field: maximum + 1})
                with self.assertRaises(LongHorizonBudgetError):
                    self.store.reserve_stage_usage(
                        plan_id,
                        claim["stage_id"],
                        worker_id="worker-a",
                        lease_token=claim["lease_token"],
                        usage=bad_usage,
                    )

    def test_elapsed_wall_clock_budget_is_enforced(self) -> None:
        plan_id = self.store.create_plan(self.manifest(budget=self.budget(elapsed_seconds=1)))
        old = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        self.memory.db.execute("UPDATE long_horizon_plans SET created_at=? WHERE id=?", (old, plan_id))
        self.store._seal_all_states_locked(plan_id)
        with self.assertRaises(LongHorizonBudgetError):
            self.store.claim_next_stage(plan_id, worker_id="worker-a")

    def test_two_workers_cannot_claim_same_stage(self) -> None:
        plan_id = self.store.create_plan(self.manifest())
        barrier = threading.Barrier(2)
        results: list[dict | None] = []
        errors: list[BaseException] = []

        def claim(worker: str) -> None:
            memory = Memory(self.path, worker_id=f"memory-{worker}")
            try:
                store = LongHorizonStore(
                    memory, project_id=1, worker_id=worker,
                    integrity_key=b"phase5-test-integrity-key-00001!",
                )
                barrier.wait()
                results.append(
                    store.claim_next_stage(plan_id, worker_id=worker, lease_seconds=60)
                )
            except BaseException as exc:  # surfaced in the main test thread
                errors.append(exc)
            finally:
                memory.close()

        threads = [
            threading.Thread(target=claim, args=("worker-a",)),
            threading.Thread(target=claim, args=("worker-b",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(sum(item is not None for item in results), 1)

    def test_expired_nonmutation_lease_recovers_and_consumes_retry(self) -> None:
        plan_id = self.store.create_plan(self.manifest())
        first = self.store.claim_next_stage(plan_id, worker_id="worker-a")
        self.memory.db.execute(
            "UPDATE long_horizon_stages SET lease_expires_at=? WHERE id=?",
            ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), first["stage_id"]),
        )
        self.store._seal_all_states_locked(plan_id)
        recovered = self.store.claim_next_stage(plan_id, worker_id="worker-b")
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered["attempt_count"], 2)
        self.assertEqual(self.store.show_plan(plan_id)["usage"]["retries"], 1)

    def test_retry_exhaustion_persists_terminal_failure(self) -> None:
        plan_id = self.store.create_plan(
            self.manifest(
                budget=self.budget(retries=0),
                stages=self.stages(stage_budget=self.budget(retries=0)),
            )
        )
        first = self.store.claim_next_stage(plan_id, worker_id="worker-a")
        self.memory.db.execute(
            "UPDATE long_horizon_stages SET lease_expires_at=? WHERE id=?",
            ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), first["stage_id"]),
        )
        self.store._seal_all_states_locked(plan_id)
        with self.assertRaises(LongHorizonBudgetError):
            self.store.claim_next_stage(plan_id, worker_id="worker-b")
        row = self.memory.db.execute("SELECT status FROM long_horizon_plans WHERE id=?", (plan_id,)).fetchone()
        self.assertEqual(row["status"], "failed")

    def test_mutation_crash_window_never_blindly_replays(self) -> None:
        plan_id = self.store.create_plan(self.manifest(mutation=True))
        self.complete_claim(plan_id)
        self.complete_claim(plan_id)
        claim = self.store.claim_next_stage(plan_id, worker_id="worker-a")
        self.store.reserve_stage_usage(plan_id, claim["stage_id"], worker_id="worker-a", lease_token=claim["lease_token"], usage=usage())
        intent = self.store.record_mutation_intent(
            plan_id,
            claim["stage_id"],
            worker_id="worker-a",
            lease_token=claim["lease_token"],
            executor_id="executor-a",
        )
        self.memory.db.execute(
            "UPDATE long_horizon_stages SET lease_expires_at=? WHERE id=?",
            ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), claim["stage_id"]),
        )
        self.store._seal_all_states_locked(plan_id)
        self.assertIsNone(self.store.claim_next_stage(plan_id, worker_id="worker-b"))
        stage = self.memory.db.execute(
            "SELECT status FROM long_horizon_stages WHERE id=?", (claim["stage_id"],)
        ).fetchone()
        self.assertEqual(stage["status"], "awaiting_reconciliation")
        self.reconcile(plan_id, claim["stage_id"], outcome="applied", evidence="external-observation")
        recovered = self.store.claim_next_stage(plan_id, worker_id="worker-b")
        self.assertEqual(recovered["mutation_state"], "reconciled_applied")
        self.store.reserve_stage_usage(
            plan_id, recovered["stage_id"], worker_id="worker-b",
            lease_token=recovered["lease_token"], usage=usage(),
        )
        self.store.record_checkpoint(
            plan_id,
            recovered["stage_id"],
            worker_id="worker-b",
            lease_token=recovered["lease_token"],
            usage=usage(),
            outcome_sha256=sha("mutation-complete"),
            artifact_sha256=sha("mutation-artifact"),
            executor_id="executor-a",
        )
        count = self.memory.db.execute(
            "SELECT COUNT(*) FROM long_horizon_mutation_receipts WHERE stage_id=? AND event_type='intent'",
            (claim["stage_id"],),
        ).fetchone()[0]
        self.assertEqual(count, 1)
        self.assertEqual(intent["effect_key"], recovered["effect_key"])

    def test_identical_mutation_result_is_idempotent(self) -> None:
        plan_id = self.store.create_plan(self.manifest(mutation=True))
        self.complete_claim(plan_id)
        self.complete_claim(plan_id)
        claim = self.store.claim_next_stage(plan_id, worker_id="worker-a")
        self.store.reserve_stage_usage(plan_id, claim["stage_id"], worker_id="worker-a", lease_token=claim["lease_token"], usage=usage())
        self.store.record_mutation_intent(
            plan_id, claim["stage_id"], worker_id="worker-a",
            lease_token=claim["lease_token"], executor_id="executor-a",
        )
        self.store.authorize_mutation_effect(
            plan_id, claim["stage_id"], worker_id="worker-a",
            lease_token=claim["lease_token"], executor_id="executor-a",
        )
        self.store.consume_mutation_effect_authorization(
            plan_id, claim["stage_id"], worker_id="worker-a",
            lease_token=claim["lease_token"], executor_id="executor-a",
        )
        one = self.store.record_mutation_result(
            plan_id, claim["stage_id"], worker_id="worker-a",
            lease_token=claim["lease_token"], executor_id="executor-a",
            outcome="applied", evidence_sha256=sha("result"),
        )
        two = self.store.record_mutation_result(
            plan_id, claim["stage_id"], worker_id="worker-a",
            lease_token=claim["lease_token"], executor_id="executor-a",
            outcome="applied", evidence_sha256=sha("result"),
        )
        self.assertEqual(one["receipt_sha256"], two["receipt_sha256"])

    def test_pause_and_global_stop_dominate_and_mutation_pause_reconciles(self) -> None:
        plan_id = self.store.create_plan(self.manifest(mutation=True))
        self.complete_claim(plan_id)
        self.complete_claim(plan_id)
        claim = self.store.claim_next_stage(plan_id, worker_id="worker-a")
        self.store.reserve_stage_usage(plan_id, claim["stage_id"], worker_id="worker-a", lease_token=claim["lease_token"], usage=usage())
        self.store.record_mutation_intent(
            plan_id, claim["stage_id"], worker_id="worker-a",
            lease_token=claim["lease_token"], executor_id="executor-a",
        )
        self.store.pause_plan(plan_id, sha("pause"))
        stage = self.memory.db.execute(
            "SELECT status FROM long_horizon_stages WHERE id=?", (claim["stage_id"],)
        ).fetchone()
        self.assertEqual(stage["status"], "awaiting_reconciliation")
        self.store.resume_plan(plan_id)
        self.assertIsNone(self.store.claim_next_stage(plan_id, worker_id="worker-b"))
        self.memory.set_control_state("stopped", "test")
        with self.assertRaises(LongHorizonStateError):
            self.store.claim_next_stage(plan_id, worker_id="worker-b")

    def test_cancellation_is_terminal_and_dominates_claims(self) -> None:
        plan_id = self.store.create_plan(self.manifest())
        status = self.store.cancel_plan(plan_id, sha("cancel"))
        self.assertEqual(status["status"], "cancelled")
        with self.assertRaises(LongHorizonStateError):
            self.store.claim_next_stage(plan_id, worker_id="worker-a")
        with self.assertRaises(LongHorizonStateError):
            self.store.resume_plan(plan_id)

    def test_tampered_resealed_checkpoint_is_quarantined(self) -> None:
        plan_id = self.store.create_plan(self.manifest())
        self.complete_claim(plan_id)
        row = self.memory.db.execute(
            "SELECT id, receipt_json FROM long_horizon_checkpoints WHERE plan_id=?", (plan_id,)
        ).fetchone()
        payload = json.loads(row["receipt_json"])
        payload["unknown"] = "resealed substitution"
        tampered = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        self.memory.db.execute(
            "UPDATE long_horizon_checkpoints SET receipt_json=?, receipt_sha256=? WHERE id=?",
            (tampered, sha(tampered), row["id"]),
        )
        self.memory.db.execute(
            "UPDATE long_horizon_plans SET checkpoint_head_sha256=? WHERE id=?",
            (sha(tampered), plan_id),
        )
        with self.assertRaises(LongHorizonIntegrityError):
            self.store.show_plan(plan_id)
        # Integrity failures are never re-MACed; every later read remains closed.
        with self.assertRaises(LongHorizonIntegrityError):
            self.store.show_plan(plan_id)

    def test_checkpoint_replay_and_out_of_order_substitution_fail_closed(self) -> None:
        plan_id = self.store.create_plan(self.manifest())
        claim = self.complete_claim(plan_id)
        with self.assertRaises(LongHorizonStateError):
            self.store.record_checkpoint(
                plan_id, claim["stage_id"], worker_id="worker-a",
                lease_token=claim["lease_token"], usage=usage(),
                outcome_sha256=sha("other"), artifact_sha256=sha("other-artifact"),
                executor_id="executor-a",
            )
        self.memory.db.execute(
            "UPDATE long_horizon_stages SET ordinal=9 WHERE plan_id=? AND stage_key='plan'",
            (plan_id,),
        )
        with self.assertRaises(LongHorizonIntegrityError):
            self.store.show_plan(plan_id)

    def test_only_independent_verifier_can_complete(self) -> None:
        plan_id = self.store.create_plan(self.manifest())
        for _ in range(5):
            self.complete_claim(plan_id, executor="same-executor")
        with self.assertRaises(LongHorizonStateError):
            self.store.record_final_verification(
                plan_id, authority_id="final", verifier_runtime_sha256=sha("verifier-runtime"),
                evidence_sha256=sha("invalid"), signature_sha256="00" * 64, passed=True,
            )
        failed = self.verify(plan_id, passed=False, evidence="failed-check")
        self.assertFalse(failed["plan_complete"])
        self.assertEqual(self.store.show_plan(plan_id)["status"], "active")
        self.verify(plan_id, passed=True, evidence="passing-check")
        self.assertEqual(self.store.show_plan(plan_id)["status"], "complete")

    def test_core_exposes_no_automatic_callback_executor(self) -> None:
        import jarvis.long_horizon as core
        self.assertFalse(hasattr(core, "LongHorizonRunner"))
        self.assertNotIn("LongHorizonRunner", core.__all__)

    def test_usage_is_charged_before_callback_and_counter_reset_fails_closed(self) -> None:
        plan_id = self.store.create_plan(self.manifest())
        claim = self.store.claim_next_stage(plan_id, worker_id="worker-a")
        self.store.reserve_stage_usage(
            plan_id, claim["stage_id"], worker_id="worker-a",
            lease_token=claim["lease_token"], usage=usage(tool_calls=7),
        )
        self.assertEqual(self.store.show_plan(plan_id)["usage"]["tool_calls"], 7)
        self.memory.db.execute(
            "UPDATE long_horizon_plans SET used_tool_calls=0 WHERE id=?", (plan_id,)
        )
        with self.assertRaises(LongHorizonIntegrityError):
            self.store.show_plan(plan_id)

    def test_deleted_usage_receipt_and_db_only_reseal_fail_closed(self) -> None:
        plan_id = self.store.create_plan(self.manifest())
        claim = self.store.claim_next_stage(plan_id, worker_id="worker-a")
        self.store.reserve_stage_usage(
            plan_id, claim["stage_id"], worker_id="worker-a",
            lease_token=claim["lease_token"], usage=usage(),
        )
        self.memory.db.execute("DELETE FROM long_horizon_usage_reservations WHERE plan_id=?", (plan_id,))
        # An attacker can recompute ordinary SHA digests but not the external-key MAC.
        with self.assertRaises(LongHorizonIntegrityError):
            self.store.show_plan(plan_id)

    def test_clock_rollback_fails_closed(self) -> None:
        plan_id = self.store.create_plan(self.manifest())
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        self.memory.db.execute("UPDATE long_horizon_plans SET clock_floor_at=? WHERE id=?", (future, plan_id))
        self.store._seal_all_states_locked(plan_id)  # simulates a prior trusted transition
        with self.assertRaises(LongHorizonIntegrityError) as caught:
            self.store.claim_next_stage(plan_id, worker_id="worker-a")
        self.assertEqual(caught.exception.reason, "clock_rollback")

    def test_strict_json_rejects_duplicate_keys_nonfinite_and_loose_integers(self) -> None:
        payload = self.manifest().to_payload()
        raw = json.dumps(payload, separators=(",", ":"))
        duplicate = raw[:-1] + ',"project_id":1}'
        with self.assertRaises(LongHorizonValidationError):
            from jarvis.long_horizon import parse_manifest_json
            parse_manifest_json(duplicate)
        payload["project_id"] = True
        with self.assertRaises(LongHorizonValidationError):
            WorkflowManifest.from_value(payload)

    def test_cross_project_store_cannot_read_plan(self) -> None:
        plan_id = self.store.create_plan(self.manifest())
        project_2 = self.memory.add_project("other-scope", "@projects/other-scope")
        other = LongHorizonStore(
            self.memory, project_id=project_2,
            integrity_key=b"phase5-test-integrity-key-00001!",
        )
        with self.assertRaises(LongHorizonValidationError):
            other.show_plan(plan_id)

    def test_mutation_requires_fresh_approval_and_applied_is_monotonic(self) -> None:
        calls: list[str] = []
        self.store._approval_validator = lambda phase, context: (
            calls.append(phase) or {"approved": True, "receipt_sha256": sha(f"{phase}:{context['effect_key']}")}
        )
        plan_id = self.store.create_plan(self.manifest(mutation=True))
        self.complete_claim(plan_id); self.complete_claim(plan_id)
        claim = self.store.claim_next_stage(plan_id, worker_id="worker-a")
        self.store.reserve_stage_usage(plan_id, claim["stage_id"], worker_id="worker-a", lease_token=claim["lease_token"], usage=usage())
        self.store.record_mutation_intent(plan_id, claim["stage_id"], worker_id="worker-a", lease_token=claim["lease_token"], executor_id="executor-a")
        self.store.authorize_mutation_effect(plan_id, claim["stage_id"], worker_id="worker-a", lease_token=claim["lease_token"], executor_id="executor-a")
        self.store.consume_mutation_effect_authorization(plan_id, claim["stage_id"], worker_id="worker-a", lease_token=claim["lease_token"], executor_id="executor-a")
        self.assertEqual(calls, ["intent", "pre_effect", "pre_effect"])
        self.store.record_mutation_result(plan_id, claim["stage_id"], worker_id="worker-a", lease_token=claim["lease_token"], executor_id="executor-a", outcome="applied", evidence_sha256=sha("applied"))
        with self.assertRaises(LongHorizonIntegrityError):
            self.reconcile(plan_id, claim["stage_id"], outcome="not_applied", evidence="conflict")

    def test_not_applied_consumes_retry_and_allows_fresh_generation(self) -> None:
        plan_id = self.store.create_plan(self.manifest(mutation=True))
        self.complete_claim(plan_id); self.complete_claim(plan_id)
        first = self.store.claim_next_stage(plan_id, worker_id="worker-a")
        self.store.reserve_stage_usage(plan_id, first["stage_id"], worker_id="worker-a", lease_token=first["lease_token"], usage=usage())
        self.store.record_mutation_intent(plan_id, first["stage_id"], worker_id="worker-a", lease_token=first["lease_token"], executor_id="executor-a")
        self.store.authorize_mutation_effect(plan_id, first["stage_id"], worker_id="worker-a", lease_token=first["lease_token"], executor_id="executor-a")
        self.store.consume_mutation_effect_authorization(plan_id, first["stage_id"], worker_id="worker-a", lease_token=first["lease_token"], executor_id="executor-a")
        self.store.record_mutation_result(plan_id, first["stage_id"], worker_id="worker-a", lease_token=first["lease_token"], executor_id="executor-a", outcome="not_applied", evidence_sha256=sha("not-applied"))
        second = self.store.claim_next_stage(plan_id, worker_id="worker-a")
        self.assertEqual(second["attempt_count"], 2)
        self.store.reserve_stage_usage(plan_id, second["stage_id"], worker_id="worker-a", lease_token=second["lease_token"], usage=usage())
        intent = self.store.record_mutation_intent(plan_id, second["stage_id"], worker_id="worker-a", lease_token=second["lease_token"], executor_id="executor-a")
        self.assertEqual(intent["generation"], 2)
        stage = self.memory.db.execute(
            "SELECT status, mutation_state FROM long_horizon_stages "
            "WHERE plan_id=? AND ordinal=3",
            (plan_id,),
        ).fetchone()
        self.assertEqual((stage["status"], stage["mutation_state"]), ("claimed", "intent_recorded"))

    def test_effect_permit_is_one_shot_and_authorized_lease_cannot_renew(self) -> None:
        plan_id = self.store.create_plan(self.manifest(mutation=True))
        self.complete_claim(plan_id); self.complete_claim(plan_id)
        claim = self.store.claim_next_stage(plan_id, worker_id="worker-a")
        self.store.reserve_stage_usage(plan_id, claim["stage_id"], worker_id="worker-a", lease_token=claim["lease_token"], usage=usage())
        self.store.record_mutation_intent(plan_id, claim["stage_id"], worker_id="worker-a", lease_token=claim["lease_token"], executor_id="executor-a")
        self.store.authorize_mutation_effect(plan_id, claim["stage_id"], worker_id="worker-a", lease_token=claim["lease_token"], executor_id="executor-a")
        with self.assertRaises(LongHorizonStateError):
            self.store.renew_stage_lease(plan_id, claim["stage_id"], worker_id="worker-a", lease_token=claim["lease_token"])
        self.store.consume_mutation_effect_authorization(plan_id, claim["stage_id"], worker_id="worker-a", lease_token=claim["lease_token"], executor_id="executor-a")
        with self.assertRaises(LongHorizonStateError):
            self.store.consume_mutation_effect_authorization(plan_id, claim["stage_id"], worker_id="worker-a", lease_token=claim["lease_token"], executor_id="executor-a")

    def test_authorized_and_consumed_effect_crashes_require_reconciliation(self) -> None:
        for consumed in (False, True):
            with self.subTest(consumed=consumed):
                manifest = WorkflowManifest.from_value({
                    **self.manifest(mutation=True).to_payload(),
                    "goal_sha256": sha(f"crash-{consumed}"),
                })
                plan_id = self.store.create_plan(manifest)
                self.complete_claim(plan_id); self.complete_claim(plan_id)
                claim = self.store.claim_next_stage(plan_id, worker_id="worker-a")
                self.store.reserve_stage_usage(plan_id, claim["stage_id"], worker_id="worker-a", lease_token=claim["lease_token"], usage=usage())
                self.store.record_mutation_intent(plan_id, claim["stage_id"], worker_id="worker-a", lease_token=claim["lease_token"], executor_id="executor-a")
                self.store.authorize_mutation_effect(plan_id, claim["stage_id"], worker_id="worker-a", lease_token=claim["lease_token"], executor_id="executor-a")
                if consumed:
                    self.store.consume_mutation_effect_authorization(plan_id, claim["stage_id"], worker_id="worker-a", lease_token=claim["lease_token"], executor_id="executor-a")
                self.memory.db.execute(
                    "UPDATE long_horizon_stages SET lease_expires_at=? WHERE id=?",
                    ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), claim["stage_id"]),
                )
                self.store._seal_all_states_locked(plan_id)
                self.assertIsNone(self.store.claim_next_stage(plan_id, worker_id="worker-b"))
                stage = self.memory.db.execute(
                    "SELECT status,attempt_count FROM long_horizon_stages WHERE id=?", (claim["stage_id"],)
                ).fetchone()
                self.assertEqual(stage["status"], "awaiting_reconciliation")
                self.assertEqual(stage["attempt_count"], 1)
                if consumed:
                    self.reconcile(
                        plan_id, claim["stage_id"], outcome="applied",
                        evidence="external-effect-confirmed",
                    )
                    recovered = self.store.claim_next_stage(plan_id, worker_id="worker-b")
                    self.assertEqual(recovered["mutation_state"], "reconciled_applied")

    def test_pause_after_consumed_permit_can_only_reconcile(self) -> None:
        plan_id = self.store.create_plan(WorkflowManifest.from_value({
            **self.manifest(mutation=True).to_payload(), "goal_sha256": sha("pause-consumed"),
        }))
        self.complete_claim(plan_id); self.complete_claim(plan_id)
        claim = self.store.claim_next_stage(plan_id, worker_id="worker-a")
        self.store.reserve_stage_usage(plan_id, claim["stage_id"], worker_id="worker-a", lease_token=claim["lease_token"], usage=usage())
        self.store.record_mutation_intent(plan_id, claim["stage_id"], worker_id="worker-a", lease_token=claim["lease_token"], executor_id="executor-a")
        self.store.authorize_mutation_effect(plan_id, claim["stage_id"], worker_id="worker-a", lease_token=claim["lease_token"], executor_id="executor-a")
        self.store.consume_mutation_effect_authorization(plan_id, claim["stage_id"], worker_id="worker-a", lease_token=claim["lease_token"], executor_id="executor-a")
        self.store.pause_plan(plan_id, sha("pause-after-permit"))
        self.assertEqual(self.memory.db.execute("SELECT status FROM long_horizon_stages WHERE id=?", (claim["stage_id"],)).fetchone()[0], "awaiting_reconciliation")
        self.store.resume_plan(plan_id)
        self.reconcile(plan_id, claim["stage_id"], outcome="applied", evidence="confirmed-after-pause")
        recovered = self.store.claim_next_stage(plan_id, worker_id="worker-b")
        self.assertEqual(recovered["mutation_state"], "reconciled_applied")

    def test_reopen_never_launders_tampered_state(self) -> None:
        plan_id = self.store.create_plan(self.manifest())
        self.memory.db.execute("UPDATE long_horizon_plans SET used_tool_calls=99 WHERE id=?", (plan_id,))
        self.memory.close()
        self.memory = Memory(self.path, worker_id="tamper-reopen")
        with self.assertRaises(LongHorizonIntegrityError):
            LongHorizonStore(
                self.memory, project_id=1,
                integrity_key=b"phase5-test-integrity-key-00001!",
            )

    def test_terminal_state_tampering_is_never_laundered_on_repeated_reads(self) -> None:
        cancelled = self.store.create_plan(self.manifest())
        self.store.cancel_plan(cancelled, sha("cancel-terminal"))
        complete = self.store.create_plan(
            WorkflowManifest.from_value({
                **self.manifest().to_payload(),
                "goal_sha256": sha("second-goal"),
            })
        )
        for _ in range(5):
            self.complete_claim(complete)
        self.verify(complete, passed=True, evidence="terminal-proof")
        for plan_id in (cancelled, complete):
            with self.subTest(plan_id=plan_id):
                self.memory.db.execute(
                    "UPDATE long_horizon_plans SET updated_at=? WHERE id=?",
                    ("2000-01-01T00:00:00+00:00", plan_id),
                )
                for _ in range(2):
                    with self.assertRaises(LongHorizonIntegrityError):
                        self.store.show_plan(plan_id)

    def test_zero_retry_not_applied_result_is_stable_failed_state(self) -> None:
        zero = self.budget(retries=0)
        plan_id = self.store.create_plan(self.manifest(
            mutation=True, budget=zero, stages=self.stages(mutation=True, stage_budget=zero),
        ))
        self.complete_claim(plan_id); self.complete_claim(plan_id)
        claim = self.store.claim_next_stage(plan_id, worker_id="worker-a")
        self.store.reserve_stage_usage(plan_id, claim["stage_id"], worker_id="worker-a", lease_token=claim["lease_token"], usage=usage())
        self.store.record_mutation_intent(plan_id, claim["stage_id"], worker_id="worker-a", lease_token=claim["lease_token"], executor_id="executor-a")
        self.store.authorize_mutation_effect(plan_id, claim["stage_id"], worker_id="worker-a", lease_token=claim["lease_token"], executor_id="executor-a")
        self.store.consume_mutation_effect_authorization(plan_id, claim["stage_id"], worker_id="worker-a", lease_token=claim["lease_token"], executor_id="executor-a")
        with self.assertRaises(LongHorizonBudgetError):
            self.store.record_mutation_result(plan_id, claim["stage_id"], worker_id="worker-a", lease_token=claim["lease_token"], executor_id="executor-a", outcome="not_applied", evidence_sha256=sha("no-effect"))
        for _ in range(2):
            self.assertEqual(self.store.show_plan(plan_id)["status"], "failed")
        with self.assertRaises(LongHorizonStateError):
            self.store.pause_plan(plan_id, sha("cannot-pause-failed"))

    def test_zero_retry_not_applied_reconciliation_is_stable_failed_state(self) -> None:
        zero = self.budget(retries=0)
        plan_id = self.store.create_plan(self.manifest(
            mutation=True, budget=zero, stages=self.stages(mutation=True, stage_budget=zero),
        ))
        self.complete_claim(plan_id); self.complete_claim(plan_id)
        claim = self.store.claim_next_stage(plan_id, worker_id="worker-a")
        self.store.reserve_stage_usage(plan_id, claim["stage_id"], worker_id="worker-a", lease_token=claim["lease_token"], usage=usage())
        self.store.record_mutation_intent(plan_id, claim["stage_id"], worker_id="worker-a", lease_token=claim["lease_token"], executor_id="executor-a")
        with self.assertRaises(LongHorizonBudgetError):
            self.reconcile(plan_id, claim["stage_id"], outcome="not_applied", evidence="confirmed-no-effect")
        for _ in range(2):
            self.assertEqual(self.store.show_plan(plan_id)["status"], "failed")
        with self.assertRaises(LongHorizonStateError):
            self.store.resume_plan(plan_id)

    def test_uncertain_reconciliation_rounds_resolve_applied_after_restart(self) -> None:
        plan_id = self.store.create_plan(WorkflowManifest.from_value({
            **self.manifest(mutation=True).to_payload(), "goal_sha256": sha("rounds-applied"),
        }))
        self.complete_claim(plan_id); self.complete_claim(plan_id)
        claim = self.store.claim_next_stage(plan_id, worker_id="worker-a")
        self.store.reserve_stage_usage(plan_id, claim["stage_id"], worker_id="worker-a", lease_token=claim["lease_token"], usage=usage())
        self.store.record_mutation_intent(plan_id, claim["stage_id"], worker_id="worker-a", lease_token=claim["lease_token"], executor_id="executor-a")
        first = self.reconcile(plan_id, claim["stage_id"], outcome="uncertain", evidence="round-one")
        self.assertEqual(first["reconciliation_round"], 1)
        self.reopen_store()
        second = self.reconcile(plan_id, claim["stage_id"], outcome="uncertain", evidence="round-two")
        final = self.reconcile(plan_id, claim["stage_id"], outcome="applied", evidence="round-three")
        self.assertEqual((second["reconciliation_round"], final["reconciliation_round"]), (2, 3))
        self.assertEqual(self.store.show_plan(plan_id)["mutation_state"]["execute"], "reconciled_applied")

    def test_uncertain_reconciliation_can_resolve_not_applied(self) -> None:
        plan_id = self.store.create_plan(WorkflowManifest.from_value({
            **self.manifest(mutation=True).to_payload(), "goal_sha256": sha("rounds-not-applied"),
        }))
        self.complete_claim(plan_id); self.complete_claim(plan_id)
        claim = self.store.claim_next_stage(plan_id, worker_id="worker-a")
        self.store.reserve_stage_usage(plan_id, claim["stage_id"], worker_id="worker-a", lease_token=claim["lease_token"], usage=usage())
        self.store.record_mutation_intent(plan_id, claim["stage_id"], worker_id="worker-a", lease_token=claim["lease_token"], executor_id="executor-a")
        first = self.reconcile(plan_id, claim["stage_id"], outcome="uncertain", evidence="still-unknown")
        second = self.reconcile(plan_id, claim["stage_id"], outcome="not_applied", evidence="confirmed-absent")
        self.assertEqual((first["reconciliation_round"], second["reconciliation_round"]), (1, 2))
        self.reopen_store()
        self.assertEqual(self.store.show_plan(plan_id)["mutation_state"]["execute"], "reconciled_not_applied")

    def test_cancel_after_consumed_effect_preserves_reconciliation_path(self) -> None:
        plan_id = self.store.create_plan(WorkflowManifest.from_value({
            **self.manifest(mutation=True).to_payload(), "goal_sha256": sha("cancel-consumed"),
        }))
        self.complete_claim(plan_id); self.complete_claim(plan_id)
        claim = self.store.claim_next_stage(plan_id, worker_id="worker-a")
        self.store.reserve_stage_usage(plan_id, claim["stage_id"], worker_id="worker-a", lease_token=claim["lease_token"], usage=usage())
        self.store.record_mutation_intent(plan_id, claim["stage_id"], worker_id="worker-a", lease_token=claim["lease_token"], executor_id="executor-a")
        self.store.authorize_mutation_effect(plan_id, claim["stage_id"], worker_id="worker-a", lease_token=claim["lease_token"], executor_id="executor-a")
        self.store.consume_mutation_effect_authorization(plan_id, claim["stage_id"], worker_id="worker-a", lease_token=claim["lease_token"], executor_id="executor-a")
        self.store.cancel_plan(plan_id, sha("cancel-after-consume"))
        self.reconcile(plan_id, claim["stage_id"], outcome="applied", evidence="confirmed-after-cancel")
        status = self.store.show_plan(plan_id)
        self.assertEqual(status["status"], "cancelled")
        self.assertEqual(status["mutation_state"]["execute"], "reconciled_applied")

    def test_reconciliation_authority_cannot_change_between_rounds(self) -> None:
        plan_id = self.store.create_plan(WorkflowManifest.from_value({
            **self.manifest(mutation=True).to_payload(), "goal_sha256": sha("authority-round"),
        }))
        self.complete_claim(plan_id); self.complete_claim(plan_id)
        claim = self.store.claim_next_stage(plan_id, worker_id="worker-a")
        self.store.reserve_stage_usage(plan_id, claim["stage_id"], worker_id="worker-a", lease_token=claim["lease_token"], usage=usage())
        self.store.record_mutation_intent(plan_id, claim["stage_id"], worker_id="worker-a", lease_token=claim["lease_token"], executor_id="executor-a")
        self.reconcile(plan_id, claim["stage_id"], outcome="uncertain", evidence="first-authority")
        other_private = Ed25519PrivateKey.generate()
        other_public = other_private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw,
        )
        self.store._authorities["other"] = {
            "scope": "mutation_reconciliation", "verifier_id": "other-reconciler",
            "runtime_sha256": sha("other-runtime"), "public_key": other_public,
        }
        with self.assertRaisesRegex(LongHorizonStateError, "cannot change"):
            self.store.reconcile_mutation(
                plan_id, claim["stage_id"], authority_id="other",
                reconciler_runtime_sha256=sha("other-runtime"), outcome="applied",
                evidence_sha256=sha("other-evidence"), signature_sha256="00" * 64,
            )

    def test_pause_abandonment_consumes_retry_and_zero_budget_fails_durably(self) -> None:
        zero = self.budget(retries=0)
        plan_id = self.store.create_plan(self.manifest(
            budget=zero, stages=self.stages(stage_budget=zero),
        ))
        self.store.claim_next_stage(plan_id, worker_id="worker-a")
        with self.assertRaises(LongHorizonBudgetError):
            self.store.pause_plan(plan_id, sha("pause-zero-retry"))
        self.assertEqual(self.store.show_plan(plan_id)["status"], "failed")
        self.reopen_store()
        self.assertEqual(self.store.show_plan(plan_id)["status"], "failed")
        with self.assertRaises(LongHorizonStateError):
            self.store.resume_plan(plan_id)

    def test_deleted_checkpoint_reservation_is_controlled_persistent_failure(self) -> None:
        plan_id = self.store.create_plan(WorkflowManifest.from_value({
            **self.manifest().to_payload(), "goal_sha256": sha("deleted-checkpoint-reservation"),
        }))
        self.complete_claim(plan_id)
        self.memory.db.execute(
            "DELETE FROM long_horizon_usage_reservations WHERE plan_id=?", (plan_id,)
        )
        for _ in range(2):
            with self.assertRaises(LongHorizonIntegrityError) as caught:
                self.store.show_plan(plan_id)
            self.assertEqual(caught.exception.reason, "checkpoint_reservation_missing")

    def test_file_sidecar_creation_and_missing_key_fail_existing_state(self) -> None:
        other_path = Path(__file__).parent / f".long-horizon-sidecar-{uuid4().hex}.db"
        key_path = Path(str(other_path) + ".long-horizon.key")
        memory = Memory(other_path, worker_id="sidecar-create")
        conversation = memory.new_conversation("sidecar", project_id=1)
        task = memory.add_task("sidecar", project_id=1)
        store = LongHorizonStore(memory, project_id=1)
        manifest = WorkflowManifest(
            project_id=1, conversation_id=conversation, task_id=task,
            goal_sha256=sha("sidecar-goal"), contract_sha256=sha("sidecar-contract"),
            constraints_sha256=sha("sidecar-constraints"), approval_scope_sha256=sha("sidecar-approval"),
            artifact_set_sha256=sha("sidecar-artifacts"), budget=self.budget(), stages=self.stages(),
        )
        store.create_plan(manifest)
        self.assertEqual(len(key_path.read_bytes()), 32)
        memory.close()
        key_path.unlink()
        reopened = Memory(other_path, worker_id="sidecar-missing")
        try:
            with self.assertRaises(LongHorizonIntegrityError):
                LongHorizonStore(reopened, project_id=1)
        finally:
            reopened.close()
            for suffix in ("", "-wal", "-shm", ".long-horizon.key"):
                candidate = Path(str(other_path) + suffix)
                if candidate.exists():
                    candidate.unlink()

    def test_signed_receipts_require_same_external_authority_configuration(self) -> None:
        plan_id = self.store.create_plan(WorkflowManifest.from_value({
            **self.manifest().to_payload(), "goal_sha256": sha("authority-reopen"),
        }))
        for _ in range(5): self.complete_claim(plan_id)
        self.verify(plan_id, passed=True, evidence="authority-bound")
        self.memory.close()
        self.memory = Memory(self.path, worker_id="authority-missing")
        store = LongHorizonStore(
            self.memory, project_id=1,
            integrity_key=b"phase5-test-integrity-key-00001!",
        )
        with self.assertRaises(LongHorizonStateError):
            store.show_plan(plan_id)

    def test_nonregular_and_symlink_sidecar_paths_fail_closed(self) -> None:
        other_path = Path(__file__).parent / f".long-horizon-keypath-{uuid4().hex}.db"
        key_path = Path(str(other_path) + ".long-horizon.key")
        memory = Memory(other_path, worker_id="keypath-test")
        key_path.mkdir()
        try:
            with self.assertRaises(LongHorizonIntegrityError):
                LongHorizonStore(memory, project_id=1)
        finally:
            key_path.rmdir()
            memory.close()
        target = Path(str(other_path) + ".target")
        target.write_bytes(b"x" * 32)
        memory = Memory(other_path, worker_id="symlink-test")
        try:
            try:
                os.symlink(target, key_path)
            except OSError:
                pass  # Windows may deny symlink creation without developer mode.
            else:
                with self.assertRaises(LongHorizonIntegrityError):
                    LongHorizonStore(memory, project_id=1)
                key_path.unlink()
        finally:
            memory.close()
            target.unlink(missing_ok=True)
            for suffix in ("", "-wal", "-shm"):
                Path(str(other_path) + suffix).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
