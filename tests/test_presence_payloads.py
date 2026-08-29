from __future__ import annotations

import json
import unittest

from jarvis.presence_payloads import (
    presence_performance_summary,
    safe_presence_event_payload,
    safe_presence_metrics,
)


class PresencePayloadBoundaryTests(unittest.TestCase):
    def test_nested_event_values_are_recursively_redacted(self) -> None:
        secret = "sk-proj-" + "A" * 32
        payload = safe_presence_event_payload({
            "message": "ordinary operator-facing text",
            "details": {
                "list": ["safe", {"credential": secret}],
                "nested": {"token": secret},
            },
        })

        encoded = json.dumps(payload, sort_keys=True)
        self.assertNotIn(secret, encoded)
        self.assertIn("[REDACTED]", encoded)
        self.assertEqual(payload["message"], "ordinary operator-facing text")

    def test_non_string_fields_and_rendering_markers_are_preserved(self) -> None:
        link = "https://example.test/docs?q=1"
        marker = "[[jarvis-image:images/verified-result.png]]"
        payload = safe_presence_event_payload({
            "job_id": "a" * 32,
            "conversation_id": 7,
            "streamed": True,
            "empty": None,
            "content": f"Result {link}\n{marker}",
            "product_comparison": {
                "products": [{"name": "Safe item", "source_url": link}],
            },
        })

        self.assertEqual(payload["conversation_id"], 7)
        self.assertIs(payload["streamed"], True)
        self.assertIsNone(payload["empty"])
        self.assertIn(link, payload["content"])
        self.assertIn(marker, payload["content"])
        self.assertEqual(
            payload["product_comparison"]["products"][0]["source_url"],
            link,
        )

    def test_metric_schema_is_closed_and_nested_tool_names_are_secret_safe(self) -> None:
        secret = "sk-proj-" + "B" * 32
        metrics = safe_presence_metrics({
            "queue_ms": 12,
            "streamed": False,
            "prompt": "private input must not be representable",
            "unknown_nested": {"secret": secret},
            "tool_counts": {"read_file": 2, "api_key": 1, secret: 1},
        })

        self.assertEqual(metrics["queue_ms"], 12)
        self.assertIs(metrics["streamed"], False)
        self.assertNotIn("prompt", metrics)
        self.assertNotIn("unknown_nested", metrics)
        self.assertEqual(metrics["tool_counts"], {"[REDACTED]": 2, "read_file": 2})
        self.assertNotIn(secret, json.dumps(metrics, sort_keys=True))

    def test_expanded_observability_metrics_survive_the_event_boundary(self) -> None:
        metrics = {
            "trace_id": "d" * 32,
            "presence_job_id": "e" * 32,
            "origin": "interactive",
            "build_id": "v0.6.3",
            "cohort": "phase1-observability",
            "queue_ms": 5,
            "end_to_end_ttft_ms": 400,
            "end_to_end_total_ms": 900,
            "preparation_ms": 20,
            "provider_ttft_ms": 380,
            "provider_total_ms": 700,
            "model_calls": 1,
            "provider_attempts": 1,
            "internal_retries": 0,
            "tool_counts": {"web_search": 1},
            "token_measurement": "actual",
            "prompt_tokens": 80,
            "completion_tokens": 20,
            "total_tokens": 100,
            "stream_transport": "delta",
            "streamed": True,
        }

        safe = safe_presence_event_payload({"metrics": metrics})["metrics"]
        self.assertEqual(safe, metrics)

    def test_performance_summary_never_reads_or_returns_private_content(self) -> None:
        secret = "sk-proj-" + "C" * 32
        rows = [
            {
                "status": "completed",
                "finished_at": "2026-08-29T12:00:00+00:00",
                "prompt": f"private prompt {secret}",
                "metrics_json": json.dumps({
                    "queue_ms": 20,
                    "total_ms": 800,
                    "time_to_first_token_ms": 300,
                    "tool_calls": 0,
                    "provider": "codex-cli",
                    "model": "codex-cli:auto",
                }),
            },
            {
                "status": "failed",
                "finished_at": "2026-08-29T12:01:00+00:00",
                "metrics_json": json.dumps({
                    "queue_ms": 40,
                    "model": secret,
                    "tool_counts": {"api_key": 1},
                }),
            },
        ]

        summary = presence_performance_summary(rows, requested_limit=200)
        encoded = json.dumps(summary, sort_keys=True)
        self.assertNotIn(secret, encoded)
        self.assertNotIn("private prompt", encoded)
        self.assertEqual(summary["records"], 2)
        self.assertEqual(summary["latency"]["queue_ms"]["p95"], 40)
        self.assertEqual(summary["latency"]["no_tool_total_ms"]["p95"], 800)
        self.assertEqual(summary["privacy"]["prompts_read"], False)
        self.assertEqual(summary["privacy"]["closed_metric_schema"], True)


if __name__ == "__main__":
    unittest.main()
