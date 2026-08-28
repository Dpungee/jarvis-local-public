from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence


FROZEN_TASK_CONTRACT_FIXTURE_V1_NAME = "task_contract_generalization_v1.json"
FROZEN_TASK_CONTRACT_FIXTURE_V1_SHA256 = (
    "3ec7a566646b0f8fdc587fa49043f87c76acae86623185022427397e353a0925"
)

TASK_CONTRACT_LANES = frozenset({
    "dialogue",
    "research",
    "creation",
    "inspection",
    "external_action",
})


class TaskContractFixtureError(ValueError):
    """A frozen TaskContract evaluation or supplied prediction set is invalid."""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def task_contract_fixture_sha256(fixture: Mapping[str, Any]) -> str:
    """Return a stable digest for one exact TaskContract gold set."""
    return hashlib.sha256(_canonical_json(fixture).encode("utf-8")).hexdigest()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise TaskContractFixtureError(f"Duplicate JSON field: {key}")
        result[key] = value
    return result


def _require_exact_fields(
    value: Mapping[str, Any],
    expected: set[str],
    label: str,
) -> None:
    observed = set(value)
    missing = expected - observed
    unknown = observed - expected
    if missing or unknown:
        details: list[str] = []
        if missing:
            details.append("missing " + ", ".join(sorted(missing)))
        if unknown:
            details.append("unknown " + ", ".join(sorted(unknown)))
        raise TaskContractFixtureError(f"{label} fields are invalid ({'; '.join(details)})")


def _validated_cases(fixture: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    routes = fixture.get("route_cases")
    clarifications = fixture.get("clarification_cases")
    if not isinstance(routes, list) or len(routes) != 30:
        raise TaskContractFixtureError("TaskContract fixture requires exactly 30 route cases")
    if not isinstance(clarifications, list) or len(clarifications) != 20:
        raise TaskContractFixtureError(
            "TaskContract fixture requires exactly 20 clarification cases"
        )

    route_ids: set[str] = set()
    clarification_ids: set[str] = set()
    prompts: set[str] = set()
    lane_counts: Counter[str] = Counter()
    for item in routes:
        if not isinstance(item, dict):
            raise TaskContractFixtureError("Every route case must be an object")
        _require_exact_fields(item, {"id", "prompt", "expected_lane"}, "route case")
        case_id = str(item["id"]).strip()
        prompt = str(item["prompt"]).strip()
        lane = str(item["expected_lane"]).strip()
        if not case_id or case_id in route_ids:
            raise TaskContractFixtureError("Route case IDs must be non-empty and unique")
        if not prompt or prompt in prompts:
            raise TaskContractFixtureError("Evaluation prompts must be non-empty and unique")
        if lane not in TASK_CONTRACT_LANES:
            raise TaskContractFixtureError(f"Unsupported expected route lane: {lane}")
        route_ids.add(case_id)
        prompts.add(prompt)
        lane_counts[lane] += 1
    if lane_counts != Counter({lane: 6 for lane in TASK_CONTRACT_LANES}):
        raise TaskContractFixtureError("Route cases must contain six cases per broad lane")

    clarification_counts: Counter[bool] = Counter()
    for item in clarifications:
        if not isinstance(item, dict):
            raise TaskContractFixtureError("Every clarification case must be an object")
        _require_exact_fields(
            item,
            {"id", "prompt", "expected_clarification"},
            "clarification case",
        )
        case_id = str(item["id"]).strip()
        prompt = str(item["prompt"]).strip()
        expected = item["expected_clarification"]
        if not case_id or case_id in clarification_ids or case_id in route_ids:
            raise TaskContractFixtureError(
                "Clarification case IDs must be non-empty and globally unique"
            )
        if not prompt or prompt in prompts:
            raise TaskContractFixtureError("Evaluation prompts must be non-empty and unique")
        if not isinstance(expected, bool):
            raise TaskContractFixtureError(
                "expected_clarification must be a boolean"
            )
        clarification_ids.add(case_id)
        prompts.add(prompt)
        clarification_counts[expected] += 1
    if clarification_counts != Counter({True: 10, False: 10}):
        raise TaskContractFixtureError(
            "Clarification cases must contain ten positive and ten negative cases"
        )
    return routes, clarifications


def _prediction_map(
    values: Sequence[Mapping[str, Any]],
    *,
    expected_ids: set[str],
    value_field: str,
    label: str,
) -> dict[str, Any]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TaskContractFixtureError(f"{label} predictions must be an array")
    result: dict[str, Any] = {}
    for item in values:
        if not isinstance(item, Mapping):
            raise TaskContractFixtureError(f"Every {label} prediction must be an object")
        _require_exact_fields(
            item,
            {"id", value_field},
            f"{label} prediction",
        )
        case_id = str(item["id"]).strip()
        if not case_id:
            raise TaskContractFixtureError(f"{label} prediction IDs must not be empty")
        if case_id in result:
            raise TaskContractFixtureError(f"Duplicate {label} prediction ID: {case_id}")
        result[case_id] = item[value_field]
    unknown = set(result) - expected_ids
    missing = expected_ids - set(result)
    if unknown or missing:
        details: list[str] = []
        if missing:
            details.append("missing " + ", ".join(sorted(missing)))
        if unknown:
            details.append("unknown " + ", ".join(sorted(unknown)))
        raise TaskContractFixtureError(
            f"{label} predictions do not match the frozen cases ({'; '.join(details)})"
        )
    return result


def score_task_contract_predictions(
    fixture: Mapping[str, Any],
    route_predictions: Sequence[Mapping[str, Any]],
    clarification_predictions: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Score supplied deterministic decisions; never invoke a model or a tool."""
    routes, clarifications = _validated_cases(fixture)
    route_by_id = _prediction_map(
        route_predictions,
        expected_ids={str(item["id"]) for item in routes},
        value_field="lane",
        label="route",
    )
    clarification_by_id = _prediction_map(
        clarification_predictions,
        expected_ids={str(item["id"]) for item in clarifications},
        value_field="clarify",
        label="clarification",
    )
    for case_id, lane in route_by_id.items():
        if not isinstance(lane, str) or lane not in TASK_CONTRACT_LANES:
            raise TaskContractFixtureError(
                f"Route prediction {case_id} has an unsupported lane"
            )
    for case_id, clarify in clarification_by_id.items():
        if not isinstance(clarify, bool):
            raise TaskContractFixtureError(
                f"Clarification prediction {case_id} must be a boolean"
            )

    route_correct = 0
    by_lane: dict[str, dict[str, Any]] = {}
    route_cases: list[dict[str, Any]] = []
    for lane in sorted(TASK_CONTRACT_LANES):
        lane_cases = [item for item in routes if item["expected_lane"] == lane]
        correct = sum(
            route_by_id[str(item["id"])] == lane for item in lane_cases
        )
        route_correct += correct
        by_lane[lane] = {
            "correct": correct,
            "total": len(lane_cases),
            "accuracy": round(correct / len(lane_cases), 6),
        }
    for item in routes:
        case_id = str(item["id"])
        predicted = str(route_by_id[case_id])
        route_cases.append({
            "id": case_id,
            "expected_lane": str(item["expected_lane"]),
            "predicted_lane": predicted,
            "correct": predicted == str(item["expected_lane"]),
        })

    ambiguous = [item for item in clarifications if item["expected_clarification"] is True]
    specified = [item for item in clarifications if item["expected_clarification"] is False]
    ambiguity_true_positives = sum(
        clarification_by_id[str(item["id"])] is True for item in ambiguous
    )
    specified_false_positives = sum(
        clarification_by_id[str(item["id"])] is True for item in specified
    )
    clarification_correct = ambiguity_true_positives + (
        len(specified) - specified_false_positives
    )
    clarification_cases = [{
        "id": str(item["id"]),
        "expected_clarification": bool(item["expected_clarification"]),
        "predicted_clarification": bool(clarification_by_id[str(item["id"])]),
        "correct": clarification_by_id[str(item["id"])]
        is item["expected_clarification"],
    } for item in clarifications]

    route_accuracy = route_correct / len(routes)
    ambiguity_recall = ambiguity_true_positives / len(ambiguous)
    specified_false_positive_rate = specified_false_positives / len(specified)
    clarification_accuracy = clarification_correct / len(clarifications)
    criteria = fixture.get("exit_criteria")
    if not isinstance(criteria, Mapping):
        raise TaskContractFixtureError("TaskContract fixture exit_criteria are unavailable")
    passes = {
        "route_accuracy": route_accuracy
        >= float(criteria["route_accuracy_min"]),
        "ambiguity_recall": ambiguity_recall
        >= float(criteria["ambiguity_recall_min"]),
        "specified_false_positive_rate": specified_false_positive_rate
        <= float(criteria["specified_false_positive_rate_max"]),
    }
    return {
        "schema_version": 1,
        "fixture_sha256": str(
            fixture.get("fixture_sha256") or task_contract_fixture_sha256(fixture)
        ),
        "route_correct": route_correct,
        "route_total": len(routes),
        "route_accuracy": round(route_accuracy, 6),
        "route_by_lane": by_lane,
        "ambiguity_true_positives": ambiguity_true_positives,
        "ambiguity_total": len(ambiguous),
        "ambiguity_recall": round(ambiguity_recall, 6),
        "specified_false_positives": specified_false_positives,
        "specified_total": len(specified),
        "specified_false_positive_rate": round(
            specified_false_positive_rate, 6
        ),
        "clarification_correct": clarification_correct,
        "clarification_total": len(clarifications),
        "clarification_accuracy": round(clarification_accuracy, 6),
        "passes": passes,
        "all_exit_criteria_passed": all(passes.values()),
        "route_cases": route_cases,
        "clarification_cases": clarification_cases,
    }


def _metric_projection(metrics: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "route_correct",
        "route_total",
        "route_accuracy",
        "route_by_lane",
        "ambiguity_true_positives",
        "ambiguity_total",
        "ambiguity_recall",
        "specified_false_positives",
        "specified_total",
        "specified_false_positive_rate",
        "clarification_correct",
        "clarification_total",
        "clarification_accuracy",
    )
    return {key: metrics[key] for key in keys}


def load_task_contract_fixture(path: Path) -> dict[str, Any]:
    """Load, structurally validate, and authenticate the frozen evaluation."""
    try:
        parsed = json.loads(
            Path(path).read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
        )
    except TaskContractFixtureError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TaskContractFixtureError(
            f"Could not load TaskContract fixture: {exc}"
        ) from exc
    if not isinstance(parsed, dict):
        raise TaskContractFixtureError("TaskContract fixture must be an object")
    _require_exact_fields(
        parsed,
        {
            "schema_version",
            "name",
            "description",
            "exit_criteria",
            "route_cases",
            "clarification_cases",
            "raw_legacy_baseline",
        },
        "TaskContract fixture",
    )
    if parsed.get("schema_version") != 1:
        raise TaskContractFixtureError("TaskContract fixture schema_version must be 1")
    if not str(parsed.get("name") or "").strip():
        raise TaskContractFixtureError("TaskContract fixture name must not be empty")
    if not str(parsed.get("description") or "").strip():
        raise TaskContractFixtureError("TaskContract fixture description must not be empty")
    criteria = parsed.get("exit_criteria")
    if not isinstance(criteria, dict):
        raise TaskContractFixtureError("exit_criteria must be an object")
    _require_exact_fields(
        criteria,
        {
            "route_accuracy_min",
            "ambiguity_recall_min",
            "specified_false_positive_rate_max",
        },
        "exit criteria",
    )
    for key in ("route_accuracy_min", "ambiguity_recall_min"):
        value = criteria[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TaskContractFixtureError(f"{key} must be numeric")
        if not 0.0 <= float(value) <= 1.0 or not math.isfinite(float(value)):
            raise TaskContractFixtureError(f"{key} must be between zero and one")
    maximum = criteria["specified_false_positive_rate_max"]
    if isinstance(maximum, bool) or not isinstance(maximum, (int, float)):
        raise TaskContractFixtureError(
            "specified_false_positive_rate_max must be numeric"
        )
    if not 0.0 <= float(maximum) <= 1.0 or not math.isfinite(float(maximum)):
        raise TaskContractFixtureError(
            "specified_false_positive_rate_max must be between zero and one"
        )
    _validated_cases(parsed)

    baseline = parsed.get("raw_legacy_baseline")
    if not isinstance(baseline, dict):
        raise TaskContractFixtureError("raw_legacy_baseline must be an object")
    _require_exact_fields(
        baseline,
        {"route_predictions", "clarification_predictions", "expected_metrics"},
        "raw legacy baseline",
    )
    expected_metrics = baseline.get("expected_metrics")
    if not isinstance(expected_metrics, dict):
        raise TaskContractFixtureError("expected_metrics must be an object")
    scored = score_task_contract_predictions(
        parsed,
        baseline["route_predictions"],
        baseline["clarification_predictions"],
    )
    if expected_metrics != _metric_projection(scored):
        raise TaskContractFixtureError(
            "Raw legacy baseline metrics do not match its frozen predictions"
        )

    digest = task_contract_fixture_sha256(parsed)
    if (
        Path(path).name == FROZEN_TASK_CONTRACT_FIXTURE_V1_NAME
        and digest != FROZEN_TASK_CONTRACT_FIXTURE_V1_SHA256
    ):
        raise TaskContractFixtureError(
            "Frozen TaskContract fixture digest does not match the code-pinned v1 set"
        )
    parsed["fixture_sha256"] = digest
    return parsed
