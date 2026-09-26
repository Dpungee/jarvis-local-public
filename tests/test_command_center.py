from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from jarvis.command_center import CommandCenterHTTPServer, CommandCenterService


class RecordingProvider:
    label = "Recording test provider"
    simulated = True
    def __init__(self) -> None: self.calls: list[dict[str, object]] = []
    def run(self, **kwargs: object) -> str:
        self.calls.append(kwargs)
        return "verified offline result"


class CommandCenterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.provider = RecordingProvider()
        self.service = CommandCenterService(root / "runtime.db", root / "execution.db", providers={"test-provider": self.provider}, max_workers=1)

    def tearDown(self) -> None:
        self.service.close(); self.temp.cleanup()

    def test_persistent_identity_runs_its_own_task_loop(self) -> None:
        agent = self.service.create_agent({"name": "Atlas", "role": "Research", "purpose": "Verify evidence", "provider": "test-provider", "model": "test-model"})
        self.service.set_lifecycle(agent["agent_id"], "start")
        task = self.service.assign_task(agent["agent_id"], {"title": "Check", "prompt": "Use synthetic facts"})
        deadline = time.time() + 3
        while time.time() < deadline and self.service.runs()[0]["state"] not in {"COMPLETED", "FAILED"}: time.sleep(0.02)
        state = self.service.state(); persisted = state["agents"][0]
        self.assertEqual(state["runs"][0]["state"], "COMPLETED")
        self.assertEqual(persisted["agent_id"], agent["agent_id"])
        self.assertEqual(persisted["tasks"][0]["task_id"], task["task_id"])
        self.assertEqual(persisted["tasks"][0]["result"], "verified offline result")
        self.assertEqual(self.provider.calls[0]["agent_id"], agent["agent_id"])
        self.assertEqual(self.provider.calls[0]["purpose"], "Verify evidence")

    def test_disabled_provider_does_not_create_task(self) -> None:
        agent = self.service.create_agent({"name": "Nova", "role": "Builder", "provider": "codex-cli", "model": "default"})
        self.service.set_lifecycle(agent["agent_id"], "start")
        with self.assertRaisesRegex(ValueError, "disabled"):
            self.service.assign_task(agent["agent_id"], {"title": "No call", "prompt": "Do not invoke"})
        self.assertEqual(self.service.list_agents()[0]["tasks"], [])

    def test_http_requires_token_and_rejects_foreign_origin(self) -> None:
        server = CommandCenterHTTPServer(("127.0.0.1", 0), self.service)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with self.assertRaises(urllib.error.HTTPError) as denied: urllib.request.urlopen(base + "/api/state")
            self.assertEqual(denied.exception.code, 401)
            request = urllib.request.Request(base + "/api/state", headers={"Authorization": f"Bearer {server.token}"})
            with urllib.request.urlopen(request) as response:
                self.assertEqual(response.status, 200); self.assertNotIn(server.token, response.read().decode())
            body = json.dumps({"name": "X", "role": "Y", "provider": "test-provider", "model": "m"}).encode()
            foreign = urllib.request.Request(base + "/api/agents", data=body, method="POST", headers={"Authorization": f"Bearer {server.token}", "Origin": "https://evil.test", "Content-Type": "application/json"})
            with self.assertRaises(urllib.error.HTTPError) as origin_denied: urllib.request.urlopen(foreign)
            self.assertEqual(origin_denied.exception.code, 401)
        finally:
            server.shutdown(); server.server_close(); thread.join(2)


if __name__ == "__main__": unittest.main()
