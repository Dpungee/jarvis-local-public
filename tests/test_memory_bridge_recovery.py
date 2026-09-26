"""Multi-process, crash, stale-worker and recovery tests for the replay bridge.

These run real OS processes against one disposable SQLite store, because the
threaded tests in ``test_memory_bridge`` cannot show what two independent
``Memory`` connections do to each other.  The crash children call ``os._exit(0)``
mid-transaction, in the same style as ``tests/sqlite_crash_fixture.py``.

Every store is a temporary file.  No live database is opened.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from jarvis import memory_bridge as mb
from jarvis.memory import Memory, now_iso

from tests.test_memory_bridge import agent_record, event

REPO_ROOT = Path(__file__).resolve().parents[1]


def _history(count: int) -> list[dict]:
    """The same synthetic stream the child preamble builds."""
    events = [event(1, "agent.created", agent_record(1))]
    for index in range(2, count + 1):
        events.append(event(index, "agent.running", agent_record(1, lifecycle="RUNNING")))
    return events

#: Shared preamble for every child process: import from this worktree, open the
#: store, and build the same synthetic history the parent uses.
_PREAMBLE = """
import json, os, sys
sys.path.insert(0, {root!r})
from jarvis.memory import Memory, now_iso
from jarvis import memory_bridge as mb
sys.path.insert(0, {tests!r})
from test_memory_bridge import event, agent_record

def history(count):
    events = [event(1, "agent.created", agent_record(1))]
    for index in range(2, count + 1):
        events.append(event(index, "agent.running", agent_record(1, lifecycle="RUNNING")))
    return events

def reader(events):
    def read(after_sequence, after_event_id, limit):
        start = int(after_sequence)
        return events[start:start + int(limit)]
    return read

path = sys.argv[1]
"""


def _child(body: str, path: Path, *args: str, timeout: int = 90) -> subprocess.CompletedProcess:
    script = _PREAMBLE.format(root=str(REPO_ROOT), tests=str(REPO_ROOT / "tests")) + body
    return subprocess.run(
        [sys.executable, "-c", script, str(path), *args],
        capture_output=True, text=True, timeout=timeout,
    )


class RecoveryCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="jxrecover-")
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "memory.db"
        # Create the schema, then close: the children own their own connections.
        with Memory(self.path) as memory:
            self.generation = mb.active_generation(memory.db)

    def _open(self) -> Memory:
        memory = Memory(self.path)
        self.addCleanup(memory.close)
        return memory

    def _counts(self) -> dict[str, int]:
        with Memory(self.path) as memory:
            return {
                table: int(memory.db.execute(
                    f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in mb.BRIDGE_PROJECTION_TABLES
            }

    def _cursor(self) -> dict:
        with Memory(self.path) as memory:
            return mb.read_cursor(memory.db)


# ---------------------------------------------------------------------------
# Two processes
# ---------------------------------------------------------------------------

INGEST_BODY = """
events = history(int(sys.argv[2]))
memory = Memory(path)
try:
    result = mb.ingest_once(
        memory.db, reader(events), now=now_iso(), owner=sys.argv[3],
        transaction=memory._immediate_transaction, batch_size=2,
    )
finally:
    memory.close()
print(json.dumps(result))
"""


class MultiProcessTests(RecoveryCase):
    def test_two_processes_ingesting_at_once_produce_one_clean_history(self) -> None:
        """Both may run; the lease decides, and neither corrupts the other."""
        first = subprocess.Popen(
            [sys.executable, "-c",
             _PREAMBLE.format(root=str(REPO_ROOT), tests=str(REPO_ROOT / "tests"))
             + INGEST_BODY, str(self.path), "8", "worker-a"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        second = subprocess.Popen(
            [sys.executable, "-c",
             _PREAMBLE.format(root=str(REPO_ROOT), tests=str(REPO_ROOT / "tests"))
             + INGEST_BODY, str(self.path), "8", "worker-b"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        out_a, err_a = first.communicate(timeout=120)
        out_b, err_b = second.communicate(timeout=120)
        self.assertEqual(first.returncode, 0, err_a)
        self.assertEqual(second.returncode, 0, err_b)
        results = [json.loads(out_a.strip()), json.loads(out_b.strip())]
        statuses = sorted(result["status"] for result in results)

        # Whatever the interleaving, the store must be exactly one clean history.
        cursor = self._cursor()
        self.assertEqual(cursor["status"], "active", results)
        self.assertEqual(cursor["last_sequence"], 8, results)
        counts = self._counts()
        self.assertEqual(counts["memory_bridge_agents"], 1, results)
        self.assertEqual(counts["memory_bridge_agent_events"], 8, results)
        self.assertFalse(
            [s for s in statuses if s not in {"ok", "lease_held_elsewhere", "idle"}],
            results,
        )

    def test_a_stale_process_cannot_commit_against_a_moved_cursor(self) -> None:
        body = """
events = history(6)
memory = Memory(path)
stale_sequence = int(sys.argv[2])
try:
    with memory._immediate_transaction():
        result = {"status": "unexpected"}
        try:
            mb.import_batch(
                memory.db, events[stale_sequence:stale_sequence + 2], now=now_iso(),
                expected_sequence=stale_sequence, expected_event_id=None,
            )
        except mb.BridgeError as error:
            result = {"status": "refused", "code": error.code}
finally:
    memory.close()
print(json.dumps(result))
"""
        # Advance the cursor in this process first.
        memory = self._open()
        events = _history(4)
        with memory._immediate_transaction():
            mb.import_batch(memory.db, events, now=now_iso())
        memory.close()
        self.assertEqual(self._cursor()["last_sequence"], 4)

        # A child that still believes the cursor is at 0 must be refused.
        completed = _child(body, self.path, "0")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout.strip())
        self.assertEqual(result["status"], "refused")
        self.assertEqual(result["code"], "cursor_mismatch")
        self.assertEqual(self._cursor()["last_sequence"], 4)


# ---------------------------------------------------------------------------
# Crash and restart
# ---------------------------------------------------------------------------

class CrashRecoveryTests(RecoveryCase):
    def test_a_crash_mid_import_leaves_no_projection_and_no_cursor_move(self) -> None:
        body = """
events = history(6)
memory = Memory(path)
memory.db.execute("BEGIN IMMEDIATE")
mb.import_batch(memory.db, events, now=now_iso())
# Die with the transaction open and uncommitted.
os._exit(0)
"""
        completed = _child(body, self.path)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        cursor = self._cursor()
        self.assertEqual(cursor["last_sequence"], 0)
        self.assertEqual(cursor["status"], "active")
        self.assertEqual(self._counts()["memory_bridge_agent_events"], 0)

    def test_the_store_recovers_and_the_next_pass_completes_the_stream(self) -> None:
        body = """
events = history(6)
memory = Memory(path)
memory.db.execute("BEGIN IMMEDIATE")
mb.import_batch(memory.db, events, now=now_iso())
os._exit(0)
"""
        self.assertEqual(_child(body, self.path).returncode, 0)
        # Reopen and ingest again: the same stream lands exactly once.
        memory = self._open()
        events = _history(6)

        def read(after_sequence, after_event_id, limit):
            del after_event_id
            start = int(after_sequence)
            return events[start:start + int(limit)]

        result = mb.ingest_once(
            memory.db, read, now=now_iso(), owner="worker-a",
            transaction=memory._immediate_transaction,
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(mb.read_cursor(memory.db)["last_sequence"], 6)
        self.assertEqual(
            int(memory.db.execute(
                "SELECT COUNT(*) FROM memory_bridge_agent_events").fetchone()[0]),
            6,
        )
        self.assertTrue(memory.verify_spine()["ok"])

    def test_a_crash_holding_the_lease_does_not_wedge_ingestion(self) -> None:
        body = """
memory = Memory(path)
with memory._immediate_transaction():
    mb.acquire_ingest_lease(
        memory.db, owner="doomed", now=now_iso(), lease_seconds=1
    )
os._exit(0)
"""
        self.assertEqual(_child(body, self.path).returncode, 0)
        with Memory(self.path) as memory:
            self.assertTrue(mb.lease_state(memory.db)["held"])
            from datetime import datetime, timedelta

            later = (
                datetime.fromisoformat(now_iso()) + timedelta(seconds=300)
            ).isoformat()
            with memory._immediate_transaction():
                token = mb.acquire_ingest_lease(
                    memory.db, owner="worker-b", now=later
                )
            self.assertIsNotNone(token, "an expired lease must be reclaimable")

    def test_a_durable_halt_survives_a_process_restart(self) -> None:
        body = """
memory = Memory(path)
try:
    with memory._immediate_transaction():
        mb.import_batch(
            memory.db, [event(2, "agent.created", agent_record(1))], now=now_iso()
        )
finally:
    memory.close()
"""
        completed = _child(body, self.path)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        cursor = self._cursor()
        self.assertEqual(cursor["status"], "halted")
        self.assertEqual(cursor["halt_code"], "sequence_gap")
        self.assertEqual(cursor["last_sequence"], 0)


# ---------------------------------------------------------------------------
# Rebuild equivalence after recovery
# ---------------------------------------------------------------------------

class PostRecoveryRebuildTests(RecoveryCase):
    def test_rebuild_after_a_crash_matches_a_clean_ingest(self) -> None:
        events = _history(6)
        crash = """
events = history(6)
memory = Memory(path)
memory.db.execute("BEGIN IMMEDIATE")
mb.import_batch(memory.db, events, now=now_iso())
os._exit(0)
"""
        self.assertEqual(_child(crash, self.path).returncode, 0)
        memory = self._open()
        with memory._immediate_transaction():
            mb.import_batch(memory.db, events, now=now_iso())
        after_ingest = {
            table: [tuple(row) for row in memory.db.execute(
                f"SELECT * FROM {table} ORDER BY rowid")]
            for table in mb.BRIDGE_PROJECTION_TABLES
        }
        with memory._immediate_transaction():
            mb.rebuild_generation(memory.db, events, now=now_iso())
        after_rebuild = {
            table: [tuple(row) for row in memory.db.execute(
                f"SELECT * FROM {table} ORDER BY rowid")]
            for table in mb.BRIDGE_PROJECTION_TABLES
        }
        self.assertEqual(after_rebuild, after_ingest)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
