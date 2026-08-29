from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from jarvis.config import Config
from jarvis.memory import Memory
from jarvis.presence import PresenceHTTPServer, PresenceRuntime


class _PerformanceRuntime:
    runtime_epoch = "a" * 32

    def __init__(self) -> None:
        self.limits: list[int] = []

    def performance_overview(self, *, limit: int = 200):
        self.limits.append(limit)
        return {
            "schema_version": 1,
            "records": 0,
            "privacy": {"prompts_read": False, "closed_metric_schema": True},
        }


class PresencePerformanceEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = _PerformanceRuntime()
        self.server = PresenceHTTPServer(("127.0.0.1", 0), self.runtime)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_bounded_readonly_performance_endpoint(self) -> None:
        with urllib.request.urlopen(
            self.base + "/api/performance?limit=25", timeout=2
        ) as response:
            payload = json.load(response)

        self.assertEqual(response.status, 200)
        self.assertEqual(self.runtime.limits, [25])
        self.assertEqual(payload["records"], 0)
        self.assertFalse(payload["privacy"]["prompts_read"])

        with self.assertRaises(urllib.error.HTTPError) as invalid:
            urllib.request.urlopen(
                self.base + "/api/performance?limit=501", timeout=2
            )
        self.assertEqual(invalid.exception.code, 400)


class PresencePerformanceRuntimeTests(unittest.TestCase):
    def test_runtime_reads_only_terminal_metrics_and_returns_aggregates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            data = root / "data"
            workspace.mkdir()
            data.mkdir()
            config = Config(
                root=root,
                workspace=workspace,
                data_dir=data,
                soul_path=root / "SOUL.md",
                model="auto",
                fast_model="openai:gpt-test",
                reasoning_model="openai:gpt-test",
                coding_model="openai:gpt-test",
                deep_model="openai:gpt-test",
                ollama_url="http://127.0.0.1:11434",
                ollama_api_key=None,
                max_steps=5,
                context_length=4096,
                command_timeout=30,
                autonomy="autonomous",
                ollama_enabled=False,
            )
            owner = "presence:" + "b" * 32
            with Memory(data / "jarvis.db") as memory:
                conversation_id = memory.new_conversation("Performance")
                for index, queue_ms in enumerate((10, 30), start=1):
                    job_id = f"{index:032x}"
                    memory.create_presence_job(
                        job_id,
                        conversation_id=conversation_id,
                        project_id=1,
                        prompt=f"private prompt {index}",
                        model_override="auto",
                    )
                    self.assertTrue(memory.claim_presence_job(job_id, owner))
                    self.assertTrue(memory.finish_presence_job(
                        job_id,
                        "completed",
                        runtime_id=owner,
                        metrics={
                            "trace_id": "f" * 32,
                            "presence_job_id": job_id,
                            "origin": "interactive",
                            "build_id": "v0.6.3",
                            "cohort": "phase1-observability",
                            "queue_ms": queue_ms,
                            "total_ms": 500 + index * 100,
                            "end_to_end_total_ms": 500 + index * 100,
                            "time_to_first_token_ms": 200 + index * 10,
                            "first_visible_ms": 200 + index * 10,
                            "end_to_end_ttft_ms": 200 + index * 10,
                            "preparation_ms": 10,
                            "provider_ttft_ms": 190 + index * 10,
                            "model_latency_ms": 400,
                            "provider_total_ms": 400,
                            "model_attempts": 1,
                            "model_calls": 1,
                            "provider_attempts": 1,
                            "retries": 0,
                            "internal_retries": 0,
                            "failovers": 0,
                            "context_chars": 2_000,
                            "logical_context_chars": 2_000,
                            "tool_schema_chars": 250,
                            "estimated_prompt_tokens": 500,
                            "prompt_tokens": 80,
                            "completion_tokens": 20,
                            "total_tokens": 100,
                            "token_measurement": "actual",
                            "tool_calls": 0,
                            "provider": "openai",
                            "model": "openai:gpt-test",
                            "profile": "fast",
                            "initial_provider": "openai",
                            "initial_model": "openai:gpt-test",
                            "initial_profile": "fast",
                            "final_provider": "openai",
                            "final_model": "openai:gpt-test",
                            "final_profile": "fast",
                            "task_contract_status": "ready",
                            "streamed": True,
                            "stream_transport": "delta",
                        },
                    ))

            result = PresenceRuntime(config).performance_overview(limit=10)
            encoded = json.dumps(result, sort_keys=True)
            self.assertEqual(result["records"], 2)
            self.assertEqual(result["latency"]["queue_ms"]["p95"], 30)
            self.assertEqual(result["latency"]["no_tool_total_ms"]["p95"], 700)
            self.assertEqual(result["routes"]["providers"], [
                {"name": "openai", "count": 2}
            ])
            self.assertNotIn("private prompt", encoded)
            self.assertTrue(result["privacy"]["closed_metric_schema"])

    def test_performance_view_is_visible_and_uses_safe_dom_rendering(self) -> None:
        root = Path(__file__).resolve().parents[1]
        html = (root / "jarvis" / "presence.html").read_text(encoding="utf-8")
        script = (root / "jarvis" / "presence.js").read_text(encoding="utf-8")
        self.assertIn('data-view="performance"', html)
        self.assertIn('api("/api/performance?limit=200")', script)
        render_start = script.index("async function renderPerformance")
        render_end = script.index("\nfunction ", render_start + 1)
        render_source = script[render_start:render_end]
        self.assertNotIn("innerHTML", render_source)
        self.assertIn("textContent", render_source)


if __name__ == "__main__":
    unittest.main()
