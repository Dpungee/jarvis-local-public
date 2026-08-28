from __future__ import annotations

import argparse
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from jarvis import cli
from jarvis.memory import Memory as DurableMemory
from jarvis.ollama_client import OllamaError


class _Cursor:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class _HealthDatabase:
    def execute(self, sql):
        if "quick_check" in sql:
            return _Cursor(("ok",))
        if "foreign_key_check" in sql:
            return _Cursor(None)
        raise AssertionError(f"unexpected query: {sql}")


class FakeMemory:
    instances = []
    tasks = []
    topics = []

    def __init__(self, path, *, worker_id=None):
        self.path = path
        self.worker_id = worker_id
        self.closed = False
        self.db = _HealthDatabase()
        type(self).instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()

    def close(self):
        self.closed = True

    def list_tasks(self, limit=None):
        return list(type(self).tasks)

    def list_learning_topics(self):
        return list(type(self).topics)

    def add_task(self, prompt):
        self.added_task = prompt
        return 11

    def add_learning_topic(self, topic, interval):
        self.added_topic = (topic, interval)
        return 12

    def set_learning_topic_enabled(self, topic_id, enabled):
        self.learning_enabled = (topic_id, enabled)
        return topic_id == 2


class WorkerMemory:
    def __init__(
        self,
        task,
        *,
        fail_status="queued",
        recovered=None,
        approval_wait_status="awaiting_approval",
    ):
        self.task = task
        self.fail_status = fail_status
        self.recovered = recovered or {"requeued": 0, "failed": 0}
        self.closed = False
        self.claim_calls = []
        self.finish_calls = []
        self.fail_calls = []
        self.renew_calls = []
        self.recover_calls = 0
        self.approval_wait_status = approval_wait_status
        self.await_approval_calls = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.closed = True

    def recover_stale_tasks(self):
        self.recover_calls += 1
        return self.recovered

    def queue_due_learning(self):
        return 0

    def claim_task(self, **kwargs):
        self.claim_calls.append(kwargs)
        task, self.task = self.task, None
        return task

    def finish_task(self, *args, **kwargs):
        self.finish_calls.append((args, kwargs))
        return True

    def fail_task(self, *args, **kwargs):
        self.fail_calls.append((args, kwargs))
        return self.fail_status

    def renew_task_lease(self, *args, **kwargs):
        self.renew_calls.append((args, kwargs))
        return True

    def await_task_approval(self, *args, **kwargs):
        self.await_approval_calls.append((args, kwargs))
        return self.approval_wait_status


class FakeHeartbeat:
    instances = []

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.lost = False
        self.started = False
        self.stopped = False
        type(self).instances.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


class _Result(str):
    def __new__(
        cls,
        content,
        *,
        status="complete",
        reason=None,
        retryable=False,
        waiting_for_approval=False,
        approval_id=None,
    ):
        value = str.__new__(cls, content)
        value.status = status
        value.reason = reason
        value.retryable = retryable
        value.waiting_for_approval = waiting_for_approval
        value.approval_id = approval_id
        return value


def fake_config():
    return SimpleNamespace(
        data_dir=Path("data"),
        workspace=Path("workspace"),
        soul_path=Path("SOUL.md"),
        fast_model="fast:1",
        reasoning_model="reason:1",
        coding_model="code:1",
        background_model="fast",
        learning_model=None,
        model="auto",
        autonomy="autonomous",
        ollama_url="http://127.0.0.1:11434",
        ollama_allow_remote=False,
        ollama_health_timeout=1.0,
        ollama_generation_timeout=2.0,
        ollama_max_response_bytes=4096,
        ollama_max_retries=1,
        ollama_retry_backoff=0.01,
    )


def task(attempt=1, maximum=4, prompt="do the work"):
    return {
        "id": 7,
        "prompt": prompt,
        "attempt_count": attempt,
        "max_attempts": maximum,
    }


class CliOfflineTests(unittest.TestCase):
    def setUp(self):
        FakeMemory.instances = []
        FakeMemory.tasks = []
        FakeMemory.topics = []

    def test_task_and_learning_lists_do_not_construct_agent(self):
        FakeMemory.tasks = [
            {
                "id": 1,
                "status": "queued",
                "attempt_count": 0,
                "max_attempts": 3,
                "prompt": "password=hunter2 inspect records",
                "result": "private result must not print",
            }
        ]
        FakeMemory.topics = [
            {"id": 2, "enabled": 1, "interval_hours": 24, "topic": "cafe research"}
        ]
        stdout = io.StringIO()
        with (
            patch.object(cli.Config, "load", return_value=fake_config()),
            patch.object(cli, "Memory", FakeMemory),
            patch.object(cli, "Agent", side_effect=AssertionError("Agent must stay offline")),
            patch("sys.stdout", stdout),
        ):
            cli.main(["task", "list"])
            cli.main(["learn", "list"])

        output = stdout.getvalue()
        self.assertNotIn("hunter2", output)
        self.assertNotIn("private result", output)
        self.assertIn("[redacted]", output)
        self.assertIn("cafe research", output)
        self.assertEqual(len(FakeMemory.instances), 2)
        self.assertTrue(all(item.closed for item in FakeMemory.instances))

    def test_learning_topics_can_be_enabled_and_disabled_offline(self):
        stdout = io.StringIO()
        with (
            patch.object(cli.Config, "load", return_value=fake_config()),
            patch.object(cli, "Memory", FakeMemory),
            patch.object(cli, "Agent", side_effect=AssertionError("Agent must stay offline")),
            patch("sys.stdout", stdout),
        ):
            cli.main(["learn", "disable", "2"])
            disabled = FakeMemory.instances[-1]
            cli.main(["learn", "enable", "2"])
            enabled = FakeMemory.instances[-1]

        self.assertEqual(disabled.learning_enabled, (2, False))
        self.assertEqual(enabled.learning_enabled, (2, True))
        self.assertIn("Learning topic #2 disabled", stdout.getvalue())
        self.assertIn("Learning topic #2 enabled", stdout.getvalue())

    def test_competence_command_renders_empty_populated_and_json_states(self):
        with tempfile.TemporaryDirectory() as directory:
            config = fake_config()
            config.data_dir = Path(directory)
            stdout = io.StringIO()
            with (
                patch.object(cli.Config, "load", return_value=config),
                patch("sys.stdout", stdout),
            ):
                self.assertEqual(cli._run_competence(
                    argparse.Namespace(family=None, json=False)
                ), 0)
            self.assertIn("No resolved predictions yet.", stdout.getvalue())

            with DurableMemory(config.data_dir / "jarvis.db") as memory:
                prediction_id = memory.record_prediction(
                    family="code_fix",
                    profile="coding",
                    model="qwen3-coder:30b",
                    predicted_success=0.65,
                    predicted_steps=12,
                    predicted_verification="process_evidence",
                )
                memory.resolve_prediction(
                    prediction_id,
                    actual_status="incomplete",
                    actual_steps=5,
                    evidence_ok=False,
                    failure_class="verification_absent",
                )

            stdout = io.StringIO()
            with (
                patch.object(cli.Config, "load", return_value=config),
                patch("sys.stdout", stdout),
            ):
                cli._run_competence(argparse.Namespace(family="code_fix", json=False))
            rendered = stdout.getvalue()
            self.assertIn("code_fix", rendered)
            self.assertIn("verification_absentx1", rendered)
            self.assertIn("prior calibration", rendered)

            stdout = io.StringIO()
            with (
                patch.object(cli.Config, "load", return_value=config),
                patch("sys.stdout", stdout),
            ):
                cli._run_competence(argparse.Namespace(family=None, json=True))
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["competence"][0]["family"], "code_fix")
            self.assertEqual(payload["open_predictions"], 0)

    def test_usage_command_renders_prompt_free_model_measurements(self):
        with tempfile.TemporaryDirectory() as directory:
            config = fake_config()
            config.data_dir = Path(directory)
            with DurableMemory(config.data_dir / "jarvis.db") as memory:
                memory.record_model_call(
                    provider="openai",
                    model="gpt-test",
                    profile="fast",
                    latency_ms=250,
                    prompt_tokens=100,
                    completion_tokens=25,
                    success=True,
                )
            stdout = io.StringIO()
            with (
                patch.object(cli.Config, "load", return_value=config),
                patch("sys.stdout", stdout),
            ):
                cli.main(["usage", "--all"])

            rendered = stdout.getvalue()
            self.assertIn("openai/gpt-test", rendered)
            self.assertIn("prompt/response content is not stored", rendered)
            self.assertIn("250", rendered)

    def test_task_show_redacts_legacy_persisted_secrets(self):
        secret = "sk-proj-" + "D" * 32
        FakeMemory.tasks = [{
            "id": 7,
            "status": "failed",
            "prompt": f"prompt {secret}",
            "result": f"result first\nsecond {secret}",
            "last_error": f"error {secret}",
        }]
        stdout = io.StringIO()
        with (
            patch.object(cli.Config, "load", return_value=fake_config()),
            patch.object(cli, "Memory", FakeMemory),
            patch("sys.stdout", stdout),
        ):
            cli.main(["task", "show", "7"])

        output = stdout.getvalue()
        self.assertNotIn(secret, output)
        self.assertEqual(output.count("[redacted]"), 3)
        self.assertIn("result first\nsecond [redacted]", output)

    def test_ask_incomplete_exits_nonzero_and_closes_memory(self):
        result = _Result("partial", status="incomplete", reason="needs evidence", retryable=True)
        agent = SimpleNamespace(run=Mock(return_value=result))
        with (
            patch.object(cli.Config, "load", return_value=fake_config()),
            patch.object(cli, "Memory", FakeMemory),
            patch.object(cli, "Agent", return_value=agent),
            patch("sys.stdout", io.StringIO()),
        ):
            with self.assertRaises(SystemExit) as raised:
                cli.main(["ask", "research", "it"])

        self.assertEqual(raised.exception.code, 2)
        self.assertTrue(FakeMemory.instances[-1].closed)
        agent.run.assert_called_once_with(
            "research it",
            model_override=None,
            allow_companion_control=True,
        )

    def test_ask_accepts_repeatable_validated_image_paths(self):
        result = _Result("described", status="complete")
        agent = SimpleNamespace(run=Mock(return_value=result))
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "screen.png"
            image_path.write_bytes(b"\x89PNG\r\n\x1a\nsynthetic")
            with (
                patch.object(cli.Config, "load", return_value=fake_config()),
                patch.object(cli, "Memory", FakeMemory),
                patch.object(cli, "Agent", return_value=agent),
                patch("sys.stdout", io.StringIO()),
            ):
                cli.main(["ask", "--image", str(image_path), "describe", "this"])

        call = agent.run.call_args
        self.assertEqual(call.args, ("describe this",))
        self.assertEqual(call.kwargs["model_override"], None)
        self.assertEqual(len(call.kwargs["attachments"]), 1)
        self.assertEqual(call.kwargs["attachments"][0].mime, "image/png")

    def test_ask_startup_failure_closes_memory(self):
        with (
            patch.object(cli.Config, "load", return_value=fake_config()),
            patch.object(cli, "Memory", FakeMemory),
            patch.object(cli, "Agent", side_effect=OllamaError("offline", retryable=True)),
            patch("sys.stderr", io.StringIO()),
        ):
            with self.assertRaises(SystemExit) as raised:
                cli.main(["ask", "hello"])

        self.assertEqual(raised.exception.code, 1)
        self.assertTrue(FakeMemory.instances[-1].closed)


class FirstRunProviderGateTests(unittest.TestCase):
    def test_foreground_model_command_can_run_the_interactive_chooser(self):
        args = argparse.Namespace(command="ask")
        with (
            patch.object(cli.sys.stdin, "isatty", return_value=True),
            patch.object(cli, "ensure_provider_ready") as ensure,
        ):
            cli._ensure_first_run_provider_setup(args)

        ensure.assert_called_once_with(interactive=True, stdin_isatty=True)

    def test_worker_first_run_is_always_headless(self):
        args = argparse.Namespace(command="worker")
        with (
            patch.object(cli.sys.stdin, "isatty", return_value=True),
            patch.object(cli, "ensure_provider_ready") as ensure,
        ):
            cli._ensure_first_run_provider_setup(args)

        ensure.assert_called_once_with(interactive=False, stdin_isatty=True)

    def test_provider_independent_command_does_not_run_the_chooser(self):
        with patch.object(cli, "ensure_provider_ready") as ensure:
            cli._ensure_first_run_provider_setup(argparse.Namespace(command="doctor"))
            cli._ensure_first_run_provider_setup(
                argparse.Namespace(command="training", training_command="status")
            )

        ensure.assert_not_called()

    def test_headless_first_run_exits_with_clear_setup_code(self):
        error = cli.ProviderSetupRequired("provider setup required")
        stderr = io.StringIO()
        with (
            patch.object(cli, "ensure_provider_ready", side_effect=error),
            patch.object(cli, "worker_pool", side_effect=AssertionError("must not start")),
            patch.object(cli.sys, "stderr", stderr),
        ):
            with self.assertRaises(SystemExit) as raised:
                cli.main(["worker"])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("provider setup required", stderr.getvalue())


class ForegroundLeaseTests(unittest.TestCase):
    def test_lease_is_visible_while_active_and_removed_afterward(self):
        data_dir = Path.cwd() / ".test-tmp"
        data_dir.mkdir(exist_ok=True)
        before = set(data_dir.glob(f"{cli.FOREGROUND_LEASE_PREFIX}*"))
        try:
            with cli._ForegroundLease(data_dir) as first:
                with cli._ForegroundLease(data_dir) as second:
                    self.assertNotEqual(first.path, second.path)
                    self.assertTrue(cli._foreground_request_active(data_dir))
                self.assertTrue(cli._foreground_request_active(data_dir))
            self.assertFalse(cli._foreground_request_active(data_dir))
        finally:
            for marker in set(data_dir.glob(f"{cli.FOREGROUND_LEASE_PREFIX}*")) - before:
                marker.unlink(missing_ok=True)

    def test_malformed_stale_and_future_markers_are_ignored(self):
        data_dir = Path.cwd() / ".test-tmp"
        data_dir.mkdir(exist_ok=True)
        token = "123-" + "a" * 32
        marker = data_dir / (
            f"{cli.FOREGROUND_LEASE_PREFIX}{token}{cli.FOREGROUND_LEASE_SUFFIX}"
        )
        malformed = data_dir / (
            f"{cli.FOREGROUND_LEASE_PREFIX}bad{cli.FOREGROUND_LEASE_SUFFIX}"
        )
        try:
            marker.write_text(f"1.0 123 {token}\n", encoding="utf-8")
            malformed.write_text("not a lease", encoding="utf-8")
            self.assertFalse(cli._foreground_request_active(data_dir, now=100.0))
            self.assertFalse(marker.exists())
            self.assertTrue(malformed.exists())
            marker.write_text(f"200.0 123 {token}\n", encoding="utf-8")
            self.assertFalse(cli._foreground_request_active(data_dir, now=100.0))
        finally:
            marker.unlink(missing_ok=True)
            malformed.unlink(missing_ok=True)


class WorkerTests(unittest.TestCase):
    def test_worker_pool_starts_bounded_independent_slots(self):
        with tempfile.TemporaryDirectory() as directory:
            config = fake_config()
            config.data_dir = Path(directory)
            config.worker_concurrency = 3
            barrier = threading.Barrier(3)
            lock = threading.Lock()
            active = 0
            maximum = 0
            calls = []

            def fake_worker(_poll, **kwargs):
                nonlocal active, maximum
                with lock:
                    active += 1
                    maximum = max(maximum, active)
                    calls.append(kwargs)
                barrier.wait(timeout=2)
                with lock:
                    active -= 1
                return 0

            with (
                patch.object(cli.Config, "load", return_value=config),
                patch.object(cli, "worker", side_effect=fake_worker),
            ):
                code = cli.worker_pool(1)

            self.assertEqual(code, 0)
            self.assertEqual(maximum, 3)
            self.assertEqual(len(calls), 3)
            self.assertEqual(sum(bool(item["status_heartbeat"]) for item in calls), 1)
            self.assertTrue(all(not item["manage_process_lock"] for item in calls))

    def setUp(self):
        FakeHeartbeat.instances = []
        foreground = patch.object(
            cli, "_foreground_request_active", return_value=False
        )
        foreground.start()
        self.addCleanup(foreground.stop)

    def run_worker(self, memory, agent_factory, *, cycles=1, sleeps=None):
        waits = [] if sleeps is None else sleeps
        with patch.object(cli.Config, "load", return_value=fake_config()):
            code = cli.worker(
                1,
                max_cycles=cycles,
                sleep=waits.append,
                memory_factory=lambda *args, **kwargs: memory,
                agent_factory=agent_factory,
                heartbeat_factory=FakeHeartbeat,
            )
        return code, waits

    def test_complete_result_uses_owner_lease_and_finishes(self):
        memory = WorkerMemory(task(), recovered={"requeued": 1, "failed": 0})
        agent = SimpleNamespace(run=Mock(return_value=_Result("done")))
        code, _ = self.run_worker(memory, lambda *args: agent)

        self.assertEqual(code, 0)
        self.assertTrue(memory.closed)
        self.assertEqual(memory.recover_calls, 1)
        claim = memory.claim_calls[0]
        self.assertTrue(claim["worker_id"].startswith("worker:"))
        self.assertEqual(claim["lease_seconds"], cli.WORKER_LEASE_SECONDS)
        args, kwargs = memory.finish_calls[0]
        self.assertEqual(args, (7, "done"))
        self.assertEqual(kwargs["worker_id"], claim["worker_id"])
        self.assertFalse(memory.fail_calls)
        self.assertTrue(FakeHeartbeat.instances[0].started)
        self.assertTrue(FakeHeartbeat.instances[0].stopped)

    def test_deep_research_learning_tasks_use_reasoning_profile(self):
        deep_learning_prompt = (
            "Continuously learn about this topic: local agents. "
            "Research current, authoritative sources."
        )
        learning_memory = WorkerMemory(task(prompt=deep_learning_prompt))
        learning_agent = SimpleNamespace(run=Mock(return_value=_Result("learned")))
        code, _ = self.run_worker(learning_memory, lambda *args: learning_agent)
        self.assertEqual(code, 0)
        learning_args, learning_kwargs = learning_agent.run.call_args
        self.assertEqual(learning_args, (deep_learning_prompt,))
        self.assertEqual(learning_kwargs["model_override"], "reasoning")
        self.assertEqual(learning_kwargs["task_id"], 7)
        self.assertFalse(learning_kwargs["cancellation_guard"]())

    def test_all_learning_uses_reasoning_profile_without_overriding_normal_tasks(self):
        learning_prompt = "Continuously learn about this topic: local agents."
        learning_memory = WorkerMemory(task(prompt=learning_prompt))
        learning_agent = SimpleNamespace(run=Mock(return_value=_Result("learned")))
        code, _ = self.run_worker(learning_memory, lambda *args: learning_agent)
        self.assertEqual(code, 0)
        learning_args, learning_kwargs = learning_agent.run.call_args
        self.assertEqual(learning_args, (learning_prompt,))
        self.assertEqual(learning_kwargs["model_override"], "reasoning")
        self.assertEqual(learning_kwargs["task_id"], 7)
        self.assertFalse(learning_kwargs["cancellation_guard"]())

        normal_memory = WorkerMemory(task(prompt="do normal work"))
        normal_agent = SimpleNamespace(run=Mock(return_value=_Result("done")))
        code, _ = self.run_worker(normal_memory, lambda *args: normal_agent)
        self.assertEqual(code, 0)
        normal_args, normal_kwargs = normal_agent.run.call_args
        self.assertEqual(normal_args, ("do normal work",))
        self.assertEqual(normal_kwargs["task_id"], 7)
        self.assertFalse(normal_kwargs["cancellation_guard"]())

    def test_dedicated_learning_model_overrides_deep_learning_profile(self):
        learning_prompt = (
            "Continuously learn about this topic: defensive security. "
            "Research current, authoritative sources."
        )
        learning_memory = WorkerMemory(task(prompt=learning_prompt))
        learning_agent = SimpleNamespace(run=Mock(return_value=_Result("learned")))
        config = fake_config()
        config.learning_model = "openai:gpt-5.6-luna"
        with patch.object(cli.Config, "load", return_value=config):
            code = cli.worker(
                1,
                max_cycles=1,
                memory_factory=lambda *args, **kwargs: learning_memory,
                agent_factory=lambda *args: learning_agent,
                heartbeat_factory=FakeHeartbeat,
            )
        self.assertEqual(code, 0)
        self.assertEqual(
            learning_agent.run.call_args.kwargs["model_override"],
            "openai:gpt-5.6-luna",
        )

    def test_finished_learning_refreshes_export_without_changing_task_result(self):
        learning_prompt = "Continuously learn about this topic: safe agents."
        memory = WorkerMemory(task(prompt=learning_prompt))
        manifest = {"total_examples": 7}
        with patch.object(cli, "export_verified_dataset", return_value=manifest) as export:
            code, _ = self.run_worker(
                memory,
                lambda *args: SimpleNamespace(run=lambda *args, **kwargs: _Result("learned")),
            )
        self.assertEqual(code, 0)
        self.assertEqual(len(memory.finish_calls), 1)
        export.assert_called_once_with(memory, Path("data") / "training_export")

    def test_continuous_worker_refreshes_status_heartbeat_and_caps_idle_wait(self):
        class StopAfterWait:
            def __init__(self):
                self.stopped = False
                self.waits = []

            def is_set(self):
                return self.stopped

            def wait(self, seconds):
                self.waits.append(seconds)
                self.stopped = True

        memory = WorkerMemory(None)
        stop = StopAfterWait()
        heartbeat_writer = Mock()
        process_lock = Mock()
        process_lock.acquire.return_value = True
        with (
            patch.object(cli.Config, "load", return_value=fake_config()),
            patch.object(cli, "_write_worker_heartbeat", heartbeat_writer),
            patch.object(cli, "_WorkerProcessLock", return_value=process_lock),
            patch.object(cli.time, "monotonic", side_effect=[0.0, 31.0, 31.1]),
        ):
            code = cli.worker(
                3600,
                stop_event=stop,
                memory_factory=lambda *args, **kwargs: memory,
                agent_factory=lambda *args: SimpleNamespace(run=Mock()),
            )

        self.assertEqual(code, 0)
        self.assertEqual(heartbeat_writer.call_count, 2)
        self.assertEqual(stop.waits, [cli.WORKER_STATUS_HEARTBEAT_SECONDS])
        process_lock.acquire.assert_called_once_with()
        process_lock.close.assert_called_once_with()

    def test_worker_yields_without_claiming_while_foreground_is_active(self):
        memory = WorkerMemory(task())
        with patch.object(cli, "_foreground_request_active", return_value=True):
            code, waits = self.run_worker(
                memory,
                lambda *args: SimpleNamespace(run=Mock()),
            )

        self.assertEqual(code, 0)
        self.assertEqual(memory.claim_calls, [])
        self.assertEqual(waits, [cli.FOREGROUND_YIELD_SECONDS])

    def test_cloud_only_profiles_allow_background_and_foreground_concurrency(self):
        config = fake_config()
        config.fast_model = "openai:gpt-5.6-luna"
        config.reasoning_model = "openai:gpt-5.6-terra"
        config.coding_model = "openai:gpt-5.6-sol"
        config.deep_model = "openai:gpt-5.6-sol"
        config.learning_model = "openai:gpt-5.6-luna"
        self.assertTrue(cli._all_routed_models_cloud(config))
        config.deep_model = "qwen3:30b"
        self.assertFalse(cli._all_routed_models_cloud(config))

    def test_subscription_cli_profiles_allow_background_and_foreground_concurrency(self):
        config = fake_config()
        config.fast_model = "codex-cli:auto"
        config.reasoning_model = "claude-cli:sonnet"
        config.coding_model = "codex-cli:auto"
        config.deep_model = "codex-cli:auto"
        config.learning_model = "claude-cli:haiku"
        self.assertTrue(cli._all_routed_models_cloud(config))

    def test_worker_integrates_with_durable_memory_for_complete_and_incomplete_results(self):
        class SnapshotMemory(DurableMemory):
            def __exit__(self, exc_type, exc, traceback):
                self.snapshot = self.list_tasks()
                super().__exit__(exc_type, exc, traceback)

        memory = SnapshotMemory(Path(":memory:"))
        memory.add_task("complete task")
        memory.add_task("incomplete task")

        def answer(prompt, **_kwargs):
            if prompt == "complete task":
                return _Result("verified output")
            return _Result(
                "partial",
                status="incomplete",
                reason="cannot verify",
                retryable=False,
            )

        code, _ = self.run_worker(memory, lambda *args: SimpleNamespace(run=answer), cycles=2)

        rows = {row["prompt"]: row for row in memory.snapshot}
        self.assertEqual(code, 0)
        self.assertTrue(memory.closed)
        self.assertEqual(rows["complete task"]["status"], "done")
        self.assertEqual(rows["complete task"]["result"], "verified output")
        self.assertEqual(rows["incomplete task"]["status"], "failed")
        self.assertEqual(rows["incomplete task"]["result"], "cannot verify")

    def test_incomplete_result_retries_with_bounded_backoff(self):
        memory = WorkerMemory(task(attempt=3), fail_status="queued")
        result = _Result(
            "partial",
            status="incomplete",
            reason="password=supersecret missing proof",
            retryable=True,
        )
        code, _ = self.run_worker(
            memory,
            lambda *args: SimpleNamespace(run=lambda prompt, **_kwargs: result),
        )

        self.assertEqual(code, 0)
        self.assertFalse(memory.finish_calls)
        args, kwargs = memory.fail_calls[0]
        self.assertEqual(args[0], 7)
        self.assertNotIn("supersecret", args[1])
        self.assertTrue(kwargs["retry"])
        self.assertEqual(kwargs["retry_delay_seconds"], 20)

    def test_approval_blocked_result_parks_without_finishing_or_retry_backoff(self):
        memory = WorkerMemory(task())
        result = _Result(
            "waiting",
            status="incomplete",
            reason="Approval request #42 is waiting",
            waiting_for_approval=True,
            approval_id=42,
        )

        code, _ = self.run_worker(
            memory,
            lambda *args: SimpleNamespace(run=lambda prompt, **_kwargs: result),
        )

        self.assertEqual(code, 0)
        self.assertEqual(
            memory.await_approval_calls,
            [((7, 42), {"worker_id": memory.claim_calls[0]["worker_id"]})],
        )
        self.assertEqual(memory.finish_calls, [])
        self.assertEqual(memory.fail_calls, [])

    def test_exception_never_marks_done_or_logs_prompt_and_secret(self):
        secret_prompt = "password=topsecret perform private task"
        memory = WorkerMemory(task(prompt=secret_prompt), fail_status="queued")

        class BrokenAgent:
            def run(self, prompt, **_kwargs):
                raise RuntimeError(f"failure while processing {prompt}")

        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("sys.stdout", stdout), patch("sys.stderr", stderr):
            code, _ = self.run_worker(memory, lambda *args: BrokenAgent())

        self.assertEqual(code, 0)
        self.assertFalse(memory.finish_calls)
        self.assertEqual(len(memory.fail_calls), 1)
        self.assertEqual(memory.fail_calls[0][0][1], "RuntimeError while running task")
        combined = stdout.getvalue() + stderr.getvalue()
        self.assertNotIn(secret_prompt, combined)
        self.assertNotIn("topsecret", combined)

    def test_keyboard_interrupt_stops_even_when_cleanup_fails(self):
        memory = WorkerMemory(task())

        def fail_cleanup(*args, **kwargs):
            raise RuntimeError("database unavailable")

        memory.fail_task = fail_cleanup

        class InterruptedAgent:
            def run(self, prompt, **_kwargs):
                raise KeyboardInterrupt

        stderr = io.StringIO()
        with patch("sys.stderr", stderr):
            code, _ = self.run_worker(memory, lambda *args: InterruptedAgent())
        self.assertEqual(code, 0)
        self.assertTrue(memory.closed)
        self.assertTrue(FakeHeartbeat.instances[-1].stopped)
        self.assertIn("interruption cleanup hit RuntimeError", stderr.getvalue())

    def test_worker_survives_ollama_startup_outage(self):
        memory = WorkerMemory(task())
        calls = 0

        def agent_factory(*args):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OllamaError("not ready", retryable=True)
            return SimpleNamespace(run=lambda prompt, **_kwargs: _Result("recovered"))

        stderr = io.StringIO()
        with patch("sys.stderr", stderr):
            code, waits = self.run_worker(memory, agent_factory, cycles=2)
        self.assertEqual(code, 0)
        self.assertEqual(calls, 2)
        self.assertEqual(waits, [1])
        self.assertEqual(len(memory.claim_calls), 1)
        self.assertEqual(len(memory.finish_calls), 1)
        self.assertIn("Model provider is unavailable", stderr.getvalue())

    def test_worker_reports_non_ollama_startup_failure_truthfully(self):
        memory = WorkerMemory(task())
        calls = 0

        def agent_factory(*args):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise AttributeError("missing provider adapter")
            return SimpleNamespace(run=lambda prompt, **_kwargs: _Result("recovered"))

        stderr = io.StringIO()
        with patch("sys.stderr", stderr):
            code, waits = self.run_worker(memory, agent_factory, cycles=2)

        self.assertEqual(code, 0)
        self.assertEqual(waits, [1])
        self.assertIn("JARVIS agent initialization failed", stderr.getvalue())
        self.assertIn("AttributeError", stderr.getvalue())
        self.assertNotIn("Model provider is unavailable", stderr.getvalue())

    def test_heartbeat_start_failure_requeues_without_running_agent(self):
        memory = WorkerMemory(task(), fail_status="queued")
        agent = SimpleNamespace(run=Mock(return_value=_Result("must not run")))

        class BrokenHeartbeat(FakeHeartbeat):
            def start(self):
                raise RuntimeError("thread unavailable")

        with (
            patch.object(cli.Config, "load", return_value=fake_config()),
            patch("sys.stderr", io.StringIO()),
        ):
            code = cli.worker(
                1,
                max_cycles=1,
                sleep=lambda seconds: None,
                memory_factory=lambda *args, **kwargs: memory,
                agent_factory=lambda *args: agent,
                heartbeat_factory=BrokenHeartbeat,
            )

        self.assertEqual(code, 0)
        agent.run.assert_not_called()
        self.assertFalse(memory.finish_calls)
        args, kwargs = memory.fail_calls[0]
        self.assertEqual(args, (7, "RuntimeError starting lease heartbeat"))
        self.assertTrue(kwargs["retry"])
        self.assertEqual(kwargs["retry_delay_seconds"], 5)

    def test_lost_lease_refuses_to_record_result(self):
        memory = WorkerMemory(task())
        memory.renew_task_lease = Mock(return_value=False)

        class LostHeartbeat(FakeHeartbeat):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.lost = True

        with patch.object(cli.Config, "load", return_value=fake_config()):
            code = cli.worker(
                1,
                max_cycles=1,
                sleep=lambda seconds: None,
                memory_factory=lambda *args, **kwargs: memory,
                agent_factory=lambda *args: SimpleNamespace(
                    run=lambda prompt, **_kwargs: _Result("unsafe")
                ),
                heartbeat_factory=LostHeartbeat,
            )
        self.assertEqual(code, 0)
        self.assertFalse(memory.finish_calls)
        self.assertFalse(memory.fail_calls)
        memory.renew_task_lease.assert_called_once()

    def test_worker_guard_aborts_agent_when_heartbeat_is_lost(self):
        memory = WorkerMemory(task(), fail_status=None)
        observed = []

        class GuardAwareAgent:
            def run(self, prompt, *, cancellation_guard, task_id):
                heartbeat = FakeHeartbeat.instances[-1]
                observed.append(task_id)
                observed.append(cancellation_guard())
                heartbeat.lost = True
                observed.append(cancellation_guard())
                raise RuntimeError("guarded agent stopped")

        with patch.object(cli.Config, "load", return_value=fake_config()):
            code = cli.worker(
                1,
                max_cycles=1,
                sleep=lambda seconds: None,
                memory_factory=lambda *args, **kwargs: memory,
                agent_factory=lambda *args: GuardAwareAgent(),
                heartbeat_factory=FakeHeartbeat,
            )

        self.assertEqual(code, 0)
        self.assertEqual(observed, [7, False, True])
        self.assertFalse(memory.finish_calls)
        self.assertEqual(memory.fail_calls[0][0][1], "RuntimeError while running task")

    def test_worker_guard_aborts_agent_when_pool_stop_is_requested(self):
        memory = WorkerMemory(task(), fail_status=None)
        stop = threading.Event()
        observed = []

        class GuardAwareAgent:
            def run(self, prompt, *, cancellation_guard, task_id):
                del prompt, task_id
                observed.append(cancellation_guard())
                stop.set()
                observed.append(cancellation_guard())
                raise RuntimeError("worker pool stopped")

        with patch.object(cli.Config, "load", return_value=fake_config()):
            code = cli.worker(
                1,
                max_cycles=1,
                stop_event=stop,
                sleep=lambda seconds: None,
                memory_factory=lambda *args, **kwargs: memory,
                agent_factory=lambda *args: GuardAwareAgent(),
                heartbeat_factory=FakeHeartbeat,
            )

        self.assertEqual(code, 0)
        self.assertEqual(observed, [False, True])
        self.assertFalse(memory.finish_calls)

    def test_retry_delay_is_exponential_and_bounded(self):
        self.assertEqual(cli._retry_delay(1), 5)
        self.assertEqual(cli._retry_delay(3), 20)
        self.assertEqual(cli._retry_delay(100), 300)


class ValidationAndDoctorTests(unittest.TestCase):
    def test_training_status_prints_readiness_and_blockers_without_agent(self):
        class EmptyTrainingMemory(FakeMemory):
            def list_training_examples(self, *, verified_only=False):
                return []

            def list_evaluation_cases(self):
                return []

        stdout = io.StringIO()
        with (
            patch.object(cli.Config, "load", return_value=fake_config()),
            patch.object(cli, "Memory", EmptyTrainingMemory),
            patch.object(
                cli,
                "Agent",
                side_effect=AssertionError("status must stay read-only"),
            ),
            patch("sys.stdout", stdout),
        ):
            cli.main(["training", "status"])

        output = stdout.getvalue()
        self.assertIn("Ready for candidate training: no", output)
        self.assertIn("Need at least 100 verified examples", output)
        self.assertIn("Need at least 70 training examples", output)
        self.assertIn("Need at least 10 validation examples", output)
        self.assertIn("Need at least 10 test examples", output)
        self.assertIn("Need at least 10 enabled evaluation cases", output)
        self.assertTrue(EmptyTrainingMemory.instances[-1].closed)

    def test_training_status_prints_yes_without_blockers(self):
        status = {
            "verified": 100,
            "total": 100,
            "splits": {"train": 80, "validation": 10, "test": 10},
            "task_kinds": {"coding": 34, "local": 33, "research": 33},
            "evaluation_cases": 10,
            "training_eligible": 100,
            "source_quarantined": 0,
            "quality_quarantined": 0,
            "quarantine_reasons": {},
            "ready_for_candidate_training": True,
            "readiness_blockers": [],
        }
        stdout = io.StringIO()
        with (
            patch.object(cli.Config, "load", return_value=fake_config()),
            patch.object(cli, "Memory", FakeMemory),
            patch.object(cli, "dataset_status", return_value=status),
            patch("sys.stdout", stdout),
        ):
            cli.main(["training", "status"])

        output = stdout.getvalue()
        self.assertIn("Ready for candidate training: yes", output)
        self.assertNotIn("\n  - ", output)

    def test_benchmark_disables_training_recording(self):
        class BenchmarkMemory(FakeMemory):
            def list_evaluation_cases(self):
                return [{
                    "id": 1,
                    "name": "identity",
                    "prompt": "Who are you?",
                    "expected_contains_json": '["JARVIS", "local"]',
                    "enabled": 1,
                }]

        config = fake_config()
        agent = SimpleNamespace(
            run=Mock(return_value=_Result("JARVIS runs local"))
        )
        agent_factory = Mock(return_value=agent)
        with (
            patch.object(cli.Config, "load", return_value=config),
            patch.object(cli, "Memory", BenchmarkMemory),
            patch.object(cli, "replace", return_value=config),
            patch.object(cli, "Agent", agent_factory),
            patch("sys.stdout", io.StringIO()),
        ):
            cli.main(["training", "benchmark"])

        self.assertFalse(agent_factory.call_args.kwargs.get("record_training", True))
        self.assertEqual(agent_factory.call_args.kwargs.get("temperature"), 0.0)
        agent.run.assert_called_once_with(
            "Who are you?",
            model_override=None,
            prediction_origin="practice",
        )

    def test_poll_and_learning_intervals_are_strict(self):
        for value in ("0", "3601", "nope"):
            with self.subTest(value=value):
                with self.assertRaises(argparse.ArgumentTypeError):
                    cli._poll_argument(value)
        for value in ("0", "8761", "1.5"):
            with self.subTest(value=value):
                with self.assertRaises(argparse.ArgumentTypeError):
                    cli._learning_interval(value)
        with patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                cli.main(["worker", "--poll", "0"])
        self.assertEqual(raised.exception.code, 2)

    def test_doctor_requires_every_specialist_model(self):
        client = SimpleNamespace(models=Mock(return_value=["fast:1"]))
        stdout = io.StringIO()
        with (
            patch.object(cli.Config, "load", return_value=fake_config()),
            patch.object(cli, "_local_health_errors", return_value=[]),
            patch.object(cli, "_new_client", return_value=client),
            patch("sys.stdout", stdout),
        ):
            code = cli.doctor()
        self.assertEqual(code, 1)
        output = stdout.getvalue()
        self.assertIn("Reasoning: reason:1 (missing)", output)
        self.assertIn("Coding: code:1 (missing)", output)
        self.assertIn("Status: not ready", output)

    def test_doctor_rejects_online_ollama_with_no_models(self):
        client = SimpleNamespace(models=Mock(return_value=[]))
        with (
            patch.object(cli.Config, "load", return_value=fake_config()),
            patch.object(cli, "_local_health_errors", return_value=[]),
            patch.object(cli, "_new_client", return_value=client),
            patch("sys.stdout", io.StringIO()),
        ):
            self.assertEqual(cli.doctor(), 1)

    def test_doctor_requires_explicit_selected_model(self):
        config = fake_config()
        config.model = "custom:9b"
        client = SimpleNamespace(models=Mock(return_value=["fast:1", "reason:1", "code:1"]))
        stdout = io.StringIO()
        with (
            patch.object(cli.Config, "load", return_value=config),
            patch.object(cli, "_local_health_errors", return_value=[]),
            patch.object(cli, "_new_client", return_value=client),
            patch("sys.stdout", stdout),
        ):
            self.assertEqual(cli.doctor(), 1)
        self.assertIn("Selected: custom:9b (missing)", stdout.getvalue())

    def test_doctor_accepts_cloud_profiles_when_their_keys_are_configured(self):
        config = fake_config()
        config.fast_model = "openai:gpt-5.6-luna"
        config.reasoning_model = "openai:gpt-5.6"
        config.coding_model = "anthropic:claude-sonnet-5"
        client = SimpleNamespace(
            models=Mock(return_value=[
                "openai:gpt-5.6",
                "anthropic:claude-sonnet-5",
            ]),
            provider_status={
                "ollama_online": False,
                "ollama_model_count": 0,
                "openai_configured": True,
                "anthropic_configured": True,
            },
        )
        stdout = io.StringIO()
        with (
            patch.object(cli.Config, "load", return_value=config),
            patch.object(cli, "_local_health_errors", return_value=[]),
            patch.object(cli, "_new_client", return_value=client),
            patch("sys.stdout", stdout),
        ):
            self.assertEqual(cli.doctor(), 0)
        output = stdout.getvalue()
        self.assertIn("OpenAI: API key configured", output)
        self.assertIn("Anthropic: API key configured", output)
        self.assertIn("Fast: openai:gpt-5.6-luna (ready)", output)
        self.assertIn("Status: ready", output)

    def test_doctor_reports_intentionally_disabled_ollama(self):
        config = fake_config()
        config.fast_model = "openai:gpt-5.6-luna"
        config.reasoning_model = "openai:gpt-5.6-terra"
        config.coding_model = "openai:gpt-5.6-sol"
        client = SimpleNamespace(
            models=Mock(return_value=["openai:gpt-5.6"]),
            provider_status={
                "ollama_enabled": False,
                "ollama_online": False,
                "ollama_model_count": 0,
                "openai_configured": True,
                "anthropic_configured": False,
            },
        )
        stdout = io.StringIO()
        with (
            patch.object(cli.Config, "load", return_value=config),
            patch.object(cli, "_local_health_errors", return_value=[]),
            patch.object(cli, "_new_client", return_value=client),
            patch("sys.stdout", stdout),
        ):
            self.assertEqual(cli.doctor(), 0)

        self.assertIn("Ollama: disabled", stdout.getvalue())

    def test_doctor_accepts_codex_subscription_profiles(self):
        config = fake_config()
        config.fast_model = "codex-cli:auto"
        config.reasoning_model = "codex-cli:auto"
        config.coding_model = "codex-cli:auto"
        client = SimpleNamespace(
            models=Mock(return_value=["codex-cli:auto"]),
            provider_status={
                "ollama_enabled": False,
                "ollama_online": False,
                "ollama_model_count": 0,
                "openai_configured": False,
                "anthropic_configured": False,
                "codex_cli_configured": True,
                "codex_cli_auth_method": "chatgpt",
                "claude_cli_configured": False,
            },
        )
        stdout = io.StringIO()
        with (
            patch.object(cli.Config, "load", return_value=config),
            patch.object(cli, "_local_health_errors", return_value=[]),
            patch.object(cli, "_new_client", return_value=client),
            patch("sys.stdout", stdout),
        ):
            self.assertEqual(cli.doctor(), 0)

        output = stdout.getvalue()
        self.assertIn("Codex CLI: ChatGPT subscription verified", output)
        self.assertIn("Fast: codex-cli:auto (ready)", output)

    def test_doctor_rejects_codex_api_key_auth_for_subscription_profiles(self):
        config = fake_config()
        config.fast_model = "codex-cli:auto"
        config.reasoning_model = "codex-cli:auto"
        config.coding_model = "codex-cli:auto"
        client = SimpleNamespace(
            models=Mock(return_value=["codex-cli:auto"]),
            provider_status={
                "ollama_enabled": False,
                "ollama_online": False,
                "ollama_model_count": 0,
                "openai_configured": False,
                "anthropic_configured": False,
                "codex_cli_configured": True,
                "codex_cli_auth_method": "api-key",
                "claude_cli_configured": False,
            },
        )
        stdout = io.StringIO()
        with (
            patch.object(cli.Config, "load", return_value=config),
            patch.object(cli, "_local_health_errors", return_value=[]),
            patch.object(cli, "_new_client", return_value=client),
            patch("sys.stdout", stdout),
        ):
            self.assertEqual(cli.doctor(), 1)

        self.assertIn("needs a verified ChatGPT sign-in", stdout.getvalue())

    def test_doctor_checks_dedicated_learning_model(self):
        config = fake_config()
        config.learning_model = "openai:gpt-5.6-luna"
        client = SimpleNamespace(
            models=Mock(return_value=[
                "fast:1",
                "reason:1",
                "code:1",
                "openai:gpt-5.6-luna",
            ]),
            provider_status={
                "ollama_online": False,
                "ollama_model_count": 0,
                "openai_configured": True,
                "anthropic_configured": False,
            },
        )
        stdout = io.StringIO()
        with (
            patch.object(cli.Config, "load", return_value=config),
            patch.object(cli, "_local_health_errors", return_value=[]),
            patch.object(cli, "_new_client", return_value=client),
            patch("sys.stdout", stdout),
        ):
            self.assertEqual(cli.doctor(), 0)
        self.assertIn(
            "Learning: openai:gpt-5.6-luna (ready)", stdout.getvalue()
        )

    def test_deep_doctor_reports_canaries_drift_and_self_inspection(self):
        class DeepMemory(FakeMemory):
            def drift_report(self):
                return [{
                    "family": "code_fix",
                    "signals": [{"signal": "evidence_rate_drop"}],
                }]

        config = fake_config()
        config.self_inspect = "read-only"
        client = SimpleNamespace(
            models=Mock(return_value=["fast:1", "reason:1", "code:1"])
        )
        stdout = io.StringIO()
        with (
            patch.object(cli.Config, "load", return_value=config),
            patch.object(cli, "_local_health_errors", return_value=[]),
            patch.object(cli, "_new_client", return_value=client),
            patch.object(cli, "Memory", DeepMemory),
            patch.object(
                cli,
                "run_capability_canaries",
                return_value=[{"tool": "list_files", "status": "pass"}],
            ),
            patch("sys.stdout", stdout),
        ):
            self.assertEqual(cli.doctor(deep=True), 0)

        output = stdout.getvalue()
        self.assertIn("Capability canaries: 1 passed, 0 failed", output)
        self.assertIn("code_fix: evidence_rate_drop", output)
        self.assertIn("Self-inspection: read-only", output)

    def test_local_health_check_closes_database(self):
        FakeMemory.instances = []
        with tempfile.TemporaryDirectory(prefix="jarvis-health-") as temp_dir:
            config = fake_config()
            config.data_dir = Path(temp_dir) / "data"
            config.workspace = Path(temp_dir) / "workspace"
            config.data_dir.mkdir()
            config.workspace.mkdir()
            with (
                patch.object(cli, "Memory", FakeMemory),
                patch.object(cli, "_directory_writable", return_value=True),
            ):
                errors = cli._local_health_errors(config)
        self.assertEqual(errors, [])
        self.assertTrue(FakeMemory.instances[-1].closed)

    def test_directory_probe_reports_cleanup_failure_without_raising(self):
        with (
            patch.object(cli.tempfile, "mkstemp", return_value=(99, "probe")),
            patch.object(cli.os, "close", side_effect=OSError("close failed")),
            patch.object(Path, "unlink", side_effect=OSError("unlink failed")),
        ):
            self.assertFalse(cli._directory_writable(Path(".")))

    def test_summary_preserves_unicode_and_redacts_secrets(self):
        result = cli._safe_summary("naive cafe - password=secret123 - bearer abcdefghijkl")
        self.assertIn("naive cafe", result)
        self.assertNotIn("secret123", result)
        self.assertNotIn("abcdefghijkl", result)
        self.assertGreaterEqual(result.count("[redacted]"), 2)

    def test_summary_uses_shared_redaction_for_jwt_aws_pem_and_github_tokens(self):
        secrets = (
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghijk",
            "AKIAIOSFODNN7EXAMPLE",
            "ghp_ABCDEFGHIJKL",
            "-----BEGIN PRIVATE KEY-----\nprivate material\n-----END PRIVATE KEY-----",
            'password="two words secret"',
        )
        for secret in secrets:
            with self.subTest(secret=secret[:20]):
                summary = cli._safe_summary(f"initialization failed: {secret}")
                self.assertNotIn(secret, summary)
                self.assertIn("[redacted]", summary)
                self.assertTrue(cli.contains_secret(secret))

    def test_event_redacts_sensitive_values_before_printing(self):
        output = io.StringIO()
        secret = "password=never-print-this-value"
        with patch.object(cli.sys, "stdout", output):
            cli.event(f"provider failed: {secret}")
        rendered = output.getvalue()
        self.assertNotIn("never-print-this-value", rendered)
        self.assertEqual(rendered.strip(), "[working]")

    def test_event_redacts_structured_sensitive_fields_before_printing(self):
        output = io.StringIO()
        secret = "sk-proj-" + "A" * 32
        bearer = "Bearer abcdefghijklmnopqrstuvwxyz"
        content_secret = "password=private-response-secret"
        message = json.dumps(
            {
                "status": "provider failed",
                "api_key": secret,
                "nested": {"authorization": bearer},
                "content": content_secret,
            }
        )
        with patch.object(cli.sys, "stdout", output):
            cli.event(message)
        rendered = output.getvalue()
        self.assertNotIn(secret, rendered)
        self.assertNotIn(bearer, rendered)
        self.assertNotIn("private-response-secret", rendered)
        self.assertEqual(rendered.strip(), "[working]")

    def test_event_reports_fixed_meaningful_progress_categories(self):
        cases = {
            "processing - step 3": "[processing]",
            "reasoning - step 2": "[reasoning]",
            "tool - web_search": "[tool activity]",
            "researching - deterministic deep evidence": "[researching]",
            "failover - provider unavailable": "[provider failover]",
            "planning implementation - model": "[planning]",
            "specialist delegated - Archivist - task #7": "[specialist coordination]",
            "image generation - preparing the requested image": "[image work]",
            "learning curriculum - scheduling recurring expert study": "[learning]",
            "skill verified - network-engineering": "[skill management]",
            "network inventory failed - fresh evidence unavailable": "[network analysis]",
            "storage report reused - one scan per request": "[storage analysis]",
            "connector readiness collected - deterministic read-only": "[connector activity]",
            "implementation checkpoint - independent review": "[implementation review]",
            "repair verification passed": "[repairing]",
            "adversarial verification passed": "[adversarial verification]",
            "artifact launch verified - healthy loopback HTTP response": "[artifact launch]",
            "gateway cycle failed safely; retrying": "[gateway activity]",
            "vault - reindexing": "[vault activity]",
        }
        for message, expected in cases.items():
            with self.subTest(message=message):
                output = io.StringIO()
                with patch.object(cli.sys, "stdout", output):
                    cli.event(message)
                self.assertEqual(output.getvalue().strip(), expected)

    def test_every_event_category_discards_sensitive_suffix_text(self):
        secret = "password=must-never-reach-terminal"
        for prefix, expected_label in cli._CLI_EVENT_LABELS:
            with self.subTest(prefix=prefix):
                output = io.StringIO()
                with patch.object(cli.sys, "stdout", output):
                    cli.event(f"{prefix} {secret}")
                rendered = output.getvalue().strip()
                self.assertEqual(rendered, f"[{expected_label}]")
                self.assertNotIn("must-never-reach-terminal", rendered)

    def test_approval_list_shows_scope_and_exact_sanitized_resource(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            config = SimpleNamespace(data_dir=data_dir, approval_ttl_hours=24)
            with DurableMemory(data_dir / "jarvis.db") as memory:
                memory.authorize_or_request(
                    "publish_external",
                    '{"arguments":{"branch":"main","path":"Documents/My  File.txt",'
                    '"remote":"origin"},"tool":"github_push"}',
                    "This publishes local commits to an external service.",
                    approval_scope="foreground",
                )
                memory.authorize_or_request(
                    "expose_private_information",
                    '{"tool":"google_drive_upload_file","padding":"'
                    + "x" * 1_300
                    + '","tail":"FULL-RESOURCE-TAIL"}',
                    "This uploads a local file to an external service.",
                    approval_scope="conversation:99",
                )
            stdout = io.StringIO()
            with (
                patch.object(cli.Config, "load", return_value=config),
                patch("sys.stdout", stdout),
            ):
                cli._run_approval(
                    argparse.Namespace(approval_command="list", limit=10)
                )

        output = stdout.getvalue()
        self.assertIn("scope: foreground", output)
        self.assertIn("github_push", output)
        self.assertIn("main", output)
        self.assertIn("origin", output)
        self.assertIn("Documents/My  File.txt", output)
        self.assertIn("FULL-RESOURCE-TAIL", output)

    def test_approval_resource_renderer_preserves_digest_after_secret_key(self):
        resource = json.dumps({
            "arguments": {
                "api_key": {"redacted": True, "bytes": 9, "sha256": "a" * 64},
                "description": "hello  there",
            },
            "tool": "future_sensitive_tool",
        }, separators=(",", ":"))

        rendered = cli._safe_resource(resource)

        self.assertIn('"sha256":"' + "a" * 64 + '"', rendered)
        self.assertIn('"description":"hello  there"', rendered)
        self.assertIn('"tool":"future_sensitive_tool"', rendered)

    def test_agents_cli_lists_delegates_and_reports_persistent_specialists(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = SimpleNamespace(data_dir=Path(temporary))
            stdout = io.StringIO()
            with (
                patch.object(cli.Config, "load", return_value=config),
                patch("sys.stdout", stdout),
            ):
                self.assertEqual(
                    cli._run_agents(argparse.Namespace(agents_command="list")), 0
                )
                self.assertEqual(
                    cli._run_agents(argparse.Namespace(
                        agents_command="delegate",
                        prompt=["Fix", "and", "test", "the", "parser"],
                        project=1,
                        max_attempts=3,
                    )),
                    0,
                )
                self.assertEqual(
                    cli._run_agents(argparse.Namespace(
                        agents_command="reports",
                        project=1,
                        task_id=None,
                        limit=20,
                    )),
                    0,
                )

        output = stdout.getvalue()
        self.assertIn("Forge [coding] ready", output)
        self.assertIn("Archivist [research] ready", output)
        self.assertIn("delegated task #1 to Forge", output)
        self.assertIn("#1 Forge [queued] model=coding", output)

    def test_stdio_is_configured_for_utf8_when_supported(self):
        class Stream:
            def __init__(self):
                self.calls = []

            def reconfigure(self, **kwargs):
                self.calls.append(kwargs)

        stdout, stderr = Stream(), Stream()
        with patch.object(cli.sys, "stdout", stdout), patch.object(cli.sys, "stderr", stderr):
            cli._configure_stdio()
        self.assertEqual(stdout.calls, [{"encoding": "utf-8", "errors": "replace"}])
        self.assertEqual(stderr.calls, [{"encoding": "utf-8", "errors": "replace"}])


class HeartbeatTests(unittest.TestCase):
    def test_heartbeat_renews_lease_with_same_owner(self):
        calls = []

        class LeaseMemory:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                pass

            def renew_task_lease(self, *args, **kwargs):
                calls.append((args, kwargs))
                return True

        class ScriptedStop:
            def __init__(self):
                self.calls = 0

            def wait(self, interval):
                self.calls += 1
                return self.calls > 1

        heartbeat = cli._LeaseHeartbeat(
            Path("db"),
            7,
            "owner-1",
            lease_seconds=30,
            interval_seconds=1,
            memory_factory=lambda *args, **kwargs: LeaseMemory(),
        )
        heartbeat._stop = ScriptedStop()
        heartbeat._run()
        self.assertEqual(calls[0][0], (7,))
        self.assertEqual(calls[0][1]["worker_id"], "owner-1")
        self.assertEqual(calls[0][1]["lease_seconds"], 30)
        self.assertFalse(heartbeat.lost)


if __name__ == "__main__":
    unittest.main()
