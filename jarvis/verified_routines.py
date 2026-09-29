"""Validation for replayable, operator-gated learned desktop routines.

This module does *not* execute input.  It defines the narrow artifact a future
desktop executor may consume after a workflow has succeeded in an isolated
benchmark and been independently replayed.  The artifact retains no screenshots,
coordinates, typed text, or account identifiers; each step references only a
closed action kind and opaque state/evidence labels.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any


SCHEMA_VERSION = 1
MAX_STATES = 64
_IDENTIFIER = re.compile(r"[a-z][a-z0-9_.-]{0,79}")
_ACTION_KINDS = frozenset({"click", "hotkey", "scroll", "type", "wait"})


class VerifiedRoutineError(ValueError):
    """Raised when a routine did not earn safe, verified reuse."""


def _identifier(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise VerifiedRoutineError(f"{label} must be a bounded identifier")
    return value


def _signals(value: Any, *, label: str) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise VerifiedRoutineError(f"{label} must be a list")
    if not 1 <= len(value) <= 16:
        raise VerifiedRoutineError(f"{label} has an invalid size")
    signals = [_identifier(item, label=label) for item in value]
    if len(set(signals)) != len(signals):
        raise VerifiedRoutineError(f"{label} must not contain duplicates")
    return sorted(signals)


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def validate_verified_routine(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a routine that has earned reuse through independent replay."""

    if not isinstance(value, Mapping):
        raise TypeError("verified routine must be a mapping")
    required = {
        "schema_version", "routine_id", "source", "verification",
        "start_state", "states", "operator_approval_required",
    }
    if set(value) != required:
        raise VerifiedRoutineError("verified routine fields are invalid")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise VerifiedRoutineError("verified routine schema is unsupported")
    if value.get("operator_approval_required") is not True:
        raise VerifiedRoutineError("verified routines must retain operator approval")
    source = value.get("source")
    if not isinstance(source, Mapping) or set(source) != {"suite_id", "case_id", "environment_fingerprint"}:
        raise VerifiedRoutineError("verified routine source is invalid")
    fingerprint = source.get("environment_fingerprint")
    if not isinstance(fingerprint, str) or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None:
        raise VerifiedRoutineError("verified routine environment fingerprint is invalid")
    verification = value.get("verification")
    if not isinstance(verification, Mapping) or set(verification) != {"independent_replay_passed", "success_count", "failure_count"}:
        raise VerifiedRoutineError("verified routine verification is invalid")
    if verification.get("independent_replay_passed") is not True:
        raise VerifiedRoutineError("verified routine requires independent replay proof")
    success_count = verification.get("success_count")
    failure_count = verification.get("failure_count")
    if isinstance(success_count, bool) or not isinstance(success_count, int) or success_count < 2:
        raise VerifiedRoutineError("verified routine requires at least two successful runs")
    if isinstance(failure_count, bool) or not isinstance(failure_count, int) or failure_count != 0:
        raise VerifiedRoutineError("verified routine cannot contain failed verification runs")
    states = value.get("states")
    if not isinstance(states, Sequence) or isinstance(states, (str, bytes)) or not 1 <= len(states) <= MAX_STATES:
        raise VerifiedRoutineError("verified routine states are invalid")
    clean_states: list[dict[str, Any]] = []
    state_ids: set[str] = set()
    for raw in states:
        if not isinstance(raw, Mapping) or set(raw) != {"state_id", "expected_signals", "action_kind", "action_ref", "next_state"}:
            raise VerifiedRoutineError("verified routine state fields are invalid")
        state_id = _identifier(raw.get("state_id"), label="state id")
        if state_id in state_ids:
            raise VerifiedRoutineError("verified routine state ids must be unique")
        state_ids.add(state_id)
        action_kind = raw.get("action_kind")
        if action_kind not in _ACTION_KINDS:
            raise VerifiedRoutineError("verified routine action is unsupported")
        next_state = raw.get("next_state")
        if next_state != "complete":
            next_state = _identifier(next_state, label="next state")
        clean_states.append({
            "state_id": state_id,
            "expected_signals": _signals(raw.get("expected_signals"), label="expected signals"),
            "action_kind": action_kind,
            "action_ref": _identifier(raw.get("action_ref"), label="action ref"),
            "next_state": next_state,
        })
    missing_targets = {state["next_state"] for state in clean_states if state["next_state"] != "complete"} - state_ids
    if missing_targets:
        raise VerifiedRoutineError("verified routine state transition is unknown")
    start_state = _identifier(value.get("start_state"), label="start state")
    if start_state not in state_ids:
        raise VerifiedRoutineError("verified routine start state is unknown")
    transitions = {state["state_id"]: state["next_state"] for state in clean_states}
    visited: set[str] = set()
    current = start_state
    while current != "complete":
        if current in visited:
            raise VerifiedRoutineError("verified routine contains a state cycle")
        visited.add(current)
        current = transitions[current]
    if visited != state_ids:
        raise VerifiedRoutineError("verified routine contains unreachable states")
    return {
        "schema_version": SCHEMA_VERSION,
        "routine_id": _identifier(value.get("routine_id"), label="routine id"),
        "source": {
            "suite_id": _identifier(source.get("suite_id"), label="suite id"),
            "case_id": _identifier(source.get("case_id"), label="case id"),
            "environment_fingerprint": fingerprint,
        },
        "verification": {
            "independent_replay_passed": True,
            "success_count": success_count,
            "failure_count": 0,
        },
        "start_state": start_state,
        "states": sorted(clean_states, key=lambda item: item["state_id"]),
        "operator_approval_required": True,
    }


def verified_routine_receipt(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return a stable, non-executable receipt for one verified routine."""

    routine = validate_verified_routine(value)
    checksum = hashlib.sha256(_canonical_json(routine).encode("utf-8")).hexdigest()
    return {
        "routine_id": routine["routine_id"],
        "state_count": len(routine["states"]),
        "operator_approval_required": True,
        "routine_checksum_sha256": checksum,
        "routine": routine,
    }


__all__ = [
    "SCHEMA_VERSION",
    "VerifiedRoutineError",
    "validate_verified_routine",
    "verified_routine_receipt",
]
