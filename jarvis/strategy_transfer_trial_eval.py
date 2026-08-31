from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .strategy_transfer import STRATEGY_SET
from .strategy_transfer_trial import (
    TRIAL_ASSIGNMENT_SCHEMA,
    TRIAL_BLOCK_SIZE,
    TRIAL_PROMPT_RECEIPT_SCHEMA,
    TRIAL_SCHEMA,
    arm_for_slot,
    sha256_json,
    strategy_transfer_runtime_sha256,
)

TRIAL_EVALUATOR_VERSION = "1.0.0"
ARMS = frozenset({"control", "treatment"})
LIVE_FAMILIES = frozenset({
    "code_build", "code_fix", "code_refactor", "code_test",
    "deep_research", "learning_brief", "file_ops", "desktop_file_ops",
    "external_publish", "security_analysis", "conversation",
})
IDENTIFIER_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,95}$")
UTC_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)$"
)
ASSIGNMENT_FIELDS = {
    "schema", "manifest_sha256", "created_at", "prediction_id",
    "project_id", "target_family", "family_sequence", "block_index",
    "block_slot", "arm", "strategies", "selection_sha256",
    "assignment_sha256",
}
OUTCOME_FIELDS = {
    "schema", "assignment_sha256", "prompt_receipt_sha256", "status",
    "status_reason", "resolved_at", "successful", "outcome_sha256",
}
PROMPT_FIELDS = {
    "schema", "assignment_sha256", "prompt_recorded_at",
    "base_prompt_sha256", "final_prompt_sha256", "advice_applied",
    "prompt_receipt_sha256",
}
DISPATCH_FIELDS = {
    "schema", "assignment_sha256", "prompt_receipt_sha256",
    "provider_dispatched_at", "provider_dispatch_sha256",
}
APPLICATION_FIELDS = {
    "schema", "created_at", "prediction_id", "memory_id", "project_id",
    "strategy", "source_family", "target_family", "mode", "applied",
    "rank", "source_observation_sha256", "source_provenance_sha256",
    "source_control_sha256", "resolved_at", "successful",
    "application_sha256",
}
EVALUATION_CONFIG = {
    "minimum_outcomes_per_arm": 20,
    "minimum_source_target_pairs": 3,
    "minimum_treatment_rate": 0.70,
    "minimum_lift_points": 15,
    "alpha": 0.05,
    "confidence_interval_method": "newcombe_wilson_95",
    "require_positive_ci_lower_bound": True,
}


class StrategyTransferTrialError(ValueError):
    """A causal-trial artifact is malformed, unsealed, or fails closed."""


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _without(value: Mapping[str, Any], field: str) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key != field}


def _require_digest(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise StrategyTransferTrialError(f"{label} must be 64 lowercase hex")
    return value


def _parse_timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or UTC_RE.fullmatch(value) is None:
        raise StrategyTransferTrialError(f"{label} must be canonical UTC")
    try:
        parsed = datetime.fromisoformat(
            value[:-1] + "+00:00" if value.endswith("Z") else value
        )
        if parsed.tzinfo != timezone.utc:
            raise ValueError("timestamp is not UTC")
        return parsed
    except ValueError as exc:
        raise StrategyTransferTrialError(
            f"{label} must be canonical UTC"
        ) from exc


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or IDENTIFIER_RE.fullmatch(value) is None:
        raise StrategyTransferTrialError(f"{label} is malformed")
    return value


def _wilson(successes: int, total: int, z: float) -> tuple[float, float]:
    if total <= 0:
        raise StrategyTransferTrialError("confidence interval has zero samples")
    rate = successes / total
    denominator = 1 + z * z / total
    center = (rate + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(
        rate * (1 - rate) / total + z * z / (4 * total * total)
    ) / denominator
    return center - margin, center + margin


def _newcombe_difference(
    treatment_successes: int,
    treatment_total: int,
    control_successes: int,
    control_total: int,
    z: float,
) -> tuple[float, float]:
    treatment_low, treatment_high = _wilson(
        treatment_successes, treatment_total, z
    )
    control_low, control_high = _wilson(control_successes, control_total, z)
    return treatment_low - control_high, treatment_high - control_low


def _validate_manifest(manifest: Mapping[str, Any]) -> None:
    required = {
        "schema", "created_at", "expires_at", "project_id",
        "target_families", "family_caps", "strategies", "sample_cap",
        "block_size",
        "seed", "evaluator_version", "evaluator_sha256", "fixture_sha256",
        "config_sha256", "runtime_sha256", "operator_confirmed",
        "manifest_sha256",
    }
    if set(manifest) != required:
        raise StrategyTransferTrialError("trial manifest fields are invalid")
    if manifest["schema"] != TRIAL_SCHEMA:
        raise StrategyTransferTrialError("trial manifest schema is unsupported")
    created = _parse_timestamp(manifest["created_at"], "created_at")
    expires = _parse_timestamp(manifest["expires_at"], "expires_at")
    if not created < expires:
        raise StrategyTransferTrialError("manifest expiry is invalid")
    if manifest["block_size"] != TRIAL_BLOCK_SIZE:
        raise StrategyTransferTrialError("only four-assignment blocks are supported")
    if manifest["operator_confirmed"] is not True:
        raise StrategyTransferTrialError("operator confirmation is required")
    if isinstance(manifest["project_id"], bool) or not isinstance(
        manifest["project_id"], int
    ) or manifest["project_id"] <= 0:
        raise StrategyTransferTrialError("project ID is invalid")
    families = manifest["target_families"]
    strategies = manifest["strategies"]
    if (
        not isinstance(families, list)
        or not 1 <= len(families) <= 3
        or families != sorted(set(families))
        or any(family not in LIVE_FAMILIES for family in families)
    ):
        raise StrategyTransferTrialError("target families are invalid")
    if (
        not isinstance(strategies, list)
        or not strategies
        or strategies != sorted(set(strategies))
        or any(strategy not in STRATEGY_SET for strategy in strategies)
    ):
        raise StrategyTransferTrialError("strategies are invalid")
    for field in (
        "seed", "evaluator_sha256", "fixture_sha256", "config_sha256",
        "runtime_sha256",
    ):
        _require_digest(manifest[field], field)
    _identifier(manifest["evaluator_version"], "evaluator_version")
    if manifest["config_sha256"] != sha256_json(EVALUATION_CONFIG):
        raise StrategyTransferTrialError("evaluation config pin mismatch")
    evaluator_digest = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    if manifest["evaluator_sha256"] != evaluator_digest:
        raise StrategyTransferTrialError("evaluator pin mismatch")
    if manifest["runtime_sha256"] != strategy_transfer_runtime_sha256():
        raise StrategyTransferTrialError("runtime pin mismatch")
    caps = manifest["family_caps"]
    if (
        not isinstance(caps, dict)
        or set(caps) != set(families)
        or any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or value <= 0
            or value % TRIAL_BLOCK_SIZE
            for value in caps.values()
        )
        or sum(caps.values()) != manifest["sample_cap"]
    ):
        raise StrategyTransferTrialError("family caps are invalid")
    expected = _digest(_without(manifest, "manifest_sha256"))
    if _require_digest(manifest["manifest_sha256"], "manifest digest") != expected:
        raise StrategyTransferTrialError("manifest digest mismatch")


def _validate_rows(artifact: Mapping[str, Any]) -> list[dict[str, Any]]:
    manifest = artifact["manifest"]
    rows = artifact["rows"]
    if not isinstance(rows, list) or not rows:
        raise StrategyTransferTrialError("trial rows must be non-empty")
    declared_at = _parse_timestamp(manifest["created_at"], "created_at")
    seen_predictions: set[int] = set()
    seen_assignments: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != {
            "block_id", "assignment", "prompt_receipt", "applications",
            "provider_dispatch", "outcome",
        }:
            raise StrategyTransferTrialError("trial row fields are invalid")
        assignment = row.get("assignment")
        prompt = row.get("prompt_receipt")
        applications = row.get("applications")
        dispatch = row.get("provider_dispatch")
        outcome = row.get("outcome")
        if (
            not isinstance(assignment, Mapping)
            or not isinstance(prompt, Mapping)
            or not isinstance(applications, list)
            or not applications
            or not isinstance(dispatch, Mapping)
            or not isinstance(outcome, Mapping)
        ):
            raise StrategyTransferTrialError("row receipts are incomplete")
        if (
            set(assignment) != ASSIGNMENT_FIELDS
            or set(prompt) != PROMPT_FIELDS
            or set(dispatch) != DISPATCH_FIELDS
            or set(outcome) != OUTCOME_FIELDS
            or any(
                not isinstance(item, Mapping) or set(item) != APPLICATION_FIELDS
                for item in applications
            )
        ):
            raise StrategyTransferTrialError("receipt fields are invalid")
        if assignment["target_family"] not in LIVE_FAMILIES:
            raise StrategyTransferTrialError("target family is unsupported")
        for field in (
            "prediction_id", "project_id", "family_sequence",
            "block_index", "block_slot",
        ):
            if isinstance(assignment[field], bool) or not isinstance(
                assignment[field], int
            ):
                raise StrategyTransferTrialError(f"{field} must be an integer")
        if assignment["prediction_id"] <= 0 or assignment["project_id"] <= 0:
            raise StrategyTransferTrialError("assignment identity is invalid")
        if (
            assignment["project_id"] != manifest["project_id"]
            or assignment["target_family"] not in manifest["target_families"]
        ):
            raise StrategyTransferTrialError("assignment scope mismatch")
        block_index, block_slot = divmod(
            assignment["family_sequence"], TRIAL_BLOCK_SIZE
        )
        if (
            assignment["block_index"] != block_index
            or assignment["block_slot"] != block_slot
        ):
            raise StrategyTransferTrialError("block coordinate mismatch")
        if block_index < 0 or not 0 <= block_slot < TRIAL_BLOCK_SIZE:
            raise StrategyTransferTrialError("block index or slot is out of bounds")
        expected_block_id = f"{assignment['target_family']}:{block_index}"
        if row["block_id"] != expected_block_id:
            raise StrategyTransferTrialError("block identity mismatch")
        expected_arm = arm_for_slot(
            seed=manifest["seed"],
            target_family=assignment["target_family"],
            block_index=block_index,
            block_slot=block_slot,
        )
        if assignment["arm"] != expected_arm:
            raise StrategyTransferTrialError("assignment randomization mismatch")
        if (
            assignment["schema"] != TRIAL_ASSIGNMENT_SCHEMA
            or assignment["manifest_sha256"] != manifest["manifest_sha256"]
            or assignment["strategies"] != manifest["strategies"]
        ):
            raise StrategyTransferTrialError("assignment contract mismatch")
        _require_digest(assignment["selection_sha256"], "selection digest")
        assignment_digest = _require_digest(
            assignment.get("assignment_sha256"), "assignment digest"
        )
        if assignment_digest != _digest(
            _without(assignment, "assignment_sha256")
        ):
            raise StrategyTransferTrialError("assignment digest mismatch")
        prompt_digest = _require_digest(
            prompt["prompt_receipt_sha256"], "prompt receipt digest"
        )
        if prompt_digest != _digest(_without(prompt, "prompt_receipt_sha256")):
            raise StrategyTransferTrialError("prompt receipt digest mismatch")
        if (
            prompt["schema"] != TRIAL_PROMPT_RECEIPT_SCHEMA
            or prompt["advice_applied"] is not (assignment["arm"] == "treatment")
            or (
                assignment["arm"] == "control"
                and prompt["base_prompt_sha256"] != prompt["final_prompt_sha256"]
            )
            or (
                assignment["arm"] == "treatment"
                and prompt["base_prompt_sha256"] == prompt["final_prompt_sha256"]
            )
        ):
            raise StrategyTransferTrialError("prompt arm contract mismatch")
        _require_digest(prompt["base_prompt_sha256"], "base prompt digest")
        _require_digest(prompt["final_prompt_sha256"], "final prompt digest")
        dispatch_digest = _require_digest(
            dispatch["provider_dispatch_sha256"], "dispatch digest"
        )
        if dispatch_digest != _digest(
            _without(dispatch, "provider_dispatch_sha256")
        ):
            raise StrategyTransferTrialError("dispatch digest mismatch")
        if dispatch["schema"] != "jarvis.strategy-transfer-trial-provider-dispatch.v1":
            raise StrategyTransferTrialError("dispatch schema mismatch")
        outcome_digest = _require_digest(outcome["outcome_sha256"], "outcome digest")
        if outcome_digest != _digest(_without(outcome, "outcome_sha256")):
            raise StrategyTransferTrialError("outcome digest mismatch")
        if outcome["schema"] != "jarvis.strategy-transfer-trial-outcome.v1":
            raise StrategyTransferTrialError("outcome schema mismatch")
        if any(
            receipt["assignment_sha256"] != assignment_digest
            for receipt in (prompt, dispatch, outcome)
        ):
            raise StrategyTransferTrialError("receipt assignment binding mismatch")
        if (
            dispatch["prompt_receipt_sha256"] != prompt_digest
            or outcome["prompt_receipt_sha256"] != prompt_digest
        ):
            raise StrategyTransferTrialError("prompt receipt binding mismatch")
        assigned_at = _parse_timestamp(assignment["created_at"], "created_at")
        prompt_at = _parse_timestamp(prompt["prompt_recorded_at"], "prompt_recorded_at")
        dispatch_at = _parse_timestamp(
            dispatch["provider_dispatched_at"], "provider_dispatched_at"
        )
        resolved_at = _parse_timestamp(outcome["resolved_at"], "resolved_at")
        application_times = []
        source_target_pairs = []
        application_order = []
        application_strategies = []
        for application in applications:
            application_digest = _require_digest(
                application["application_sha256"], "application digest"
            )
            if application_digest != _digest(
                _without(application, "application_sha256")
            ):
                raise StrategyTransferTrialError("application digest mismatch")
            if (
                application["schema"] != "jarvis.strategy-transfer-application.v1"
                or application["prediction_id"] != assignment["prediction_id"]
                or application["project_id"] != manifest["project_id"]
                or application["target_family"] != assignment["target_family"]
                or application["source_family"] not in LIVE_FAMILIES
                or application["source_family"] == assignment["target_family"]
                or application["strategy"] not in assignment["strategies"]
                or application["mode"] != "trial"
                or application["applied"] != (assignment["arm"] == "treatment")
                or application["successful"] != outcome["successful"]
                or application["resolved_at"] != outcome["resolved_at"]
            ):
                raise StrategyTransferTrialError("application contract mismatch")
            for integer_field in ("memory_id", "rank"):
                value = application[integer_field]
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise StrategyTransferTrialError(
                        f"application {integer_field} is invalid"
                    )
            for digest_field in (
                "source_observation_sha256", "source_provenance_sha256",
                "source_control_sha256",
            ):
                _require_digest(application[digest_field], digest_field)
            application_times.append(
                _parse_timestamp(application["created_at"], "application created_at")
            )
            application_order.append((application["rank"], application["memory_id"]))
            application_strategies.append(application["strategy"])
            source_target_pairs.append(
                (application["source_family"], application["target_family"])
            )
        if application_order != sorted(application_order):
            raise StrategyTransferTrialError("applications are not in persisted order")
        if len({rank for rank, _ in application_order}) != len(application_order):
            raise StrategyTransferTrialError("application ranks are not unique")
        if sorted(set(application_strategies)) != assignment["strategies"]:
            raise StrategyTransferTrialError(
                "application strategies do not exactly match assignment"
            )
        if not (
            declared_at <= assigned_at <= prompt_at
            <= min(application_times) <= max(application_times)
            <= dispatch_at < resolved_at
        ):
            raise StrategyTransferTrialError("assignment was not persisted pre-outcome")
        if assignment["prediction_id"] in seen_predictions or assignment_digest in seen_assignments:
            raise StrategyTransferTrialError("replayed trial assignment")
        seen_predictions.add(assignment["prediction_id"])
        seen_assignments.add(assignment_digest)
        if outcome["status"] != "resolved" or outcome["status_reason"] is not None:
            raise StrategyTransferTrialError("invalid or contaminated outcome row")
        if not isinstance(outcome["successful"], int) or isinstance(
            outcome["successful"], bool
        ) or outcome["successful"] not in {0, 1}:
            raise StrategyTransferTrialError("outcome success is invalid")
        normalized.append({
            "block_id": row["block_id"],
            "assignment": dict(assignment),
            "outcome": dict(outcome),
            "pairs": source_target_pairs,
        })
    return normalized


def _evaluate_strategy_transfer_trial_artifact(
    artifact: Mapping[str, Any],
    *,
    artifact_digest: str,
    expected_manifest_sha256: str,
) -> dict[str, Any]:
    """Evaluate an already-authenticated closed trial evidence mapping."""
    if not isinstance(artifact, Mapping):
        raise StrategyTransferTrialError("trial artifact must be an object")
    if set(artifact) != {
        "schema", "phase4a_benchmark_attestation_sha256", "manifest", "rows"
    }:
        raise StrategyTransferTrialError("trial artifact fields are invalid")
    if artifact["schema"] != "jarvis.strategy-transfer-trial-evidence.v1":
        raise StrategyTransferTrialError("trial evidence schema is unsupported")
    _require_digest(
        artifact["phase4a_benchmark_attestation_sha256"],
        "Phase 4A benchmark attestation",
    )
    manifest = artifact["manifest"]
    _validate_manifest(manifest)
    if manifest["manifest_sha256"] != _require_digest(
        expected_manifest_sha256, "expected manifest digest"
    ):
        raise StrategyTransferTrialError("independent manifest pin mismatch")
    rows = _validate_rows(artifact)
    blocks: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        blocks.setdefault(row["block_id"], []).append(row)
    for block_rows in blocks.values():
        arms = [row["assignment"]["arm"] for row in block_rows]
        slots = [row["assignment"]["block_slot"] for row in block_rows]
        indices = {row["assignment"]["block_index"] for row in block_rows}
        families = {row["assignment"]["target_family"] for row in block_rows}
        invalid_block = (
            len(block_rows) != manifest["block_size"]
            or arms.count("control") != 2
            or arms.count("treatment") != 2
            or len(slots) != len(set(slots))
            or set(slots) != {0, 1, 2, 3}
            or len(indices) != 1
            or len(families) != 1
        )
        if invalid_block:
            raise StrategyTransferTrialError("incomplete or imbalanced trial block")
    arm_rows = {
        arm: [row for row in rows if row["assignment"]["arm"] == arm]
        for arm in ARMS
    }
    minimum = EVALUATION_CONFIG["minimum_outcomes_per_arm"]
    if any(len(items) < minimum for items in arm_rows.values()):
        raise StrategyTransferTrialError("insufficient outcomes per arm")
    pairs = {pair for row in rows for pair in row["pairs"]}
    if len(pairs) < EVALUATION_CONFIG["minimum_source_target_pairs"]:
        raise StrategyTransferTrialError("insufficient source-target pairs")
    successes = {
        arm: sum(bool(row["outcome"]["successful"]) for row in items)
        for arm, items in arm_rows.items()
    }
    rates = {
        arm: successes[arm] / len(arm_rows[arm]) for arm in ARMS
    }
    lift_points = round(
        100 * (rates["treatment"] - rates["control"]), 6
    )
    ci_low, ci_high = _newcombe_difference(
        successes["treatment"],
        len(arm_rows["treatment"]),
        successes["control"],
        len(arm_rows["control"]),
        1.959963984540054,
    )
    families = sorted({row["assignment"]["target_family"] for row in rows})
    family_effects: dict[str, float] = {}
    for family in families:
        family_rates = {}
        for arm in ARMS:
            selected = [
                row for row in arm_rows[arm]
                if row["assignment"]["target_family"] == family
            ]
            if not selected:
                raise StrategyTransferTrialError("family missing from one arm")
            family_rates[arm] = sum(
                bool(row["outcome"]["successful"]) for row in selected
            ) / len(selected)
        family_effects[family] = (
            family_rates["treatment"] - family_rates["control"]
        )
    passes = {
        "balanced_complete_blocks": True,
        "minimum_outcomes": True,
        "minimum_pairs": True,
        "treatment_rate": (
            rates["treatment"] >= EVALUATION_CONFIG["minimum_treatment_rate"]
        ),
        "lift": lift_points >= EVALUATION_CONFIG["minimum_lift_points"],
        "predeclared_significance": (
            not EVALUATION_CONFIG["require_positive_ci_lower_bound"] or ci_low > 0
        ),
        "no_negative_family_effect": all(
            effect >= 0 for effect in family_effects.values()
        ),
        "zero_invalid_or_contaminated_rows": True,
    }
    report = {
        "schema": "jarvis.strategy-transfer-trial-attestation.v1",
        "artifact_sha256": artifact_digest,
        "manifest_sha256": manifest["manifest_sha256"],
        "evaluator_version": TRIAL_EVALUATOR_VERSION,
        "outcomes_per_arm": {arm: len(arm_rows[arm]) for arm in sorted(ARMS)},
        "successes_per_arm": {arm: successes[arm] for arm in sorted(ARMS)},
        "rates": {arm: rates[arm] for arm in sorted(ARMS)},
        "lift_points": lift_points,
        "difference_ci_95": [ci_low, ci_high],
        "source_target_pairs": len(pairs),
        "completed_blocks": len(blocks),
        "target_family_effects": family_effects,
        "passes": passes,
        "all_exit_criteria_passed": all(passes.values()),
        "claim_scope": "sealed_randomized_trial_evidence_only",
        "activation_authorized": False,
    }
    report["attestation_sha256"] = _digest(report)
    return report


def evaluate_strategy_transfer_trial_artifact(
    artifact: Mapping[str, Any],
    *,
    expected_manifest_sha256: str,
) -> dict[str, Any]:
    """Validate an exact in-memory v39 export without filesystem staging."""
    return _evaluate_strategy_transfer_trial_artifact(
        artifact,
        artifact_digest=sha256_json(artifact),
        expected_manifest_sha256=expected_manifest_sha256,
    )


def evaluate_strategy_transfer_trial(
    path: Path,
    *,
    expected_artifact_sha256: str,
    expected_manifest_sha256: str,
) -> dict[str, Any]:
    """Verify raw artifact bytes, then evaluate the same pure core."""
    if not isinstance(path, Path):
        raise StrategyTransferTrialError("trial execution requires a Path")
    raw = path.read_bytes()
    artifact_digest = hashlib.sha256(raw).hexdigest()
    if artifact_digest != _require_digest(
        expected_artifact_sha256, "expected artifact digest"
    ):
        raise StrategyTransferTrialError("trial artifact digest mismatch")
    artifact = json.loads(raw.decode("utf-8"))
    return _evaluate_strategy_transfer_trial_artifact(
        artifact,
        artifact_digest=artifact_digest,
        expected_manifest_sha256=expected_manifest_sha256,
    )
