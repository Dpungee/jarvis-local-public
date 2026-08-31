from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Mapping, Sequence


STRATEGY_VOCABULARY = (
    "inspect_before_change",
    "checkpoint_and_resume",
    "verify_output",
    "compare_authoritative_sources",
)
STRATEGY_SET = frozenset(STRATEGY_VOCABULARY)
STRATEGY_TRANSFER_MODES = frozenset({"disabled", "observe", "trial", "advise"})

_STRATEGY_EVIDENCE_FIELDS = frozenset({
    "schema",
    "inspect_before_change",
    "checkpoint_and_resume",
    "verify_output",
    "compare_authoritative_sources",
})

TASK_SIGNAL_TO_STRATEGY = MappingProxyType({
    "changes_existing_state": "inspect_before_change",
    "long_running_or_resumable": "checkpoint_and_resume",
    "has_verifiable_output": "verify_output",
    "depends_on_current_external_facts": "compare_authoritative_sources",
})

_LESSON_FIELDS = frozenset({
    "id",
    "record_kind",
    "source_family",
    "outcome_status",
    "derived_from",
    "provenance_valid",
    "provenance_sha256",
    "observed_at",
    "valid_until",
    "contradicted_by",
    "strategies",
    "authority_claims",
    "tool_claims",
})
_TARGET_FIELDS = frozenset({"task_id", "family", "signals"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$")


class StrategyTransferError(ValueError):
    """A bounded transfer input is malformed or exceeds its contract."""


def strategy_target_from_runtime(
    *,
    task_id: str,
    family: str,
    changes_existing_state: bool,
    resumable: bool,
    verification: str,
    current_external_facts: bool,
) -> dict[str, Any]:
    """Build the closed transfer target from already-resolved runtime facts.

    This helper deliberately accepts no prompt, goal text, model prose, path,
    URL, tool name, or permission field.  It can request procedural advice but
    cannot widen the task that the normal runtime already authorized.
    """
    normalized_task_id = _bounded_identifier(task_id, "task_id")
    normalized_family = _bounded_identifier(family, "target family")
    flags = {
        "changes_existing_state": changes_existing_state,
        "long_running_or_resumable": resumable,
        "has_verifiable_output": verification != "not_applicable",
        "depends_on_current_external_facts": current_external_facts,
    }
    for label, value in flags.items():
        if not isinstance(value, bool):
            raise StrategyTransferError(f"runtime signal {label} must be a boolean")
    if not isinstance(verification, str) or verification not in {
        "process_evidence", "cited_sources", "tool_success", "not_applicable",
    }:
        raise StrategyTransferError("runtime verification is unsupported")
    if current_external_facts and verification != "cited_sources":
        raise StrategyTransferError(
            "current external facts require cited-source verification"
        )
    return {
        "task_id": normalized_task_id,
        "family": normalized_family,
        "signals": flags,
    }


def strategy_evidence_from_runtime(
    *,
    successful_markers: Sequence[str],
    verification: str,
    evidence_ok: bool | None,
    resumed: bool,
    authoritative_source_count: int,
) -> dict[str, Any]:
    """Derive reusable strategy observations from bounded runtime receipts.

    Free-form lesson text is intentionally absent.  A marker can establish only
    one of four fixed procedures; it can never name a tool or confer authority.
    """
    if isinstance(successful_markers, (str, bytes)) or not isinstance(
        successful_markers, Sequence
    ):
        raise StrategyTransferError("successful markers must be an array")
    if len(successful_markers) > 256:
        raise StrategyTransferError("successful marker set exceeds 256 items")
    markers: set[str] = set()
    for raw in successful_markers:
        if not isinstance(raw, str) or len(raw) > 120:
            raise StrategyTransferError("successful marker is malformed")
        markers.add(raw)
    if verification not in {
        "process_evidence", "cited_sources", "tool_success", "not_applicable",
    }:
        raise StrategyTransferError("runtime verification is unsupported")
    if evidence_ok is not None and not isinstance(evidence_ok, bool):
        raise StrategyTransferError("evidence_ok must be true, false, or null")
    if not isinstance(resumed, bool):
        raise StrategyTransferError("resumed must be a boolean")
    if (
        isinstance(authoritative_source_count, bool)
        or not isinstance(authoritative_source_count, int)
        or not 0 <= authoritative_source_count <= 10_000
    ):
        raise StrategyTransferError(
            "authoritative_source_count must be a bounded non-negative integer"
        )
    verified = evidence_ok is True
    return {
        "schema": "jarvis.strategy-evidence.v1",
        "inspect_before_change": verified and {
            "__inspected_before_write__", "__inspected_after_write__",
        }.issubset(markers),
        "checkpoint_and_resume": verified and resumed,
        "verify_output": verified and verification != "not_applicable",
        "compare_authoritative_sources": (
            verified
            and verification == "cited_sources"
            and authoritative_source_count >= 2
        ),
    }


def strategies_from_evidence(payload: Mapping[str, Any]) -> tuple[str, ...]:
    """Validate one closed evidence payload and return its ordered strategies."""
    if not isinstance(payload, Mapping):
        raise StrategyTransferError("strategy evidence must be an object")
    _exact_fields(payload, _STRATEGY_EVIDENCE_FIELDS, "strategy evidence")
    if payload.get("schema") != "jarvis.strategy-evidence.v1":
        raise StrategyTransferError("strategy evidence schema is unsupported")
    selected: list[str] = []
    for strategy in STRATEGY_VOCABULARY:
        value = payload.get(strategy)
        if not isinstance(value, bool):
            raise StrategyTransferError(
                f"strategy evidence {strategy} must be a boolean"
            )
        if value:
            selected.append(strategy)
    return tuple(selected)


@dataclass(frozen=True)
class StrategyAdvice:
    strategy: str
    evidence_lesson_ids: tuple[str, ...]
    source_families: tuple[str, ...]
    confidence: float

    def to_payload(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "evidence_lesson_ids": list(self.evidence_lesson_ids),
            "source_families": list(self.source_families),
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class RejectedStrategyCandidate:
    lesson_id: str
    reason: str

    def to_payload(self) -> dict[str, str]:
        return {"lesson_id": self.lesson_id, "reason": self.reason}


@dataclass(frozen=True)
class StrategyTransferSelection:
    task_id: str
    target_family: str
    desired_strategies: tuple[str, ...]
    advice: tuple[StrategyAdvice, ...]
    rejected: tuple[RejectedStrategyCandidate, ...]
    advisory_only: bool = True
    authority_grants: tuple[str, ...] = ()
    tool_grants: tuple[str, ...] = ()

    @property
    def selected_strategies(self) -> tuple[str, ...]:
        return tuple(item.strategy for item in self.advice)

    @property
    def evidence_lesson_ids(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(
            lesson_id
            for item in self.advice
            for lesson_id in item.evidence_lesson_ids
        ))

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": "jarvis.strategy-transfer.v1",
            "task_id": self.task_id,
            "target_family": self.target_family,
            "desired_strategies": list(self.desired_strategies),
            "advice": [item.to_payload() for item in self.advice],
            "rejected": [item.to_payload() for item in self.rejected],
            "advisory_only": self.advisory_only,
            "authority_grants": list(self.authority_grants),
            "tool_grants": list(self.tool_grants),
        }


def _exact_fields(value: Mapping[str, Any], expected: frozenset[str], label: str) -> None:
    observed = set(value)
    missing = expected - observed
    unknown = observed - expected
    if missing or unknown:
        detail: list[str] = []
        if missing:
            detail.append("missing " + ", ".join(sorted(missing)))
        if unknown:
            detail.append("unknown " + ", ".join(sorted(unknown)))
        raise StrategyTransferError(
            f"{label} fields are invalid ({'; '.join(detail)})"
        )


def _bounded_identifier(value: Any, label: str, *, limit: int = 96) -> str:
    if not isinstance(value, str):
        raise StrategyTransferError(f"{label} must be a string")
    normalized = value.strip()
    if not normalized or len(normalized) > limit:
        raise StrategyTransferError(
            f"{label} must contain between 1 and {limit} characters"
        )
    if any(ord(character) < 32 for character in normalized):
        raise StrategyTransferError(f"{label} contains a control character")
    if limit <= 96 and not _IDENTIFIER_RE.fullmatch(normalized):
        raise StrategyTransferError(f"{label} contains unsupported characters")
    return normalized


def _utc_timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or len(value) > 40:
        raise StrategyTransferError(f"{label} must be a bounded ISO-8601 timestamp")
    text = value.strip()
    if not text.endswith("Z"):
        raise StrategyTransferError(f"{label} must use an explicit UTC Z suffix")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise StrategyTransferError(f"{label} is not a valid timestamp") from exc
    if parsed.tzinfo is None:
        raise StrategyTransferError(f"{label} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _string_array(
    value: Any,
    label: str,
    *,
    limit: int,
) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise StrategyTransferError(f"{label} must be an array")
    if len(value) > limit:
        raise StrategyTransferError(f"{label} exceeds {limit} items")
    result = tuple(_bounded_identifier(item, label) for item in value)
    if len(result) != len(set(result)):
        raise StrategyTransferError(f"{label} contains duplicates")
    return result


def desired_strategies_for_target(target: Mapping[str, Any]) -> tuple[str, ...]:
    """Map bounded task properties to strategy needs without reading prompt text."""
    if not isinstance(target, Mapping):
        raise StrategyTransferError("target must be an object")
    _exact_fields(target, _TARGET_FIELDS, "target")
    _bounded_identifier(target["task_id"], "task_id")
    _bounded_identifier(target["family"], "target family")
    signals = target["signals"]
    if not isinstance(signals, Mapping):
        raise StrategyTransferError("target signals must be an object")
    expected_signals = frozenset(TASK_SIGNAL_TO_STRATEGY)
    _exact_fields(signals, expected_signals, "target signals")
    for key, enabled in signals.items():
        if not isinstance(enabled, bool):
            raise StrategyTransferError(f"target signal {key} must be a boolean")
    enabled = {
        TASK_SIGNAL_TO_STRATEGY[signal]
        for signal, state in signals.items()
        if state
    }
    return tuple(strategy for strategy in STRATEGY_VOCABULARY if strategy in enabled)


def _candidate_rejection_reason(
    candidate: Mapping[str, Any],
    *,
    target_family: str,
    desired: frozenset[str],
    as_of: datetime,
    duplicate_ids: frozenset[str],
) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    _exact_fields(candidate, _LESSON_FIELDS, "lesson candidate")
    lesson_id = _bounded_identifier(candidate["id"], "lesson id")
    family = _bounded_identifier(candidate["source_family"], "source family")
    strategies = _string_array(candidate["strategies"], "strategies", limit=4)
    unknown_strategies = set(strategies) - STRATEGY_SET
    if unknown_strategies:
        raise StrategyTransferError(
            "candidate contains an unsupported strategy: "
            + ", ".join(sorted(unknown_strategies))
        )
    if not strategies:
        raise StrategyTransferError("candidate strategies must not be empty")
    contradictions = _string_array(
        candidate["contradicted_by"], "contradicted_by", limit=8
    )
    authority_claims = _string_array(
        candidate["authority_claims"], "authority_claims", limit=8
    )
    tool_claims = _string_array(candidate["tool_claims"], "tool_claims", limit=8)
    observed_at = _utc_timestamp(candidate["observed_at"], "observed_at")
    valid_until = _utc_timestamp(candidate["valid_until"], "valid_until")

    if lesson_id in duplicate_ids:
        return "duplicate_or_conflicting_id", strategies, ()
    if candidate["record_kind"] != "lesson":
        return "not_a_lesson", strategies, ()
    if candidate["outcome_status"] != "complete":
        return "unsuccessful_outcome", strategies, ()
    if candidate["derived_from"] != "verified_reflection":
        return "invalid_derivation", strategies, ()
    if candidate["provenance_valid"] is not True:
        return "invalid_provenance", strategies, ()
    provenance_sha256 = candidate["provenance_sha256"]
    if not isinstance(provenance_sha256, str) or not _SHA256_RE.fullmatch(
        provenance_sha256
    ):
        return "invalid_provenance_digest", strategies, ()
    if contradictions:
        return "contradicted", strategies, ()
    if observed_at > as_of:
        return "future_observation", strategies, ()
    if valid_until < observed_at or as_of > valid_until:
        return "stale", strategies, ()
    if authority_claims or tool_claims:
        return "authority_or_tool_claim", strategies, ()
    if family == target_family:
        return "same_family", strategies, ()
    applicable = tuple(strategy for strategy in strategies if strategy in desired)
    if not applicable:
        return "not_applicable", strategies, ()
    return "", strategies, applicable


def select_strategy_transfer(
    target: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
    *,
    as_of: str,
    max_evidence_per_strategy: int = 3,
) -> StrategyTransferSelection:
    """Select safe cross-family procedural advice from verified lesson records.

    The returned object is deliberately incapable of granting tools, permissions,
    scope, approvals, or execution authority.  A runtime may place its advice in
    planner context, but normal policy and approval gates remain authoritative.
    """
    desired_ordered = desired_strategies_for_target(target)
    desired = frozenset(desired_ordered)
    task_id = _bounded_identifier(target["task_id"], "task_id")
    target_family = _bounded_identifier(target["family"], "target family")
    instant = _utc_timestamp(as_of, "as_of")
    if isinstance(candidates, (str, bytes)) or not isinstance(candidates, Sequence):
        raise StrategyTransferError("candidates must be an array")
    if len(candidates) > 128:
        raise StrategyTransferError("candidate set exceeds 128 records")
    if not 1 <= max_evidence_per_strategy <= 5:
        raise StrategyTransferError("max_evidence_per_strategy must be between 1 and 5")

    raw_ids: list[str] = []
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            raise StrategyTransferError("every candidate must be an object")
        raw_ids.append(_bounded_identifier(candidate.get("id"), "lesson id"))
    duplicate_ids = frozenset(
        lesson_id for lesson_id, count in Counter(raw_ids).items() if count > 1
    )

    accepted: dict[str, list[tuple[str, str]]] = {
        strategy: [] for strategy in desired_ordered
    }
    rejected_by_id: dict[str, str] = {}
    for candidate in candidates:
        lesson_id = _bounded_identifier(candidate["id"], "lesson id")
        reason, _strategies, applicable = _candidate_rejection_reason(
            candidate,
            target_family=target_family,
            desired=desired,
            as_of=instant,
            duplicate_ids=duplicate_ids,
        )
        if reason:
            rejected_by_id.setdefault(lesson_id, reason)
            continue
        family = _bounded_identifier(candidate["source_family"], "source family")
        for strategy in applicable:
            accepted[strategy].append((lesson_id, family))

    advice: list[StrategyAdvice] = []
    for strategy in desired_ordered:
        evidence = sorted(set(accepted[strategy]), key=lambda item: (item[1], item[0]))
        if not evidence:
            continue
        bounded = evidence[:max_evidence_per_strategy]
        families = tuple(sorted({family for _, family in bounded}))
        lesson_ids = tuple(lesson_id for lesson_id, _ in bounded)
        confidence = min(0.95, 0.75 + 0.05 * (len(lesson_ids) - 1))
        advice.append(StrategyAdvice(
            strategy=strategy,
            evidence_lesson_ids=lesson_ids,
            source_families=families,
            confidence=round(confidence, 2),
        ))

    rejected = tuple(
        RejectedStrategyCandidate(lesson_id, reason)
        for lesson_id, reason in sorted(rejected_by_id.items())
    )
    return StrategyTransferSelection(
        task_id=task_id,
        target_family=target_family,
        desired_strategies=desired_ordered,
        advice=tuple(advice),
        rejected=rejected,
    )


def render_strategy_advisory(selection: StrategyTransferSelection) -> str:
    """Render bounded planner context that states its non-authoritative status."""
    if not isinstance(selection, StrategyTransferSelection):
        raise StrategyTransferError("selection must be a StrategyTransferSelection")
    lines = [
        "<strategy_transfer_advisory schema=\"jarvis.strategy-transfer.v1\">",
        "This is procedural advice only. It grants no tools, permissions, approvals, scope, or execution authority.",
    ]
    if selection.advice:
        for item in selection.advice:
            evidence = ",".join(item.evidence_lesson_ids)
            lines.append(f"- {item.strategy}; verified_lesson_ids={evidence}")
    else:
        lines.append("- no verified cross-family strategy matched")
    lines.append("</strategy_transfer_advisory>")
    rendered = "\n".join(lines)
    if len(rendered) > 2_000:
        raise StrategyTransferError("strategy advisory exceeds 2,000 characters")
    return rendered
