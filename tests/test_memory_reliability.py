import os
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jarvis.memory import Memory, SCHEMA_VERSION
from tests.sqlite_crash_fixture import (
    create_future_schema_in_hot_wal,
    create_hot_future_database,
    snapshot_directory,
)


@contextmanager
def database_path():
    descriptor, name = tempfile.mkstemp(prefix="jarvis-memory-", suffix=".db")
    os.close(descriptor)
    path = Path(name)
    try:
        yield path
    finally:
        for suffix in ("-wal", "-shm", ""):
            Path(f"{path}{suffix}").unlink(missing_ok=True)


class MemoryReliabilityTests(unittest.TestCase):
    def test_paused_or_stopped_control_prevents_claim_inside_transaction(self):
        for state in ("paused", "stopped"):
            with self.subTest(state=state):
                with Memory(Path(":memory:"), worker_id="control-worker") as memory:
                    task_id = memory.add_task(f"task while {state}")
                    memory.set_control_state(state, "operator control")

                    self.assertIsNone(memory.claim_task())
                    row = next(
                        item for item in memory.list_tasks() if item["id"] == task_id
                    )
                    self.assertEqual(row["status"], "queued")
                    self.assertEqual(row["attempt_count"], 0)

                    memory.set_control_state("running", "operator resumed")
                    claimed = memory.claim_task()
                    self.assertIsNotNone(claimed)
                    self.assertEqual(claimed["id"], task_id)

    def test_future_schema_in_hot_wal_is_rejected_without_recovery(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "memory.db"
            with Memory(path):
                pass
            create_future_schema_in_hot_wal(path, user_version=SCHEMA_VERSION + 1)
            before = snapshot_directory(path)
            with self.assertRaisesRegex(RuntimeError, "newer than supported"):
                Memory(path)
            self.assertEqual(snapshot_directory(path), before)

    def test_future_hot_journal_is_rejected_without_recovery_or_sidecar_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "memory.db"
            create_hot_future_database(path, user_version=SCHEMA_VERSION + 1)
            before = snapshot_directory(path)
            with self.assertRaisesRegex(RuntimeError, "newer than supported"):
                Memory(path)
            self.assertEqual(snapshot_directory(path), before)

    def test_v40_tasks_migrate_with_unverifiable_legacy_schedule_binding(self):
        with database_path() as path:
            with Memory(path) as memory:
                task_id = memory.add_task(
                    "legacy task",
                    idempotency_key="legacy-schedule-key",
                )
            legacy = sqlite3.connect(path)
            try:
                legacy.execute("DROP TRIGGER IF EXISTS tasks_schedule_binding_immutable")
                legacy.execute("ALTER TABLE tasks DROP COLUMN availability_mode")
                legacy.execute("ALTER TABLE tasks DROP COLUMN initial_available_at")
                legacy.execute("PRAGMA user_version=40")
                legacy.commit()
            finally:
                legacy.close()

            with Memory(path) as migrated:
                self.assertEqual(
                    int(migrated.db.execute("PRAGMA user_version").fetchone()[0]),
                    SCHEMA_VERSION,
                )
                row = migrated.list_tasks()[0]
                self.assertEqual(row["availability_mode"], "legacy_unknown")
                self.assertIsNone(row["initial_available_at"])
                self.assertEqual(
                    migrated.add_task(
                        "legacy task",
                        idempotency_key="legacy-schedule-key",
                    ),
                    task_id,
                )
                with self.assertRaisesRegex(ValueError, "unverifiable"):
                    migrated.add_task(
                        "legacy task",
                        idempotency_key="legacy-schedule-key",
                        available_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
                    )
                with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                    migrated.db.execute(
                        """UPDATE tasks SET availability_mode='immediate'
                           WHERE id=?""",
                        (task_id,),
                    )

    def test_v40_non_immediate_legacy_task_replay_fails_closed(self):
        with database_path() as path:
            with Memory(path) as memory:
                task_id = memory.add_task(
                    "legacy delayed task", idempotency_key="legacy-delayed-key"
                )
            legacy = sqlite3.connect(path)
            try:
                legacy.execute("DROP TRIGGER IF EXISTS tasks_schedule_binding_immutable")
                legacy.execute(
                    "UPDATE tasks SET available_at=? WHERE id=?",
                    ("2026-09-02T00:00:00+00:00", task_id),
                )
                legacy.execute("ALTER TABLE tasks DROP COLUMN availability_mode")
                legacy.execute("ALTER TABLE tasks DROP COLUMN initial_available_at")
                legacy.execute("PRAGMA user_version=40")
                legacy.commit()
            finally:
                legacy.close()
            with Memory(path) as migrated:
                with self.assertRaisesRegex(ValueError, "unverifiable"):
                    migrated.add_task(
                        "legacy delayed task", idempotency_key="legacy-delayed-key"
                    )

    def test_future_schema_is_rejected_before_journal_or_file_mutation(self):
        with database_path() as path:
            db = sqlite3.connect(path)
            try:
                db.execute("CREATE TABLE future_only(value TEXT)")
                db.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
                db.commit()
            finally:
                db.close()
            before = path.read_bytes()

            with self.assertRaisesRegex(RuntimeError, "newer than supported"):
                Memory(path)

            self.assertEqual(path.read_bytes(), before)
            self.assertFalse(Path(f"{path}-wal").exists())
            self.assertFalse(Path(f"{path}-shm").exists())
            db = sqlite3.connect(path)
            try:
                self.assertEqual(str(db.execute("PRAGMA journal_mode").fetchone()[0]), "delete")
                self.assertEqual(
                    int(db.execute("PRAGMA user_version").fetchone()[0]),
                    SCHEMA_VERSION + 1,
                )
                self.assertEqual(
                    [str(row[0]) for row in db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                    ).fetchall()],
                    ["future_only"],
                )
            finally:
                db.close()

    def test_v7_database_migrates_to_prediction_schema_without_data_loss(self):
        with database_path() as path:
            with Memory(path) as memory:
                conversation_id = memory.new_conversation("existing conversation")
            legacy = sqlite3.connect(path)
            legacy.execute("DROP INDEX IF EXISTS idx_predictions_family")
            legacy.execute("DROP INDEX IF EXISTS idx_predictions_open")
            legacy.execute("DROP TABLE IF EXISTS task_predictions")
            legacy.execute("PRAGMA user_version=7")
            legacy.commit()
            legacy.close()

            with Memory(path) as memory:
                self.assertEqual(
                    memory.db.execute("PRAGMA user_version").fetchone()[0],
                    SCHEMA_VERSION,
                )
                self.assertIsNotNone(memory.db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name='task_predictions'"
                ).fetchone())
                self.assertEqual(
                    memory.db.execute(
                        "SELECT title FROM conversations WHERE id=?",
                        (conversation_id,),
                    ).fetchone()[0],
                    "existing conversation",
                )

    def test_migrates_legacy_database_and_enables_pragmas(self):
        with database_path() as path:
            legacy = sqlite3.connect(path)
            legacy.execute(
                """CREATE TABLE tasks (
                    id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    status TEXT NOT NULL, prompt TEXT NOT NULL, result TEXT
                )"""
            )
            legacy.execute(
                "INSERT INTO tasks(created_at, updated_at, status, prompt) VALUES (?, ?, ?, ?)",
                (
                    "2025-01-01T00:00:00+00:00",
                    "2025-01-01T00:00:00+00:00",
                    "running",
                    "legacy task",
                ),
            )
            legacy.commit()
            legacy.close()

            with Memory(path, busy_timeout_ms=3210) as memory:
                self.assertEqual(memory.db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
                self.assertEqual(memory.db.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
                self.assertEqual(memory.db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                self.assertEqual(memory.db.execute("PRAGMA busy_timeout").fetchone()[0], 3210)
                columns = {row["name"] for row in memory.db.execute("PRAGMA table_info(tasks)")}
                self.assertTrue(
                    {
                        "available_at",
                        "lease_owner",
                        "lease_expires_at",
                        "attempt_count",
                        "max_attempts",
                        "last_error",
                        "idempotency_key",
                    }.issubset(columns)
                )
                self.assertEqual(memory.list_tasks()[0]["prompt"], "legacy task")
                recovered = memory.recover_stale_tasks(
                    now=datetime(2025, 1, 2, tzinfo=timezone.utc)
                )
                self.assertEqual(recovered, {"requeued": 1, "failed": 0})
                self.assertEqual(memory.list_tasks()[0]["status"], "queued")
                with self.assertRaises(sqlite3.IntegrityError):
                    memory.add_message(999, "user", "orphan")

    def test_like_wildcards_are_literals(self):
        with Memory(Path(":memory:")) as memory:
            for content in (
                "literal abc% marker",
                "ordinary abcZZ marker",
                "literal abc_value",
                "ordinary abcXvalue",
            ):
                memory.remember_verified(
                    content,
                    origin="verified_import",
                )

            percent = memory.search("abc%")
            underscore = memory.search("abc_value")

            self.assertEqual([item["content"] for item in percent], ["literal abc% marker"])
            self.assertEqual([item["content"] for item in underscore], ["literal abc_value"])

    def test_context_manager_closes_cleanly_and_close_is_idempotent(self):
        memory = Memory(Path(":memory:"))
        with memory:
            memory.new_conversation("test")
        self.assertTrue(memory.closed)
        memory.close()
        with self.assertRaises(RuntimeError):
            memory.list_tasks()

    def test_idempotency_key_is_bound_to_the_immutable_task_effect(self):
        with Memory(Path(":memory:")) as memory:
            first_goal = memory.add_goal("First goal")
            second_goal = memory.add_goal("Second goal")
            first = memory.add_task(
                "first payload",
                idempotency_key="same-operation",
                max_attempts=3,
                goal_id=first_goal,
            )
            second = memory.add_task(
                "first payload",
                idempotency_key="same-operation",
                max_attempts=3,
                goal_id=first_goal,
            )
            self.assertEqual(first, second)
            self.assertEqual(len(memory.list_tasks()), 1)
            self.assertEqual(memory.list_tasks()[0]["prompt"], "first payload")
            with self.assertRaisesRegex(ValueError, "different prompt"):
                memory.add_task(
                    "different payload",
                    idempotency_key="same-operation",
                    max_attempts=3,
                    goal_id=first_goal,
                )
            with self.assertRaisesRegex(ValueError, "retry policy"):
                memory.add_task(
                    "first payload",
                    idempotency_key="same-operation",
                    max_attempts=4,
                    goal_id=first_goal,
                )
            with self.assertRaisesRegex(ValueError, "goal or backlog provenance"):
                memory.add_task(
                    "first payload",
                    idempotency_key="same-operation",
                    max_attempts=3,
                    goal_id=second_goal,
                )
            self.assertEqual(memory.list_tasks()[0]["goal_id"], first_goal)

    def test_idempotency_distinguishes_immediate_and_scheduled_effects(self):
        scheduled_at = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
        with Memory(Path(":memory:")) as memory:
            immediate = memory.add_task(
                "immediate payload",
                idempotency_key="immediate-key",
            )
            with self.assertRaisesRegex(ValueError, "availability mode"):
                memory.add_task(
                    "immediate payload",
                    idempotency_key="immediate-key",
                    available_at=scheduled_at,
                )

            scheduled = memory.add_task(
                "scheduled payload",
                idempotency_key="scheduled-key",
                available_at=scheduled_at,
            )
            self.assertEqual(
                memory.add_task(
                    "scheduled payload",
                    idempotency_key="scheduled-key",
                    available_at=scheduled_at,
                ),
                scheduled,
            )
            with self.assertRaisesRegex(ValueError, "availability mode"):
                memory.add_task(
                    "scheduled payload",
                    idempotency_key="scheduled-key",
                )
            with self.assertRaisesRegex(ValueError, "different original schedule"):
                memory.add_task(
                    "scheduled payload",
                    idempotency_key="scheduled-key",
                    available_at=scheduled_at + timedelta(minutes=1),
                )
            rows = {row["id"]: row for row in memory.list_tasks()}
            self.assertEqual(rows[immediate]["availability_mode"], "immediate")
            self.assertIsNone(rows[immediate]["initial_available_at"])
            self.assertEqual(rows[scheduled]["availability_mode"], "scheduled")
            self.assertEqual(
                rows[scheduled]["initial_available_at"],
                scheduled_at.isoformat(),
            )

    def test_scheduled_idempotent_replay_uses_original_time_after_backoff(self):
        initial = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
        with Memory(Path(":memory:"), worker_id="schedule-worker") as memory:
            task_id = memory.add_task(
                "retry scheduled payload",
                idempotency_key="scheduled-retry-key",
                available_at=initial,
                max_attempts=3,
            )
            claimed = memory.claim_task(now=initial + timedelta(seconds=1))
            self.assertEqual(claimed["id"], task_id)
            self.assertEqual(
                memory.fail_task(
                    task_id,
                    "retry later",
                    now=initial + timedelta(seconds=2),
                    retry_delay_seconds=60,
                ),
                "queued",
            )
            backed_off = memory.list_tasks()[0]
            self.assertEqual(
                backed_off["available_at"],
                (initial + timedelta(seconds=62)).isoformat(),
            )
            self.assertEqual(backed_off["initial_available_at"], initial.isoformat())

            self.assertEqual(
                memory.add_task(
                    "retry scheduled payload",
                    idempotency_key="scheduled-retry-key",
                    available_at=initial,
                    max_attempts=3,
                ),
                task_id,
            )
            replayed = memory.list_tasks()[0]
            self.assertEqual(replayed["available_at"], backed_off["available_at"])
            self.assertEqual(replayed["initial_available_at"], initial.isoformat())

    def test_atomic_claim_allows_only_one_worker(self):
        with database_path() as path:
            with Memory(path) as memory:
                task_id = memory.add_task("exactly once")

            barrier = threading.Barrier(2)

            def claim(owner):
                with Memory(path, worker_id=owner, busy_timeout_ms=10_000) as worker_memory:
                    barrier.wait(timeout=10)
                    return worker_memory.claim_task(lease_seconds=60)

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(claim, ("worker-a", "worker-b")))

            claimed = [item for item in results if item is not None]
            self.assertEqual(len(claimed), 1)
            self.assertEqual(claimed[0]["id"], task_id)
            self.assertEqual(claimed[0]["attempt_count"], 1)

    def test_lease_renewal_recovery_and_owner_protection(self):
        base = datetime.now(timezone.utc) + timedelta(seconds=1)
        with Memory(Path(":memory:"), worker_id="worker-a") as memory:
            task_id = memory.add_task("leased task", max_attempts=2)
            claimed = memory.claim_task(lease_seconds=10, now=base)
            self.assertEqual(claimed["lease_owner"], "worker-a")
            self.assertFalse(memory.finish_task(task_id, "wrong owner", worker_id="worker-b"))
            self.assertTrue(
                memory.renew_task_lease(
                    task_id,
                    worker_id="worker-a",
                    lease_seconds=10,
                    now=base + timedelta(seconds=5),
                )
            )
            self.assertEqual(
                memory.recover_stale_tasks(now=base + timedelta(seconds=12)),
                {"requeued": 0, "failed": 0},
            )
            self.assertEqual(
                memory.recover_stale_tasks(now=base + timedelta(seconds=16)),
                {"requeued": 1, "failed": 0},
            )
            second = memory.claim_task(
                worker_id="worker-b",
                lease_seconds=10,
                now=base + timedelta(seconds=16),
            )
            self.assertEqual(second["attempt_count"], 2)
            self.assertEqual(
                memory.recover_stale_tasks(now=base + timedelta(seconds=27)),
                {"requeued": 0, "failed": 1},
            )
            task = memory.list_tasks()[0]
            self.assertEqual(task["status"], "failed")
            self.assertIsNone(task["lease_owner"])
            self.assertFalse(memory.finish_task(task_id, "late completion", worker_id="worker-b"))

    def test_explicit_failure_retries_until_attempts_are_exhausted(self):
        base = datetime.now(timezone.utc) + timedelta(seconds=1)
        with Memory(Path(":memory:"), worker_id="worker-a") as memory:
            task_id = memory.add_task("retry task", max_attempts=2)
            memory.claim_task(now=base)
            self.assertEqual(
                memory.fail_task(task_id, "transient", now=base + timedelta(seconds=1)),
                "queued",
            )
            self.assertIsNone(memory.claim_task(now=base + timedelta(milliseconds=500)))
            second = memory.claim_task(
                worker_id="worker-b", now=base + timedelta(seconds=2)
            )
            self.assertEqual(second["attempt_count"], 2)
            self.assertEqual(
                memory.fail_task(
                    task_id,
                    "still failing",
                    worker_id="worker-b",
                    now=base + timedelta(seconds=3),
                ),
                "failed",
            )
            task = memory.list_tasks()[0]
            self.assertEqual(task["status"], "failed")
            self.assertEqual(task["last_error"], "still failing")

    def test_due_learning_queue_is_atomic_and_idempotent(self):
        with database_path() as path:
            with Memory(path) as memory:
                memory.add_learning_topic("durable agents", 12)

            current = datetime.now(timezone.utc) + timedelta(seconds=1)
            barrier = threading.Barrier(2)

            def queue_due(owner):
                with Memory(path, worker_id=owner, busy_timeout_ms=10_000) as worker_memory:
                    barrier.wait(timeout=10)
                    return worker_memory.queue_due_learning(now=current)

            with ThreadPoolExecutor(max_workers=2) as pool:
                counts = list(pool.map(queue_due, ("scheduler-a", "scheduler-b")))

            self.assertEqual(sorted(counts), [0, 1])
            with Memory(path) as memory:
                self.assertEqual(len(memory.list_tasks()), 1)
                runs = memory.list_learning_runs()
                self.assertEqual(len(runs), 1)
                scheduled_for = runs[0]["scheduled_for"]
                memory.db.execute(
                    "UPDATE learning_topics SET next_run=? WHERE id=?",
                    (scheduled_for, runs[0]["topic_id"]),
                )
                self.assertEqual(memory.queue_due_learning(now=current), 0)
                self.assertEqual(len(memory.list_tasks()), 1)
                self.assertEqual(len(memory.list_learning_runs()), 1)


if __name__ == "__main__":
    unittest.main()
