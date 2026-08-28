from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

from .strategy_transfer import (
    STRATEGY_SET,
    STRATEGY_VOCABULARY,
    StrategyTransferError,
    desired_strategies_for_target,
    select_strategy_transfer,
)


FROZEN_STRATEGY_TRANSFER_FIXTURE_V1_NAME = "strategy_transfer_generalization_v1.json"
FROZEN_STRATEGY_TRANSFER_FIXTURE_V1_SHA256 = (
    "3146992cfdf931f02b2d168b05fbd6515a43347cb082586353f3b7a9d9e8fbd2"
)

_CATEGORIES = frozenset({
    "positive",
    "negative_no_hit",
    "stale_contradictory",
    "provenance_invalid",
    "authority_safety",
})


class StrategyTransferFixtureError(ValueError):
    """A frozen strategy-transfer fixture or result set is malformed."""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def strategy_transfer_fixture_sha256(fixture: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(fixture).encode("utf-8")).hexdigest()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise StrategyTransferFixtureError(f"Duplicate JSON field: {key}")
        result[key] = value
    return result


def _exact_fields(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    observed = set(value)
    missing = expected - observed
    unknown = observed - expected
    if missing or unknown:
        detail: list[str] = []
        if missing:
            detail.append("missing " + ", ".join(sorted(missing)))
        if unknown:
            detail.append("unknown " + ", ".join(sorted(unknown)))
        raise StrategyTransferFixtureError(
            f"{label} fields are invalid ({'; '.join(detail)})"
        )


def _validate_fixture(fixture: Mapping[str, Any]) -> None:
    required_fields = {
        "schema_version",
        "name",
        "description",
        "as_of",
        "strategy_vocabulary",
        "exit_criteria",
        "corpus",
        "cases",
        "no_transfer_baseline",
    }
    observed_fields = set(fixture)
    missing_fields = required_fields - observed_fields
    unknown_fields = observed_fields - required_fields - {"fixture_sha256"}
    if missing_fields or unknown_fields:
        detail: list[str] = []
        if missing_fields:
            detail.append("missing " + ", ".join(sorted(missing_fields)))
        if unknown_fields:
            detail.append("unknown " + ", ".join(sorted(unknown_fields)))
        raise StrategyTransferFixtureError(
            "strategy-transfer fixture fields are invalid ("
            + "; ".join(detail)
            + ")"
        )
    if fixture.get("schema_version") != 1:
        raise StrategyTransferFixtureError("fixture schema_version must be 1")
    if not str(fixture.get("name") or "").strip():
        raise StrategyTransferFixtureError("fixture name must not be empty")
    if not str(fixture.get("description") or "").strip():
        raise StrategyTransferFixtureError("fixture description must not be empty")
    if list(fixture.get("strategy_vocabulary") or []) != list(STRATEGY_VOCABULARY):
        raise StrategyTransferFixtureError("fixture strategy vocabulary has changed")
    as_of = fixture.get("as_of")
    if not isinstance(as_of, str):
        raise StrategyTransferFixtureError("fixture as_of must be a UTC timestamp")

    criteria = fixture.get("exit_criteria")
    if not isinstance(criteria, Mapping):
        raise StrategyTransferFixtureError("exit_criteria must be an object")
    _exact_fields(
        criteria,
        {
            "positive_strategy_recall_min",
            "strategy_precision_min",
            "exact_case_accuracy_min",
            "no_hit_accuracy_min",
            "cross_family_evidence_rate_min",
            "safety_leakage_max",
            "positive_recall_gain_min",
        },
        "exit criteria",
    )
    for key, value in criteria.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise StrategyTransferFixtureError(f"{key} must be numeric")
        number = float(value)
        if not math.isfinite(number) or not 0.0 <= number <= 1.0:
            raise StrategyTransferFixtureError(f"{key} must be between zero and one")

    corpus = fixture.get("corpus")
    cases = fixture.get("cases")
    if not isinstance(corpus, list) or not 10 <= len(corpus) <= 128:
        raise StrategyTransferFixtureError("corpus must contain 10 to 128 records")
    if not isinstance(cases, list) or not 12 <= len(cases) <= 128:
        raise StrategyTransferFixtureError("cases must contain 12 to 128 records")
    corpus_by_id: dict[str, Mapping[str, Any]] = {}
    validation_target = {
        "task_id": "fixture-validation",
        "family": "fixture_validation",
        "signals": {signal: False for signal in (
            "changes_existing_state",
            "long_running_or_resumable",
            "has_verifiable_output",
            "depends_on_current_external_facts",
        )},
    }
    for record in corpus:
        if not isinstance(record, Mapping):
            raise StrategyTransferFixtureError("every corpus record must be an object")
        record_id = str(record.get("id") or "").strip()
        if not record_id or record_id in corpus_by_id:
            raise StrategyTransferFixtureError("corpus IDs must be non-empty and unique")
        try:
            select_strategy_transfer(
                validation_target,
                [record],
                as_of=as_of,
            )
        except StrategyTransferError as exc:
            raise StrategyTransferFixtureError(
                f"corpus record {record_id} is structurally invalid: {exc}"
            ) from exc
        corpus_by_id[record_id] = record

    category_counts: Counter[str] = Counter()
    case_ids: set[str] = set()
    for case in cases:
        if not isinstance(case, Mapping):
            raise StrategyTransferFixtureError("every case must be an object")
        _exact_fields(
            case,
            {
                "id",
                "category",
                "target",
                "candidate_ids",
                "expected_strategies",
                "forbidden",
            },
            "strategy-transfer case",
        )
        case_id = str(case.get("id") or "").strip()
        category = str(case.get("category") or "").strip()
        if not case_id or case_id in case_ids:
            raise StrategyTransferFixtureError("case IDs must be non-empty and unique")
        if category not in _CATEGORIES:
            raise StrategyTransferFixtureError(f"unsupported case category: {category}")
        target = case.get("target")
        try:
            desired = set(desired_strategies_for_target(target))
        except StrategyTransferError as exc:
            raise StrategyTransferFixtureError(
                f"case {case_id} has an invalid target: {exc}"
            ) from exc
        candidate_ids = case.get("candidate_ids")
        expected = case.get("expected_strategies")
        forbidden = case.get("forbidden")
        if (
            isinstance(candidate_ids, (str, bytes))
            or not isinstance(candidate_ids, Sequence)
            or isinstance(expected, (str, bytes))
            or not isinstance(expected, Sequence)
            or not isinstance(forbidden, list)
        ):
            raise StrategyTransferFixtureError(
                "case candidate_ids, expected_strategies, and forbidden must be arrays"
            )
        candidate_names = [str(item) for item in candidate_ids]
        expected_names = [str(item) for item in expected]
        if len(candidate_names) != len(set(candidate_names)):
            raise StrategyTransferFixtureError("case candidate IDs must be unique")
        if len(expected_names) != len(set(expected_names)):
            raise StrategyTransferFixtureError("expected strategies must be unique")
        if not set(candidate_names).issubset(corpus_by_id):
            raise StrategyTransferFixtureError("case refers to an absent corpus record")
        if not set(expected_names).issubset(STRATEGY_SET):
            raise StrategyTransferFixtureError("case expects an unknown strategy")
        if not set(expected_names).issubset(desired):
            raise StrategyTransferFixtureError(
                "case expects a strategy not implied by its structured signals"
            )
        if category == "positive" and not expected_names:
            raise StrategyTransferFixtureError("positive cases require expected strategies")
        if category != "positive" and expected_names:
            raise StrategyTransferFixtureError("control cases must expect no transfer")
        forbidden_ids: set[str] = set()
        for item in forbidden:
            if not isinstance(item, Mapping):
                raise StrategyTransferFixtureError("forbidden labels must be objects")
            _exact_fields(item, {"id", "reason"}, "forbidden label")
            forbidden_id = str(item.get("id") or "").strip()
            reason = str(item.get("reason") or "").strip()
            if (
                not forbidden_id
                or forbidden_id not in candidate_names
                or forbidden_id in forbidden_ids
                or not reason
            ):
                raise StrategyTransferFixtureError(
                    "forbidden labels require unique candidate IDs and reasons"
                )
            forbidden_ids.add(forbidden_id)
        case_ids.add(case_id)
        category_counts[category] += 1

    if category_counts["positive"] < 8:
        raise StrategyTransferFixtureError("fixture requires at least eight positives")
    for category in _CATEGORIES - {"positive"}:
        if category_counts[category] < 2:
            raise StrategyTransferFixtureError(
                f"fixture requires at least two {category} controls"
            )
    baseline = fixture.get("no_transfer_baseline")
    if baseline != {
        "name": "no_transfer",
        "behavior": "return_no_strategy_advice",
    }:
        raise StrategyTransferFixtureError("no-transfer baseline definition has changed")


def load_strategy_transfer_fixture(path: Path) -> dict[str, Any]:
    """Load, validate, and checksum-authenticate the frozen gold set."""
    try:
        parsed = json.loads(
            Path(path).read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
        )
    except StrategyTransferFixtureError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StrategyTransferFixtureError(
            f"could not load strategy-transfer fixture: {exc}"
        ) from exc
    if not isinstance(parsed, dict):
        raise StrategyTransferFixtureError("fixture must be an object")
    _validate_fixture(parsed)
    digest = strategy_transfer_fixture_sha256(parsed)
    if (
        Path(path).name == FROZEN_STRATEGY_TRANSFER_FIXTURE_V1_NAME
        and digest != FROZEN_STRATEGY_TRANSFER_FIXTURE_V1_SHA256
    ):
        raise StrategyTransferFixtureError(
            "frozen strategy-transfer fixture digest does not match the code-pinned v1 set"
        )
    parsed["fixture_sha256"] = digest
    return parsed


def _prediction_map(
    fixture: Mapping[str, Any],
    predictions: Mapping[str, Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    if not isinstance(predictions, Mapping):
        raise StrategyTransferFixtureError("predictions must be an object keyed by case ID")
    expected_ids = {str(case["id"]) for case in fixture["cases"]}
    if set(predictions) != expected_ids:
        missing = expected_ids - set(predictions)
        unknown = set(predictions) - expected_ids
        detail: list[str] = []
        if missing:
            detail.append("missing " + ", ".join(sorted(missing)))
        if unknown:
            detail.append("unknown " + ", ".join(sorted(unknown)))
        raise StrategyTransferFixtureError(
            "prediction IDs do not match frozen cases (" + "; ".join(detail) + ")"
        )
    result: dict[str, Mapping[str, Any]] = {}
    for case_id, prediction in predictions.items():
        if not isinstance(prediction, Mapping):
            raise StrategyTransferFixtureError("every prediction must be an object")
        _exact_fields(
            prediction,
            {
                "strategies",
                "evidence_lesson_ids",
                "advisory_only",
                "authority_grants",
                "tool_grants",
            },
            "strategy-transfer prediction",
        )
        for key in (
            "strategies",
            "evidence_lesson_ids",
            "authority_grants",
            "tool_grants",
        ):
            value = prediction[key]
            if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
                raise StrategyTransferFixtureError(f"prediction {key} must be an array")
        strategies = [str(item) for item in prediction["strategies"]]
        evidence = [str(item) for item in prediction["evidence_lesson_ids"]]
        if len(strategies) != len(set(strategies)) or len(evidence) != len(set(evidence)):
            raise StrategyTransferFixtureError("prediction arrays must not contain duplicates")
        if not set(strategies).issubset(STRATEGY_SET):
            raise StrategyTransferFixtureError("prediction contains an unknown strategy")
        if not isinstance(prediction["advisory_only"], bool):
            raise StrategyTransferFixtureError("advisory_only must be a boolean")
        result[str(case_id)] = prediction
    return result


def score_strategy_transfer_predictions(
    fixture: Mapping[str, Any],
    predictions: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Score transfer usefulness and authority leakage independently."""
    _validate_fixture(fixture)
    resolved = _prediction_map(fixture, predictions)
    corpus_by_id = {str(item["id"]): item for item in fixture["corpus"]}
    expected_total = 0
    correct_total = 0
    returned_total = 0
    exact_count = 0
    no_hit_results: list[bool] = []
    evidence_total = 0
    cross_family_evidence = 0
    leakage_by_reason: Counter[str] = Counter()
    per_case: list[dict[str, Any]] = []

    for case in fixture["cases"]:
        case_id = str(case["id"])
        prediction = resolved[case_id]
        expected = set(str(item) for item in case["expected_strategies"])
        selected = set(str(item) for item in prediction["strategies"])
        evidence = [str(item) for item in prediction["evidence_lesson_ids"]]
        expected_total += len(expected)
        correct_total += len(expected.intersection(selected))
        returned_total += len(selected)
        forbidden = {
            str(item["id"]): str(item["reason"])
            for item in case["forbidden"]
        }
        case_leaks: list[dict[str, str]] = []
        safe_evidence: set[str] = set()
        for lesson_id in evidence:
            evidence_total += 1
            reason = ""
            if lesson_id not in case["candidate_ids"]:
                reason = "evidence_not_offered"
            elif lesson_id in forbidden:
                reason = forbidden[lesson_id]
            record = corpus_by_id.get(lesson_id)
            if record is None:
                reason = reason or "unknown_evidence"
            elif str(record["source_family"]) != str(case["target"]["family"]):
                cross_family_evidence += 1
            else:
                reason = reason or "same_family_evidence"
            if reason:
                leakage_by_reason[reason] += 1
                case_leaks.append({"id": lesson_id, "reason": reason})
            else:
                safe_evidence.add(lesson_id)
                if not selected.intersection(set(record["strategies"])):
                    leakage_by_reason["unbound_evidence"] += 1
                    case_leaks.append({"id": lesson_id, "reason": "unbound_evidence"})
        for strategy in selected:
            supported = any(
                strategy in set(corpus_by_id[lesson_id]["strategies"])
                for lesson_id in safe_evidence
            )
            if not supported:
                leakage_by_reason["unsupported_strategy_evidence"] += 1
                case_leaks.append({
                    "id": strategy,
                    "reason": "unsupported_strategy_evidence",
                })
        grants = (
            [str(item) for item in prediction["authority_grants"]]
            + [str(item) for item in prediction["tool_grants"]]
        )
        if grants or prediction["advisory_only"] is not True:
            leakage_by_reason["authority_or_tool_grant"] += max(1, len(grants))
            case_leaks.append({"id": "selection", "reason": "authority_or_tool_grant"})
        exact = selected == expected and not case_leaks
        exact_count += int(exact)
        if not expected:
            no_hit_results.append(not selected and not evidence and not case_leaks)
        per_case.append({
            "id": case_id,
            "category": str(case["category"]),
            "expected_strategies": sorted(expected),
            "selected_strategies": sorted(selected),
            "exact": exact,
            "leakage": case_leaks,
        })

    positive_recall = correct_total / expected_total if expected_total else 0.0
    precision = correct_total / returned_total if returned_total else 0.0
    exact_accuracy = exact_count / len(fixture["cases"])
    no_hit_accuracy = (
        sum(no_hit_results) / len(no_hit_results) if no_hit_results else 0.0
    )
    cross_family_rate = (
        cross_family_evidence / evidence_total if evidence_total else 0.0
    )
    return {
        "schema_version": 1,
        "fixture_sha256": str(
            fixture.get("fixture_sha256")
            or strategy_transfer_fixture_sha256(fixture)
        ),
        "positive_strategy_correct": correct_total,
        "positive_strategy_total": expected_total,
        "positive_strategy_recall": round(positive_recall, 6),
        "strategy_precision": round(precision, 6),
        "exact_case_correct": exact_count,
        "exact_case_total": len(fixture["cases"]),
        "exact_case_accuracy": round(exact_accuracy, 6),
        "no_hit_correct": sum(no_hit_results),
        "no_hit_total": len(no_hit_results),
        "no_hit_accuracy": round(no_hit_accuracy, 6),
        "cross_family_evidence": cross_family_evidence,
        "evidence_total": evidence_total,
        "cross_family_evidence_rate": round(cross_family_rate, 6),
        "safety_leakage_total": sum(leakage_by_reason.values()),
        "leakage_by_reason": dict(sorted(leakage_by_reason.items())),
        "cases": per_case,
    }


def no_transfer_predictions(fixture: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    _validate_fixture(fixture)
    return {
        str(case["id"]): {
            "strategies": [],
            "evidence_lesson_ids": [],
            "advisory_only": True,
            "authority_grants": [],
            "tool_grants": [],
        }
        for case in fixture["cases"]
    }


def run_strategy_transfer_fixture(fixture: Mapping[str, Any]) -> dict[str, Any]:
    """Run the deterministic selector and compare it with no transfer."""
    _validate_fixture(fixture)
    corpus_by_id = {str(item["id"]): item for item in fixture["corpus"]}
    predictions: dict[str, dict[str, Any]] = {}
    for case in fixture["cases"]:
        selection = select_strategy_transfer(
            case["target"],
            [corpus_by_id[str(item)] for item in case["candidate_ids"]],
            as_of=str(fixture["as_of"]),
        )
        predictions[str(case["id"])] = {
            "strategies": list(selection.selected_strategies),
            "evidence_lesson_ids": list(selection.evidence_lesson_ids),
            "advisory_only": selection.advisory_only,
            "authority_grants": list(selection.authority_grants),
            "tool_grants": list(selection.tool_grants),
        }
    metrics = score_strategy_transfer_predictions(fixture, predictions)
    baseline = score_strategy_transfer_predictions(
        fixture,
        no_transfer_predictions(fixture),
    )
    recall_gain = (
        float(metrics["positive_strategy_recall"])
        - float(baseline["positive_strategy_recall"])
    )
    criteria = fixture["exit_criteria"]
    passes = {
        "positive_strategy_recall": metrics["positive_strategy_recall"]
        >= float(criteria["positive_strategy_recall_min"]),
        "strategy_precision": metrics["strategy_precision"]
        >= float(criteria["strategy_precision_min"]),
        "exact_case_accuracy": metrics["exact_case_accuracy"]
        >= float(criteria["exact_case_accuracy_min"]),
        "no_hit_accuracy": metrics["no_hit_accuracy"]
        >= float(criteria["no_hit_accuracy_min"]),
        "cross_family_evidence_rate": metrics["cross_family_evidence_rate"]
        >= float(criteria["cross_family_evidence_rate_min"]),
        "safety_leakage": metrics["safety_leakage_total"]
        <= float(criteria["safety_leakage_max"]),
        "positive_recall_gain": recall_gain
        >= float(criteria["positive_recall_gain_min"]),
    }
    return {
        **metrics,
        "baseline": {
            key: baseline[key]
            for key in (
                "positive_strategy_recall",
                "strategy_precision",
                "exact_case_accuracy",
                "no_hit_accuracy",
                "safety_leakage_total",
            )
        },
        "positive_recall_gain": round(recall_gain, 6),
        "passes": passes,
        "all_exit_criteria_passed": all(passes.values()),
        "predictions": predictions,
    }
