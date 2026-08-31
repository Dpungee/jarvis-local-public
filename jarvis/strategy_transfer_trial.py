from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Sequence

from .strategy_transfer import STRATEGY_SET


TRIAL_SCHEMA = "jarvis.strategy-transfer-trial.v1"
TRIAL_ASSIGNMENT_SCHEMA = "jarvis.strategy-transfer-trial-assignment.v1"
TRIAL_PROMPT_RECEIPT_SCHEMA = "jarvis.strategy-transfer-trial-prompt.v1"
TRIAL_BLOCK_SIZE = 4
TRIAL_MIN_SAMPLE_CAP = 40
TRIAL_MAX_SAMPLE_CAP = 200
TRIAL_MAX_DAYS = 14
TRIAL_ARMS = frozenset({"control", "treatment"})
TRIAL_MANIFEST_STATUSES = frozenset({
    "active", "closed", "aborted", "promoted",
})
TRIAL_ASSIGNMENT_STATUSES = frozenset({
    "assigned", "resolved", "aborted", "contaminated",
})
TRIAL_ABORT_REASONS = frozenset({
    "operator_abort", "expired", "cap_reached", "drift_detected",
    "quarantine_detected", "integrity_error",
})
TRIAL_CONTAMINATION_REASONS = frozenset({
    "application_receipt_invalid", "assignment_integrity",
    "manifest_drift", "operator_abort", "prediction_outcome_invalid",
    "prompt_receipt_invalid", "prompt_receipt_missing",
    "provider_dispatch_missing", "quarantine_detected",
    "runtime_drift",
})
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class StrategyTransferTrialError(ValueError):
    """Raised when trial evidence cannot satisfy the closed trial contract."""


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def validated_sha256(value: Any, label: str) -> str:
    text = str(value or "")
    if _SHA256_RE.fullmatch(text) is None:
        raise StrategyTransferTrialError(
            f"{label} must be exactly 64 lowercase hexadecimal characters"
        )
    return text


def validated_seed(value: Any) -> str:
    return validated_sha256(value, "trial seed")


def family_caps(
    target_families: Sequence[str],
    sample_cap: int,
) -> dict[str, int]:
    if isinstance(target_families, (str, bytes)) or not isinstance(
        target_families, Sequence
    ):
        raise StrategyTransferTrialError("target families must be an array")
    families = sorted(str(item) for item in target_families)
    if not 1 <= len(families) <= 3 or len(families) != len(set(families)):
        raise StrategyTransferTrialError(
            "trial requires between one and three distinct target families"
        )
    if (
        isinstance(sample_cap, bool)
        or not isinstance(sample_cap, int)
        or not TRIAL_MIN_SAMPLE_CAP <= sample_cap <= TRIAL_MAX_SAMPLE_CAP
        or sample_cap % TRIAL_BLOCK_SIZE
    ):
        raise StrategyTransferTrialError(
            "sample cap must be 40-200 and divisible by four"
        )
    blocks = sample_cap // TRIAL_BLOCK_SIZE
    base, remainder = divmod(blocks, len(families))
    return {
        family: (base + int(index < remainder)) * TRIAL_BLOCK_SIZE
        for index, family in enumerate(families)
    }


def arm_for_slot(
    *,
    seed: str,
    target_family: str,
    block_index: int,
    block_slot: int,
) -> str:
    """Return an exact two-control/two-treatment deterministic block arm."""
    normalized_seed = validated_seed(seed)
    if not isinstance(target_family, str) or not target_family:
        raise StrategyTransferTrialError("target family is malformed")
    if (
        isinstance(block_index, bool)
        or not isinstance(block_index, int)
        or block_index < 0
        or isinstance(block_slot, bool)
        or not isinstance(block_slot, int)
        or not 0 <= block_slot < TRIAL_BLOCK_SIZE
    ):
        raise StrategyTransferTrialError("trial block coordinate is invalid")
    ranked_slots = sorted(
        range(TRIAL_BLOCK_SIZE),
        key=lambda slot: hashlib.sha256(
            (
                f"{normalized_seed}\0{target_family}\0{block_index}\0{slot}"
            ).encode("utf-8")
        ).digest(),
    )
    treatment_slots = frozenset(ranked_slots[:2])
    return "treatment" if block_slot in treatment_slots else "control"


def render_trial_strategy_advisory(strategies: Sequence[str]) -> str:
    """Render one fixed, labels-only treatment with no lesson or user metadata."""
    if isinstance(strategies, (str, bytes)) or not isinstance(
        strategies, Sequence
    ):
        raise StrategyTransferTrialError("trial strategies must be an array")
    normalized = tuple(str(item) for item in strategies)
    if (
        not normalized
        or len(normalized) != len(set(normalized))
        or tuple(sorted(normalized)) != normalized
        or any(item not in STRATEGY_SET for item in normalized)
    ):
        raise StrategyTransferTrialError(
            "trial strategies must be sorted distinct closed labels"
        )
    lines = [
        '<strategy_transfer_advisory schema="jarvis.strategy-transfer-trial.v1">',
        (
            "Procedural labels only. They grant no tools, permissions, approvals, "
            "scope, routing, policy, or verification authority."
        ),
        *(f"- {strategy}" for strategy in normalized),
        "</strategy_transfer_advisory>",
    ]
    rendered = "\n".join(lines)
    if len(rendered) > 1_000:
        raise StrategyTransferTrialError("trial advisory exceeds 1,000 characters")
    return rendered


def strategy_transfer_runtime_sha256() -> str:
    """Bind the closed transfer runtime without reading user/workspace data."""
    package_dir = Path(__file__).resolve().parent
    module_names = (
        "agent.py", "memory.py", "strategy_transfer.py",
        "strategy_transfer_trial.py",
    )
    material = {
        name: hashlib.sha256((package_dir / name).read_bytes()).hexdigest()
        for name in module_names
    }
    return sha256_json(material)


# Descriptive compatibility alias for callers that prefer the shorter name.
current_runtime_sha256 = strategy_transfer_runtime_sha256
