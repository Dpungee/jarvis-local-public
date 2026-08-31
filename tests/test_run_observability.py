from __future__ import annotations

import json
import unittest

from jarvis.run_observability import (
    REDACTED,
    aggregate_run_metrics,
    aggregate_run_metrics_by_cohort,
    new_trace_id,
    numeric_summary,
    percentile,
    sanitize_run_metrics,
    trace_id_from_scope,
    trace_scope,
    validate_trace_id,
)


class RunObservabilityTests(unittest.TestCase):
    def test_trace_ids_are_opaque_canonical_and_unique(self) -> None:
        values = {new_trace_id() for _ in range(64)}
        self.assertEqual(len(values), 64)
        for value in values:
            self.assertRegex(value, r"^[0-9a-f]{32}$")
            self.assertEqual(validate_trace_id(value), value)
        for bad in (None, "", "A" * 32, "a" * 31, "a" * 33, "not-a-trace-id"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_trace_id(bad)

    def test_trace_scope_round_trips_without_free_form_correlation_data(self) -> None:
        trace_id = "a" * 32
        scope = trace_scope(trace_id, kind="request")
        self.assertEqual(scope, "request:" + trace_id)
        self.assertEqual(trace_id_from_scope(scope), trace_id)
        self.assertEqual(trace_id_from_scope(scope, expected_kind="request"), trace_id)
        with self.assertRaisesRegex(ValueError, "unsupported"):
            trace_scope(trace_id, kind="private user prompt")
        with self.assertRaisesRegex(ValueError, "does not match"):
            trace_id_from_scope(scope, expected_kind="worker")
        with self.assertRaisesRegex(ValueError, "invalid"):
            trace_id_from_scope("request:" + "a" * 32 + ":prompt")

    def test_presence_job_id_uses_the_runtime_canonical_32_hex_shape(self) -> None:
        # PresenceRuntime creates jobs with uuid4().hex and Memory enforces this
        # same lowercase 32-hex contract at its persistence boundary.
        job_id = "c" * 32
        safe = sanitize_run_metrics({"presence_job_id": job_id})
        self.assertEqual(safe["presence_job_id"], job_id)
        for invalid in (123, "C" * 32, "c" * 31, "presence-123"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                sanitize_run_metrics({"presence_job_id": invalid})

    def test_sanitizer_accepts_current_and_phase_one_prompt_free_metrics(self) -> None:
        trace_id = "b" * 32
        metrics = sanitize_run_metrics(
            {
                "trace_id": trace_id,
                "origin": "presence",
                "queue_ms": 4,
                "preparation_ms": 7,
                "provider_ttft_ms": 600,
                "end_to_end_ttft_ms": 611,
                "agent_total_ms": 900,
                "total_ms": 911,
                "model_latency_ms": 850,
                "model_attempts": 1,
                "internal_retries": 0,
                "retries": 0,
                "context_chars": 4200,
                "wire_request_bytes": 5000,
                "estimated_prompt_tokens": 1050,
                "prompt_tokens": None,
                "tool_calls": 2,
                "tool_counts": {"web_search": 1, "web_fetch": 1},
                "provider": "codex-cli",
                "model": "codex-cli:gpt-5.6-luna",
                "profile": "fast",
                "route_reason": "clear_dialogue",
                "task_contract_status": "not_attempted",
                "strategy_transfer_mode": "observe",
                "strategy_transfer_status": "observed",
                "strategy_transfer_selected": 2,
                "strategy_transfer_applied": False,
                "strategy_transfer_trial_manifest_id": 41,
                "strategy_transfer_trial_arm": "control",
                "strategy_transfer_trial_prompt_recorded": True,
                "strategy_transfer_trial_dispatched": True,
                "token_measurement": "unknown",
                "build_id": "v0.6.3+abcdef0",
                "cohort": "phase1-baseline",
                "streamed": True,
            }
        )
        self.assertEqual(metrics["trace_id"], trace_id)
        self.assertEqual(metrics["tool_counts"], {"web_fetch": 1, "web_search": 1})
        self.assertNotIn("prompt_tokens", metrics)
        self.assertTrue(metrics["streamed"])
        self.assertEqual(metrics["strategy_transfer_mode"], "observe")
        self.assertEqual(metrics["strategy_transfer_status"], "observed")
        self.assertEqual(metrics["strategy_transfer_selected"], 2)
        self.assertFalse(metrics["strategy_transfer_applied"])
        self.assertEqual(metrics["strategy_transfer_trial_manifest_id"], 41)
        self.assertEqual(metrics["strategy_transfer_trial_arm"], "control")
        self.assertTrue(metrics["strategy_transfer_trial_prompt_recorded"])
        self.assertTrue(metrics["strategy_transfer_trial_dispatched"])

    def test_sanitizer_is_closed_and_never_echoes_a_secret_key(self) -> None:
        secret = "sk-proj-" + "A" * 32
        for payload in (
            {"prompt": "ordinary private prompt"},
            {secret: 1},
            {"tool_counts": {"api_key": 1}},
            {"tool_counts": {secret: 1}},
        ):
            with self.subTest(payload=list(payload)), self.assertRaises((TypeError, ValueError)) as caught:
                sanitize_run_metrics(payload)
            self.assertNotIn(secret, str(caught.exception))

    def test_sanitizer_can_redact_secret_values_at_root_and_nested_levels(self) -> None:
        secret = "sk-proj-" + "B" * 32
        safe = sanitize_run_metrics(
            {
                "model": secret,
                "tool_counts": {secret: 1, "api_key": 2, "read_file": 3},
            },
            secret_policy="redact",
        )
        self.assertEqual(safe["model"], REDACTED)
        self.assertEqual(safe["tool_counts"], {REDACTED: 3, "read_file": 3})
        encoded = json.dumps(safe, sort_keys=True)
        self.assertNotIn(secret, encoded)
        self.assertNotIn("api_key", encoded)

    def test_sanitizer_rejects_free_text_wrong_types_and_unbounded_nesting(self) -> None:
        cases = (
            {"route_reason": "this is a sentence from the prompt"},
            {"queue_ms": True},
            {"streamed": 1},
            {"tool_counts": {"read_file": {"arguments": "private"}}},
            {"tool_counts": {f"tool_{index}": 1 for index in range(65)}},
        )
        for payload in cases:
            with self.subTest(payload=list(payload)), self.assertRaises((TypeError, ValueError)):
                sanitize_run_metrics(payload)

    def test_percentile_uses_nearest_rank_and_empty_summary_is_unknown(self) -> None:
        self.assertEqual(percentile([100, 200, 900], 95), 900)
        self.assertEqual(percentile([4, 1, 3, 2], 50), 2)
        self.assertEqual(percentile([4, 1, 3, 2], 0), 1)
        self.assertIsNone(percentile([], 95))
        self.assertEqual(
            numeric_summary([]),
            {"samples": 0, "min": None, "mean": None, "p50": None, "p95": None, "max": None},
        )
        with self.assertRaises(ValueError):
            percentile([1, float("nan")], 95)

    def test_aggregate_keeps_unknown_tokens_unknown_and_filters_cohorts(self) -> None:
        records = [
            {
                "build_id": "build-a",
                "cohort": "baseline",
                "queue_ms": 10,
                "total_ms": 100,
                "prompt_tokens": 8,
                "completion_tokens": 4,
                "total_tokens": 12,
                "token_measurement": "actual",
            },
            {
                "build_id": "build-a",
                "cohort": "baseline",
                "queue_ms": 20,
                "total_ms": 200,
                "prompt_tokens": 10,
                "token_measurement": "estimated",
            },
            {
                "build_id": "build-b",
                "cohort": "candidate",
                "queue_ms": 30,
                "total_ms": 300,
                "token_measurement": "unknown",
            },
        ]
        baseline = aggregate_run_metrics(records, build_id="build-a", cohort="baseline")
        self.assertEqual(baseline["records"], 2)
        self.assertEqual(baseline["metrics"]["queue_ms"]["mean"], 15)
        prompt = baseline["tokens"]["prompt_tokens"]
        self.assertEqual(prompt["known_samples"], 2)
        self.assertEqual(prompt["unknown_samples"], 0)
        self.assertEqual(prompt["total"], 18)
        self.assertEqual(prompt["measurement_samples"]["actual"], 1)
        self.assertEqual(prompt["measurement_samples"]["estimated"], 1)
        completion = baseline["tokens"]["completion_tokens"]
        self.assertEqual(completion["known_samples"], 1)
        self.assertEqual(completion["unknown_samples"], 1)
        self.assertEqual(completion["total"], 4)

        candidate = aggregate_run_metrics(records, cohort="candidate")
        self.assertEqual(candidate["records"], 1)
        self.assertEqual(candidate["tokens"]["prompt_tokens"]["known_samples"], 0)
        self.assertEqual(candidate["tokens"]["prompt_tokens"]["unknown_samples"], 1)
        self.assertIsNone(candidate["tokens"]["prompt_tokens"]["total"])

    def test_grouped_aggregates_do_not_mix_builds_or_cohorts(self) -> None:
        records = [
            {"build_id": "build-b", "cohort": "candidate", "total_ms": 30},
            {"build_id": "build-a", "cohort": "baseline", "total_ms": 10},
            {"build_id": "build-a", "cohort": "baseline", "total_ms": 20},
        ]
        groups = aggregate_run_metrics_by_cohort(records)
        self.assertEqual(
            [(row["build_id"], row["cohort"], row["records"]) for row in groups],
            [("build-a", "baseline", 2), ("build-b", "candidate", 1)],
        )
        self.assertEqual(groups[0]["metrics"]["total_ms"]["p95"], 20)

    def test_module_contains_no_prompt_or_payload_metric_field(self) -> None:
        allowed = sanitize_run_metrics({"status": "complete"})
        self.assertEqual(allowed, {"status": "complete"})
        for forbidden in ("prompt", "response", "content", "arguments", "result", "url"):
            with self.subTest(forbidden=forbidden), self.assertRaisesRegex(
                ValueError, "unsupported run metric field"
            ):
                sanitize_run_metrics({forbidden: "private"})


if __name__ == "__main__":
    unittest.main()
