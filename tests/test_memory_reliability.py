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

    def test_idempotency_key_returns_existing_task(self):
        with Memory(Path(":memory:")) as memory:
            first = memory.add_task("first payload", idempotency_key="same-operation")
            second = memory.add_task("different payload", idempotency_key="same-operation")
            self.assertEqual(first, second)
            self.assertEqual(len(memory.list_tasks()), 1)
            self.assertEqual(memory.list_tasks()[0]["prompt"], "first payload")

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
