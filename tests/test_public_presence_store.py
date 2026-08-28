from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from jarvis.public_bridge import (
    ApprovedProjectSummary,
    PrivateDataRejected,
    PublicBridgeObject,
    PublicProvenance,
    public_bridge_payload_digest,
)
from jarvis.public_presence_store import (
    ApprovalError,
    ApprovalExpired,
    ApprovalMismatch,
    ApprovalReplay,
    IdempotencyConflict,
    PublicPresenceStopped,
    PUBLIC_PRESENCE_APPLICATION_ID,
    PUBLIC_PRESENCE_SCHEMA_VERSION,
    PublicPresenceStore,
    PublicPresenceStoreError,
)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class PublicPresenceStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "public_presence.db"
        self.store = PublicPresenceStore(self.path)
        self.now = time.time()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _approval(self, **overrides: object) -> dict[str, object]:
        values: dict[str, object] = {
            "exact_text": "Verified public test update.",
            "media_hashes": (_hash("image"),),
            "source_hashes": (_hash("source"),),
            "idempotency_key": "attempt:1",
            "platform": "simulation",
            "destination": "feed:public",
            "account_id": "account:jarvis",
            "reply_target": None,
            "expires_at": self.now + 600,
            "now": self.now,
        }
        values.update(overrides)
        return self.store.create_approval(**values)  # type: ignore[arg-type]

    def _reserve(self, approval_id: str, **overrides: object) -> dict[str, object]:
        values: dict[str, object] = {
            "approval_id": approval_id,
            "idempotency_key": "attempt:1",
            "exact_text": "Verified public test update.",
            "media_hashes": (_hash("image"),),
            "source_hashes": (_hash("source"),),
            "platform": "simulation",
            "destination": "feed:public",
            "account_id": "account:jarvis",
            "reply_target": None,
            "now": self.now + 2,
        }
        values.update(overrides)
        return self.store.reserve_approved_action(**values)  # type: ignore[arg-type]

    def _ready(self) -> None:
        self.store.set_enabled(True)
        self.store.set_paused(False)

    def test_only_separate_public_database_name_is_allowed(self) -> None:
        with self.assertRaisesRegex(ValueError, "public_presence.db"):
            PublicPresenceStore(Path(self.temp.name) / "jarvis.db")

    def test_schema_is_marked_public_and_contains_no_private_memory_tables(self) -> None:
        db = sqlite3.connect(self.path)
        try:
            application_id = int(db.execute("PRAGMA application_id").fetchone()[0])
            user_version = int(db.execute("PRAGMA user_version").fetchone()[0])
            tables = {
                str(row[0])
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        finally:
            db.close()
        self.assertEqual(application_id, PUBLIC_PRESENCE_APPLICATION_ID)
        self.assertEqual(user_version, PUBLIC_PRESENCE_SCHEMA_VERSION)
        self.assertIn("public_control_state", tables)
        self.assertTrue(all(name.startswith(("public_", "sqlite_")) for name in tables))
        self.assertFalse({"memories", "claims", "conversations", "tasks"} & tables)

    def test_defaults_fail_closed_and_pause_kill_persist_across_restart(self) -> None:
        self.assertEqual(self.store.status()["effective_state"], "disabled")
        approval = self._approval()
        self.store.decide_approval(str(approval["approval_id"]), True, now=self.now + 1)
        with self.assertRaises(PublicPresenceStopped):
            self._reserve(str(approval["approval_id"]))

        self._ready()
        self.assertTrue(self.store.status()["can_external_action"])
        self.store.emergency_stop()
        restarted = PublicPresenceStore(self.path)
        self.assertEqual(restarted.status()["effective_state"], "emergency_stopped")
        with self.assertRaises(PublicPresenceStopped):
            restarted.set_enabled(True)
        cleared = restarted.clear_emergency_stop()
        self.assertFalse(cleared["enabled"])
        self.assertTrue(cleared["paused"])
        self.assertFalse(cleared["emergency_stopped"])

    def test_bridge_object_is_digest_bound_idempotent_and_restart_safe(self) -> None:
        payload = ApprovedProjectSummary(
            project_id="project:1", title="Project", summary="Safe public project summary."
        )
        provenance = (
            PublicProvenance(
                source_kind="operator_approval",
                source_id="approval:bridge",
                observed_at=self.now,
                content_sha256=public_bridge_payload_digest(payload),
            ),
        )
        value = PublicBridgeObject(
            bridge_id="bridge:project",
            payload=payload,
            provenance=provenance,
            confidence=1.0,
            created_at=self.now,
            expires_at=self.now + 600,
        )
        with self.assertRaisesRegex(ApprovalError, "trusted operator"):
            self.store.accept_bridge_object(value, now=self.now + 1)
        requested = self.store.request_bridge_authorization(value, now=self.now)
        self.assertEqual(requested["status"], "pending")
        self.store.decide_bridge_authorization(
            "approval:bridge", True, now=self.now + 0.5
        )
        first = self.store.accept_bridge_object(value, now=self.now + 1)
        second = self.store.accept_bridge_object(value, now=self.now + 2)
        self.assertEqual(first["record_sha256"], second["record_sha256"])
        restored = PublicPresenceStore(self.path).get_bridge_object(
            "bridge:project", now=self.now + 3
        )
        self.assertEqual(restored, value)

        substituted_payload = ApprovedProjectSummary(
            project_id="project:1", title="Project", summary="Different safe summary."
        )
        substituted = PublicBridgeObject(
            bridge_id="bridge:project",
            payload=substituted_payload,
            provenance=(PublicProvenance(
                source_kind="operator_approval",
                source_id="approval:bridge",
                observed_at=self.now,
                content_sha256=public_bridge_payload_digest(substituted_payload),
            ),),
            confidence=1.0,
            created_at=self.now,
            expires_at=self.now + 600,
        )
        with self.assertRaises(IdempotencyConflict):
            self.store.accept_bridge_object(substituted, now=self.now + 2)

        second_payload = ApprovedProjectSummary(
            project_id="project:2", title="Second", summary="Another safe summary."
        )
        second = PublicBridgeObject(
            bridge_id="bridge:second",
            payload=second_payload,
            provenance=(PublicProvenance(
                source_kind="operator_approval",
                source_id="approval:second",
                observed_at=self.now,
                content_sha256=public_bridge_payload_digest(second_payload),
            ),),
            confidence=1.0,
            created_at=self.now,
            expires_at=self.now + 600,
        )
        self.store.request_bridge_authorization(second, now=self.now)
        self.store.decide_bridge_authorization(
            "approval:second", True, now=self.now + 0.5
        )
        self.store.accept_bridge_object(second, now=self.now + 1)
        db = sqlite3.connect(self.path)
        try:
            swapped = db.execute(
                "SELECT record_json, record_sha256 FROM public_bridge_inbox WHERE bridge_id=?",
                ("bridge:second",),
            ).fetchone()
            db.execute(
                "DELETE FROM public_bridge_inbox WHERE bridge_id=?",
                ("bridge:second",),
            )
            db.execute(
                """UPDATE public_bridge_inbox
                   SET record_json=?, record_sha256=? WHERE bridge_id=?""",
                (*swapped, "bridge:project"),
            )
            db.commit()
        finally:
            db.close()
        with self.assertRaisesRegex(PublicPresenceStoreError, "identity"):
            self.store.get_bridge_object("bridge:project", now=self.now + 3)

    def test_bridge_authorization_is_exact_one_shot_and_restart_safe(self) -> None:
        payload = ApprovedProjectSummary(
            project_id="project:authorized",
            title="Authorized project",
            summary="A sanitized summary approved for the public bridge.",
        )
        value = PublicBridgeObject(
            bridge_id="bridge:authorized",
            payload=payload,
            provenance=(PublicProvenance(
                source_kind="operator_approval",
                source_id="approval:authorized",
                observed_at=self.now,
                content_sha256=public_bridge_payload_digest(payload),
            ),),
            confidence=1.0,
            created_at=self.now,
            expires_at=self.now + 600,
        )
        self.store.request_bridge_authorization(value, now=self.now)
        with self.assertRaisesRegex(ApprovalError, "pending"):
            self.store.accept_bridge_object(value, now=self.now + 1)
        self.store.decide_bridge_authorization(
            "approval:authorized", True, now=self.now + 2
        )

        accepted = PublicPresenceStore(self.path).accept_bridge_object(
            value, now=self.now + 3
        )
        repeated = self.store.accept_bridge_object(value, now=self.now + 4)
        self.assertEqual(accepted["record_sha256"], repeated["record_sha256"])

        different_payload = ApprovedProjectSummary(
            project_id="project:authorized",
            title="Authorized project",
            summary="A substituted public summary.",
        )
        substituted = PublicBridgeObject(
            bridge_id="bridge:substituted",
            payload=different_payload,
            provenance=(PublicProvenance(
                source_kind="operator_approval",
                source_id="approval:authorized",
                observed_at=self.now,
                content_sha256=public_bridge_payload_digest(different_payload),
            ),),
            confidence=1.0,
            created_at=self.now,
            expires_at=self.now + 600,
        )
        with self.assertRaises(ApprovalMismatch):
            self.store.accept_bridge_object(substituted, now=self.now + 5)

        stop_payload = ApprovedProjectSummary(
            project_id="project:stopped",
            title="Stopped project",
            summary="This bridge intake must stop with the public kill switch.",
        )
        stopped = PublicBridgeObject(
            bridge_id="bridge:stopped",
            payload=stop_payload,
            provenance=(PublicProvenance(
                source_kind="operator_approval",
                source_id="approval:stopped",
                observed_at=self.now,
                content_sha256=public_bridge_payload_digest(stop_payload),
            ),),
            confidence=1.0,
            created_at=self.now,
            expires_at=self.now + 600,
        )
        self.store.request_bridge_authorization(stopped, now=self.now)
        self.store.decide_bridge_authorization(
            "approval:stopped", True, now=self.now + 1
        )
        self.store.emergency_stop()
        with self.assertRaises(PublicPresenceStopped):
            self.store.accept_bridge_object(stopped, now=self.now + 6)
        receipts = self.store.list_audit_receipts()
        self.assertTrue(any(
            receipt["event_type"] == "bridge.rejected"
            and receipt["outcome"] == "blocked"
            for receipt in receipts
        ))

    def test_private_data_is_rejected_before_approval_persistence(self) -> None:
        with self.assertRaises(PrivateDataRejected):
            self._approval(exact_text="My API_KEY=sk-proj-abcdefghijklmnop")
        db = sqlite3.connect(self.path)
        try:
            count = db.execute("SELECT COUNT(*) FROM public_approvals").fetchone()[0]
        finally:
            db.close()
        self.assertEqual(count, 0)

    def test_exact_approval_blocks_every_substitution_dimension(self) -> None:
        substitutions = (
            {"exact_text": "Changed public text."},
            {"media_hashes": (_hash("different image"),)},
            {"source_hashes": (_hash("different source"),)},
            {"platform": "moltbook"},
            {"destination": "feed:other"},
            {"account_id": "account:other"},
            {"reply_target": "thread:other"},
        )
        self._ready()
        for index, substitution in enumerate(substitutions):
            with self.subTest(substitution=substitution):
                idempotency_key = f"attempt:substitution:{index}"
                approval = self._approval(idempotency_key=idempotency_key)
                approval_id = str(approval["approval_id"])
                self.store.decide_approval(approval_id, True, now=self.now + 1)
                with self.assertRaises(ApprovalMismatch):
                    self._reserve(
                        approval_id,
                        idempotency_key=idempotency_key,
                        **substitution,
                    )
                self.assertEqual(self.store.get_approval(approval_id)["status"], "approved")

    def test_one_shot_approval_and_idempotency_prevent_duplicate_simulation(self) -> None:
        self._ready()
        approval = self._approval()
        approval_id = str(approval["approval_id"])
        self.store.decide_approval(approval_id, True, now=self.now + 1)
        first = self._reserve(approval_id)
        retry = self._reserve(approval_id)
        self.assertEqual(first["reservation_id"], retry["reservation_id"])

        self.store.emergency_stop()
        with self.assertRaises(PublicPresenceStopped):
            self._reserve(approval_id)
        self.store.clear_emergency_stop()
        self._ready()

        with self.assertRaises(ApprovalReplay):
            self._reserve(approval_id, idempotency_key="attempt:2")
        outcome = self.store.record_simulation_outcome(
            str(first["reservation_id"]),
            "simulated_success",
            external_receipt_sha256=_hash("platform receipt"),
            now=self.now + 3,
        )
        repeated = self.store.record_simulation_outcome(
            str(first["reservation_id"]),
            "simulated_success",
            external_receipt_sha256=_hash("platform receipt"),
            now=self.now + 4,
        )
        self.assertEqual(outcome["reservation_id"], repeated["reservation_id"])
        with self.assertRaises(IdempotencyConflict):
            self.store.record_simulation_outcome(
                str(first["reservation_id"]), "simulated_failure", now=self.now + 5
            )

    def test_concurrent_approval_decisions_have_exactly_one_winner(self) -> None:
        approval = self._approval()
        approval_id = str(approval["approval_id"])
        barrier = threading.Barrier(12)

        def decide(index: int) -> str:
            barrier.wait()
            try:
                result = self.store.decide_approval(
                    approval_id,
                    index % 2 == 0,
                    actor=f"operator:{index}",
                    now=self.now + 1,
                )
            except ApprovalReplay:
                return "replay"
            return str(result["status"])

        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(decide, range(12)))
        winners = [item for item in results if item in {"approved", "rejected"}]
        self.assertEqual(len(winners), 1, results)
        self.assertEqual(results.count("replay"), 11, results)
        self.assertEqual(self.store.get_approval(approval_id)["status"], winners[0])
        self.assertTrue(self.store.verify_audit_chain())

    def test_concurrent_simulation_outcomes_are_single_assignment(self) -> None:
        self._ready()
        approval = self._approval()
        approval_id = str(approval["approval_id"])
        self.store.decide_approval(approval_id, True, now=self.now + 1)
        reservation = self._reserve(approval_id)
        reservation_id = str(reservation["reservation_id"])
        barrier = threading.Barrier(12)

        def finish(index: int) -> str:
            outcome = "simulated_success" if index % 2 == 0 else "simulated_failure"
            barrier.wait()
            try:
                result = self.store.record_simulation_outcome(
                    reservation_id,
                    outcome,
                    now=self.now + 3 + (index / 100),
                )
            except IdempotencyConflict:
                return "conflict"
            return str(result["status"])

        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(finish, range(12)))
        terminal = {item for item in results if item != "conflict"}
        self.assertEqual(len(terminal), 1, results)
        selected = terminal.pop()
        self.assertIn(selected, {"simulated_success", "simulated_failure"})
        self.assertEqual(results.count("conflict"), 6, results)
        self.assertTrue(self.store.verify_audit_chain())

    def test_approval_expiry_is_durable_and_fails_closed(self) -> None:
        approval = self._approval(expires_at=self.now + 1)
        approval_id = str(approval["approval_id"])
        with self.assertRaises(ApprovalExpired):
            self.store.decide_approval(approval_id, True, now=self.now + 2)
        self.assertEqual(self.store.get_approval(approval_id)["status"], "expired")

        self._ready()
        second = self._approval(
            expires_at=self.now + 5,
            idempotency_key="attempt:expiry:2",
        )
        second_id = str(second["approval_id"])
        self.store.decide_approval(second_id, True, now=self.now + 1)
        with self.assertRaises(ApprovalExpired):
            self._reserve(
                second_id,
                idempotency_key="attempt:expiry:2",
                now=self.now + 6,
            )
        self.assertEqual(self.store.get_approval(second_id)["status"], "expired")

    def test_audit_receipts_are_restart_safe_and_tamper_evident(self) -> None:
        self._ready()
        approval = self._approval()
        self.store.decide_approval(str(approval["approval_id"]), False, now=self.now + 1)
        self.assertTrue(self.store.verify_audit_chain())
        restarted = PublicPresenceStore(self.path)
        self.assertTrue(restarted.verify_audit_chain())
        self.assertGreaterEqual(len(restarted.list_audit_receipts()), 4)

        db = sqlite3.connect(self.path)
        try:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                db.execute(
                    "UPDATE public_audit_receipts SET outcome='tampered' WHERE sequence=1"
                )
            db.rollback()
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                db.execute(
                    "DELETE FROM public_audit_receipts WHERE sequence=(SELECT MAX(sequence) FROM public_audit_receipts)"
                )
            db.rollback()
        finally:
            db.close()
        self.assertTrue(restarted.verify_audit_chain())

    def test_total_audit_truncation_does_not_verify_as_an_empty_chain(self) -> None:
        self._ready()
        self.assertTrue(self.store.verify_audit_chain())
        db = sqlite3.connect(self.path)
        try:
            db.execute("DROP TRIGGER public_audit_receipts_no_delete")
            db.execute("DELETE FROM public_audit_receipts")
            db.commit()
        finally:
            db.close()
        self.assertFalse(self.store.verify_audit_chain())

    def test_audit_tail_truncation_does_not_verify(self) -> None:
        self._ready()
        self.assertTrue(self.store.verify_audit_chain())
        db = sqlite3.connect(self.path)
        try:
            db.execute("DROP TRIGGER public_audit_receipts_no_delete")
            db.execute(
                "DELETE FROM public_audit_receipts WHERE sequence="
                "(SELECT MAX(sequence) FROM public_audit_receipts)"
            )
            db.commit()
        finally:
            db.close()
        self.assertFalse(self.store.verify_audit_chain())


if __name__ == "__main__":
    unittest.main()
