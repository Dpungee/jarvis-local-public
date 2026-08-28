from __future__ import annotations

import json
import io
import os
import shutil
import socket
import time
import unittest
from dataclasses import replace
from pathlib import Path

from jarvis.config import Config
from jarvis.memory import Memory
from jarvis.tools import ToolBox, _FileOutputCollector


TEMP_ROOT = Path(__file__).resolve().parent / ".tmp"
TEMP_ROOT.mkdir(exist_ok=True)


class ProcessLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.test_dir = TEMP_ROOT / f"process-lifecycle-{os.getpid()}-{self._testMethodName}"
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir)
        self.workspace = self.test_dir / "workspace"
        self.data_dir = self.test_dir / "data"
        self.workspace.mkdir(parents=True)
        self.data_dir.mkdir()
        self.config = replace(
            Config.load(),
            workspace=self.workspace,
            data_dir=self.data_dir,
            execution_mode="trusted-host",
            autonomy="autonomous",
            computer_access="disabled",
            command_timeout=10,
        )
        self.memory = Memory(self.data_dir / "test.db")
        self.toolbox = ToolBox(self.config, self.memory)

    def tearDown(self) -> None:
        try:
            for item in self.toolbox.process_status()["processes"]:
                if item["running"]:
                    self.toolbox.stop_process(item["process_id"])
        finally:
            self.memory.close()
            resolved = self.test_dir.resolve()
            self.assertEqual(resolved.parent, TEMP_ROOT.resolve())
            shutil.rmtree(resolved)

    @staticmethod
    def _unused_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            return int(listener.getsockname()[1])

    def test_detect_project_reports_manifests_entrypoints_and_structured_commands(self) -> None:
        (self.workspace / "package.json").write_text(
            json.dumps({"scripts": {"test": "node tests.js", "build": "node build.js", "dev": "node server.js"}}),
            encoding="utf-8",
        )
        (self.workspace / "server.js").write_text("console.log('server')\n", encoding="utf-8")
        result = self.toolbox.detect_project()

        self.assertTrue(result["detected"])
        self.assertEqual(result["types"], ["node"])
        self.assertIn("package.json", result["markers"])
        self.assertIn("server.js", result["entrypoints"])
        self.assertEqual(result["package_scripts"], ["build", "dev", "test"])
        command_tuples = {
            (item["purpose"], item["program"], tuple(item["arguments"]))
            for item in result["commands"]
        }
        self.assertIn(("start", "node", ("server.js",)), command_tuples)
        self.assertIn(("test", "npm", ("run", "test")), command_tuples)

    def test_start_health_logs_status_and_stop_form_a_complete_lifecycle(self) -> None:
        port = self._unused_port()
        (self.workspace / "server.py").write_text(
            "import http.server, sys\n"
            "port = int(sys.argv[1])\n"
            "class Handler(http.server.BaseHTTPRequestHandler):\n"
            "    def do_GET(self):\n"
            "        self.send_response(200)\n"
            "        self.end_headers()\n"
            "        self.wfile.write(b'jarvis-ready')\n"
            "    def log_message(self, format, *args):\n"
            "        print('REQUEST', self.path, file=sys.stderr, flush=True)\n"
            "print(f'READY {port}', flush=True)\n"
            "http.server.ThreadingHTTPServer(('127.0.0.1', port), Handler).serve_forever()\n",
            encoding="utf-8",
        )

        started = self.toolbox.start_process(
            "python", ["server.py", str(port)], name="test-health-server"
        )
        process_id = started["process_id"]
        self.assertRegex(process_id, r"^[0-9a-f]{12}$")
        self.assertEqual(started["name"], "test-health-server")
        self.assertTrue(started["running"])

        health = self.toolbox.http_health(
            f"http://127.0.0.1:{port}/health", timeout=2, retries=10, interval_ms=100
        )
        self.assertTrue(health["healthy"], health)
        self.assertEqual(health["status"], 200)
        self.assertEqual(health["body_preview"], "jarvis-ready")

        deadline = time.monotonic() + 3
        logs = {}
        while time.monotonic() < deadline:
            logs = self.toolbox.process_logs(process_id, lines=20)
            if "READY" in logs["stdout"]["content"] and "REQUEST" in logs["stderr"]["content"]:
                break
            time.sleep(0.05)
        self.assertIn(f"READY {port}", logs["stdout"]["content"])
        self.assertIn("REQUEST /health", logs["stderr"]["content"])
        self.assertTrue((self.data_dir / f"processes/{process_id}.stdout.log").is_file())

        listed = self.toolbox.process_status()
        self.assertEqual(listed["count"], 1)
        self.assertEqual(listed["active"], 1)
        followup_toolbox = ToolBox(self.config, self.memory)
        followup_status = followup_toolbox.process_status(process_id)
        self.assertTrue(followup_status["running"])
        self.assertEqual(followup_status["name"], "test-health-server")
        self.assertIn("READY", followup_toolbox.process_logs(process_id)["stdout"]["content"])
        stopped = self.toolbox.stop_process(process_id)
        self.assertEqual(stopped["state"], "stopped")
        self.assertFalse(stopped["running"])
        self.assertFalse(stopped["already_exited"])
        self.assertEqual(self.toolbox.process_status(process_id)["state"], "stopped")
        self.assertIn("READY", self.toolbox.process_logs(process_id)["stdout"]["content"])

        other_workspace = self.test_dir / "other-workspace"
        other_workspace.mkdir()
        other_toolbox = ToolBox(
            replace(self.config, workspace=other_workspace), self.memory
        )
        self.assertIs(other_toolbox._processes, self.toolbox._processes)
        self.assertEqual(other_toolbox.process_status()["count"], 0)
        with self.assertRaises(KeyError):
            other_toolbox.process_status(process_id)

    def test_natural_exit_remains_inspectable_and_stop_is_idempotent(self) -> None:
        (self.workspace / "once.py").write_text(
            "print('finished-output', flush=True)\n", encoding="utf-8"
        )
        started = self.toolbox.start_process("python", ["once.py"])
        process_id = started["process_id"]
        deadline = time.monotonic() + 5
        status = started
        while status["running"] and time.monotonic() < deadline:
            time.sleep(0.05)
            status = self.toolbox.process_status(process_id)
        self.assertEqual(status["state"], "exited")
        self.assertEqual(status["exit_code"], 0)
        self.assertIn("finished-output", self.toolbox.process_logs(process_id)["stdout"]["content"])
        stopped = self.toolbox.stop_process(process_id)
        self.assertTrue(stopped["already_exited"])
        self.assertEqual(stopped["state"], "exited")

    def test_health_check_bound_to_exited_process_rejects_unrelated_server(self) -> None:
        port = self._unused_port()
        (self.workspace / "server.py").write_text(
            "import http.server, sys\n"
            "port = int(sys.argv[1])\n"
            "class Handler(http.server.BaseHTTPRequestHandler):\n"
            "    def do_GET(self):\n"
            "        self.send_response(200); self.end_headers(); self.wfile.write(b'unrelated')\n"
            "    def log_message(self, format, *args): pass\n"
            "http.server.ThreadingHTTPServer(('127.0.0.1', port), Handler).serve_forever()\n",
            encoding="utf-8",
        )
        (self.workspace / "crash.py").write_text(
            "raise RuntimeError('target app crashed')\n", encoding="utf-8"
        )
        unrelated = self.toolbox.start_process("python", ["server.py", str(port)])
        target = self.toolbox.start_process("python", ["crash.py"])
        deadline = time.monotonic() + 5
        target_status = target
        while target_status["running"] and time.monotonic() < deadline:
            time.sleep(0.05)
            target_status = self.toolbox.process_status(target["process_id"])
        self.assertFalse(target_status["running"])

        unbound = self.toolbox.http_health(
            f"http://127.0.0.1:{port}/", retries=10, interval_ms=50
        )
        self.assertTrue(unbound["healthy"], unbound)
        bound = self.toolbox.http_health(
            f"http://127.0.0.1:{port}/",
            process_id=target["process_id"],
            retries=10,
            interval_ms=50,
        )
        self.assertFalse(bound["healthy"], bound)
        self.assertFalse(bound["process_running"])
        self.assertIn("exited", bound["error"].casefold())
        self.assertTrue(self.toolbox.process_status(unrelated["process_id"])["running"])

    def test_lifecycle_rejects_unmanaged_execution_and_nonlocal_health_checks(self) -> None:
        with self.assertRaises(PermissionError):
            self.toolbox.start_process("powershell", ["Get-Process"])
        with self.assertRaises(PermissionError):
            self.toolbox.http_health("http://example.com/")
        with self.assertRaises(ValueError):
            self.toolbox.process_status("not-a-process-id")

        disabled = ToolBox(replace(self.config, execution_mode="disabled"), self.memory)
        self.assertNotIn("start_process", disabled.tools)
        self.assertNotIn("http_health", disabled.tools)
        with self.assertRaisesRegex(PermissionError, "disabled"):
            disabled.start_process("python", ["once.py"])
        with self.assertRaisesRegex(PermissionError, "disabled"):
            disabled.http_health("http://127.0.0.1:1/")

    def test_managed_log_retains_terminal_tail_after_size_limit(self) -> None:
        path = self.data_dir / "bounded.log"
        terminal = b"TERMINAL_CRASH_SENTINEL\n"
        stream = io.BytesIO(b"A" * 2048 + terminal)
        collector = _FileOutputCollector(stream, path, limit=1024)
        collector.start()
        collector.finish()

        raw, captured, total = collector.snapshot()

        self.assertIn(terminal.strip(), raw)
        self.assertIn(b"retained tail", raw)
        self.assertLessEqual(captured, 1024)
        self.assertEqual(total, 2048 + len(terminal))


if __name__ == "__main__":
    unittest.main()
