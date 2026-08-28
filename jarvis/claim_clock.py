"""Deterministic read-time confidence decay for structured memory claims.

The clock is deliberately narrower than a general memory model.  It estimates
how quickly a predicate changes from timestamped, cross-source observations and
uses that rate to age a stored confidence toward ignorance.  It never rewrites
claim history, changes claim authority, or promotes lower-authority evidence.
"""

from __future__ import annotations

import hashlib
import math
from datetime import datetime, timezone
from typing import Iterable


DEFAULT_HAZARD_PER_DAY = 1.0 / 240.0
MIN_HAZARD_PAIRS = 6
MAX_HAZARD_PAIRS = 900
HAZARD_GRID = tuple(10 ** (-4.0 + 4.7 * index / 63.0) for index in range(64))
PROTECTED_PREDICATE_PREFIXES = (
    "identity:",
    "permission:",
    "preference:",
    "safety:",
)


def source_key(authority: str, stable_source: str | None = None) -> str:
    """Return a non-reversible stable identity without persisting source text."""
    authority = str(authority).strip().casefold()
    stable = str(stable_source or authority).strip().casefold()
    canonical = f"jarvis-claim-source-v1\0{authority}\0{stable}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def protected_predicate(predicate: str) -> bool:
    normalized = " ".join(str(predicate).casefold().split())
    return normalized.startswith(PROTECTED_PREDICATE_PREFIXES)


def _finite_probability(value: object, default: float = 0.5) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return min(0.98, max(0.03, number))


def estimate_hazard(
    pairs: Iterable[tuple[float, bool, float, float]],
    *,
    vocabulary_size: int,
    prior: float = DEFAULT_HAZARD_PER_DAY,
) -> tuple[float, int]:
    """Fit an exponential change rate from cross-source agreement pairs.

    Each pair is ``(delta_days, agreed, reliability_1, reliability_2)``.
    The weak log-space prior keeps sparse predicates conservative and makes the
    result deterministic across processes and platforms.
    """
    bounded: list[tuple[float, bool, float, float]] = []
    for delta_days, agreed, reliability_1, reliability_2 in pairs:
        try:
            delta = float(delta_days)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(delta) or delta <= 0:
            continue
        bounded.append(
            (
                min(delta, 36_500.0),
                bool(agreed),
                _finite_probability(reliability_1),
                _finite_probability(reliability_2),
            )
        )
        if len(bounded) >= MAX_HAZARD_PAIRS:
            break
    if len(bounded) < MIN_HAZARD_PAIRS:
        return prior, len(bounded)

    candidates = max(2.0, float(max(2, int(vocabulary_size))))
    best_hazard = prior
    best_likelihood = -math.inf
    for hazard in HAZARD_GRID:
        likelihood = -0.5 * (math.log(hazard) - math.log(prior)) ** 2 / 9.0
        for delta, agreed, first, second in bounded:
            same = first * second + (1.0 - first) * (1.0 - second) / (
                candidates - 1.0
            )
            different = (
                first * (1.0 - second) + second * (1.0 - first)
            ) / (candidates - 1.0)
            different += (
                (1.0 - first)
                * (1.0 - second)
                * (candidates - 2.0)
                / ((candidates - 1.0) ** 2)
            )
            survival = math.exp(-hazard * delta)
            agreement_probability = survival * same + (1.0 - survival) * different
            agreement_probability = min(1.0 - 1e-6, max(1e-6, agreement_probability))
            likelihood += math.log(
                agreement_probability if agreed else 1.0 - agreement_probability
            )
        if likelihood > best_likelihood:
            best_likelihood = likelihood
            best_hazard = hazard
    return best_hazard, len(bounded)


def age_days(supported_at: str, as_of: str | None = None) -> float:
    def parse(value: str) -> datetime:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    try:
        supported = parse(supported_at)
        current = parse(as_of) if as_of is not None else datetime.now(timezone.utc)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, (current - supported).total_seconds() / 86_400.0)


def effective_confidence(
    stored_confidence: float,
    *,
    hazard_per_day: float,
    elapsed_days: float,
    vocabulary_size: int,
    immutable: bool = False,
) -> float:
    stored = min(1.0, max(0.0, float(stored_confidence)))
    if immutable:
        return stored
    hazard = max(0.0, float(hazard_per_day))
    elapsed = max(0.0, float(elapsed_days))
    ignorance = 1.0 / max(2.0, float(max(2, int(vocabulary_size))))
    survival = math.exp(-hazard * elapsed)
    return min(1.0, max(0.0, survival * stored + (1.0 - survival) * ignorance))
