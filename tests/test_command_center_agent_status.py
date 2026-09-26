"""Truthful agent status: create, enable, start work, monitor, fail and restart.

Every case drives the real service and live-turn engine against disposable databases.
The chat transport is a synthetic stand-in whose timing the test controls, so each
state is observed while it is actually true rather than asserted after the fact.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.request
import uuid
from contextlib import closing
from pathlib import Path

from jarvis.command_center import (
    CommandCenterHTTPServer,
    CommandCenterService,
    OfflineDemoProvider,
    agent_status,
)
from jarvis.multi_agent_runtime import (
    AgentLifecycle,
    MultiAgentRuntimeStore,
    TaskStatus,
)
from jarvis.subscription_chat import ChatTransportError


class GatedChat:
    """Synthetic text transport. Nothing leaves the process."""

    label, simulated = "Synthetic test transport", False

    def __init__(self, status: str = "UNTESTED") -> None:
        self.status, self.reason = status, "Test transport."
        self.connected = threading.Event()  # released -> first progress text arrives
        self.finish = threading.Event()     # released -> turn returns
        self.fail_with: str | None = None
        self.calls = 0

    def open(self) -> None:
        self.connected.set()
        self.finish.set()

    def chat(self, model, messages, cancel, progress):
        self.calls += 1
        while not self.connected.wait(0.01):
            if cancel.is_set():
                raise ChatTransportError("Cancelled")
        if self.fail_with:
            raise ChatTransportError(self.fail_with)
        progress("Partial synthetic response")
        while not self.finish.wait(0.01):
            if cancel.is_set():
                raise ChatTransportError("Cancelled")
        return "Synthetic final answer", {"input_tokens": 3, "output_tokens": 2}


class AgentStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.chat = GatedChat()
        self.service: CommandCenterService | None = None

    def tearDown(self) -> None:
        self.chat.open()
        if self.service is not None:
            self.service.close()
        self.temp.cleanup()

    # -- helpers ---------------------------------------------------------------------
    def start(self, providers=None, workers: int = 2) -> CommandCenterService:
        self.service = CommandCenterService(
            self.root / "runtime.db", self.root / "execution.db",
            providers={"claude-cli": self.chat} if providers is None else providers,
            max_workers=workers, simulator_interval=0.01,
        )
        return self.service

    def restart(self, providers=None, workers: int = 2) -> CommandCenterService:
        assert self.service is not None
        self.service.close()
        return self.start(providers, workers)

    def create(self, name: str = "Atlas", *, provider: str = "claude-cli", enable: bool = True) -> str:
        payload = {"name": name, "role": "Researcher", "provider": provider,
                   "model": "default", "project_id": "lab"}
        if enable:
            payload["enable"] = "on"
        return self.service.create_agent(payload)["agent_id"]

    def send(self, agent: str, body: str = "Summarize the synthetic notes", kind: str = "work"):
        return self.service.conversation(agent, "lab", {"body": body, "kind": kind,
                                                        "request_id": str(uuid.uuid4())})

    def status(self, agent: str) -> dict:
        return next(a for a in self.service.state()["agents"] if a["agent_id"] == agent)["status"]

    def turns(self, agent: str) -> list[dict]:
        return self.service.conversation(agent, "lab")["live_turns"]

    def wait(self, predicate, what: str, timeout: float = 5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.01)
        self.fail(f"timed out waiting for {what}")

    def turn_in(self, agent: str, state: str) -> dict:
        return self.wait(lambda: next((t for t in self.turns(agent) if t["state"] == state), None),
                         f"a {state} turn")

    # -- create and configure --------------------------------------------------------
    def test_new_agent_is_not_enabled_and_offers_enable(self) -> None:
        self.start()
        agent = self.create(enable=False)
        status = self.status(agent)
        self.assertEqual(status["code"], "not_enabled")
        self.assertEqual(status["action"], "start")
        with self.assertRaisesRegex(ValueError, "Enable or resume"):
            self.send(agent)
        self.assertEqual(self.turns(agent), [])

    def test_create_with_enable_over_http_is_idle_live_and_hides_token(self) -> None:
        self.start()
        server = CommandCenterHTTPServer(("127.0.0.1", 0), self.service)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base, auth = f"http://127.0.0.1:{server.server_port}", {"Authorization": f"Bearer {server.token}"}
        try:
            body = json.dumps({"name": "Nova", "role": "Builder", "provider": "claude-cli",
                               "model": "default", "enable": "on"}).encode()
            request = urllib.request.Request(base + "/api/agents", data=body, method="POST",
                                             headers=auth | {"Content-Type": "application/json"})
            with urllib.request.urlopen(request) as response:
                created = json.loads(response.read())
            self.assertEqual(created["lifecycle"], "RUNNING")
            with urllib.request.urlopen(urllib.request.Request(base + "/api/state", headers=auth)) as response:
                raw = response.read().decode()
            self.assertNotIn(server.token, raw)
            status = json.loads(raw)["agents"][0]["status"]
            self.assertEqual((status["code"], status["real_provider"], status["simulated"]), ("idle", True, False))
            self.assertIn("not yet contacted", status["detail"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

    # -- start and monitor -----------------------------------------------------------
    def test_live_work_is_observed_connecting_responding_then_completed(self) -> None:
        self.start()
        agent = self.create()
        self.send(agent)

        running = self.turn_in(agent, "RUNNING")
        self.assertIsNotNone(running["started_at"])
        self.assertIsNone(running["finished_at"])
        self.assertEqual(running["response_chars"], 0, "a placeholder is not provider output")
        self.wait(lambda: self.status(agent)["detail"].startswith("Connecting"), "connecting phase")
        self.assertEqual(self.status(agent)["code"], "working")
        self.assertTrue(self.status(agent)["real_provider"])

        self.chat.connected.set()
        responding = self.wait(lambda: next((t for t in self.turns(agent)
                                             if t["phase"] == "responding"), None), "responding phase")
        self.assertEqual(responding["response_chars"], len("Partial synthetic response"))
        self.assertTrue(self.status(agent)["detail"].startswith("Receiving response"))

        self.chat.finish.set()
        done = self.turn_in(agent, "COMPLETED")
        self.assertEqual(done["result_preview"], "Synthetic final answer")
        self.assertIsNone(done["error"])
        self.assertGreaterEqual(done["finished_at"], done["started_at"])
        self.wait(lambda: self.status(agent)["code"] == "idle", "idle after completion")

    def test_second_agent_queues_with_a_reason_when_capacity_is_full(self) -> None:
        self.start(workers=1)
        first, second = self.create("First"), self.create("Second")
        self.send(first)
        self.turn_in(first, "RUNNING")
        self.send(second)
        queued = self.wait(lambda: self.status(second) if self.status(second)["code"] == "queued" else None,
                           "second agent queued")
        self.assertIn("1 of 1 in use", queued["detail"])
        self.assertEqual(self.status(first)["code"], "working")
        self.assertEqual(self.turns(second)[0]["state"], "QUEUED", "queued work must not read as running")
        self.chat.open()
        self.turn_in(second, "COMPLETED")

    def test_paused_agent_holds_its_turn(self) -> None:
        self.start()
        agent = self.create()
        self.send(agent)
        self.turn_in(agent, "RUNNING")
        self.service.set_lifecycle(agent, "pause")
        self.turn_in(agent, "PAUSED")
        status = self.status(agent)
        self.assertEqual((status["code"], status["action"]), ("paused", "resume"))
        self.assertIn("1 item(s) held", status["detail"])

    # -- failure ---------------------------------------------------------------------
    def test_provider_failure_is_reported_on_the_turn_and_the_agent(self) -> None:
        self.start()
        agent = self.create()
        self.chat.fail_with = "Synthetic provider refused the request."
        self.chat.connected.set()
        self.send(agent)
        failed = self.turn_in(agent, "FAILED")
        self.assertEqual(failed["error"], "Synthetic provider refused the request.")
        self.assertIsNone(failed["result_preview"])
        self.assertIsNotNone(failed["finished_at"])
        status = self.wait(lambda: self.status(agent) if self.status(agent)["code"] == "failed" else None,
                           "failed status")
        self.assertEqual(status["label"], "Last turn failed")
        self.assertIn("refused", status["detail"])

    def test_unavailable_provider_refuses_without_queueing(self) -> None:
        self.chat.status, self.chat.reason = "UNAVAILABLE", "Synthetic CLI is not installed."
        self.start()
        agent = self.create()
        self.assertEqual(self.status(agent)["code"], "unavailable")
        with self.assertRaisesRegex(ValueError, "Nothing was sent or queued"):
            self.send(agent)
        self.assertEqual(self.turns(agent), [])
        self.assertEqual(self.chat.calls, 0)

    # -- restart ---------------------------------------------------------------------
    def test_restart_interrupts_running_work_and_resume_runs_it_again(self) -> None:
        self.start()
        agent = self.create()
        self.send(agent)
        before = self.turn_in(agent, "RUNNING")

        self.restart()
        interrupted = self.turn_in(agent, "INTERRUPTED")
        self.assertIsNone(interrupted["finished_at"], "an interrupted turn did not finish")
        status = self.status(agent)
        self.assertEqual((status["code"], status["action"]), ("waiting", "resume"))
        self.assertIn("interrupted by a restart", status["detail"])

        self.chat.open()
        self.service.set_lifecycle(agent, "resume")
        done = self.turn_in(agent, "COMPLETED")
        self.assertEqual(done["turn_id"], before["turn_id"])
        self.assertGreater(done["started_at"], before["started_at"], "resume must restart the clock")

    def test_a_resumed_turn_waiting_in_the_queue_claims_no_start_time(self) -> None:
        self.start(workers=1)
        resumed, busy = self.create("Resumed"), self.create("Busy")
        self.send(resumed)
        self.turn_in(resumed, "RUNNING")
        self.restart(workers=1)
        self.assertIsNotNone(self.turn_in(resumed, "INTERRUPTED")["started_at"], "it did start before")
        self.send(busy)                      # occupies the only slot
        self.turn_in(busy, "RUNNING")
        self.service.set_lifecycle(resumed, "resume")
        waiting = self.turn_in(resumed, "QUEUED")
        self.assertIsNone(waiting["started_at"], "a queued turn must not look like it is running")
        self.assertIn("1 of 1 in use", self.status(resumed)["detail"])

    def test_queued_task_run_is_failed_not_left_waiting_after_a_crash(self) -> None:
        # Recreate the on-disk state a crash leaves: one run executing, one never started.
        with MultiAgentRuntimeStore(self.root / "runtime.db") as store:
            agent = store.create_agent(display_name="Orion", role="Worker", purpose="",
                                       specialties=(), model_provider="test", model_name="m",
                                       project_id="lab", idempotency_key="seed-agent")
            store.set_agent_lifecycle(agent.agent_id, AgentLifecycle.RUNNING, actor_id="owner",
                                      idempotency_key="seed-start")
            tasks = [store.create_task(creator_id=agent.agent_id, owner_id=agent.agent_id,
                                       title=f"t{i}", description="synthetic", project_id="lab",
                                       idempotency_key=f"seed-task-{i}") for i in range(2)]
            store.update_task_status(task_id=tasks[0].task_id, actor_id=agent.agent_id,
                                     status=TaskStatus.RUNNING, idempotency_key="seed-run")
            self.assertEqual(store.get_task(tasks[0].task_id).status, TaskStatus.RUNNING)
        now = time.time()
        (self.root / "execution.db").parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.root / "execution.db")) as db:
            db.execute("""CREATE TABLE command_center_runs(run_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL,
                task_id TEXT NOT NULL UNIQUE, state TEXT NOT NULL, provider TEXT NOT NULL,
                simulated INTEGER NOT NULL, created_at REAL NOT NULL, started_at REAL,
                finished_at REAL, detail TEXT NOT NULL)""")
            db.execute("INSERT INTO command_center_runs VALUES ('run-a',?,?,'RUNNING','test',0,?,?,NULL,'x')",
                       (agent.agent_id, tasks[0].task_id, now, now))
            db.execute("INSERT INTO command_center_runs VALUES ('run-b',?,?,'QUEUED','test',0,?,NULL,NULL,'x')",
                       (agent.agent_id, tasks[1].task_id, now))
            db.commit()

        self.start(providers={})
        runs = {r["run_id"]: r for r in self.service.runs()}
        self.assertEqual(runs["run-a"]["state"], "FAILED")
        self.assertEqual(runs["run-b"]["state"], "FAILED")
        self.assertIn("Not started", runs["run-b"]["detail"])
        with MultiAgentRuntimeStore(self.root / "runtime.db") as store:
            statuses = {store.get_task(t.task_id).status for t in tasks}
        self.assertNotIn(TaskStatus.RUNNING, statuses, "no task may claim to run after a restart")

    # -- real versus demo ------------------------------------------------------------
    def test_demo_agent_is_labeled_simulated_and_never_live(self) -> None:
        self.start(providers={"claude-cli": self.chat, "offline-demo": OfflineDemoProvider()})
        agent = self.create("Echo", provider="offline-demo")
        idle = self.status(agent)
        self.assertEqual((idle["code"], idle["label"], idle["real_provider"], idle["simulated"]),
                         ("idle", "Idle · demo", False, True))
        self.send(agent)
        busy = self.wait(lambda: self.status(agent) if self.status(agent)["code"] in {"working", "waiting"}
                         else None, "simulated work")
        self.assertFalse(busy["real_provider"])
        self.assertNotEqual(busy["label"], "Working", "simulation must not use the live label")
        self.assertEqual(self.turns(agent), [], "simulated work never creates a live turn")

    def test_demo_agent_without_its_adapter_is_unavailable_not_idle(self) -> None:
        self.start(providers={"claude-cli": self.chat, "offline-demo": OfflineDemoProvider()})
        agent = self.create("Echo", provider="offline-demo")
        self.restart(providers={"claude-cli": self.chat})
        status = self.status(agent)
        self.assertEqual(status["code"], "unavailable")
        self.assertIn("--demo", status["detail"])
        self.assertFalse(status["real_provider"])


class AgentStatusRulesTests(unittest.TestCase):
    """The pure status function, including orderings the service cannot easily reach."""

    def status(self, **overrides):
        args = {"lifecycle": "RUNNING", "provider_name": "codex-cli",
                "provider": {"connection": "CONNECTED", "simulated": False}, "registered": True,
                "live": None, "work": {}, "running_total": 0, "capacity": 2}
        return agent_status(**(args | overrides))

    def test_lifecycle_outranks_everything(self) -> None:
        live = {"counts": {"QUEUED": 1}, "running": None, "latest": None}
        self.assertEqual(self.status(lifecycle="STOPPED", live=live)["code"], "stopped")
        self.assertEqual(self.status(lifecycle="CREATED", registered=False)["code"], "not_enabled")

    def test_blocked_provider_is_unavailable_even_with_queued_work(self) -> None:
        live = {"counts": {"QUEUED": 2}, "running": None, "latest": None}
        status = self.status(provider={"connection": "BLOCKED", "reason": "Login expired."}, live=live)
        self.assertEqual((status["code"], status["detail"]), ("unavailable", "Login expired."))

    def test_a_later_success_clears_an_earlier_failure(self) -> None:
        latest = {"state": "COMPLETED", "error": None}
        status = self.status(live={"counts": {}, "running": None, "latest": latest})
        self.assertEqual(status["code"], "idle")

    def test_waiting_for_reply_outranks_queued(self) -> None:
        status = self.status(provider_name="offline-demo", provider={"simulated": True},
                             work={"AWAITING_REPLY": 1, "QUEUED": 1})
        self.assertEqual(status["code"], "waiting")


if __name__ == "__main__":
    unittest.main()
