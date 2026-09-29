"""Prompt-free failure artifacts for Jarvis reliability regression tests.

The Reliability Lab turns a *sanitized operational failure* into a deterministic
fixture that a focused test can reference later.  It deliberately cannot retain
operator text, model output, tool arguments, tool results, paths, URLs, or free
form exception messages.  Those belong in the original protected trace, not in a
fixture that may be copied into a repository or test report.

This is a small, standalone substrate.  Live request code may opt into it later;
merely importing this module changes no runtime behaviour.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from .redaction import contains_secret, is_sensitive_key, redact_secrets
from .run_observability import sanitize_run_metrics, validate_trace_id


SCHEMA_VERSION = 1
MAX_EVENTS = 128
MAX_OPERATION_LENGTH = 80
_IDENTIFIER = re.compile(r"[a-z][a-z0-9_.-]{0,79}")
_PHASES = frozenset({"routing", "model", "tool", "verification", "policy", "terminal"})
_OUTCOMES = frozenset({"started", "succeeded", "failed", "blocked", "cancelled", "skipped"})
_TERMINAL_FAILURES = frozenset({"failed", "blocked", "cancelled"})


class ReliabilityCaseError(ValueError):
    """Raised when a proposed failure fixture is unsafe or malformed."""


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _safe_identifier(value: Any, *, label: str) -> str:
    if not isinstance(value, str):
        raise ReliabilityCaseError(f"{label} must be a string")
    cleaned = redact_secrets(value, "[REDACTED]")
    if (
        not cleaned
        or len(cleaned) > MAX_OPERATION_LENGTH
        or is_sensitive_key(cleaned)
        or contains_secret(cleaned)
        or _IDENTIFIER.fullmatch(cleaned) is None
    ):
        raise ReliabilityCaseError(f"{label} must be a bounded operational identifier")
    return cleaned


def sanitize_reliability_event(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one strictly operational event with no free-form payload field."""

    if not isinstance(value, Mapping):
        raise TypeError("reliability event must be a mapping")
    allowed = {"phase", "outcome", "operation", "attempt", "duration_ms", "error_class"}
    if any(not isinstance(key, str) or key not in allowed for key in value):
        raise ReliabilityCaseError("reliability event contains an unsupported field")
    phase = value.get("phase")
    outcome = value.get("outcome")
    if phase not in _PHASES:
        raise ReliabilityCaseError("reliability event phase is unsupported")
    if outcome not in _OUTCOMES:
        raise ReliabilityCaseError("reliability event outcome is unsupported")
    safe: dict[str, Any] = {
        "phase": phase,
        "outcome": outcome,
        "operation": _safe_identifier(value.get("operation"), label="operation"),
    }
    for key in ("attempt", "duration_ms"):
        if key not in value:
            continue
        raw = value[key]
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            raise ReliabilityCaseError(f"reliability event {key} must be a non-negative integer")
        safe[key] = raw
    if "error_class" in value:
        safe["error_class"] = _safe_identifier(value["error_class"], label="error class")
    return safe


def _case_body(
    *,
    trace_id: str,
    failure_kind: str,
    metrics: Mapping[str, Any] | None,
    events: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    if not isinstance(events, Sequence) or isinstance(events, (str, bytes)):
        raise TypeError("reliability events must be a sequence")
    if not 1 <= len(events) <= MAX_EVENTS:
        raise ReliabilityCaseError("reliability event count is out of bounds")
    safe_events = [sanitize_reliability_event(event) for event in events]
    terminal = safe_events[-1]
    if terminal["phase"] != "terminal" or terminal["outcome"] not in _TERMINAL_FAILURES:
        raise ReliabilityCaseError("failure artifact requires one terminal failed, blocked, or cancelled event")
    return {
        "schema_version": SCHEMA_VERSION,
        "source_trace_id": validate_trace_id(trace_id),
        "failure_kind": _safe_identifier(failure_kind, label="failure kind"),
        "metrics": sanitize_run_metrics(metrics),
        "events": safe_events,
    }


def freeze_failure_case(
    *,
    trace_id: str,
    failure_kind: str,
    metrics: Mapping[str, Any] | None,
    events: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Create a deterministic, redacted regression artifact from one failed run.

    The returned value is JSON-serializable and contains only closed operational
    fields.  Equal sanitized inputs produce the same ID, making duplicate failure
    promotion safe without storing raw conversation content.
    """

    body = _case_body(
        trace_id=trace_id,
        failure_kind=failure_kind,
        metrics=metrics,
        events=events,
    )
    digest = hashlib.sha256(_canonical_json(body).encode("utf-8")).hexdigest()
    return {"case_id": f"regression-{digest[:24]}", "checksum_sha256": digest, **body}


def freeze_failed_run_metrics(
    metrics: Mapping[str, Any],
    *,
    error_class: str | None = None,
) -> dict[str, Any]:
    """Promote sanitized non-complete run telemetry into a failure case.

    This adapter consumes only the already-closed observability schema.  It has
    no representable field for prompts, answers, paths, URLs, arguments, or
    exception messages.
    """

    safe_metrics = sanitize_run_metrics(metrics)
    trace_id = safe_metrics.get("trace_id")
    if not isinstance(trace_id, str):
        raise ReliabilityCaseError("failed run metrics require a trace id")
    status = str(safe_metrics.get("status") or "")
    if status == "complete":
        raise ReliabilityCaseError("completed runs are not failure cases")
    terminal_outcome = (
        "cancelled" if status == "cancelled"
        else "blocked" if status == "blocked"
        else "failed"
    )
    failure_kind = safe_metrics.get("failure_kind") or error_class or "run_incomplete"
    failure_kind = re.sub(
        r"[^a-z0-9_.-]+", "_", str(failure_kind).casefold()
    ).strip("_.-")
    if error_class is not None:
        error_class = re.sub(
            r"[^a-z0-9_.-]+", "_", str(error_class).casefold()
        ).strip("_.-")
    events: list[dict[str, Any]] = []
    if safe_metrics.get("initial_profile") or safe_metrics.get("profile"):
        events.append({
            "phase": "routing",
            "outcome": "succeeded",
            "operation": "route.select",
        })
    attempts = int(safe_metrics.get("model_attempts") or 0)
    if attempts:
        model_event: dict[str, Any] = {
            "phase": "model",
            "outcome": "failed",
            "operation": "model.request",
            "attempt": attempts,
        }
        if "model_latency_ms" in safe_metrics:
            model_event["duration_ms"] = int(safe_metrics["model_latency_ms"])
        if error_class is not None:
            model_event["error_class"] = error_class
        events.append(model_event)
    terminal: dict[str, Any] = {
        "phase": "terminal",
        "outcome": terminal_outcome,
        "operation": "agent.run",
    }
    if error_class is not None:
        terminal["error_class"] = error_class
    events.append(terminal)
    return freeze_failure_case(
        trace_id=trace_id,
        failure_kind=str(failure_kind),
        metrics=safe_metrics,
        events=events,
    )


def validate_failure_case(value: Mapping[str, Any]) -> dict[str, Any]:
    """Fail closed unless a serialized artifact exactly matches its checksum."""

    if not isinstance(value, Mapping):
        raise TypeError("failure artifact must be a mapping")
    allowed = {"case_id", "checksum_sha256", "schema_version", "source_trace_id", "failure_kind", "metrics", "events"}
    if set(value) != allowed:
        raise ReliabilityCaseError("failure artifact fields are invalid")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise ReliabilityCaseError("failure artifact schema version is unsupported")
    body = _case_body(
        trace_id=value.get("source_trace_id"),
        failure_kind=value.get("failure_kind"),
        metrics=value.get("metrics"),
        events=value.get("events"),
    )
    digest = hashlib.sha256(_canonical_json(body).encode("utf-8")).hexdigest()
    expected_id = f"regression-{digest[:24]}"
    if value.get("checksum_sha256") != digest or value.get("case_id") != expected_id:
        raise ReliabilityCaseError("failure artifact integrity check failed")
    return {"case_id": expected_id, "checksum_sha256": digest, **body}


__all__ = [
    "MAX_EVENTS",
    "ReliabilityCaseError",
    "SCHEMA_VERSION",
    "freeze_failure_case",
    "freeze_failed_run_metrics",
    "sanitize_reliability_event",
    "validate_failure_case",
]
