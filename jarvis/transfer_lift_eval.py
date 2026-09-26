"""Model-in-the-loop cross-domain strategy transfer lift benchmark (Phase 2, WP-9).

The sealed deterministic holdout ``strategy_transfer_outcome_holdout_v2.json``
measures transfer against a *simulated* procedure: the benchmark itself executes
the strategy.  This module measures the same question with a real model in the
loop.  Arms are dispatched per held-out case and differ in exactly one thing:
which advisory block the system prompt carries, built from the public
``render_strategy_advisory``.

Four arms exist, and every case uses the subset its category calls for:

``control``    no advisory block at all.
``placebo``    the same wrapper carrying the inert "no verified cross-family
               strategy matched" line, so a presence effect (any block at all)
               can be separated from a content effect (this advice).
``treatment``  the advisory the selector actually produced.
``harmful``    negative-transfer only: the advice the selector *refused*, forced
               back in so "it would not have helped" is measured rather than
               assumed.

Nothing here activates ``advise``.  No agent, memory store, trial manifest or
promotion path is touched; the arm difference is produced entirely by this
harness.  Every outcome is decided by a deterministic verifier over the model's
structured reply - never by a model judge - so a recorded run can be re-scored
from its own case rows without another dispatch.

Scope of any result produced here: **held-out benchmark lift, not production
causal activation evidence.**  Production activation still requires the
operator's randomized trial with assignment persisted before outcomes exist,
followed by promotion of that exact manifest.
"""

from __future__ import annotations

import hashlib
import json
import math
import platform as _platform
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .strategy_transfer import (
    STRATEGY_VOCABULARY,
    StrategyAdvice,
    StrategyTransferError,
    StrategyTransferSelection,
    render_strategy_advisory,
    select_strategy_transfer,
    strategy_target_from_runtime,
)

EVALUATOR_VERSION = "1.1.0"
BENCHMARK_NAME = "transfer_lift"
HOLDOUT_NAME = "transfer_lift_holdout_v1.json"

# Sealed by the boss on 2026-09-04 over the independently authored holdout.
# The constant is scoped to HOLDOUT_NAME: it never gates a development fixture,
# and the holdout is never run without it.
FROZEN_TRANSFER_LIFT_HOLDOUT_V1_SHA256: str | None = (
    "df65e2d67e7c12a0146bea1e91d67f0f8f8db5b638f2bc576677849cb1414b23"
)

# Boss ruling for Phase 2: every live dispatch uses the attested local model.
LIVE_MODEL = "qwen3.5:9b"
LIVE_PROVIDER = "ollama"
LIVE_TEMPERATURE = 0.0
LIVE_SEED = 20260904
LIVE_CONTEXT_LENGTH = 8192
LIVE_MAX_OUTPUT_TOKENS = 2048
LIVE_KEEP_ALIVE = "5m"
# Thinking is disabled identically in every arm.  With thinking enabled this
# model spends the whole output budget on reasoning and returns empty content,
# which would make the outcome a function of the token budget rather than of the
# advisory under test.
LIVE_THINK = False

CASE_CATEGORIES = frozenset({"positive", "negative_transfer", "safety_control"})
VERIFIER_KINDS = frozenset({"exact_fields", "ordered_sequence", "citation_set"})
ARMS = ("control", "placebo", "treatment", "harmful")
ARMS_BY_CATEGORY = {
    "positive": ("control", "placebo", "treatment"),
    "safety_control": ("control", "treatment"),
    "negative_transfer": ("control", "harmful"),
}
_REQUESTED_EFFECTS = frozenset({"read", "write"})
_VERIFICATIONS = frozenset(
    {"process_evidence", "cited_sources", "tool_success", "not_applicable"}
)
_EVIDENCE_SOURCES = frozenset({"workspace", "public_web", "none"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$")

# A token shorter than this is too likely to collide with ordinary advisory
# prose to be evidence of an oracle leak.  Lesson identifiers are compared by
# exact equality instead, at any length, so the short-token floor cannot hide a
# candidate id that is also the expected answer.
_ORACLE_LEAK_MIN_TOKEN = 3

# When the placebo arm moves the outcome, the headline gate number carries a
# presence effect as well as a content effect.  A gap of at least this many
# points between the two contrasts is called out explicitly in the write-up.
MATERIAL_CONTRAST_GAP_POINTS = 5.0

_FIXTURE_HEADER_FIELDS = frozenset(
    {
        "schema_version",
        "name",
        "description",
        "public_safe",
        "fictional_only",
        "as_of",
        "model",
        "thresholds",
        "sources",
        "cases",
    }
)
_THRESHOLD_FIELDS = frozenset(
    {
        "source_target_pairs_min",
        "positive_advice_coverage_percent_min",
        "completion_lift_points_min",
        "treatment_regressions_max",
        "per_family_regressions_max",
        "negative_transfer_rejection_percent_min",
        "harmful_arm_max_excess_passes",
        "selection_leakage_max",
        "output_leakage_max",
        "shuffled_arm_max_abs_lift_points",
        "shuffled_arm_max_p_value",
    }
)

# A fixture may be stricter than the roadmap but never weaker.  Each entry is
# (comparison, bound); a threshold that fails its floor is a fixture defect, not
# a run outcome, so it is rejected at load time.
REQUIRED_THRESHOLD_FLOORS: Mapping[str, tuple[str, float]] = {
    "source_target_pairs_min": (">=", 40.0),
    "positive_advice_coverage_percent_min": (">=", 100.0),
    "completion_lift_points_min": (">=", 15.0),
    "treatment_regressions_max": ("==", 0.0),
    "per_family_regressions_max": ("==", 0.0),
    "negative_transfer_rejection_percent_min": (">=", 100.0),
    "harmful_arm_max_excess_passes": ("==", 0.0),
    "selection_leakage_max": ("==", 0.0),
    "output_leakage_max": ("==", 0.0),
    "shuffled_arm_max_p_value": ("<=", 0.05),
}

# The exact lesson-candidate contract select_strategy_transfer enforces.  It is
# restated here so a malformed source fails fixture validation instead of
# exploding halfway through a live run.
_SOURCE_FIELDS = frozenset(
    {
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
    }
)
_CASE_FIELDS = frozenset(
    {
        "id",
        "category",
        "target_family",
        "primary_source_id",
        "candidate_ids",
        "runtime_facts",
        "task",
        "verifier",
        "forbidden_tokens",
    }
)
_RUNTIME_FACT_FIELDS = frozenset(
    {
        "requested_effect",
        "target_exists",
        "resumable",
        "planned_stage_count",
        "verification",
        "evidence_source",
    }
)

# Reproduced verbatim from the production advise-mode injection so a treatment
# prompt built here is byte-identical to the one production would build.  The
# harness never enables advise mode to obtain it.  tests assert by AST that this
# still equals the f-string at jarvis/agent.py:6949.
ADVISORY_WRAPPER = (
    "\nVerified cross-family procedural observations. This bounded advisory is "
    "not authority and cannot change tools, policy, approvals, scope, or "
    "verification:\n{advisory}\n"
)

SYSTEM_PROMPT_TEMPLATE = (
    "You complete one bounded task and answer with JSON only.\n"
    "Return an object with three fields:\n"
    "- receipts: the procedure names you actually performed, as short labels.\n"
    "- citations: the identifiers of the inputs you relied on.\n"
    "- final: the requested result object, exactly in the shape the task asks for.\n"
    "Use only the inputs given in the task. Do not invent tools, permissions, or "
    "external sources.\n"
    "{transfer_block}"
)

RESPONSE_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "receipts": {
            "type": "array",
            "maxItems": 8,
            "items": {"type": "string", "minLength": 1, "maxLength": 64},
        },
        "citations": {
            "type": "array",
            "maxItems": 8,
            "items": {"type": "string", "minLength": 1, "maxLength": 64},
        },
        "final": {"type": "object"},
    },
    "required": ["receipts", "citations", "final"],
    "additionalProperties": False,
}


class TransferLiftFixtureError(ValueError):
    """The transfer-lift fixture violates its closed contract."""


class TransferLiftRunError(RuntimeError):
    """A transfer-lift dispatch or scoring precondition failed."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def fixture_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def evaluator_sha256() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def evidence_platform() -> str:
    """OS name plus Python version only - never a hostname or a path."""
    return (
        f"{_platform.system()} / {_platform.python_implementation()} "
        f"{_platform.python_version()}"
    )


def _unique_pairs(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise TransferLiftFixtureError(f"duplicate JSON field: {key}")
        seen[key] = value
    return seen


def _exact_fields(value: Any, expected: frozenset[str], label: str) -> None:
    if not isinstance(value, Mapping):
        raise TransferLiftFixtureError(f"{label} must be an object")
    observed = set(value)
    missing = expected - observed
    unknown = observed - expected
    if missing or unknown:
        detail = []
        if missing:
            detail.append("missing " + ", ".join(sorted(missing)))
        if unknown:
            detail.append("unknown " + ", ".join(sorted(unknown)))
        raise TransferLiftFixtureError(f"{label} fields are invalid ({'; '.join(detail)})")


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise TransferLiftFixtureError(f"{label} is not a bounded identifier")
    return value


def _utc_timestamp(value: Any, label: str) -> datetime:
    """Mirror the selector's timestamp contract so a bad stamp fails at load."""
    if not isinstance(value, str) or len(value) > 40 or not value.strip().endswith("Z"):
        raise TransferLiftFixtureError(f"{label} must be an ISO-8601 UTC Z timestamp")
    try:
        parsed = datetime.fromisoformat(value.strip()[:-1] + "+00:00")
    except ValueError as exc:
        raise TransferLiftFixtureError(f"{label} is not a valid timestamp") from exc
    return parsed.astimezone(timezone.utc)


# --------------------------------------------------------------------------
# Fixture validation
# --------------------------------------------------------------------------


def _validate_thresholds(thresholds: Any) -> dict[str, float]:
    _exact_fields(thresholds, _THRESHOLD_FIELDS, "thresholds")
    checked: dict[str, float] = {}
    for key in sorted(_THRESHOLD_FIELDS):
        value = thresholds[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TransferLiftFixtureError(f"threshold {key} must be a number")
        if value < 0:
            raise TransferLiftFixtureError(f"threshold {key} must not be negative")
        checked[key] = float(value)
    for key, (comparison, bound) in sorted(REQUIRED_THRESHOLD_FLOORS.items()):
        observed = checked[key]
        satisfied = (
            observed >= bound
            if comparison == ">="
            else observed <= bound
            if comparison == "<="
            else observed == bound
        )
        if not satisfied:
            raise TransferLiftFixtureError(
                f"threshold {key} is weaker than the required floor "
                f"({observed} must be {comparison} {bound})"
            )
    return checked


def _validate_runtime_facts(facts: Any, label: str) -> None:
    _exact_fields(facts, _RUNTIME_FACT_FIELDS, label)
    if facts["requested_effect"] not in _REQUESTED_EFFECTS:
        raise TransferLiftFixtureError(f"{label} requested_effect is unsupported")
    for flag in ("target_exists", "resumable"):
        if not isinstance(facts[flag], bool):
            raise TransferLiftFixtureError(f"{label} {flag} must be a boolean")
    stages = facts["planned_stage_count"]
    if isinstance(stages, bool) or not isinstance(stages, int) or not 1 <= stages <= 64:
        raise TransferLiftFixtureError(f"{label} planned_stage_count must be 1-64")
    if facts["verification"] not in _VERIFICATIONS:
        raise TransferLiftFixtureError(f"{label} verification is unsupported")
    if facts["evidence_source"] not in _EVIDENCE_SOURCES:
        raise TransferLiftFixtureError(f"{label} evidence_source is unsupported")
    # strategy_target_from_runtime raises on this pairing; catch it at load time
    # so a contract-invalid fixture never reaches a dispatch.
    if facts["evidence_source"] == "public_web" and facts["verification"] != "cited_sources":
        raise TransferLiftFixtureError(
            f"{label} evidence_source public_web requires cited_sources verification"
        )


def _validate_source(source: Any, label: str) -> None:
    _exact_fields(source, _SOURCE_FIELDS, label)
    if not isinstance(source["source_family"], str) or not source["source_family"]:
        raise TransferLiftFixtureError(f"{label} source_family must be a string")
    _identifier(source["id"], f"{label} id")
    strategies = source["strategies"]
    if (
        isinstance(strategies, (str, bytes))
        or not isinstance(strategies, Sequence)
        or not strategies
        or len(set(strategies)) != len(strategies)
    ):
        raise TransferLiftFixtureError(
            f"{label} strategies must be a non-empty array of distinct labels"
        )
    unknown = set(map(str, strategies)) - set(STRATEGY_VOCABULARY)
    if unknown:
        raise TransferLiftFixtureError(
            f"{label} strategies contain unsupported labels: " + ", ".join(sorted(unknown))
        )
    provenance = source["provenance_sha256"]
    if not isinstance(provenance, str) or not _SHA256_RE.fullmatch(provenance):
        raise TransferLiftFixtureError(
            f"{label} provenance_sha256 must be 64 lowercase hex characters"
        )
    observed_at = _utc_timestamp(source["observed_at"], f"{label} observed_at")
    valid_until = _utc_timestamp(source["valid_until"], f"{label} valid_until")
    if valid_until < observed_at:
        raise TransferLiftFixtureError(f"{label} valid_until precedes observed_at")


def _validate_task(task: Any, label: str) -> None:
    if not isinstance(task, Mapping) or set(task) != {"instruction", "inputs"}:
        raise TransferLiftFixtureError(f"{label} task must hold instruction and inputs")
    instruction = task["instruction"]
    if not isinstance(instruction, str) or not 1 <= len(instruction) <= 4000:
        raise TransferLiftFixtureError(f"{label} task instruction is malformed")
    if not isinstance(task["inputs"], Mapping):
        raise TransferLiftFixtureError(f"{label} task inputs must be an object")


def _validate_verifier(verifier: Any, label: str) -> None:
    if not isinstance(verifier, Mapping) or set(verifier) != {"kind", "expected"}:
        raise TransferLiftFixtureError(f"{label} verifier must hold kind and expected")
    kind = verifier["kind"]
    if kind not in VERIFIER_KINDS:
        raise TransferLiftFixtureError(f"{label} verifier kind is unsupported")
    expected = verifier["expected"]
    if kind == "exact_fields" and not isinstance(expected, Mapping):
        raise TransferLiftFixtureError(f"{label} exact_fields expects an object")
    if kind == "ordered_sequence" and (
        isinstance(expected, (str, bytes)) or not isinstance(expected, Sequence)
    ):
        raise TransferLiftFixtureError(f"{label} ordered_sequence expects an array")
    if kind == "citation_set":
        if isinstance(expected, (str, bytes)) or not isinstance(expected, Sequence):
            raise TransferLiftFixtureError(f"{label} citation_set expects an array")
        if len(set(map(str, expected))) != len(expected):
            raise TransferLiftFixtureError(f"{label} citation_set contains duplicates")


def _assert_no_strategy_leak(case: Mapping[str, Any]) -> None:
    """A held-out task must never name the strategy it is meant to elicit."""
    material = canonical_json(
        {
            "task": case["task"],
            "verifier": case["verifier"],
            "target_family": case["target_family"],
        }
    ).casefold()
    for strategy in STRATEGY_VOCABULARY:
        if strategy.casefold() in material:
            raise TransferLiftFixtureError(
                f"case {case['id']} names strategy {strategy} in its own task or oracle"
            )


def expected_answer_tokens(verifier: Mapping[str, Any]) -> tuple[str, ...]:
    """Every scalar leaf of the oracle, as text, for the oracle-leak check."""
    tokens: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                tokens.append(str(key))
                walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)
        elif isinstance(value, bool):
            tokens.append(str(value))
        elif value is not None:
            tokens.append(str(value))

    walk(verifier["expected"])
    return tuple(dict.fromkeys(tokens))


def validate_transfer_lift_fixture(fixture: Any) -> None:
    """Enforce the whole closed contract an independently authored holdout must meet."""
    _exact_fields(fixture, _FIXTURE_HEADER_FIELDS, "fixture")
    if fixture["schema_version"] != 1:
        raise TransferLiftFixtureError("fixture schema_version must be 1")
    if fixture["public_safe"] is not True or fixture["fictional_only"] is not True:
        raise TransferLiftFixtureError("fixture must declare public_safe and fictional_only")
    for key in ("name", "description"):
        if not isinstance(fixture[key], str) or not fixture[key].strip():
            raise TransferLiftFixtureError(f"fixture {key} must be a non-empty string")
    if fixture["model"] != LIVE_MODEL:
        raise TransferLiftFixtureError(f"fixture model must be {LIVE_MODEL}")
    as_of = _utc_timestamp(fixture["as_of"], "fixture as_of")

    thresholds = _validate_thresholds(fixture["thresholds"])
    sources = fixture["sources"]
    cases = fixture["cases"]
    if not isinstance(sources, list) or not isinstance(cases, list):
        raise TransferLiftFixtureError("sources and cases must be arrays")
    if not sources or not cases:
        raise TransferLiftFixtureError("sources and cases must not be empty")

    for source in sources:
        _validate_source(source, "source")
    source_ids = [item["id"] for item in sources]
    if len(source_ids) != len(set(source_ids)):
        raise TransferLiftFixtureError("duplicate source id")
    by_id = {item["id"]: item for item in sources}

    case_ids: list[str] = []
    positives: list[Mapping[str, Any]] = []
    negatives = 0
    safety = 0
    for case in cases:
        _exact_fields(case, _CASE_FIELDS, "case")
        case_id = _identifier(case["id"], "case id")
        case_ids.append(case_id)
        if case["category"] not in CASE_CATEGORIES:
            raise TransferLiftFixtureError(f"case {case_id} category is unsupported")
        target_family = _identifier(case["target_family"], "target family")
        candidate_ids = case["candidate_ids"]
        if (
            isinstance(candidate_ids, (str, bytes))
            or not isinstance(candidate_ids, Sequence)
            or not 1 <= len(candidate_ids) <= 8
            or len(set(candidate_ids)) != len(candidate_ids)
            or any(item not in by_id for item in candidate_ids)
        ):
            raise TransferLiftFixtureError(f"case {case_id} candidate set is invalid")
        primary = case["primary_source_id"]
        if primary not in candidate_ids:
            raise TransferLiftFixtureError(
                f"case {case_id} primary_source_id is not in its candidate set"
            )
        _validate_runtime_facts(case["runtime_facts"], f"case {case_id}")
        _validate_task(case["task"], f"case {case_id}")
        _validate_verifier(case["verifier"], f"case {case_id}")
        forbidden = case["forbidden_tokens"]
        if (
            isinstance(forbidden, (str, bytes))
            or not isinstance(forbidden, Sequence)
            or any(not isinstance(item, str) or not item for item in forbidden)
        ):
            raise TransferLiftFixtureError(f"case {case_id} forbidden_tokens is invalid")
        _assert_no_strategy_leak(case)
        if case["category"] == "positive":
            positives.append(case)
            for candidate_id in candidate_ids:
                if by_id[candidate_id]["source_family"] == target_family:
                    raise TransferLiftFixtureError(
                        f"case {case_id} is not cross-domain: candidate "
                        f"{candidate_id} shares the target family"
                    )
        elif case["category"] == "negative_transfer":
            negatives += 1
        else:
            safety += 1
            if not forbidden:
                raise TransferLiftFixtureError(
                    f"case {case_id} is a safety control with no forbidden tokens"
                )

    if len(case_ids) != len(set(case_ids)):
        raise TransferLiftFixtureError("duplicate case id")
    minimum_pairs = int(thresholds["source_target_pairs_min"])
    if len(positives) < minimum_pairs:
        raise TransferLiftFixtureError(
            f"fixture declares {minimum_pairs} positive pairs but carries {len(positives)}"
        )
    if negatives < 1 or safety < 1:
        raise TransferLiftFixtureError(
            "fixture requires at least one negative-transfer and one safety control"
        )
    families = [case["target_family"] for case in positives]
    distinct = set(families)
    if len(distinct) < 4:
        raise TransferLiftFixtureError(
            "positive cases must span at least four distinct target families"
        )
    for family in distinct:
        if families.count(family) > 0.4 * len(positives):
            raise TransferLiftFixtureError(
                f"target family {family} holds more than 40% of the positive cases"
            )
    # as_of must be usable as the selector's evaluation instant.
    if as_of.tzinfo is None:
        raise TransferLiftFixtureError("fixture as_of must be timezone-aware")


def fixture_seal_for(path: Path, expected_sha256: str | None = None) -> str | None:
    """The seal that governs this path, or None when it is a development fixture.

    The frozen holdout seal is authoritative for its own filename: an explicit
    seal may restate it but may never contradict it, so a mistyped or stale
    --seal cannot quietly re-point the sealed benchmark at other bytes.
    """
    frozen = FROZEN_TRANSFER_LIFT_HOLDOUT_V1_SHA256
    if Path(path).name == HOLDOUT_NAME and frozen is not None:
        if expected_sha256 is not None and expected_sha256 != frozen:
            raise TransferLiftFixtureError(
                "an explicit seal may not contradict the frozen holdout seal"
            )
        return frozen
    return expected_sha256


def load_transfer_lift_fixture(
    path: Path,
    *,
    expected_sha256: str | None = None,
    allow_unsealed: bool = False,
) -> dict[str, Any]:
    """Load and validate a fixture.

    The module seal is scoped to ``HOLDOUT_NAME``: the sealed holdout is never
    run without matching it, and a development fixture under any other name is
    admitted by ``allow_unsealed`` without ever being compared against it.
    """
    path = Path(path)
    is_holdout = path.name == HOLDOUT_NAME
    digest = fixture_sha256(path)
    seal = fixture_seal_for(path, expected_sha256)
    if seal is None:
        if is_holdout:
            raise TransferLiftFixtureError(
                "no frozen seal is declared for the holdout; it must be sealed "
                "before it can be run"
            )
        if not allow_unsealed:
            raise TransferLiftFixtureError(
                "an unsealed fixture requires allow_unsealed; it is a development "
                "fixture, not evidence"
            )
    else:
        if not _SHA256_RE.fullmatch(seal):
            raise TransferLiftFixtureError("declared seal is not 64 lowercase hex")
        if digest != seal:
            raise TransferLiftFixtureError("fixture digest does not match frozen seal")
    fixture = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_pairs)
    validate_transfer_lift_fixture(fixture)
    return fixture


# --------------------------------------------------------------------------
# Prompt construction - the only place where the arms differ
# --------------------------------------------------------------------------


def render_advisory_block(selection: StrategyTransferSelection) -> str:
    """Wrap the public advisory exactly as production advise mode wraps it."""
    return ADVISORY_WRAPPER.format(advisory=render_strategy_advisory(selection))


def placebo_selection(
    *, task_id: str, target_family: str, desired: Sequence[str]
) -> StrategyTransferSelection:
    """An advice-free selection: the same wrapper, the inert no-match line.

    Dispatching this separates a *presence* effect - the model behaving
    differently because any advisory block is present - from the *content*
    effect the benchmark is meant to measure.
    """
    return StrategyTransferSelection(
        task_id=task_id,
        target_family=target_family,
        desired_strategies=tuple(desired),
        advice=(),
        rejected=(),
    )


def forced_harmful_selection(
    *,
    task_id: str,
    target_family: str,
    desired: Sequence[str],
    sources: Sequence[Mapping[str, Any]],
) -> StrategyTransferSelection:
    """Build the advisory the selector refused, to test that it would not have helped.

    First the off-target strategies are narrowed to those the target actually
    wanted, which is the sharpest form of harmful advice.  When the refused
    sources share no strategy with the target's needs - the *irrelevant* case -
    the narrowing is retried with no desired filter so the candidates' own
    strategies are forced instead.  Otherwise the arm would render the inert
    no-match line and measure nothing.

    This deliberately bypasses the selector's eligibility rules using only
    public dataclasses; it never changes the selector itself.
    """
    for filter_desired in (tuple(desired), ()):
        advice: list[StrategyAdvice] = []
        for strategy in STRATEGY_VOCABULARY:
            if filter_desired and strategy not in filter_desired:
                continue
            supporting = [
                source
                for source in sources
                if strategy in tuple(source.get("strategies") or ())
            ]
            if not supporting:
                continue
            advice.append(
                StrategyAdvice(
                    strategy=strategy,
                    evidence_lesson_ids=tuple(str(item["id"]) for item in supporting[:3]),
                    source_families=tuple(
                        sorted({str(item["source_family"]) for item in supporting[:3]})
                    ),
                    confidence=0.75,
                )
            )
        if advice:
            return StrategyTransferSelection(
                task_id=task_id,
                target_family=target_family,
                desired_strategies=tuple(desired),
                advice=tuple(advice),
                rejected=(),
            )
    raise TransferLiftRunError(
        f"case {task_id}: the negative-transfer arm rendered no advice; its "
        "candidates declare no strategy at all, so nothing harmful can be forced"
    )


def build_user_message(case: Mapping[str, Any]) -> str:
    task = case["task"]
    lines = [task["instruction"].strip(), "", "Inputs:"]
    for key in sorted(task["inputs"]):
        lines.append(f"{key} = {canonical_json(task['inputs'][key])}")
    return "\n".join(lines)


def build_messages(case: Mapping[str, Any], transfer_block: str) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": SYSTEM_PROMPT_TEMPLATE.format(transfer_block=transfer_block),
        },
        {"role": "user", "content": build_user_message(case)},
    ]


def prompt_difference_report(
    control_messages: Sequence[Mapping[str, str]],
    variant_messages: Sequence[Mapping[str, str]],
    advisory_block: str,
) -> dict[str, Any]:
    """Prove the two prompts differ only by the advisory block.

    ``advisory_only`` is true only when the block is non-empty, the two prompts
    actually differ, and deleting the block from the variant reproduces the
    control prompt byte for byte with nothing else moved.
    """
    control_text = canonical_json(list(control_messages))
    variant_text = canonical_json(list(variant_messages))
    stripped = [dict(message) for message in variant_messages]
    removed = 0
    for message in stripped:
        if advisory_block and advisory_block in message.get("content", ""):
            message["content"] = message["content"].replace(advisory_block, "", 1)
            removed += 1
    advisory_only = (
        bool(advisory_block)
        and control_text != variant_text
        and canonical_json(stripped) == control_text
        and removed == 1
        and len(stripped) == len(control_messages)
    )
    return {
        "control_prompt_sha256": sha256_text(control_text),
        "variant_prompt_sha256": sha256_text(variant_text),
        "advisory_block_sha256": sha256_text(advisory_block) if advisory_block else None,
        "advisory_block_chars": len(advisory_block),
        "blocks_removed": removed,
        "advisory_only": advisory_only,
    }


def oracle_leak_report(
    case: Mapping[str, Any],
    transfer_block: str,
    lesson_ids: Sequence[str],
) -> dict[str, Any]:
    """No advisory may hand the model the answer its oracle is about to check."""
    haystack = transfer_block.casefold()
    tokens = expected_answer_tokens(case["verifier"])
    leaked = [
        token
        for token in tokens
        if len(token) >= _ORACLE_LEAK_MIN_TOKEN and token.casefold() in haystack
    ]
    # Lesson identifiers are compared exactly at any length, so a one- or
    # two-character id that is also the expected answer cannot slip past the
    # short-token floor.
    lowered = {token.casefold() for token in tokens}
    leaked_ids = [item for item in lesson_ids if str(item).casefold() in lowered]
    return {
        "expected_token_count": len(tokens),
        "leaked_tokens": sorted(set(leaked)),
        "leaked_lesson_ids": sorted(set(leaked_ids)),
        "clean": not leaked and not leaked_ids,
    }


# --------------------------------------------------------------------------
# Deterministic verifiers - no model judge anywhere
# --------------------------------------------------------------------------


def parse_reply(raw: str) -> dict[str, Any]:
    """Parse one structured reply; an unparsable reply is a failed case, not a crash."""
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {"parsed": False, "receipts": [], "citations": [], "final": None}
    if not isinstance(parsed, dict):
        return {"parsed": False, "receipts": [], "citations": [], "final": None}
    receipts = parsed.get("receipts")
    citations = parsed.get("citations")
    return {
        "parsed": True,
        "receipts": [str(item) for item in receipts] if isinstance(receipts, list) else [],
        "citations": [str(item) for item in citations] if isinstance(citations, list) else [],
        "final": parsed.get("final"),
    }


def verify_reply(verifier: Mapping[str, Any], reply: Mapping[str, Any]) -> bool:
    """Decide one case deterministically. Pure function of the verifier and reply."""
    if not reply.get("parsed"):
        return False
    kind = verifier["kind"]
    expected = verifier["expected"]
    if kind == "exact_fields":
        return canonical_json(reply.get("final")) == canonical_json(expected)
    if kind == "ordered_sequence":
        final = reply.get("final")
        if not isinstance(final, Mapping) or len(final) != 1:
            return False
        observed = next(iter(final.values()))
        if isinstance(observed, (str, bytes)) or not isinstance(observed, Sequence):
            return False
        return canonical_json(list(observed)) == canonical_json(list(expected))
    if kind == "citation_set":
        return sorted(set(reply.get("citations") or ())) == sorted(set(map(str, expected)))
    raise TransferLiftRunError(f"unsupported verifier kind {kind}")


def citation_count_consistent(reply: Mapping[str, Any]) -> bool | None:
    """Non-gating diagnostic: does a self-reported count match the citations?

    The ``citation_set`` oracle deliberately does not read ``final``; every
    holdout case expects exactly two citations, so scoring a self-reported count
    would only add a guessable failure mode.  This records whether the model was
    internally consistent, and nothing depends on it.
    """
    final = reply.get("final")
    if not isinstance(final, Mapping) or "citation_count" not in final:
        return None
    value = final["citation_count"]
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    return value == len(reply.get("citations") or ())


def output_leakage(case: Mapping[str, Any], raw: str) -> int:
    """Count forbidden tokens the model echoed back. Case-insensitive, bounded."""
    haystack = (raw or "").casefold()
    return sum(1 for token in case["forbidden_tokens"] if str(token).casefold() in haystack)


# --------------------------------------------------------------------------
# Case planning
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ArmPlan:
    arm: str
    messages: tuple[dict[str, str], ...]
    transfer_block: str
    advice_count: int = 0
    prompt_diff: dict[str, Any] = field(default_factory=dict)
    oracle_leak: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CasePlan:
    case_id: str
    category: str
    target_family: str
    verifier_kind: str
    desired_strategies: tuple[str, ...]
    selection_payload: dict[str, Any]
    advice_count: int
    evidence_count: int
    rejected_reasons: tuple[str, ...]
    arms: tuple[ArmPlan, ...]

    def arm(self, name: str) -> ArmPlan:
        for item in self.arms:
            if item.arm == name:
                return item
        raise TransferLiftRunError(f"case {self.case_id} has no {name} arm")

    @property
    def variant_arm(self) -> str:
        return "harmful" if self.category == "negative_transfer" else "treatment"


def _lesson_ids(selection: StrategyTransferSelection) -> tuple[str, ...]:
    return tuple(
        lesson_id
        for item in selection.advice
        for lesson_id in item.evidence_lesson_ids
    )


def plan_case(fixture: Mapping[str, Any], case: Mapping[str, Any]) -> CasePlan:
    """Resolve the selector once and build every arm's prompt for one case."""
    by_id = {item["id"]: item for item in fixture["sources"]}
    facts = case["runtime_facts"]
    try:
        target = strategy_target_from_runtime(
            task_id=case["id"],
            family=case["target_family"],
            changes_existing_state=(
                facts["requested_effect"] == "write" and bool(facts["target_exists"])
            ),
            resumable=bool(facts["resumable"]) and facts["planned_stage_count"] > 1,
            verification=facts["verification"],
            current_external_facts=facts["evidence_source"] == "public_web",
        )
        candidates = [by_id[item] for item in case["candidate_ids"]]
        selection = select_strategy_transfer(target, candidates, as_of=fixture["as_of"])
        reversed_selection = select_strategy_transfer(
            target, list(reversed(candidates)), as_of=fixture["as_of"]
        )
    except StrategyTransferError as exc:
        # A contract-valid fixture can never reach here; surface it as a fixture
        # defect rather than a traceback.
        raise TransferLiftFixtureError(
            f"case {case['id']} violates the strategy-transfer contract: {exc}"
        ) from None
    if selection.to_payload() != reversed_selection.to_payload():
        raise TransferLiftRunError(
            f"case {case['id']}: candidate order changed the selection"
        )
    payload = selection.to_payload()
    control = build_messages(case, "")
    arms = [ArmPlan(arm="control", messages=tuple(control), transfer_block="")]

    def add(arm: str, chosen: StrategyTransferSelection) -> None:
        block = render_advisory_block(chosen)
        messages = build_messages(case, block)
        arms.append(
            ArmPlan(
                arm=arm,
                messages=tuple(messages),
                transfer_block=block,
                advice_count=len(chosen.advice),
                prompt_diff=prompt_difference_report(control, messages, block),
                oracle_leak=oracle_leak_report(case, block, _lesson_ids(chosen)),
            )
        )

    wanted = ARMS_BY_CATEGORY[case["category"]]
    if "placebo" in wanted:
        add(
            "placebo",
            placebo_selection(
                task_id=case["id"],
                target_family=case["target_family"],
                desired=selection.desired_strategies,
            ),
        )
    if "harmful" in wanted:
        add(
            "harmful",
            forced_harmful_selection(
                task_id=case["id"],
                target_family=case["target_family"],
                desired=selection.desired_strategies,
                sources=candidates,
            ),
        )
    if "treatment" in wanted:
        add("treatment", selection)

    ordered = {item.arm: item for item in arms}
    return CasePlan(
        case_id=case["id"],
        category=case["category"],
        target_family=case["target_family"],
        verifier_kind=case["verifier"]["kind"],
        desired_strategies=selection.desired_strategies,
        selection_payload=payload,
        advice_count=len(payload["advice"]),
        evidence_count=len(selection.evidence_lesson_ids),
        rejected_reasons=tuple(sorted({item["reason"] for item in payload["rejected"]})),
        arms=tuple(ordered[name] for name in wanted),
    )


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------

# A dispatcher receives (messages, seed) and returns the raw reply text.
Dispatcher = Callable[[Sequence[Mapping[str, str]], int], str]


def ollama_dispatcher(
    *,
    base_url: str = "http://127.0.0.1:11434",
    model: str = LIVE_MODEL,
    temperature: float = LIVE_TEMPERATURE,
    context_length: int = LIVE_CONTEXT_LENGTH,
    max_output_tokens: int = LIVE_MAX_OUTPUT_TOKENS,
    keep_alive: str = LIVE_KEEP_ALIVE,
    generation_timeout: float = 300.0,
) -> Dispatcher:
    """Build the live dispatcher. Every call is a fresh two-message conversation."""
    if model != LIVE_MODEL:
        raise TransferLiftRunError(
            f"Phase 2 pins the live model to {LIVE_MODEL}; refusing {model}"
        )
    from .ollama_client import OllamaClient

    client = OllamaClient(
        base_url,
        model=model,
        generation_timeout=generation_timeout,
        max_output_tokens=max_output_tokens,
        keep_alive=keep_alive,
    )

    def dispatch(messages: Sequence[Mapping[str, str]], seed: int) -> str:
        response = client.chat(
            [dict(message) for message in messages],
            [],
            model=model,
            context_length=context_length,
            think=LIVE_THINK,
            temperature=temperature,
            response_format=dict(RESPONSE_SCHEMA),
            seed=seed,
            keep_alive=keep_alive,
        )
        content = response.get("content")
        return content if isinstance(content, str) else ""

    # Declared so the attestation records what was actually dispatched rather
    # than what the caller said it dispatched.
    dispatch.provider = LIVE_PROVIDER
    dispatch.model = model
    dispatch.temperature = float(temperature)
    dispatch.num_predict = int(max_output_tokens)
    dispatch.num_ctx = int(context_length)
    dispatch.keep_alive = str(keep_alive)
    dispatch.think = LIVE_THINK
    return dispatch


def run_case(
    fixture: Mapping[str, Any],
    case: Mapping[str, Any],
    dispatcher: Dispatcher,
    *,
    seed: int = LIVE_SEED,
    null_probe: bool = False,
) -> dict[str, Any]:
    """Dispatch every arm of one case and decide it with the deterministic verifier."""
    plan = plan_case(fixture, case)
    verifier = case["verifier"]
    row: dict[str, Any] = {
        "id": plan.case_id,
        "category": plan.category,
        "target_family": plan.target_family,
        "verifier_kind": plan.verifier_kind,
        "desired_strategies": list(plan.desired_strategies),
        "advice_count": plan.advice_count,
        "evidence_count": plan.evidence_count,
        "rejected_reasons": list(plan.rejected_reasons),
        "prompt_diff_ok": True,
        "oracle_leak_ok": True,
        "arms": {},
    }
    control_plan = plan.arm("control")
    for arm_plan in plan.arms:
        messages = arm_plan.messages
        if null_probe and arm_plan.arm != "control":
            # A/A sham: every labelled variant arm receives the control prompt.
            messages = control_plan.messages
        raw = dispatcher(messages, seed)
        reply = parse_reply(raw)
        row["arms"][arm_plan.arm] = {
            "passed": bool(verify_reply(verifier, reply)),
            "parsed": bool(reply["parsed"]),
            "receipt_count": len(reply["receipts"]),
            "citation_count": len(reply["citations"]),
            "citation_count_consistent": citation_count_consistent(reply),
            "reply_sha256": sha256_text(raw or ""),
            "output_leakage": output_leakage(case, raw or ""),
            "advice_count": arm_plan.advice_count,
            "prompt_diff": dict(arm_plan.prompt_diff),
            "oracle_leak": dict(arm_plan.oracle_leak),
        }
        if arm_plan.prompt_diff and not arm_plan.prompt_diff.get("advisory_only"):
            row["prompt_diff_ok"] = False
        if arm_plan.oracle_leak and not arm_plan.oracle_leak.get("clean"):
            row["oracle_leak_ok"] = False

    variant = plan.variant_arm
    row["variant_arm"] = variant
    row["control_passed"] = bool(row["arms"]["control"]["passed"])
    row["variant_passed"] = bool(row["arms"][variant]["passed"])
    row["placebo_passed"] = (
        bool(row["arms"]["placebo"]["passed"]) if "placebo" in row["arms"] else None
    )
    row["treatment_passed"] = row["variant_passed"] if variant == "treatment" else None
    row["harmful_passed"] = row["variant_passed"] if variant == "harmful" else None
    row["harmful_advice_count"] = (
        plan.arm("harmful").advice_count if variant == "harmful" else None
    )
    row["selection_leakage"] = (
        plan.advice_count + plan.evidence_count
        if plan.category == "safety_control"
        else 0
    )
    row["output_leakage"] = sum(arm["output_leakage"] for arm in row["arms"].values())
    row["null_probe"] = bool(null_probe)
    return row


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------


def _one_sided_sign_p(better: int, discordant: int) -> float:
    """P(at least `better` of `discordant` fair coin flips land positive).

    Exact and closed form over the 2**discordant sign atoms; there is no Monte
    Carlo and therefore no seed.  With no losses this collapses to 2**-better.
    """
    if discordant <= 0:
        return 1.0
    tail = sum(math.comb(discordant, k) for k in range(better, discordant + 1))
    return tail / (2**discordant)


def arm_label_null_test(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Exact sign test over the paired positive outcomes.

    Permuting which observed arm is called treatment must destroy the lift.  The
    permutation distribution of the paired statistic is exactly the distribution
    of a sum of independent signs over the discordant pairs, so it is computed in
    closed form rather than sampled: concordant pairs contribute zero under every
    relabelling and cannot carry evidence.
    """
    pairs = [
        (bool(row["control_passed"]), bool(row["variant_passed"]))
        for row in rows
        if row["category"] == "positive"
    ]
    if not pairs:
        raise TransferLiftRunError("the arm-label null test needs positive cases")
    better = sum(1 for control, treatment in pairs if treatment and not control)
    worse = sum(1 for control, treatment in pairs if control and not treatment)
    discordant = better + worse
    total = len(pairs)
    return {
        "method": "exact_sign_test",
        "randomized": False,
        "pairs": total,
        "discordant_pairs": discordant,
        "treatment_better": better,
        "treatment_worse": worse,
        "observed_lift_points": round(100.0 * (better - worse) / total, 3),
        "permutation_null_mean": 0.0,
        "max_lift_points": round(100.0 * discordant / total, 3),
        "min_lift_points": round(-100.0 * discordant / total, 3),
        # Not rounded: an exact tail can be as small as 2**-40, and rounding
        # would report a real result as zero.
        "sign_test_p_value": _one_sided_sign_p(better, discordant),
    }


def _format_p(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.4f}"


def _join_and(items: Sequence[str]) -> str:
    items = list(items)
    if not items:
        return "none"
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


def paired_sign_test(
    rows: Sequence[Mapping[str, Any]], arm_a: str, arm_b: str
) -> dict[str, Any]:
    """Exact one-sided sign test on the positive cases, arm_a against arm_b.

    Concordant pairs contribute nothing under any relabelling, so the null is
    exactly a sum of independent signs over the discordant pairs and the tail is
    computed in closed form. No sampling, no seed.
    """
    pairs = [
        row
        for row in rows
        if row["category"] == "positive" and arm_a in row["arms"] and arm_b in row["arms"]
    ]
    if not pairs:
        return {
            "arms": [arm_a, arm_b],
            "pairs": 0,
            "better": 0,
            "worse": 0,
            "discordant_pairs": 0,
            "lift_points": None,
            "p_value": 1.0,
        }
    better = [
        row["id"]
        for row in pairs
        if row["arms"][arm_a]["passed"] and not row["arms"][arm_b]["passed"]
    ]
    worse = [
        row["id"]
        for row in pairs
        if row["arms"][arm_b]["passed"] and not row["arms"][arm_a]["passed"]
    ]
    discordant = len(better) + len(worse)
    return {
        "arms": [arm_a, arm_b],
        "pairs": len(pairs),
        "better": len(better),
        "worse": len(worse),
        "better_ids": sorted(better),
        "worse_ids": sorted(worse),
        "discordant_pairs": discordant,
        "lift_points": round(100.0 * (len(better) - len(worse)) / len(pairs), 3),
        "p_value": _one_sided_sign_p(len(better), discordant),
    }


def outcome_decomposition(
    rows: Sequence[Mapping[str, Any]],
    *,
    completion_lift_points_min: float | None = None,
    design_power_at_observed_rates: float | None = None,
) -> dict[str, Any]:
    """Split the positives into what any arm could and could not reach.

    A case every arm already passes carries no signal; a case no arm passes
    carries none either. Only the remainder - control failed, some arm passed -
    can express lift at all, which bounds what the fixture could have shown.
    """
    positives = [row for row in rows if row["category"] == "positive"]
    if not positives:
        raise TransferLiftRunError("the outcome decomposition needs positive cases")
    arms_of = lambda row: [name for name in ARMS if name in row["arms"]]  # noqa: E731
    all_pass = [
        row["id"]
        for row in positives
        if all(row["arms"][name]["passed"] for name in arms_of(row))
    ]
    none_pass = [
        row["id"]
        for row in positives
        if not any(row["arms"][name]["passed"] for name in arms_of(row))
    ]
    winnable = [
        row["id"]
        for row in positives
        if not row["arms"]["control"]["passed"]
        and any(
            row["arms"][name]["passed"] for name in arms_of(row) if name != "control"
        )
    ]
    control_failures = [
        row["id"] for row in positives if not row["arms"]["control"]["passed"]
    ]
    ceiling = round(100.0 * len(winnable) / len(positives), 3)

    # Case-level attribution of the wins over control.
    wins = [
        row
        for row in positives
        if "treatment" in row["arms"]
        and row["arms"]["treatment"]["passed"]
        and not row["arms"]["control"]["passed"]
    ]
    shared: list[str] = []
    identical: list[str] = []
    content_only: list[str] = []
    for row in wins:
        placebo = row["arms"].get("placebo")
        if placebo is not None and placebo["passed"]:
            shared.append(row["id"])
            if placebo["reply_sha256"] == row["arms"]["treatment"]["reply_sha256"]:
                identical.append(row["id"])
        else:
            content_only.append(row["id"])

    unsolvable_by_verifier: dict[str, dict[str, int]] = {}
    for row in positives:
        bucket = unsolvable_by_verifier.setdefault(
            row["verifier_kind"], {"total": 0, "unsolvable_by_any_arm": 0}
        )
        bucket["total"] += 1
        if not any(row["arms"][name]["passed"] for name in arms_of(row)):
            bucket["unsolvable_by_any_arm"] += 1

    decomposition: dict[str, Any] = {
        "positives": len(positives),
        "cases_all_arms_pass": len(all_pass),
        "cases_no_arm_pass": len(none_pass),
        "cases_no_arm_pass_ids": sorted(none_pass),
        "control_failures": len(control_failures),
        "winnable_cases": len(winnable),
        "winnable_case_ids": sorted(winnable),
        "arithmetic_ceiling_points": ceiling,
        "completion_lift_points_min": completion_lift_points_min,
        "ceiling_below_threshold": (
            None
            if completion_lift_points_min is None
            else ceiling < completion_lift_points_min
        ),
        "wins_over_control": sorted(row["id"] for row in wins),
        "wins_shared_with_placebo": sorted(shared),
        "wins_shared_with_identical_replies": sorted(identical),
        "wins_content_attributable": sorted(content_only),
        "unsolvable_by_verifier": dict(sorted(unsolvable_by_verifier.items())),
        "design_power_at_observed_rates": design_power_at_observed_rates,
        "design_power_source": (
            None
            if design_power_at_observed_rates is None
            else "supplied by the WP-9 review: joint probability of clearing both "
            "the completion-lift threshold and the significance threshold at the "
            "observed per-case rates; not derived by this harness"
        ),
    }
    return decomposition


def interpretation_text(
    *,
    gate: Mapping[str, Any],
    content: Mapping[str, Any],
    presence: Mapping[str, Any],
    decomposition: Mapping[str, Any],
    alpha: float,
) -> str:
    """State plainly which contrast the lift is attributable to, and how firmly."""
    gate_lift = gate["lift_points"]
    content_lift = content["lift_points"]
    presence_lift = presence["lift_points"]
    if content_lift is None:
        return (
            "No placebo arm was dispatched, so a presence effect cannot be "
            "separated from a content effect."
        )
    if content_lift > gate_lift:
        comparison = "beat placebo by more than it beat control"
        verdict = (
            "so what lift there is is content-attributable rather than a presence "
            "artifact"
        )
    elif content_lift < gate_lift:
        comparison = "beat placebo by less than it beat control"
        verdict = "so part of the measured lift is a presence artifact, not content"
    else:
        comparison = "beat placebo by exactly as much as it beat control"
        verdict = "so placebo and control are interchangeable here"
    first = (
        f"Placebo moved the outcome by {presence_lift} points "
        f"(p = {_format_p(presence['p_value'])}), and treatment {comparison} "
        f"({content_lift:+.1f} vs {gate_lift:+.1f}), {verdict}."
    )

    gate_sig = gate["p_value"] <= alpha
    content_sig = content["p_value"] <= alpha
    both = f"(p = {_format_p(gate['p_value'])}, p = {_format_p(content['p_value'])})"
    if not gate_sig and not content_sig:
        second = f"Neither contrast is significant {both}"
    elif gate_sig and content_sig:
        second = f"Both contrasts are significant {both}"
    elif gate_sig:
        second = f"Only the contrast against control is significant {both}"
    else:
        second = f"Only the contrast against placebo is significant {both}"

    wins = decomposition["wins_over_control"]
    shared = decomposition["wins_shared_with_placebo"]
    identical = decomposition["wins_shared_with_identical_replies"]
    content_ids = decomposition["wins_content_attributable"]
    if not wins:
        third = "there were no wins over control"
    elif not shared:
        third = (
            f"and all {len(wins)} wins over control "
            f"({_join_and(wins)}) are content-attributable at case level"
        )
    else:
        qualifier = (
            " with byte-identical placebo and treatment replies"
            if len(identical) == len(shared)
            else f" ({len(identical)} with byte-identical placebo and treatment replies)"
        )
        tail = (
            f"only {_join_and(content_ids)} "
            f"{'is' if len(content_ids) == 1 else 'are'} content-attributable at "
            "case level"
            if content_ids
            else "none of them is content-attributable at case level"
        )
        third = (
            f"and {len(shared)} of the {len(wins)} wins over control "
            f"({', '.join(shared)}) were also placebo wins{qualifier}; {tail}"
        )
    return f"{first} {second}, {third}."


def _binomial_context(better: int, worse: int) -> dict[str, Any]:
    """Exact context for a gate whose sealed threshold is a hard zero."""
    discordant = better + worse
    return {
        "discordant_pairs": discordant,
        "harmful_better": better,
        "control_better": worse,
        "sign_test_p_value": _one_sided_sign_p(better, discordant),
        "gate_fires_at_excess": 1,
        "note": (
            "the sealed threshold is an exact zero, so one extra harmful pass "
            "fails the gate; the p-value states how surprising that would be"
        ),
    }


def score_transfer_lift_results(
    fixture: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Score recorded case rows. Re-runnable from a saved report without dispatch."""
    thresholds = fixture["thresholds"]
    positives = [row for row in rows if row["category"] == "positive"]
    negatives = [row for row in rows if row["category"] == "negative_transfer"]
    safety = [row for row in rows if row["category"] == "safety_control"]
    if not positives:
        raise TransferLiftRunError("no positive cases were scored")

    control_passes = sum(1 for row in positives if row["control_passed"])
    treatment_passes = sum(1 for row in positives if row["variant_passed"])
    placebo_rows = [row for row in positives if row.get("placebo_passed") is not None]
    placebo_passes = sum(1 for row in placebo_rows if row["placebo_passed"])
    lift = round(100.0 * (treatment_passes - control_passes) / len(positives), 3)
    lift_vs_placebo = (
        round(100.0 * (treatment_passes - placebo_passes) / len(placebo_rows), 3)
        if placebo_rows
        else None
    )
    presence_effect = (
        round(100.0 * (placebo_passes - control_passes) / len(placebo_rows), 3)
        if placebo_rows
        else None
    )
    # The content-attributable contrast. Not gated on this sealed run: the gate
    # is treatment minus control, as the fixture defines it. A v2 fixture may
    # add content_lift_points_min.
    content_lift = lift_vs_placebo
    presence_flagged = bool(presence_effect not in (None, 0.0))
    material = bool(
        presence_flagged
        and content_lift is not None
        and lift - content_lift >= MATERIAL_CONTRAST_GAP_POINTS
    )
    alpha = float(thresholds["shuffled_arm_max_p_value"])
    gate_test = paired_sign_test(rows, "treatment", "control")
    content_test = paired_sign_test(rows, "treatment", "placebo")
    presence_test = paired_sign_test(rows, "placebo", "control")
    decomposition = outcome_decomposition(
        rows,
        completion_lift_points_min=float(thresholds["completion_lift_points_min"]),
    )
    interpretation = interpretation_text(
        gate=gate_test,
        content=content_test,
        presence=presence_test,
        decomposition=decomposition,
        alpha=alpha,
    )

    advised = sum(1 for row in positives if row["advice_count"] > 0)
    advice_coverage = round(100.0 * advised / len(positives), 3)

    regressions = sum(
        1 for row in positives if row["control_passed"] and not row["variant_passed"]
    )
    per_family: dict[str, int] = {}
    for row in positives:
        if row["control_passed"] and not row["variant_passed"]:
            per_family[row["target_family"]] = per_family.get(row["target_family"], 0) + 1
    worst_family = max(per_family.values()) if per_family else 0

    rejected = sum(
        1 for row in negatives if row["advice_count"] == row["evidence_count"] == 0
    )
    rejection_percent = (
        round(100.0 * rejected / len(negatives), 3) if negatives else None
    )
    harmful_passes = sum(1 for row in negatives if row["variant_passed"])
    negative_control_passes = sum(1 for row in negatives if row["control_passed"])
    harmful_excess = max(0, harmful_passes - negative_control_passes)
    harmful_advice_counts = [
        int(row.get("harmful_advice_count") or 0) for row in negatives
    ]
    harmful_context = _binomial_context(
        sum(
            1
            for row in negatives
            if row["variant_passed"] and not row["control_passed"]
        ),
        sum(
            1
            for row in negatives
            if row["control_passed"] and not row["variant_passed"]
        ),
    )

    selection_leak = sum(row["selection_leakage"] for row in safety)
    output_leak = sum(row["output_leakage"] for row in rows)
    prompt_diff_failures = sum(1 for row in rows if not row["prompt_diff_ok"])
    oracle_leak_failures = sum(1 for row in rows if not row.get("oracle_leak_ok", True))

    parse_rates: dict[str, dict[str, Any]] = {}
    for arm in ARMS:
        seen = [row["arms"][arm] for row in rows if arm in row["arms"]]
        if not seen:
            continue
        parsed = sum(1 for item in seen if item["parsed"])
        parse_rates[arm] = {
            "parsed": parsed,
            "total": len(seen),
            "rate": round(parsed / len(seen), 4),
        }

    by_verifier: dict[str, dict[str, Any]] = {}
    for row in positives:
        bucket = by_verifier.setdefault(
            row["verifier_kind"],
            {"total": 0, "control_passes": 0, "treatment_passes": 0, "placebo_passes": 0},
        )
        bucket["total"] += 1
        bucket["control_passes"] += int(bool(row["control_passed"]))
        bucket["treatment_passes"] += int(bool(row["variant_passed"]))
        bucket["placebo_passes"] += int(bool(row.get("placebo_passed")))
    for bucket in by_verifier.values():
        bucket["control_rate"] = round(bucket["control_passes"] / bucket["total"], 4)
        bucket["treatment_rate"] = round(bucket["treatment_passes"] / bucket["total"], 4)
        bucket["lift_points"] = round(
            100.0 * (bucket["treatment_passes"] - bucket["control_passes"]) / bucket["total"],
            3,
        )

    consistency = {"true": 0, "false": 0, "absent": 0}
    for row in rows:
        for arm in row["arms"].values():
            flag = arm.get("citation_count_consistent")
            consistency["absent" if flag is None else "true" if flag else "false"] += 1

    null_test = arm_label_null_test(rows)

    passes = {
        "pairs": len(positives) >= thresholds["source_target_pairs_min"],
        "positive_advice_coverage": (
            advice_coverage >= thresholds["positive_advice_coverage_percent_min"]
        ),
        "lift": lift >= thresholds["completion_lift_points_min"],
        "zero_regressions": regressions <= thresholds["treatment_regressions_max"],
        "per_family_regressions": worst_family <= thresholds["per_family_regressions_max"],
        "negative_rejection": (
            True
            if rejection_percent is None
            else rejection_percent
            >= thresholds["negative_transfer_rejection_percent_min"]
        ),
        "harmful_arm_not_better": (
            harmful_excess <= thresholds["harmful_arm_max_excess_passes"]
        ),
        "harmful_arm_carries_advice": all(count > 0 for count in harmful_advice_counts),
        "selection_leakage": selection_leak <= thresholds["selection_leakage_max"],
        "output_leakage": output_leak <= thresholds["output_leakage_max"],
        "prompt_diff_advisory_only": prompt_diff_failures == 0,
        "no_oracle_leak": oracle_leak_failures == 0,
        "shuffled_arm_null": (
            abs(null_test["permutation_null_mean"])
            <= thresholds["shuffled_arm_max_abs_lift_points"]
            and null_test["sign_test_p_value"] <= thresholds["shuffled_arm_max_p_value"]
        ),
    }
    return {
        "source_target_pairs": len(positives),
        "control_passes": control_passes,
        "treatment_passes": treatment_passes,
        "placebo_passes": placebo_passes if placebo_rows else None,
        "placebo_total": len(placebo_rows),
        "target_total": len(positives),
        "completion_lift_count": treatment_passes - control_passes,
        "completion_lift_points": lift,
        "treatment_minus_control_points": lift,
        "treatment_minus_placebo_points": lift_vs_placebo,
        "content_lift_points": content_lift,
        "presence_effect_points": presence_effect,
        "presence_effect_flagged": presence_flagged,
        "presence_effect_material": material,
        "interpretation": interpretation,
        "outcome_decomposition": decomposition,
        "contrast_tests": {
            "treatment_vs_control": gate_test,
            "treatment_vs_placebo": content_test,
            "placebo_vs_control": presence_test,
        },
        "positive_advice_coverage_percent": advice_coverage,
        "control_completion_rate": round(control_passes / len(positives), 4),
        "treatment_completion_rate": round(treatment_passes / len(positives), 4),
        "treatment_regressions": regressions,
        "per_family_regressions": dict(sorted(per_family.items())),
        "worst_family_regressions": worst_family,
        "negative_total": len(negatives),
        "negative_transfer_rejections": rejected,
        "negative_rejection_percent": rejection_percent,
        "harmful_arm_passes": harmful_passes,
        "negative_control_passes": negative_control_passes,
        "harmful_arm_excess_passes": harmful_excess,
        "harmful_arm_advice_count": sum(harmful_advice_counts),
        "harmful_arm_min_advice_count": min(harmful_advice_counts, default=0),
        "harmful_arm_context": harmful_context,
        "safety_control_total": len(safety),
        "selection_leakage": selection_leak,
        "output_leakage": output_leak,
        "prompt_diff_failures": prompt_diff_failures,
        "oracle_leak_failures": oracle_leak_failures,
        "parse_rates": parse_rates,
        "pass_rate_by_verifier": dict(sorted(by_verifier.items())),
        "citation_count_consistency": consistency,
        "arm_label_null_test": null_test,
        "passes": passes,
        "all_exit_criteria_passed": all(passes.values()),
    }


_SEALED_FIELDS = (
    "schema_version",
    "benchmark",
    "benchmark_version",
    "fixture_name",
    "fixture_sha256",
    "fixture_sealed",
    "evaluator_module",
    "evaluator_version",
    "evaluator_sha256",
    "config_sha256",
    "provider",
    "model",
    "temperature",
    "seed",
    "num_predict",
    "num_ctx",
    "keep_alive",
    "think",
    "null_probe",
    "source_target_pairs",
    "control_passes",
    "treatment_passes",
    "placebo_passes",
    "completion_lift_points",
    "treatment_minus_placebo_points",
    "presence_effect_points",
    "positive_advice_coverage_percent",
    "treatment_regressions",
    "worst_family_regressions",
    "negative_transfer_rejections",
    "negative_total",
    "harmful_arm_excess_passes",
    "harmful_arm_advice_count",
    "selection_leakage",
    "output_leakage",
    "prompt_diff_failures",
    "oracle_leak_failures",
    "arm_label_null_test",
    "passes",
    "all_exit_criteria_passed",
    "claim_scope",
)


def _dispatcher_setting(dispatcher: Dispatcher, name: str, fallback: Any) -> Any:
    declared = getattr(dispatcher, name, None)
    return fallback if declared is None else declared


def run_transfer_lift_fixture(
    path: Path,
    dispatcher: Dispatcher,
    *,
    expected_sha256: str | None = None,
    allow_unsealed: bool = False,
    seed: int = LIVE_SEED,
    model: str = LIVE_MODEL,
    provider: str = LIVE_PROVIDER,
    temperature: float = LIVE_TEMPERATURE,
    null_probe: bool = False,
) -> dict[str, Any]:
    """Run every case, score the run, and stamp an attestation over sealed fields."""
    path = Path(path)
    # A dispatcher that declares its own settings is authoritative: the
    # attestation must record what was dispatched, not what the caller claimed.
    for attribute, claimed in (
        ("provider", provider),
        ("model", model),
        ("temperature", temperature),
    ):
        declared = getattr(dispatcher, attribute, None)
        if declared is not None and declared != claimed:
            raise TransferLiftRunError(
                f"dispatcher {attribute} is {declared!r} but the run declares {claimed!r}"
            )
    fixture = load_transfer_lift_fixture(
        path, expected_sha256=expected_sha256, allow_unsealed=allow_unsealed
    )
    rows = [
        run_case(fixture, case, dispatcher, seed=seed, null_probe=null_probe)
        for case in sorted(fixture["cases"], key=lambda item: item["id"])
    ]
    report = score_transfer_lift_results(fixture, rows)
    sealed = fixture_seal_for(path, expected_sha256)
    if not sealed:
        claim_scope = "unsealed_development_run_not_evidence"
    elif null_probe:
        claim_scope = "null_arm_probe_not_a_lift_measurement"
    else:
        claim_scope = "held_out_model_in_the_loop_benchmark_not_production_ab_activation"
    report.update(
        schema_version="transfer_lift_attestation/v1",
        benchmark=BENCHMARK_NAME,
        benchmark_version=EVALUATOR_VERSION,
        fixture_name=path.name,
        fixture_sha256=fixture_sha256(path),
        fixture_sealed=bool(sealed),
        evaluator_module="jarvis.transfer_lift_eval",
        evaluator_version=EVALUATOR_VERSION,
        evaluator_sha256=evaluator_sha256(),
        config_sha256=sha256_json(fixture["thresholds"]),
        provider=provider,
        model=model,
        temperature=temperature,
        seed=seed,
        num_predict=_dispatcher_setting(dispatcher, "num_predict", LIVE_MAX_OUTPUT_TOKENS),
        num_ctx=_dispatcher_setting(dispatcher, "num_ctx", LIVE_CONTEXT_LENGTH),
        keep_alive=_dispatcher_setting(dispatcher, "keep_alive", LIVE_KEEP_ALIVE),
        think=_dispatcher_setting(dispatcher, "think", LIVE_THINK),
        platform=evidence_platform(),
        null_probe=bool(null_probe),
        claim_scope=claim_scope,
        cases=rows,
    )
    report["attestation_sha256"] = sha256_json(
        {key: report[key] for key in _SEALED_FIELDS}
    )
    return report


def rescore_report(report: Mapping[str, Any], fixture: Mapping[str, Any]) -> dict[str, Any]:
    """Recompute a saved report's score and attestation from its own case rows."""
    if not isinstance(report, Mapping) or "cases" not in report:
        raise TransferLiftRunError(
            "this file carries no case rows, so it cannot be re-scored; pass the "
            "run report written by --report, not the evidence document"
        )
    rescored = score_transfer_lift_results(fixture, report["cases"])
    rescored.update(
        {
            key: report[key]
            for key in _SEALED_FIELDS
            if key not in rescored and key in report
        }
    )
    missing = [key for key in _SEALED_FIELDS if key not in rescored]
    if missing:
        raise TransferLiftRunError(
            "saved report is missing sealed fields: " + ", ".join(sorted(missing))
        )
    rescored["attestation_sha256"] = sha256_json(
        {key: rescored[key] for key in _SEALED_FIELDS}
    )
    return rescored


def derived_report_fields(
    report: Mapping[str, Any],
    *,
    completion_lift_points_min: float,
    alpha: float,
    design_power_at_observed_rates: float | None = None,
) -> dict[str, Any]:
    """Recompute the non-sealed derived fields from a report's own case rows.

    None of these enter the attestation, so a report saved by an earlier
    evaluator can be re-decorated without disturbing its sealed numbers.
    """
    rows = report["cases"]
    gate_test = paired_sign_test(rows, "treatment", "control")
    content_test = paired_sign_test(rows, "treatment", "placebo")
    presence_test = paired_sign_test(rows, "placebo", "control")
    decomposition = outcome_decomposition(
        rows,
        completion_lift_points_min=completion_lift_points_min,
        design_power_at_observed_rates=design_power_at_observed_rates,
    )
    return {
        "outcome_decomposition": decomposition,
        "contrast_tests": {
            "treatment_vs_control": gate_test,
            "treatment_vs_placebo": content_test,
            "placebo_vs_control": presence_test,
        },
        "interpretation": interpretation_text(
            gate=gate_test,
            content=content_test,
            presence=presence_test,
            decomposition=decomposition,
            alpha=alpha,
        ),
    }


def underpowered_limitation(report: Mapping[str, Any]) -> str | None:
    """State the arithmetic ceiling when the fixture could not express its gate."""
    decomposition = report.get("outcome_decomposition") or {}
    threshold = decomposition.get("completion_lift_points_min")
    ceiling = decomposition.get("arithmetic_ceiling_points")
    if threshold is None or ceiling is None or ceiling >= threshold:
        return None
    gate_test = (report.get("contrast_tests") or {}).get("treatment_vs_control") or {}
    return (
        "This run is underpowered for its own gate: with "
        f"{gate_test.get('discordant_pairs')} discordant pairs the exact sign test "
        f"gives p = {_format_p(gate_test.get('p_value'))}, and only "
        f"{decomposition['winnable_cases']} of the "
        f"{decomposition['control_failures']} cases the control arm failed were "
        "solved by any arm, so the maximum lift this fixture could express was "
        f"{ceiling} pp against a {threshold} pp threshold. The result is "
        "inconclusive, not evidence that transfer does not help."
    )


def regeneration_limitation(report: Mapping[str, Any]) -> str | None:
    """Disclose a regeneration performed by a later evaluator than the run.

    The sealed numbers and the attestation always come from the evaluator that
    dispatched the run. When --regenerate-evidence rebuilds the descriptive
    fields under a newer module, that split is stated in the artifact rather
    than left for a reader to infer from two hashes.
    """
    ran_under = report.get("evaluator_sha256")
    now = evaluator_sha256()
    if not ran_under or ran_under == now:
        return None
    return (
        "Descriptive fields (interpretation, outcome_decomposition, "
        "contrast_tests and these limitations) were regenerated by evaluator "
        f"{now}; every sealed number and the attestation come from the run under "
        f"evaluator {ran_under}."
    )


def evidence_document(
    report: Mapping[str, Any],
    *,
    test_command: str,
    rescore_verified: bool | None = None,
) -> dict[str, Any]:
    """Shape one recorded run into the repository's evidence contract.

    Carries no prompt, reply, log, path, hostname or account data: the platform
    field is the OS name and Python version only.
    """
    return {
        "schema_version": "phase2_evidence/v1",
        "benchmark": BENCHMARK_NAME,
        "benchmark_version": report["benchmark_version"],
        "configuration_class": (
            "local_model_only; cloud disabled; external access disabled; "
            "execution disabled; computer access disabled"
        ),
        "test_command": test_command,
        "platform": report["platform"],
        "provider": report["provider"],
        "model": report["model"],
        "temperature": report["temperature"],
        "seed": report["seed"],
        "num_predict": report["num_predict"],
        "num_ctx": report["num_ctx"],
        "keep_alive": report["keep_alive"],
        "think": report["think"],
        "null_probe": report["null_probe"],
        "fixture_name": report["fixture_name"],
        "fixture_sha256": report["fixture_sha256"],
        "fixture_sealed": report["fixture_sealed"],
        "evaluator_module": report["evaluator_module"],
        "evaluator_sha256": report["evaluator_sha256"],
        "regenerated_by_evaluator_sha256": evaluator_sha256(),
        "config_sha256": report["config_sha256"],
        "attestation_sha256": report["attestation_sha256"],
        "rescore_verified": rescore_verified,
        "result": {
            key: report[key]
            for key in (
                "source_target_pairs",
                "control_passes",
                "treatment_passes",
                "placebo_passes",
                "completion_lift_points",
                "treatment_minus_control_points",
                "treatment_minus_placebo_points",
                "content_lift_points",
                "presence_effect_points",
                "presence_effect_flagged",
                "presence_effect_material",
                "interpretation",
                "outcome_decomposition",
                "contrast_tests",
                "positive_advice_coverage_percent",
                "control_completion_rate",
                "treatment_completion_rate",
                "treatment_regressions",
                "per_family_regressions",
                "worst_family_regressions",
                "negative_total",
                "negative_transfer_rejections",
                "negative_rejection_percent",
                "harmful_arm_excess_passes",
                "harmful_arm_advice_count",
                "harmful_arm_min_advice_count",
                "harmful_arm_context",
                "selection_leakage",
                "output_leakage",
                "prompt_diff_failures",
                "oracle_leak_failures",
                "parse_rates",
                "pass_rate_by_verifier",
                "citation_count_consistency",
                "arm_label_null_test",
            )
        },
        "passed": bool(report["all_exit_criteria_passed"]),
        "passes": dict(report["passes"]),
        "claim_scope": report["claim_scope"],
        "known_limitations": [
            "Held-out benchmark lift with a model in the loop; not production "
            "causal activation evidence.",
            "advise mode remains gated on a promoted operator trial that persists "
            "randomized assignment before outcomes exist; this run does not "
            "activate it.",
            "The gate is treatment minus control, as the sealed fixture defines "
            "it. treatment minus placebo is reported beside it; a divergence "
            "between the two is a presence effect, not transfer.",
            "One local model at temperature 0 on one host; a different model, "
            "quantisation or Ollama build can move the numbers.",
            "Verifiers are exact-match oracles over structured replies, so a "
            "correct answer in an unexpected shape scores as a failure.",
            "The harmful arm forces advice the selector refused; with a small "
            "negative set the zero-excess gate can fire on one discordant case, "
            "so read harmful_arm_context beside it.",
            *([underpowered_limitation(report)] if underpowered_limitation(report) else []),
            *([regeneration_limitation(report)] if regeneration_limitation(report) else []),
        ],
    }


__all__ = [
    "ADVISORY_WRAPPER",
    "ARMS",
    "ARMS_BY_CATEGORY",
    "BENCHMARK_NAME",
    "CASE_CATEGORIES",
    "EVALUATOR_VERSION",
    "FROZEN_TRANSFER_LIFT_HOLDOUT_V1_SHA256",
    "HOLDOUT_NAME",
    "LIVE_CONTEXT_LENGTH",
    "LIVE_KEEP_ALIVE",
    "LIVE_MAX_OUTPUT_TOKENS",
    "LIVE_MODEL",
    "LIVE_SEED",
    "LIVE_THINK",
    "REQUIRED_THRESHOLD_FLOORS",
    "RESPONSE_SCHEMA",
    "SYSTEM_PROMPT_TEMPLATE",
    "VERIFIER_KINDS",
    "ArmPlan",
    "CasePlan",
    "TransferLiftFixtureError",
    "TransferLiftRunError",
    "arm_label_null_test",
    "build_messages",
    "derived_report_fields",
    "interpretation_text",
    "outcome_decomposition",
    "paired_sign_test",
    "regeneration_limitation",
    "underpowered_limitation",
    "build_user_message",
    "canonical_json",
    "citation_count_consistent",
    "evaluator_sha256",
    "evidence_document",
    "evidence_platform",
    "expected_answer_tokens",
    "fixture_seal_for",
    "fixture_sha256",
    "forced_harmful_selection",
    "load_transfer_lift_fixture",
    "ollama_dispatcher",
    "oracle_leak_report",
    "output_leakage",
    "parse_reply",
    "placebo_selection",
    "plan_case",
    "prompt_difference_report",
    "render_advisory_block",
    "rescore_report",
    "run_case",
    "run_transfer_lift_fixture",
    "score_transfer_lift_results",
    "sha256_json",
    "sha256_text",
    "validate_transfer_lift_fixture",
    "verify_reply",
]
