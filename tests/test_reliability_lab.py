from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from jarvis.memory import Memory
from jarvis.reliability_lab import (
    ReliabilityCaseError,
    freeze_failure_case,
    freeze_failed_run_metrics,
    sanitize_reliability_event,
    validate_failure_case,
)


class ReliabilityLabTests(unittest.TestCase):
    trace_id = "a" * 32

    @staticmethod
    def _events() -> list[dict]:
        return [
            {"phase": "routing", "outcome": "started", "operation": "route_select"},
            {
                "phase": "model",
                "outcome": "failed",
                "operation": "provider_request",
                "attempt": 1,
                "duration_ms": 920,
                "error_class": "provider_timeout",
            },
            {"phase": "terminal", "outcome": "failed", "operation": "run_complete"},
        ]

    def test_freezes_prompt_free_deterministic_failure_artifact(self) -> None:
        first = freeze_failure_case(
            trace_id=self.trace_id,
            failure_kind="provider_timeout",
            metrics={"total_ms": 1_240, "tool_counts": {"web_search": 1}},
            events=self._events(),
        )
        second = freeze_failure_case(
            trace_id=self.trace_id,
            failure_kind="provider_timeout",
            metrics={"total_ms": 1_240, "tool_counts": {"web_search": 1}},
            events=self._events(),
        )
        self.assertEqual(first, second)
        self.assertEqual(first["case_id"][:11], "regression-")
        self.assertEqual(len(first["checksum_sha256"]), 64)
        self.assertEqual(validate_failure_case(first), first)
        self.assertNotIn("prompt", repr(first).casefold())
        self.assertNotIn("content", repr(first).casefold())

    def test_rejects_raw_text_unknown_fields_and_nonterminal_failure(self) -> None:
        bad_event = self._events()[1] | {"message": "operator secret should never persist"}
        with self.assertRaisesRegex(ReliabilityCaseError, "unsupported field"):
            sanitize_reliability_event(bad_event)
        with self.assertRaisesRegex(ReliabilityCaseError, "terminal"):
            freeze_failure_case(
                trace_id=self.trace_id,
                failure_kind="provider_timeout",
                metrics={},
                events=[{"phase": "terminal", "outcome": "succeeded", "operation": "run_complete"}],
            )
        with self.assertRaises(ValueError):
            freeze_failure_case(
                trace_id=self.trace_id,
                failure_kind="provider_timeout",
                metrics={"route_reason": "api_key=sk-secret"},
                events=self._events(),
            )

    def test_integrity_check_rejects_tampering_and_prompt_shaped_fields(self) -> None:
        artifact = freeze_failure_case(
            trace_id=self.trace_id,
            failure_kind="provider_timeout",
            metrics={"total_ms": 1},
            events=self._events(),
        )
        modified = copy.deepcopy(artifact)
        modified["failure_kind"] = "different_failure"
        with self.assertRaisesRegex(ReliabilityCaseError, "integrity"):
            validate_failure_case(modified)
        modified = copy.deepcopy(artifact)
        modified["prompt"] = "do not retain this"
        with self.assertRaisesRegex(ReliabilityCaseError, "fields"):
            validate_failure_case(modified)

    def test_memory_promotion_is_validated_and_idempotent(self) -> None:
        artifact = freeze_failure_case(
            trace_id="4" * 32,
            failure_kind="provider_timeout",
            metrics={"status": "incomplete", "model_attempts": 2},
            events=[
                {
                    "phase": "model",
                    "outcome": "failed",
                    "operation": "provider.request",
                    "attempt": 2,
                    "error_class": "timeout",
                },
                {
                    "phase": "terminal",
                    "outcome": "failed",
                    "operation": "agent.run",
                },
            ],
        )
        with tempfile.TemporaryDirectory() as temporary:
            memory = Memory(Path(temporary) / "jarvis.db")
            try:
                first = memory.record_reliability_case(artifact)
                second = memory.record_reliability_case(artifact)
                rows = memory.list_reliability_cases()
            finally:
                memory.close()

        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["activity_id"], second["activity_id"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["case_id"], artifact["case_id"])

    def test_memory_promotion_rejects_tampering_before_write(self) -> None:
        artifact = freeze_failure_case(
            trace_id="5" * 32,
            failure_kind="verification_absent",
            metrics={},
            events=[{
                "phase": "terminal",
                "outcome": "blocked",
                "operation": "agent.run",
            }],
        )
        artifact["events"][0]["operation"] = "agent.changed"
        with tempfile.TemporaryDirectory() as temporary:
            memory = Memory(Path(temporary) / "jarvis.db")
            try:
                with self.assertRaises(ReliabilityCaseError):
                    memory.record_reliability_case(artifact)
                self.assertEqual(memory.list_reliability_cases(), [])
            finally:
                memory.close()

    def test_failed_run_adapter_uses_only_closed_metrics(self) -> None:
        artifact = freeze_failed_run_metrics({
            "trace_id": "6" * 32,
            "status": "incomplete",
            "failure_kind": "ProviderTimeout",
            "initial_profile": "fast",
            "model_attempts": 2,
            "model_latency_ms": 850,
            "tool_calls": 1,
        })

        self.assertEqual(artifact["failure_kind"], "providertimeout")
        self.assertEqual(artifact["events"][-1]["outcome"], "failed")
        self.assertNotIn("prompt", repr(artifact).casefold())
        with self.assertRaisesRegex(ReliabilityCaseError, "completed"):
            freeze_failed_run_metrics({
                "trace_id": "7" * 32,
                "status": "complete",
            })


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
