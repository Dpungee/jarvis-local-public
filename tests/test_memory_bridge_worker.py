"""Worker ingestion, the ingest lease, and the bridge operator surfaces.

Disposable temporary stores only.  No live database is opened, the bridge is
never activated against a real runtime store, and no projection is exposed to
any agent surface.
"""
from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from jarvis import cli, memory_bridge as mb
from jarvis.config import Config
from jarvis.memory import Memory, now_iso

from tests.test_memory_bridge import _agt, _evt, agent_record, event


def _later(seconds: int) -> str:
    return (datetime.fromisoformat(now_iso()) + timedelta(seconds=seconds)).isoformat()


class WorkerBridgeCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="jxworker-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.path = self.root / "memory.db"
        self.memory = Memory(self.path)
        self.addCleanup(self.memory.close)
        self.db = self.memory.db

    def _history(self, count: int = 6) -> list[dict]:
        events = [event(1, "agent.created", agent_record(1))]
        for index in range(2, count + 1):
            events.append(
                event(index, "agent.running", agent_record(1, lifecycle="RUNNING"))
            )
        return events

    def _reader(self, events):
        def read(after_sequence, after_event_id, limit):
            del after_event_id
            start = int(after_sequence)
            return events[start:start + int(limit)]
        return read


# ---------------------------------------------------------------------------
# The ingest lease
# ---------------------------------------------------------------------------

class IngestLeaseTests(WorkerBridgeCase):
    def test_one_holder_at_a_time_and_release_frees_it(self) -> None:
        with self.memory._immediate_transaction():
            token = mb.acquire_ingest_lease(self.db, owner="worker-a", now=now_iso())
        self.assertIsNotNone(token)
        with self.memory._immediate_transaction():
            self.assertIsNone(
                mb.acquire_ingest_lease(self.db, owner="worker-b", now=now_iso())
            )
        with self.memory._immediate_transaction():
            self.assertTrue(
                mb.release_ingest_lease(self.db, owner="worker-a", token=token)
            )
        with self.memory._immediate_transaction():
            self.assertIsNotNone(
                mb.acquire_ingest_lease(self.db, owner="worker-b", now=now_iso())
            )

    def test_an_expired_lease_is_reclaimed_so_a_dead_worker_cannot_wedge_it(self) -> None:
        with self.memory._immediate_transaction():
            mb.acquire_ingest_lease(
                self.db, owner="crashed", now=now_iso(), lease_seconds=1
            )
        with self.memory._immediate_transaction():
            reclaimed = mb.acquire_ingest_lease(
                self.db, owner="worker-b", now=_later(120)
            )
        self.assertIsNotNone(reclaimed)
        self.assertEqual(mb.lease_state(self.db)["owner"], "worker-b")

    def test_release_with_a_foreign_token_does_nothing(self) -> None:
        with self.memory._immediate_transaction():
            mb.acquire_ingest_lease(self.db, owner="worker-a", now=now_iso())
        with self.memory._immediate_transaction():
            self.assertFalse(
                mb.release_ingest_lease(self.db, owner="worker-b", token="0" * 32)
            )
        self.assertTrue(mb.lease_state(self.db)["held"])

    def test_the_lease_row_rejects_incoherent_state(self) -> None:
        import sqlite3

        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("UPDATE memory_bridge_lease SET owner='w'")


# ---------------------------------------------------------------------------
# Bounded ingestion
# ---------------------------------------------------------------------------

class BoundedIngestTests(WorkerBridgeCase):
    def test_ingest_consumes_the_stream_and_releases_the_lease(self) -> None:
        events = self._history(6)
        result = mb.ingest_once(
            self.db, self._reader(events), now=now_iso(), owner="worker-a",
            transaction=self.memory._immediate_transaction, batch_size=2,
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["events"], 6)
        self.assertEqual(result["batches"], 3)
        self.assertEqual(result["lease"], "released")
        self.assertFalse(mb.lease_state(self.db)["held"])
        self.assertEqual(mb.read_cursor(self.db)["last_sequence"], 6)

    def test_the_per_pass_bound_stops_early_and_leaves_the_rest(self) -> None:
        events = self._history(10)
        result = mb.ingest_once(
            self.db, self._reader(events), now=now_iso(), owner="worker-a",
            transaction=self.memory._immediate_transaction,
            max_events=4, batch_size=2,
        )
        self.assertEqual(result["status"], "bounded")
        self.assertEqual(result["events"], 4)
        self.assertEqual(mb.read_cursor(self.db)["last_sequence"], 4)
        # The next pass resumes exactly where the bound stopped.
        again = mb.ingest_once(
            self.db, self._reader(events), now=now_iso(), owner="worker-a",
            transaction=self.memory._immediate_transaction, max_events=100,
        )
        self.assertEqual(again["status"], "ok")
        self.assertEqual(mb.read_cursor(self.db)["last_sequence"], 10)

    def test_ingest_refuses_while_another_worker_holds_the_lease(self) -> None:
        with self.memory._immediate_transaction():
            mb.acquire_ingest_lease(self.db, owner="worker-a", now=now_iso())
        result = mb.ingest_once(
            self.db, self._reader(self._history()), now=now_iso(), owner="worker-b",
            transaction=self.memory._immediate_transaction,
        )
        self.assertEqual(result["status"], "lease_held_elsewhere")
        self.assertEqual(mb.read_cursor(self.db)["last_sequence"], 0)

    def test_a_halted_bridge_is_not_read_at_all(self) -> None:
        reads: list[int] = []

        def counting_reader(after_sequence, after_event_id, limit):
            reads.append(int(after_sequence))
            return [event(2, "agent.created", agent_record(1))]  # gap from 0

        first = mb.ingest_once(
            self.db, counting_reader, now=now_iso(), owner="worker-a",
            transaction=self.memory._immediate_transaction,
        )
        self.assertEqual(first["status"], "halted")
        self.assertEqual(first["halt_code"], "sequence_gap")
        before = len(reads)
        second = mb.ingest_once(
            self.db, counting_reader, now=now_iso(), owner="worker-a",
            transaction=self.memory._immediate_transaction,
        )
        self.assertEqual(second["status"], "halted")
        self.assertEqual(second["lease"], "not_acquired")
        self.assertEqual(len(reads), before, "a halted bridge must not read")

    def test_a_halt_mid_pass_releases_the_lease(self) -> None:
        events = self._history(3)
        events.append(event(5, "agent.running", agent_record(1, lifecycle="RUNNING")))
        result = mb.ingest_once(
            self.db, self._reader(events), now=now_iso(), owner="worker-a",
            transaction=self.memory._immediate_transaction, batch_size=1,
        )
        self.assertEqual(result["status"], "halted")
        self.assertEqual(result["lease"], "released")
        self.assertFalse(mb.lease_state(self.db)["held"])
        self.assertEqual(mb.read_cursor(self.db)["last_sequence"], 3)


# ---------------------------------------------------------------------------
# The worker pass and runtime_control
# ---------------------------------------------------------------------------

class WorkerPassTests(WorkerBridgeCase):
    def _config(self, **overrides) -> Config:
        base = Config.load()
        return replace(base, data_dir=self.root, **overrides)

    def test_the_bridge_is_disabled_by_default(self) -> None:
        result = cli._run_bridge_pass(self._config(), self.memory, "worker-a")
        self.assertEqual(result["status"], "disabled")

    def test_worker_mode_without_a_runtime_path_is_unconfigured(self) -> None:
        config = self._config(memory_bridge="worker", memory_bridge_runtime_db="")
        self.assertEqual(
            cli._run_bridge_pass(config, self.memory, "worker-a")["status"],
            "unconfigured",
        )

    def test_a_missing_runtime_store_does_not_ingest(self) -> None:
        config = self._config(
            memory_bridge="worker",
            memory_bridge_runtime_db=str(self.root / "absent.db"),
        )
        self.assertEqual(
            cli._run_bridge_pass(config, self.memory, "worker-a")["status"],
            "runtime_unavailable",
        )

    def test_an_operator_pause_suppresses_ingestion(self) -> None:
        self.memory.set_control_state("paused", reason="operator test")
        config = self._config(
            memory_bridge="worker",
            memory_bridge_runtime_db=str(self.root / "runtime.db"),
        )
        self.assertEqual(
            cli._run_bridge_pass(config, self.memory, "worker-a")["status"],
            "runtime_paused",
        )

    def test_an_emergency_stop_suppresses_ingestion(self) -> None:
        self.memory.set_control_state("stopped", reason="operator test")
        config = self._config(
            memory_bridge="worker",
            memory_bridge_runtime_db=str(self.root / "runtime.db"),
        )
        self.assertEqual(
            cli._run_bridge_pass(config, self.memory, "worker-a")["status"],
            "runtime_stopped",
        )

    def test_a_real_runtime_store_ingests_through_the_worker_pass(self) -> None:
        try:
            from jarvis import multi_agent_runtime as runtime
        except ImportError as exc:  # pragma: no cover
            self.skipTest(f"multi_agent_runtime unavailable: {exc}")
        runtime_path = self.root / "runtime.db"
        store = runtime.MultiAgentRuntimeStore(runtime_path)
        forge = store.create_agent(
            display_name="Forge", role="impl", specialties=("coding",),
            idempotency_key="a",
        )
        store.bind_agent(forge.agent_id).start(idempotency_key="s")
        store.close()

        config = self._config(
            memory_bridge="worker", memory_bridge_runtime_db=str(runtime_path)
        )
        result = cli._run_bridge_pass(config, self.memory, "worker-a")
        self.assertEqual(result["status"], "ok", result)
        self.assertGreaterEqual(result["events"], 2)
        self.assertEqual(
            int(self.db.execute(
                "SELECT COUNT(*) FROM memory_bridge_agents").fetchone()[0]),
            1,
        )
        self.assertFalse(mb.lease_state(self.db)["held"])


# ---------------------------------------------------------------------------
# Operator surfaces: status and resume
# ---------------------------------------------------------------------------

class ResumeTests(WorkerBridgeCase):
    def _halt(self) -> str:
        with self.memory._immediate_transaction():
            mb.import_batch(
                self.db, [event(2, "agent.created", agent_record(1))], now=now_iso()
            )
        state = mb.read_cursor(self.db)
        self.assertEqual(state["status"], "halted")
        return str(state["halt_code"])

    def test_resume_requires_an_operator_and_the_exact_halt_code(self) -> None:
        code = self._halt()
        with self.memory._immediate_transaction():
            with self.assertRaises(mb.BridgeError):
                mb.resume(self.db, operator="", acknowledge=code, now=now_iso())
            with self.assertRaises(mb.BridgeError):
                mb.resume(
                    self.db, operator="operator", acknowledge="something_else",
                    now=now_iso(),
                )
        self.assertEqual(mb.read_cursor(self.db)["status"], "halted")

    def test_resume_clears_the_flag_without_advancing_the_cursor(self) -> None:
        code = self._halt()
        before = mb.read_cursor(self.db)["last_sequence"]
        with self.memory._immediate_transaction():
            outcome = mb.resume(
                self.db, operator="operator", acknowledge=code, now=now_iso(),
                note="investigated",
            )
        self.assertFalse(outcome["cursor_advanced"])
        state = mb.read_cursor(self.db)
        self.assertEqual(state["status"], "active")
        self.assertEqual(state["last_sequence"], before)
        self.assertIsNone(state["halt_code"])

    def test_resume_does_not_bypass_an_unresolved_failure(self) -> None:
        """The failing event is not skipped: an unfixed cause halts again."""
        code = self._halt()
        with self.memory._immediate_transaction():
            mb.resume(self.db, operator="operator", acknowledge=code, now=now_iso())
        # Same stream, same defect, same sequence.
        result = mb.ingest_once(
            self.db,
            self._reader([event(2, "agent.created", agent_record(1))]),
            now=now_iso(), owner="worker-a",
            transaction=self.memory._immediate_transaction,
        )
        self.assertEqual(result["status"], "halted")
        self.assertEqual(result["halt_code"], "sequence_gap")
        self.assertEqual(mb.read_cursor(self.db)["last_sequence"], 0)

    def test_every_resume_is_audited(self) -> None:
        code = self._halt()
        with self.memory._immediate_transaction():
            mb.resume(
                self.db, operator="ada", acknowledge=code, now=now_iso(),
                note="checked the runtime store",
            )
        row = self.db.execute(
            """SELECT operator, halt_code, cursor_sequence, note
               FROM memory_bridge_resumes"""
        ).fetchone()
        self.assertEqual(row[0], "ada")
        self.assertEqual(row[1], code)
        self.assertEqual(row[2], 0)
        self.assertEqual(row[3], "checked the runtime store")
        self.assertEqual(mb.bridge_status(self.db)["resumes"], 1)

    def test_resume_refuses_when_the_bridge_is_not_halted(self) -> None:
        with self.memory._immediate_transaction():
            with self.assertRaises(mb.BridgeError):
                mb.resume(
                    self.db, operator="operator", acknowledge="sequence_gap",
                    now=now_iso(),
                )

    def test_status_reports_state_without_projection_content(self) -> None:
        events = [
            event(1, "agent.created", agent_record(1)),
            event(2, "message.direct_sent", {
                "message_id": "msg_" + "1" * 32, "sender_id": _agt(1),
                "recipient_id": _agt(1), "body": "a private body",
                "reply_to_message_id": None, "task_id": None, "created_at": 1.0,
            }),
        ]
        with self.memory._immediate_transaction():
            mb.import_batch(self.db, events, now=now_iso())
        report = mb.bridge_status(self.db)
        self.assertEqual(report["status"], "active")
        self.assertFalse(report["agent_facing"])
        self.assertNotIn("a private body", repr(report))
        self.assertEqual(report["last_event_id"], _evt(2))
        self.assertIn("memory_bridge_messages", report["projection_counts"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
