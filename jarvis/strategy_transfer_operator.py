from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from .strategy_transfer import STRATEGY_SET, STRATEGY_VOCABULARY


TRIAL_MIN_SAMPLE_CAP = 40
TRIAL_MAX_SAMPLE_CAP = 200
TRIAL_MAX_DAYS = 14
TRIAL_MAX_FAMILIES = 3
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
_STATE_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


class StrategyTransferOperatorError(ValueError):
    """A Phase 4B operator input is malformed or exceeds its closed contract."""


def _positive_integer(value: Any, label: str) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        parsed = value
    elif isinstance(value, str) and re.fullmatch(r"[1-9][0-9]*", value):
        parsed = int(value)
    else:
        raise StrategyTransferOperatorError(f"{label} must be a positive integer")
    if parsed < 1 or parsed > 2_147_483_647:
        raise StrategyTransferOperatorError(f"{label} is outside the supported range")
    return parsed


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise StrategyTransferOperatorError(
            f"{label} must be exactly 64 lowercase hexadecimal characters"
        )
    return value


def _unique_closed_values(
    values: Sequence[str],
    *,
    label: str,
    allowed: frozenset[str],
    minimum: int,
    maximum: int,
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise StrategyTransferOperatorError(f"{label} must be a repeated option")
    normalized = tuple(str(value).strip() for value in values)
    if not minimum <= len(normalized) <= maximum:
        raise StrategyTransferOperatorError(
            f"{label} requires between {minimum} and {maximum} unique values"
        )
    if len(normalized) != len(set(normalized)):
        raise StrategyTransferOperatorError(f"{label} contains duplicates")
    unknown = set(normalized) - allowed
    if unknown:
        raise StrategyTransferOperatorError(
            f"{label} contains unsupported values: {', '.join(sorted(unknown))}"
        )
    return normalized


def build_trial_manifest_input(
    *,
    project_id: Any,
    target_families: Sequence[str],
    allowed_families: Sequence[str],
    strategies: Sequence[str],
    sample_cap: Any,
    duration_days: Any,
    seed: Any,
    evaluator_version: Any,
    evaluator_sha256: Any,
    fixture_sha256: Any,
    config_sha256: Any,
    runtime_sha256: Any,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the closed, prompt-free manifest accepted by Phase 4B storage.

    The function accepts only opaque identifiers, closed labels, bounded counts,
    timestamps, and digests. There is deliberately no prompt, goal, task text,
    path, URL, user identifier, or notes field.
    """
    project = _positive_integer(project_id, "project")
    families = _unique_closed_values(
        target_families,
        label="target family",
        allowed=frozenset(str(item) for item in allowed_families),
        minimum=1,
        maximum=TRIAL_MAX_FAMILIES,
    )
    selected_strategies = _unique_closed_values(
        strategies,
        label="strategy",
        allowed=STRATEGY_SET,
        minimum=1,
        maximum=len(STRATEGY_VOCABULARY),
    )
    cap = _positive_integer(sample_cap, "sample cap")
    if not TRIAL_MIN_SAMPLE_CAP <= cap <= TRIAL_MAX_SAMPLE_CAP or cap % 4:
        raise StrategyTransferOperatorError(
            "sample cap must be 40-200 inclusive and divisible by 4"
        )
    days = _positive_integer(duration_days, "duration days")
    if days > TRIAL_MAX_DAYS:
        raise StrategyTransferOperatorError("duration days must be between 1 and 14")
    if not isinstance(evaluator_version, str) or _VERSION_RE.fullmatch(
        evaluator_version
    ) is None:
        raise StrategyTransferOperatorError(
            "evaluator version must be a bounded release identifier"
        )
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        raise StrategyTransferOperatorError("trial clock must be timezone-aware")
    expires_at = (
        instant.astimezone(timezone.utc) + timedelta(days=days)
    ).isoformat().replace("+00:00", "Z")
    return {
        "project_id": project,
        "target_families": families,
        "strategies": selected_strategies,
        "sample_cap": cap,
        "expires_at": expires_at,
        "seed": _sha256(seed, "seed"),
        "evaluator_version": evaluator_version,
        "evaluator_sha256": _sha256(evaluator_sha256, "evaluator SHA-256"),
        "fixture_sha256": _sha256(fixture_sha256, "fixture SHA-256"),
        "config_sha256": _sha256(config_sha256, "config SHA-256"),
        "runtime_sha256": _sha256(runtime_sha256, "runtime SHA-256"),
        "operator_confirmed": True,
    }


_SAFE_STATUS_INTEGER_FIELDS = frozenset({
    "aborted_assignments",
    "assigned",
    "block_size",
    "complete_blocks",
    "control_assigned",
    "control_predictions",
    "id",
    "invalid_assignments",
    "manifest_id",
    "project_id",
    "remaining",
    "resolved",
    "sample_cap",
    "source_target_pairs",
    "assigned_count",
    "assignment_count",
    "resolved_count",
    "control_count",
    "treatment_count",
    "treatment_assigned",
    "treatment_predictions",
    "control_successes",
    "treatment_successes",
})
_SAFE_STATUS_FLOAT_FIELDS = frozenset({
    "completion_lift_points",
    "control_success_rate",
    "lift_pp",
    "treatment_success_rate",
})
_SAFE_STATUS_BOOLEAN_FIELDS = frozenset({
    "aborted",
    "activation_allowed",
    "attestation_valid",
    "available",
    "causal_attestation_valid",
    "contaminated",
    "expired",
    "operator_promoted",
    "promoted",
    "promotion_ready",
})
_SAFE_STATUS_HASH_FIELDS = frozenset({
    "attestation_sha256",
    "artifact_sha256",
    "config_sha256",
    "evaluator_sha256",
    "fixture_sha256",
    "manifest_sha256",
    "runtime_sha256",
})
_SAFE_STATUS_TEXT_FIELDS = frozenset({
    "abort_reason_code",
    "aborted_at",
    "created_at",
    "evaluator_version",
    "effective_status",
    "expires_at",
    "mode",
    "promoted_at",
    "started_at",
    "state",
    "status",
    "status_reason",
    "updated_at",
})
_SAFE_STATUS_ARRAY_FIELDS = frozenset({
    "reason_codes",
    "strategies",
    "target_families",
})
_SAFE_REASON_CODES = frozenset({
    "application_receipt_invalid",
    "assignment_integrity",
    "attestation_invalid",
    "cap_reached",
    "causal_threshold_not_met",
    "contaminated",
    "drift",
    "drift_detected",
    "expired",
    "imbalance",
    "insufficient_samples",
    "integrity_error",
    "ledger_error",
    "manifest_drift",
    "operator_abort",
    "pin_mismatch",
    "prediction_outcome_invalid",
    "prompt_receipt_invalid",
    "prompt_receipt_missing",
    "quarantine",
    "quarantine_detected",
    "runtime_drift",
})
_SAFE_STATES = frozenset({
    "aborted",
    "active",
    "blocked",
    "closed",
    "complete",
    "expired",
    "pending",
    "promoted",
    "quarantined",
})
_SAFE_MODES = frozenset({"advise", "disabled", "observe", "trial"})


def _safe_utc_timestamp(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 40 or not value.endswith("Z"):
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return value


def sanitized_trial_status(
    value: Any,
    *,
    allowed_families: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """Return only closed, prompt-free status fields for CLI/UI rendering."""
    if value is None:
        rows: Sequence[Any] = ()
    elif isinstance(value, Mapping):
        rows = (value,)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        rows = value
    else:
        raise StrategyTransferOperatorError("trial status payload is malformed")
    result: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise StrategyTransferOperatorError("trial status row is malformed")
        clean: dict[str, Any] = {}
        for key in _SAFE_STATUS_INTEGER_FIELDS:
            raw = row.get(key)
            if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
                clean[key] = raw
        for key in _SAFE_STATUS_FLOAT_FIELDS:
            raw = row.get(key)
            if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                clean[key] = float(raw)
        for key in _SAFE_STATUS_BOOLEAN_FIELDS:
            raw = row.get(key)
            if isinstance(raw, bool):
                clean[key] = raw
        for key in _SAFE_STATUS_HASH_FIELDS:
            raw = row.get(key)
            if isinstance(raw, str) and _SHA256_RE.fullmatch(raw):
                clean[key] = raw
        for key in _SAFE_STATUS_TEXT_FIELDS:
            raw = row.get(key)
            if key in {
                "aborted_at",
                "created_at",
                "expires_at",
                "promoted_at",
                "started_at",
                "updated_at",
            }:
                safe_timestamp = _safe_utc_timestamp(raw)
                if safe_timestamp is not None:
                    clean[key] = safe_timestamp
            elif key in {"effective_status", "state", "status"} and (
                raw in _SAFE_STATES
            ):
                clean[key] = raw
            elif key == "mode" and raw in _SAFE_MODES:
                clean[key] = raw
            elif key in {"abort_reason_code", "status_reason"} and (
                raw in _SAFE_REASON_CODES
            ):
                clean[key] = raw
            elif key == "evaluator_version" and isinstance(raw, str) and (
                _VERSION_RE.fullmatch(raw) is not None
            ):
                clean[key] = raw
        for key in _SAFE_STATUS_ARRAY_FIELDS:
            raw = row.get(key)
            if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
                continue
            if key == "strategies":
                allowed_values = STRATEGY_SET
            elif key == "target_families":
                allowed_values = frozenset(
                    str(entry) for entry in allowed_families
                )
            else:
                allowed_values = _SAFE_REASON_CODES
            bounded: list[str] = []
            for item in raw[:16]:
                if not isinstance(item, str):
                    continue
                if item in allowed_values:
                    bounded.append(item)
            clean[key] = bounded
        family_caps = row.get("family_caps")
        if isinstance(family_caps, Mapping):
            allowed_family_set = frozenset(
                str(entry) for entry in allowed_families
            )
            clean_caps: dict[str, int] = {}
            for family, cap in family_caps.items():
                if (
                    family in allowed_family_set
                    and isinstance(cap, int)
                    and not isinstance(cap, bool)
                    and 0 <= cap <= TRIAL_MAX_SAMPLE_CAP
                ):
                    clean_caps[str(family)] = cap
            clean["family_caps"] = clean_caps
        result.append(clean)
    return result


def trial_status_line(row: Mapping[str, Any]) -> str:
    manifest_id = row.get("manifest_id", row.get("id", "?"))
    state = row.get("state", row.get("status", "unknown"))
    project_id = row.get("project_id", "?")
    families = ",".join(row.get("target_families", [])) or "none"
    assigned = row.get(
        "assigned", row.get("assigned_count", row.get("assignment_count", 0))
    )
    cap = row.get("sample_cap", "?")
    return (
        f"#{manifest_id} {state} project={project_id} "
        f"families={families} assignments={assigned}/{cap}"
    )
