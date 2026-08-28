from __future__ import annotations

import io
import os
import shutil
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from jarvis import cli
from tests.test_cli import WorkerMemory, fake_config


TEMP_ROOT = Path(__file__).resolve().parent / ".tmp"
TEMP_ROOT.mkdir(exist_ok=True)


class WorkerFileAndLoggingTests(unittest.TestCase):
    def setUp(self):
        self.test_dir = TEMP_ROOT / f"cli-files-{os.getpid()}-{self._testMethodName}"
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir)
        self.test_dir.mkdir()

    def tearDown(self):
        resolved = self.test_dir.resolve()
        self.assertEqual(resolved.parent, TEMP_ROOT.resolve())
        shutil.rmtree(resolved)

    def test_worker_heartbeat_is_atomic_utf8_and_contains_identity(self):
        config = SimpleNamespace(data_dir=self.test_dir)
        target = self.test_dir / "worker.heartbeat"
        target.write_text("stale", encoding="utf-8")

        with patch.object(cli.time, "time", return_value=123.5), patch.object(
            cli.os, "getpid", return_value=42
        ):
            cli._write_worker_heartbeat(config, "worker:42:abc")

        self.assertEqual(target.read_text(encoding="utf-8"), "123.500000 42 worker:42:abc\n")
        self.assertEqual(list(self.test_dir.glob(".worker-heartbeat-*")), [])

    def test_worker_process_lock_allows_exactly_one_owner_and_releases(self):
        first = cli._WorkerProcessLock(self.test_dir)
        second = cli._WorkerProcessLock(self.test_dir)
        try:
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
            first.close()
            self.assertTrue(second.acquire())
        finally:
            first.close()
            second.close()

    def test_long_running_worker_writes_heartbeat_before_polling(self):
        config = fake_config()
        config.data_dir = self.test_dir
        memory = WorkerMemory(None)
        stop = threading.Event()
        stop.set()
        heartbeat = Mock()
        with (
            patch.object(cli.Config, "load", return_value=config),
            patch.object(cli, "_write_worker_heartbeat", heartbeat),
            patch("sys.stdout", io.StringIO()),
        ):
            code = cli.worker(
                1,
                stop_event=stop,
                memory_factory=lambda *args, **kwargs: memory,
                agent_factory=Mock(side_effect=AssertionError("agent must not start")),
            )

        self.assertEqual(code, 0)
        self.assertTrue(memory.closed)
        heartbeat.assert_called_once()
        called_config, worker_id = heartbeat.call_args.args
        self.assertIs(called_config, config)
        self.assertTrue(worker_id.startswith("worker:"))
        replacement = cli._WorkerProcessLock(self.test_dir)
        try:
            self.assertTrue(replacement.acquire())
        finally:
            replacement.close()

    def test_rotating_writer_uses_byte_limits_and_bounded_backups(self):
        path = self.test_dir / "worker.log"
        with cli._RotatingTextWriter(path, max_bytes=10, backups=2) as writer:
            writer.write("12345\n")
            writer.write("abcdef\n")
            writer.write("ghijkl\n")

        self.assertEqual(path.read_text(encoding="utf-8"), "ghijkl\n")
        self.assertEqual((self.test_dir / "worker.log.1").read_text(encoding="utf-8"), "abcdef\n")
        self.assertEqual((self.test_dir / "worker.log.2").read_text(encoding="utf-8"), "12345\n")
        self.assertFalse((self.test_dir / "worker.log.3").exists())

    def test_logged_worker_is_pinned_to_data_log_and_captures_both_streams(self):
        config = fake_config()
        config.data_dir = self.test_dir
        expected = self.test_dir / "worker.log"

        def fake_worker_pool(poll_seconds, concurrency):
            print(f"stdout poll={poll_seconds}")
            print("stderr captured", file=sys.stderr)
            return 7

        with patch.object(cli.Config, "load", return_value=config), patch.object(
            cli, "worker_pool", side_effect=fake_worker_pool
        ):
            code = cli._run_logged_worker(9, expected)

        self.assertEqual(code, 7)
        log = expected.read_text(encoding="utf-8")
        self.assertIn("stdout poll=9", log)
        self.assertIn("stderr captured", log)

        outside = self.test_dir.parent / "outside-worker.log"
        with (
            patch.object(cli.Config, "load", return_value=config),
            patch.object(cli, "worker_pool") as worker,
            self.assertRaisesRegex(PermissionError, "data/worker.log"),
        ):
            cli._run_logged_worker(9, outside)
        worker.assert_not_called()

    def test_log_writer_rejects_non_regular_target(self):
        path = self.test_dir / "worker.log"
        path.mkdir()
        with self.assertRaisesRegex(PermissionError, "ordinary single-link"):
            cli._RotatingTextWriter(path, max_bytes=100, backups=2)


if __name__ == "__main__":
    unittest.main()
