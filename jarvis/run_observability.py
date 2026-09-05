"""Prompt-free run correlation, sanitization, and aggregation helpers.

This module deliberately has no database, provider, or agent dependencies.  It is
the narrow boundary shared by foreground turns and Presence jobs: callers may
persist operational measurements, but never prompts, responses, tool arguments,
or tool results.
"""

from __future__ import annotations

import math
import re
import secrets
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from statistics import fmean
from typing import Any, Literal

from .redaction import contains_secret, is_sensitive_key, redact_secrets


TRACE_ID_PATTERN = re.compile(r"[0-9a-f]{32}")
MAX_METRIC_INTEGER = 2**63 - 1
MAX_METRIC_TEXT = 200
MAX_TOOL_COUNTERS = 64
REDACTED = "[REDACTED]"

_SCOPE_KINDS = frozenset(
    {
        "gateway",
        "presence",
        "public_presence",
        "request",
        "specialist",
        "tool",
        "worker",
    }
)
_ORIGINS = frozenset(
    {
        "cli",
        "companion",
        "gateway",
        "interactive",
        "presence",
        "proactive",
        "public_presence",
        "telegram",
        "test",
        "unknown",
        "worker",
    }
)
_TOKEN_MEASUREMENTS = frozenset({"actual", "estimated", "mixed", "unknown"})
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+@-]{0,199}")
_TOOL_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}")

_COUNT_FIELDS = frozenset(
    {
        "agent_total_ms",
        "approval_wait_ms",
        "completion_tokens",
        "context_chars",
        "end_to_end_total_ms",
        "end_to_end_ttft_ms",
        "estimated_prompt_tokens",
        "failovers",
        "first_visible_ms",
        "internal_retries",
        "logical_context_chars",
        "model_attempts",
        "model_calls",
        "model_latency_ms",
        "postprocess_ms",
        "preparation_ms",
        "prompt_tokens",
        "provider_attempts",
        "provider_total_ms",
        "provider_ttft_ms",
        "queue_ms",
        "retries",
        "strategy_transfer_selected",
        "strategy_transfer_trial_manifest_id",
        "task_id",
        "time_to_first_token_ms",
        "tool_calls",
        "tool_ms",
        "tool_schema_chars",
        "total_ms",
        "total_tokens",
        "verification_ms",
        "wire_request_bytes",
    }
)
_TEXT_FIELDS = frozenset(
    {
        "build_id",
        "cohort",
        "failure_kind",
        "final_model",
        "final_profile",
        "final_provider",
        "initial_model",
        "initial_profile",
        "initial_provider",
        # The learning channel's per-turn diagnostic (VTMF M4 design 5.4, M-4).
        # Two closed-vocabulary strings only -- the merged mode and its reason
        # sub-code.  Never a lesson, a document, a digest, an epoch number or
        # a confirmation code: those are operator surfaces, and a run metric is
        # not one.  sanitize_run_metrics RAISES on an unlisted key, so this is
        # what makes the pair recordable at all rather than a silent drop.
        "learning_channel_mode",
        "learning_channel_reason",
        "model",
        "profile",
        "provider",
        "route_reason",
        "status",
        "stream_transport",
        "strategy_transfer_mode",
        "strategy_transfer_status",
        "strategy_transfer_trial_arm",
        "task_contract_status",
    }
)
_BOOLEAN_FIELDS = frozenset({
    "streamed",
    "strategy_transfer_applied",
    "strategy_transfer_trial_dispatched",
    "strategy_transfer_trial_prompt_recorded",
})
_TRACE_FIELDS = frozenset({"trace_id", "presence_job_id"})
_NESTED_COUNTER_FIELDS = frozenset({"tool_counts"})
_ALLOWED_FIELDS = (
    _COUNT_FIELDS
    | _TEXT_FIELDS
    | _BOOLEAN_FIELDS
    | _TRACE_FIELDS
    | _NESTED_COUNTER_FIELDS
    | {"origin", "token_measurement"}
)
_AGGREGATED_COUNT_FIELDS = _COUNT_FIELDS - {
    "completion_tokens",
    "prompt_tokens",
    "task_id",
    "total_tokens",
}


def new_trace_id() -> str:
    """Return an opaque, canonical 128-bit correlation identifier."""

    return secrets.token_hex(16)


def validate_trace_id(value: Any) -> str:
    """Validate a canonical trace id without accepting prompt-derived labels."""

    if not isinstance(value, str) or TRACE_ID_PATTERN.fullmatch(value) is None:
        raise ValueError("trace id must be exactly 32 lowercase hexadecimal characters")
    return value


def trace_scope(trace_id: Any, *, kind: str = "request") -> str:
    """Create a bounded internal scope that is reversibly tied to ``trace_id``.

    ``kind`` is a closed operational enum rather than arbitrary user text.  This
    prevents callers from embedding a prompt, filename, or account identifier in
    a model-budget or telemetry correlation scope.
    """

    if kind not in _SCOPE_KINDS:
        raise ValueError("trace scope kind is unsupported")
    return f"{kind}:{validate_trace_id(trace_id)}"


def trace_id_from_scope(scope: Any, *, expected_kind: str | None = None) -> str:
    """Recover the trace id from a scope produced by :func:`trace_scope`."""

    if expected_kind is not None and expected_kind not in _SCOPE_KINDS:
        raise ValueError("trace scope kind is unsupported")
    if not isinstance(scope, str):
        raise ValueError("trace scope is invalid")
    match = re.fullmatch(r"([a-z_]+):([0-9a-f]{32})", scope)
    if match is None or match.group(1) not in _SCOPE_KINDS:
        raise ValueError("trace scope is invalid")
    if expected_kind is not None and match.group(1) != expected_kind:
        raise ValueError("trace scope kind does not match")
    return validate_trace_id(match.group(2))


def _secret_safe_text(
    value: Any,
    *,
    secret_policy: Literal["reject", "redact"],
) -> str:
    if not isinstance(value, str):
        raise TypeError("run metric text values must be strings")
    if is_sensitive_key(value) or contains_secret(value):
        if secret_policy == "reject":
            raise ValueError("run metrics may not contain credentials or secrets")
        return REDACTED
    cleaned = redact_secrets(value, REDACTED)
    if not cleaned or len(cleaned) > MAX_METRIC_TEXT:
        raise ValueError("run metric text is empty or too long")
    if _IDENTIFIER.fullmatch(cleaned) is None:
        raise ValueError("run metric text must be a bounded operational identifier")
    return cleaned


def _safe_count(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"run metric {field} must be a non-negative integer")
    return min(value, MAX_METRIC_INTEGER)


def _safe_tool_counts(
    value: Any,
    *,
    secret_policy: Literal["reject", "redact"],
) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise TypeError("run metric tool_counts must be a mapping")
    if len(value) > MAX_TOOL_COUNTERS:
        raise ValueError("run metric tool_counts contains too many tools")
    cleaned: dict[str, int] = {}
    for raw_name, raw_count in value.items():
        if not isinstance(raw_name, str):
            raise TypeError("run metric tool names must be strings")
        secret_shaped = is_sensitive_key(raw_name) or contains_secret(raw_name)
        if secret_shaped:
            if secret_policy == "reject":
                raise ValueError("run metrics may not contain credentials or secrets")
            name = REDACTED
        else:
            name = redact_secrets(raw_name, REDACTED)
            if _TOOL_NAME.fullmatch(name) is None:
                raise ValueError("run metric tool names must be bounded identifiers")
        count = _safe_count(raw_count, "tool_counts")
        cleaned[name] = min(MAX_METRIC_INTEGER, cleaned.get(name, 0) + count)
    return dict(sorted(cleaned.items()))


def sanitize_run_metrics(
    metrics: Mapping[str, Any] | None,
    *,
    secret_policy: Literal["reject", "redact"] = "reject",
) -> dict[str, Any]:
    """Return the closed, prompt-free subset allowed in run telemetry.

    Unknown fields are rejected rather than silently persisted.  Strings are
    identifier-shaped, not free-form, and nested data is limited to bounded tool
    name counters.  Secret-looking values can either fail the whole record (the
    persistence default) or become a literal ``[REDACTED]`` marker.  Tool
    arguments and results have no representable field in this schema.
    """

    if secret_policy not in {"reject", "redact"}:
        raise ValueError("secret_policy must be reject or redact")
    if metrics is None:
        return {}
    if not isinstance(metrics, Mapping):
        raise TypeError("run metrics must be a mapping")
    safe: dict[str, Any] = {}
    for raw_key, value in metrics.items():
        if not isinstance(raw_key, str):
            raise TypeError("run metric names must be strings")
        # Never echo an untrusted key in an exception: it may itself be secret.
        if raw_key not in _ALLOWED_FIELDS or is_sensitive_key(raw_key):
            raise ValueError("unsupported run metric field")
        if value is None:
            continue
        if raw_key in _COUNT_FIELDS:
            safe[raw_key] = _safe_count(value, raw_key)
        elif raw_key in _BOOLEAN_FIELDS:
            if not isinstance(value, bool):
                raise TypeError(f"run metric {raw_key} must be a boolean")
            safe[raw_key] = value
        elif raw_key in _TRACE_FIELDS:
            safe[raw_key] = validate_trace_id(value)
        elif raw_key == "origin":
            if value not in _ORIGINS:
                raise ValueError("run metric origin is unsupported")
            safe[raw_key] = value
        elif raw_key == "token_measurement":
            if value not in _TOKEN_MEASUREMENTS:
                raise ValueError("run metric token_measurement is unsupported")
            safe[raw_key] = value
        elif raw_key in _TEXT_FIELDS:
            safe[raw_key] = _secret_safe_text(value, secret_policy=secret_policy)
        elif raw_key in _NESTED_COUNTER_FIELDS:
            safe[raw_key] = _safe_tool_counts(value, secret_policy=secret_policy)
        else:  # pragma: no cover - guarded by the closed field set above.
            raise ValueError("unsupported run metric field")
    return safe


def percentile(values: Iterable[int | float], percent: int | float) -> int | float | None:
    """Return the nearest-rank percentile, or ``None`` for an empty sample."""

    if isinstance(percent, bool) or not isinstance(percent, (int, float)):
        raise TypeError("percent must be numeric")
    numeric_percent = float(percent)
    if not math.isfinite(numeric_percent) or not 0 <= numeric_percent <= 100:
        raise ValueError("percent must be between 0 and 100")
    sample: list[int | float] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError("percentile samples must be numeric")
        if not math.isfinite(float(value)):
            raise ValueError("percentile samples must be finite")
        sample.append(value)
    if not sample:
        return None
    sample.sort()
    if numeric_percent == 0:
        return sample[0]
    index = math.ceil((numeric_percent / 100.0) * len(sample)) - 1
    return sample[index]


def numeric_summary(values: Iterable[int | float]) -> dict[str, Any]:
    """Summarize a numeric sample without turning absence into zero."""

    sample = list(values)
    if not sample:
        return {
            "samples": 0,
            "min": None,
            "mean": None,
            "p50": None,
            "p95": None,
            "max": None,
        }
    # ``percentile`` performs all type and finiteness validation.
    p50 = percentile(sample, 50)
    p95 = percentile(sample, 95)
    return {
        "samples": len(sample),
        "min": min(sample),
        "mean": fmean(sample),
        "p50": p50,
        "p95": p95,
        "max": max(sample),
    }


def _token_summary(records: Sequence[Mapping[str, Any]], field: str) -> dict[str, Any]:
    known = [int(record[field]) for record in records if field in record]
    kinds = {kind: 0 for kind in (*sorted(_TOKEN_MEASUREMENTS), "unspecified")}
    for record in records:
        if field not in record:
            continue
        kind = str(record.get("token_measurement", "unspecified"))
        kinds[kind if kind in kinds else "unspecified"] += 1
    return {
        "known_samples": len(known),
        "unknown_samples": len(records) - len(known),
        "total": sum(known) if known else None,
        "mean": fmean(known) if known else None,
        "measurement_samples": kinds,
    }


def aggregate_run_metrics(
    records: Iterable[Mapping[str, Any]],
    *,
    build_id: str | None = None,
    cohort: str | None = None,
) -> dict[str, Any]:
    """Aggregate sanitized turn records, optionally selecting a build/cohort.

    Missing token measurements remain explicitly unknown.  The returned build
    and cohort lists let callers avoid mixing pre-change and post-change latency
    populations accidentally.
    """

    safe_records = [sanitize_run_metrics(record) for record in records]
    if build_id is not None:
        build_id = _secret_safe_text(build_id, secret_policy="reject")
        safe_records = [row for row in safe_records if row.get("build_id") == build_id]
    if cohort is not None:
        cohort = _secret_safe_text(cohort, secret_policy="reject")
        safe_records = [row for row in safe_records if row.get("cohort") == cohort]

    numeric: dict[str, Any] = {}
    for field in sorted(_AGGREGATED_COUNT_FIELDS):
        values = [row[field] for row in safe_records if field in row]
        if values:
            numeric[field] = numeric_summary(values)

    return {
        "records": len(safe_records),
        "filter": {"build_id": build_id, "cohort": cohort},
        "build_ids": sorted({str(row["build_id"]) for row in safe_records if "build_id" in row}),
        "cohorts": sorted({str(row["cohort"]) for row in safe_records if "cohort" in row}),
        "metrics": numeric,
        "tokens": {
            "prompt_tokens": _token_summary(safe_records, "prompt_tokens"),
            "completion_tokens": _token_summary(safe_records, "completion_tokens"),
            "total_tokens": _token_summary(safe_records, "total_tokens"),
        },
    }


def aggregate_run_metrics_by_cohort(
    records: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Return deterministic aggregates separated by build and cohort."""

    safe_records = [sanitize_run_metrics(record) for record in records]
    groups: dict[tuple[str | None, str | None], list[dict[str, Any]]] = defaultdict(list)
    for record in safe_records:
        groups[(record.get("build_id"), record.get("cohort"))].append(record)
    output: list[dict[str, Any]] = []
    for (build_id, cohort), group in sorted(
        groups.items(), key=lambda item: ((item[0][0] or ""), (item[0][1] or ""))
    ):
        summary = aggregate_run_metrics(group)
        summary["build_id"] = build_id
        summary["cohort"] = cohort
        output.append(summary)
    return output


__all__ = [
    "MAX_METRIC_INTEGER",
    "REDACTED",
    "aggregate_run_metrics",
    "aggregate_run_metrics_by_cohort",
    "new_trace_id",
    "numeric_summary",
    "percentile",
    "sanitize_run_metrics",
    "trace_id_from_scope",
    "trace_scope",
    "validate_trace_id",
]
