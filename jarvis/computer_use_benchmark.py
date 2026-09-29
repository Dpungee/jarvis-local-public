"""Synthetic-only benchmark contracts for Jarvis computer-use reliability.

The suite format intentionally excludes real accounts, live URLs, personal files,
and unrestricted networks.  It can be executed by a disposable VM runner later,
but validation and scoring are useful now and prevent a benchmark from quietly
turning into a probe of the operator's computer.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any


SCHEMA_VERSION = 1
MAX_CASES = 128
MAX_EVIDENCE = 16
_IDENTIFIER = re.compile(r"[a-z][a-z0-9_.-]{0,79}")
_TASK_KINDS = frozenset({"browser_fixture", "file_manager", "image_editor", "local_document", "window_navigation"})
_RESULT_STATUSES = frozenset({"blocked", "cancelled", "failed", "succeeded"})


class ComputerUseBenchmarkError(ValueError):
    """Raised when a benchmark case is not isolated, deterministic, or safe."""


def _identifier(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ComputerUseBenchmarkError(f"{label} must be a bounded identifier")
    return value


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _safe_evidence(value: Any, *, label: str) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ComputerUseBenchmarkError(f"{label} must be a list")
    if not 1 <= len(value) <= MAX_EVIDENCE:
        raise ComputerUseBenchmarkError(f"{label} has an invalid size")
    values = [_identifier(item, label=label) for item in value]
    if len(set(values)) != len(values):
        raise ComputerUseBenchmarkError(f"{label} must not contain duplicates")
    return sorted(values)


def validate_computer_use_suite(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a closed suite of synthetic, disposable-environment tasks."""

    if not isinstance(value, Mapping):
        raise TypeError("computer-use benchmark suite must be a mapping")
    required = {"schema_version", "suite_id", "environment", "cases"}
    if set(value) != required:
        raise ComputerUseBenchmarkError("computer-use suite fields are invalid")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise ComputerUseBenchmarkError("computer-use suite schema is unsupported")
    environment = value.get("environment")
    if environment != {"kind": "isolated_vm", "network": "disabled", "accounts": "synthetic"}:
        raise ComputerUseBenchmarkError("computer-use suite must use an offline isolated VM and synthetic accounts")
    cases = value.get("cases")
    if not isinstance(cases, Sequence) or isinstance(cases, (str, bytes)):
        raise ComputerUseBenchmarkError("computer-use cases must be a list")
    if not 1 <= len(cases) <= MAX_CASES:
        raise ComputerUseBenchmarkError("computer-use case count is invalid")
    clean_cases: list[dict[str, Any]] = []
    case_ids: set[str] = set()
    for raw in cases:
        if not isinstance(raw, Mapping):
            raise ComputerUseBenchmarkError("computer-use case must be a mapping")
        allowed = {"case_id", "task_kind", "required_evidence"}
        if set(raw) != allowed:
            raise ComputerUseBenchmarkError("computer-use case fields are invalid")
        case_id = _identifier(raw.get("case_id"), label="case id")
        if case_id in case_ids:
            raise ComputerUseBenchmarkError("computer-use case ids must be unique")
        case_ids.add(case_id)
        task_kind = raw.get("task_kind")
        if task_kind not in _TASK_KINDS:
            raise ComputerUseBenchmarkError("computer-use task kind is unsupported")
        clean_cases.append({
            "case_id": case_id,
            "task_kind": task_kind,
            "required_evidence": _safe_evidence(raw.get("required_evidence"), label="required evidence"),
        })
    return {
        "schema_version": SCHEMA_VERSION,
        "suite_id": _identifier(value.get("suite_id"), label="suite id"),
        "environment": dict(environment),
        "cases": sorted(clean_cases, key=lambda item: item["case_id"]),
    }


def score_computer_use_suite(
    suite: Mapping[str, Any],
    results: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Score synthetic runner receipts without retaining screenshots or text."""

    safe_suite = validate_computer_use_suite(suite)
    if not isinstance(results, Sequence) or isinstance(results, (str, bytes)):
        raise TypeError("computer-use results must be a list")
    expected = {case["case_id"]: case for case in safe_suite["cases"]}
    if len(results) != len(expected):
        raise ComputerUseBenchmarkError("computer-use results must cover every case exactly once")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in results:
        if not isinstance(raw, Mapping) or set(raw) != {"case_id", "status", "evidence"}:
            raise ComputerUseBenchmarkError("computer-use result fields are invalid")
        case_id = _identifier(raw.get("case_id"), label="case id")
        if case_id not in expected or case_id in seen:
            raise ComputerUseBenchmarkError("computer-use result case ids are invalid")
        seen.add(case_id)
        status = raw.get("status")
        if status not in _RESULT_STATUSES:
            raise ComputerUseBenchmarkError("computer-use result status is unsupported")
        evidence = _safe_evidence(raw.get("evidence"), label="result evidence")
        required = set(expected[case_id]["required_evidence"])
        observed = set(evidence)
        missing = sorted(required - observed)
        passed = status == "succeeded" and not missing
        rows.append({
            "case_id": case_id,
            "status": status,
            "passed": passed,
            "missing_evidence": missing,
        })
    rows.sort(key=lambda item: item["case_id"])
    passed = sum(1 for row in rows if row["passed"])
    receipt_body = {"suite": safe_suite, "results": rows}
    return {
        "suite_id": safe_suite["suite_id"],
        "cases": len(rows),
        "passed": passed,
        "pass_rate": passed / len(rows),
        "results": rows,
        "receipt_checksum_sha256": hashlib.sha256(_canonical_json(receipt_body).encode("utf-8")).hexdigest(),
    }


__all__ = [
    "ComputerUseBenchmarkError",
    "SCHEMA_VERSION",
    "score_computer_use_suite",
    "validate_computer_use_suite",
]
