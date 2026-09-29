"""Agent Hub concurrency: no cap on running agents or helpers, and provider start-up failures retry."""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jarvis.agent_hub import HubService
from jarvis.agent_hub_runtime import (MAX_PROVIDER_RESUMES, TRANSIENT_PROVIDER_BLOCKER, TRANSIENT_RETRY_DELAYS,
                                      classify_failure)

UNAVAILABLE = "model provider unavailable after automatic retries and fallbacks"


class Result(str):
    model = "claude-cli:claude-sonnet-5"
    tool_calls = 0

    def __new__(cls, text, status="complete", reason=""):
        value = super().__new__(cls, text)
        value.status, value.reason = status, reason
        return value


class HubFixture(unittest.TestCase):
    def open_service(self, **kwargs):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        service = HubService(state_dir=Path(tmp.name), provider_profile_dir=Path(tmp.name) / "profile",
                             runtime_kwargs={"autostart": False, "provider_probe": lambda _: {
                                 "installed": True, "authenticated": True, "detail": "t", "version": "t"}},
                             **kwargs)
        self.addCleanup(service.close)
        return service

    def agents(self, service, count):
        # Read-only agents: several writers in one project would wait for its write lock.
        return [service.create_agent({"name": f"A{i}", "role": "r", "provider": "claude-cli",
                                      "model": "claude-sonnet-5", "enable": True, "tool_groups": ["files_read"]})
                for i in range(count)]

    def run_all(self, runtime, runner_class):
        # A fresh runner per task, as the Hub builds one per task. The patches stay on for the
        # whole test: task threads build their runner after dispatch_once returns.
        self.runner_class = runner_class
        if not getattr(runtime, "_test_patched", False):
            runtime._test_patched = True
            for name, value in (("_agent_config", lambda *a, **k: SimpleNamespace()),
                                ("_open_memory", lambda *a, **k: SimpleNamespace()),
                                ("_make_client", lambda *a, **k: SimpleNamespace()),
                                ("_make_agent", lambda *a, **k: self.runner_class())):
                patcher = patch.object(runtime, name, side_effect=value)
                patcher.start()
                self.addCleanup(patcher.stop)
        runtime.dispatch_once()
        return list(runtime.running_ids())

    def wait_idle(self, runtime):
        deadline = time.monotonic() + 20
        while runtime.running_ids() and time.monotonic() < deadline:
            time.sleep(0.01)

    def blocking_runner(self, release):
        class Runner:
            def __init__(inner):
                inner.toolbox = SimpleNamespace(tools={}, execute=lambda name, arguments: '{"ok": true}')

            def run(inner, prompt, **kwargs):
                release.wait(5)
                return Result("done")
        return Runner


class HubConcurrencyTests(HubFixture):
    def test_every_agent_runs_at_once_by_default(self):
        service = self.open_service()
        runtime = service.runtime
        self.assertIsNone(runtime.capacity)
        for agent in self.agents(service, 6):
            runtime.create_task(agent["agent_id"], title="", request="work")
        release = threading.Event()
        try:
            self.assertEqual(len(self.run_all(runtime, self.blocking_runner(release))), 6)
            self.assertEqual(service.overview()["capacity"]["slots"], None)
        finally:
            release.set()
            self.wait_idle(runtime)

    def test_an_explicit_capacity_still_bounds_it(self):
        service = self.open_service(capacity=2)
        runtime = service.runtime
        for agent in self.agents(service, 4):
            runtime.create_task(agent["agent_id"], title="", request="work")
        release = threading.Event()
        try:
            self.assertEqual(len(self.run_all(runtime, self.blocking_runner(release))), 2)
        finally:
            release.set()
            self.wait_idle(runtime)

    def test_one_agent_still_runs_one_task_at_a_time(self):
        service = self.open_service()
        runtime = service.runtime
        agent = self.agents(service, 1)[0]
        for _ in range(3):
            runtime.create_task(agent["agent_id"], title="", request="work")
        release = threading.Event()
        try:
            self.assertEqual(len(self.run_all(runtime, self.blocking_runner(release))), 1)
        finally:
            release.set()
            self.wait_idle(runtime)


class ProviderStartupRetryTests(HubFixture):
    def test_an_unavailable_provider_is_a_retry_not_a_failure(self):
        self.assertEqual(classify_failure(UNAVAILABLE, "I cannot answer it yet"),
                         ("WAITING_PROVIDER", TRANSIENT_PROVIDER_BLOCKER))
        # Real failures are still failures.
        self.assertEqual(classify_failure("tool budget reached", "")[0], "FAILED")

    def test_a_burst_failure_retries_after_a_short_jittered_wait_and_then_completes(self):
        service = self.open_service()
        runtime = service.runtime
        agent = self.agents(service, 1)[0]
        task = runtime.create_task(agent["agent_id"], title="", request="write a short project plan for building a garden shed")
        outcomes = [Result("I cannot answer it yet", "incomplete", UNAVAILABLE), Result("397.8")]

        class Runner:
            def __init__(inner):
                inner.toolbox = SimpleNamespace(tools={}, execute=lambda name, arguments: '{"ok": true}')

            def run(inner, prompt, **kwargs):
                return outcomes.pop(0)

        runner = Runner
        before = time.time()
        self.run_all(runtime, runner)
        self.wait_idle(runtime)
        detail = runtime.task(task["task_id"])
        self.assertEqual(detail["state"], "WAITING_PROVIDER")
        self.assertEqual(detail["blocker"], TRANSIENT_PROVIDER_BLOCKER)
        with runtime.db() as db:
            retry_after = db.execute("SELECT retry_after FROM hub_tasks WHERE task_id=?",
                                     (task["task_id"],)).fetchone()[0]
        first = TRANSIENT_RETRY_DELAYS[0]
        self.assertGreaterEqual(retry_after, before + first)
        self.assertLessEqual(retry_after, time.time() + first * 1.5)
        # Not before the wait is over...
        self.run_all(runtime, runner)
        self.assertEqual(runtime.task(task["task_id"])["state"], "WAITING_PROVIDER")
        # ...and then it runs again and completes.
        with runtime.db() as db:
            db.execute("UPDATE hub_tasks SET retry_after=? WHERE task_id=?", (time.time() - 1, task["task_id"]))
        self.run_all(runtime, runner)
        self.wait_idle(runtime)
        detail = runtime.task(task["task_id"])
        self.assertEqual(detail["state"], "COMPLETED", detail.get("blocker"))
        self.assertEqual(detail["result"], "397.8")

    def test_it_gives_up_after_the_bounded_retries(self):
        service = self.open_service()
        runtime = service.runtime
        agent = self.agents(service, 1)[0]
        task = runtime.create_task(agent["agent_id"], title="", request="write a short project plan for building a garden shed")
        with runtime.db() as db:
            db.execute("UPDATE hub_tasks SET provider_resumes=? WHERE task_id=?",
                       (MAX_PROVIDER_RESUMES, task["task_id"]))

        class Runner:
            def __init__(inner):
                inner.toolbox = SimpleNamespace(tools={}, execute=lambda name, arguments: '{"ok": true}')

            def run(inner, prompt, **kwargs):
                return Result("I cannot answer it yet", "incomplete", UNAVAILABLE)

        self.run_all(runtime, Runner)
        self.wait_idle(runtime)
        detail = runtime.task(task["task_id"])
        self.assertEqual(detail["state"], "FAILED")
        self.assertIn(f"after {MAX_PROVIDER_RESUMES} automatic retries", detail["blocker"])


class NestedLockTests(unittest.TestCase):
    def test_no_write_block_opens_a_second_write_lock(self):
        """db() takes the write lock on entry; a nested db() on the same thread waits 15 s and fails.

        Found live: every turn's usage write called model_window() inside db(), cost 15 s and
        lost the record ("Usage not recorded: database is locked")."""
        import ast
        import inspect
        from jarvis import agent_hub_runtime

        tree = ast.parse(inspect.getsource(agent_hub_runtime.AgentRuntime))
        methods = {f.name: f for f in tree.body[0].body if isinstance(f, ast.FunctionDef)}

        def is_db_block(node):
            return isinstance(node, ast.With) and any(
                isinstance(i.context_expr, ast.Call) and isinstance(i.context_expr.func, ast.Attribute)
                and i.context_expr.func.attr == "db" for i in node.items)

        def self_calls(node):
            return {n.func.attr for n in ast.walk(node) if isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)
                    and n.func.value.id == "self"}

        opens = {name for name, f in methods.items() if any(is_db_block(n) for n in ast.walk(f))}
        grew = True
        while grew:
            grew = False
            for name, f in methods.items():
                if name not in opens and self_calls(f) & opens:
                    opens.add(name)
                    grew = True
        nested = [f"{name}:{block.lineno} -> {sorted(found)}" for name, f in methods.items()
                  for block in ast.walk(f) if is_db_block(block)
                  for found in [set().union(*(self_calls(s) for s in block.body)) & opens - {"db"}] if found]
        self.assertEqual(nested, [])

    def test_usage_without_a_reported_window_is_recorded_quickly(self):
        """A Claude CLI turn that reports no context window reads the stored window via _setting().

        That read opened a second db() inside the usage write: 16.5 s, then "database is locked".
        (Codex and OpenRouter windows come from their catalogues and never touched the database.)"""
        with tempfile.TemporaryDirectory() as tmp:
            service = HubService(state_dir=Path(tmp), provider_profile_dir=Path(tmp) / "profile",
                                 runtime_kwargs={"autostart": False, "provider_probe": lambda _: {
                                     "installed": True, "authenticated": True, "detail": "t", "version": "t"}})
            try:
                runtime = service.runtime
                agent = service.create_agent({"name": "C", "role": "r", "provider": "claude-cli",
                                              "model": "claude-sonnet-5", "enable": True, "tool_groups": ["files_read"]})
                task = runtime.create_task(agent["agent_id"], title="", request="work")
                task = dict(task, agent_id=agent["agent_id"])
                client = SimpleNamespace(claude_cli=SimpleNamespace(call_usage=[
                    {"context_tokens": 1200, "output_tokens": 40, "context_window": None}], rate_limits=None))
                memory = SimpleNamespace(recent_messages=lambda conversation_id, limit: [{"content": "hello"}])
                started = time.monotonic()
                runtime._record_turn_usage(task, client, "claude-cli:claude-sonnet-5", memory, 7, 10_000)
                self.assertLess(time.monotonic() - started, 3.0)
                with runtime.db() as db:
                    row = db.execute("SELECT * FROM hub_turn_usage WHERE task_id=?", (task["task_id"],)).fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(row["context_tokens"], 1200)
                self.assertEqual(row["transcript_chars"], 5)
                self.assertEqual(row["window_tokens"] if "window_tokens" in row.keys() else row[7],
                                 runtime.model_window("claude-cli", "claude-sonnet-5")["tokens"])
            finally:
                service.close()


if __name__ == "__main__":
    unittest.main()
