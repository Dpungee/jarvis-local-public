"""The learning ladder: constants, statistics, and the model-facing seam (M4).

This module is deliberately thin and mostly pure.  It owns three things the
rest of M4 composes with, and owns them *alone* so no two callers can drift:

1. **The constants.**  ``LADDER_GATE_THRESHOLDS`` is the single spelling of
   the five values every ladder gate reads, so ``Memory.calibration_gate``
   is always called the same way from the store, the CLI, the tests and the
   sealed holdout fixture.  A drift guard in ``tests/test_learning_ladder.py``
   pins all five against their sources.
2. **The statistics.**  ``monotonicity_verdict`` is the whole of the design's
   2.3 predicate as a pure function over plain dicts: ``memory.py`` owns the
   query, this module owns the arithmetic, and neither can reimplement the
   other's half.
3. **The read-path vocabulary.**  The sixteen lesson-recall modes, the skill
   channel's nine, and the one abstention-cue predicate that the Agent and
   every test call.  ``LESSON_EXITS`` is the shared mapping the store writes
   its report from and the holdout scorer reads its expectations from.

Nothing here touches a database, and nothing here is a capability.  The
approval value for a promotion is a **confirmation code** held in the
``ladder_promotions`` row and shown only on operator surfaces; it never
reaches this module, a spine payload, an activity-log line, or the model.

Seam note.  ``approved_skills`` and ``skill_channel_report`` call three
``Memory`` methods that ``store-integration`` owns:

    memory.ladder_promotions(*, project_id=None, family=None, stages=None,
                             skill_name=None) -> list[dict]
        rows carrying at least id, project_id, family, skill_name, stage
    memory.ladder_unverified_promotions(*, workspace, project_id=None)
                             -> list[dict]
        each carrying at least skill_name and reason, and promotion_id when a
        row exists
    memory.withdraw_ladder_promotion(promotion_id, *, reason, workspace=None)
        idempotent per (promotion_id, reason)

The Agent additionally calls, only when the gate is shut, store-integration's
``memory.lesson_candidate_count(family, project_id, limit=LADDER_WITHHELD_CAP)``
and adds it to this module's ``withheld`` count to get the
``withheld_candidates`` that :func:`abstention_cue_expected` needs.

Until those land, both functions fail **closed**: an unavailable ladder means
no document is approved, which is the safe direction.

See ``VTMF_M4_LEARNING_LADDER_DESIGN.md`` and ``docs/LEARNING_LADDER.md``.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import math
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Mapping, NamedTuple, Sequence

from .redaction import contains_secret, screen_endpoint
from .skill_library import LEARNED_SKILL_DIRECTORY, read_learned_documents
from .skill_evolution import auto_skill_name, matching_auto_distilled_skills

# --- 3.0 constants ---------------------------------------------------------

#: The five values every ladder gate passes to ``Memory.calibration_gate``.
#: The first three mirror ``proactive.META_GATE_*``; the last two mirror that
#: method's own bound defaults.  A drift in either source fails a test rather
#: than silently changing what the sealed holdout means.
LADDER_GATE_THRESHOLDS: dict[str, Any] = {
    "minimum_attempts": 20,
    "maximum_brier": 0.25,
    "maximum_calibration_error": 0.15,
    "minimum_success_rate": 0.70,
    "minimum_evidence_rate": 0.70,
}

LADDER_EPOCH_SIZE = 20
LADDER_MONOTONE_MAX_SLACK = 0.15
LADDER_MONOTONE_BRIER_SLACK = 0.10

#: How many consecutive regressed epochs the **runtime** refusal needs before
#: it fires -- one grace epoch (boss ruling, 2026-09-04).  The per-epoch
#: verdict of design 2.3 is unchanged and is what the ledger records and
#: ``ladder ledger`` prints; this constant governs only
#: ``currently_regressed``, the value ``stage_ladder_promotion``,
#: ``apply_ladder_promotion`` and the withdrawal path refuse on.
#:
#: Measured, not assumed: on a perfectly calibrated family at n=20 the
#: per-epoch predicate calls 8.65 % of epochs regressed by sampling noise
#: alone, so a single-epoch runtime rule would silence a healthy family's
#: skill about one epoch in twelve.  Requiring two consecutive epochs cuts
#: that to well under 1 % while a genuine regression still trips within
#: 2 * LADDER_EPOCH_SIZE = 40 resolved predictions.  Both rates are pinned in
#: ``tests/test_learning_ladder.py``.
LADDER_REGRESSION_STREAK = 2

#: The bound on the withheld-candidate count.  Nothing needs the true total --
#: the cue only asks "is there any advice being withheld?" -- so the count is a
#: bounded ``COUNT`` and never a full scan on the turn path.
LADDER_WITHHELD_CAP = 50
LADDER_MIN_VERIFIED_REUSES = 3
LADDER_MIN_DISTINCT_LESSONS = 1
LADDER_EFFECTIVENESS_MIN_APPLIED = 10
LADDER_PRIOR_DOCUMENT_RETAINED = 1
LADDER_PROOF_WINDOW_DAYS = 180

#: The z multiplier for a one-sided 95 % band.
LADDER_MONOTONE_Z = 1.645

#: The conversation family is excluded: its predictions carry ``evidence_ok``
#: NULL and ``calibration_gate`` skips the evidence clause entirely when
#: ``evidence_applicable == 0``, so a conversation-family promotion would rest
#: on no verification at all.
LADDER_EXCLUDED_FAMILIES: frozenset[str] = frozenset({"conversation"})

#: Mirrors ``Memory.PREDICTION_FAMILIES`` minus the excluded set.  Spelled out
#: rather than imported so this module never pulls in ``memory.py``; the
#: module test asserts the two agree.  **This set governs staging and approval
#: only.**
LADDER_FAMILIES: frozenset[str] = frozenset({
    "code_build", "code_fix", "code_refactor", "code_test", "deep_research",
    "learning_brief", "file_ops", "desktop_file_ops", "external_publish",
    "security_analysis",
})

#: The families the **read path** runs on -- every prediction family, the
#: `conversation` one included.  This is ``Memory.PREDICTION_FAMILIES`` by
#: construction, and the boss's cue ruling of 2026-09-04 is why the two sets
#: differ: the read path is unchanged by M4, so a conversation-family turn
#: still consults the channel and still gets a cue when the gate is shut; only
#: staging and approval are narrowed.  It also keeps a pre-M4
#: ``learned-conversation`` document reaching the model at stage
#: ``unapproved_legacy`` (design 3.7 / S-4: the status quo, made visible),
#: which a read gated on LADDER_FAMILIES would have silently withdrawn.
LADDER_READ_FAMILIES: frozenset[str] = LADDER_FAMILIES | LADDER_EXCLUDED_FAMILIES

#: The four files the sealed ladder holdout pins, in a fixed order.
LADDER_RUNTIME_FILES: tuple[str, ...] = (
    "jarvis/learning_ladder.py",
    "jarvis/memory.py",
    "jarvis/skill_evolution.py",
    "jarvis/skill_library.py",
)

_FAMILY_SHAPE = re.compile(r"[a-z][a-z0-9_]{0,39}\Z")
_COMPONENT_SHAPE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


class ScreenedComponent(ValueError):
    """One component of a staged document failed the privacy screen.

    The message names the component *kind* and the screen's own reason code,
    never the offending text: a refusal must not become a disclosure channel.
    """

    def __init__(self, component: str, reason: str) -> None:
        super().__init__(f"screened_component: {component} ({reason})")
        self.component = str(component)
        self.reason = str(reason)


# --- 5.4 the sixteen lesson-recall modes over twenty-one exits -------------

class LessonExit(NamedTuple):
    """One ``match_lessons`` exit: its mode, its sub-code, and whether it cues.

    ``line`` is the ``memory.py`` line the exit sat on at ``ec4e655`` and is
    documentation only -- nothing resolves an exit by line number.
    """

    mode: str
    reason: str | None
    cue: bool
    line: int | None


#: Every way the lesson lane can end, keyed by a stable name.  ``memory.py``
#: writes its diagnostic record through :func:`lesson_recall_record` with one
#: of these keys; the sealed holdout scorer reads the expected ``mode`` from
#: the same table.  Twenty-one exits collapse onto sixteen modes.
LESSON_EXITS: Mapping[str, LessonExit] = {
    "family_unsupported": LessonExit("family-unsupported", None, False, 14579),
    "secret_query": LessonExit("screened", "secret", True, 14581),
    "private_identifier_query": LessonExit("screened", "private_identifier", True, 14583),
    "project_ambiguous": LessonExit("project-ambiguous", None, True, 14592),
    "authority_evasion": LessonExit("authority-evasion", None, True, 14598),
    "no_discovery_terms": LessonExit("no-match", "no_terms", False, 14617),
    "chunk_overflow": LessonExit("pool-overflow", "chunk", True, 14709),
    "pool_overflow": LessonExit("pool-overflow", "pool", True, 14718),
    "database_error": LessonExit("error", None, True, 14720),
    "no_anchor": LessonExit("no-match", "no_anchor", False, 14746),
    "unknown_identity": LessonExit("unknown-identity", None, True, 14756),
    "no_query_terms": LessonExit("no-match", "no_terms", False, 14758),
    "cross_family_stronger": LessonExit("cross-family-stronger", None, True, 14810),
    "out_of_project": LessonExit("out-of-project", None, True, 14816),
    "cross_project_stronger": LessonExit("cross-project-stronger", None, True, 14841),
    "none_eligible": LessonExit("none-eligible", None, True, 14856),
    "ineligible_shadow": LessonExit("ineligible-shadow", None, True, 14885),
    "ineligible_prefix": LessonExit("ineligible-prefix", None, True, 14939),
    "ranker_floor": LessonExit("no-match", "ranker_floor", False, None),
    "rows_returned": LessonExit("complete", None, False, None),
    "idle": LessonExit("idle", None, False, None),
}

#: The sixteen closed ``mode`` values of ``Memory.lesson_recall_report()``.
LESSON_RECALL_MODES: frozenset[str] = frozenset(
    item.mode for item in LESSON_EXITS.values()
)

#: The closed ``reason`` sub-codes a ``no-match`` may carry.
LESSON_NO_MATCH_REASONS: frozenset[str] = frozenset({
    "no_terms", "no_anchor", "ranker_floor",
})

#: The twelve lesson modes that fire the abstention cue: every refusal except
#: ``no-match`` (the store looked and found nothing relevant, which is the
#: ordinary case) and the three availability facts.
LESSON_ABSTENTION_MODES: frozenset[str] = frozenset(
    item.mode for item in LESSON_EXITS.values() if item.cue
)

#: The closed ``mode`` values of :func:`skill_channel_report`, over which the
#: sealed holdout's ``expect_skill_mode`` ranges.  Design 5.4 and 7.14 list
#: eight; ``legacy-live`` is the ninth, added by the boss's ruling of
#: 2026-09-04 so an off-ladder family with a live pre-M4 document says so
#: instead of reporting a flat ``none-approved`` that ``ladder status`` would
#: contradict.  A family outside :data:`LADDER_READ_FAMILIES` entirely is
#: still ``none-approved`` with a ``reason`` sub-code, never a mode of its own.
SKILL_CHANNEL_MODES: frozenset[str] = frozenset({
    "idle", "no-prediction", "no-project", "gate-closed",
    "none-approved", "unverified-withdrawn", "legacy-only", "legacy-live",
    "complete",
})

#: The closed reasons ``Memory.ladder_unverified_promotions`` may give for a
#: live artefact that no longer verifies.  Design 3.7 listed eight; the store
#: returns **ten**, and the two extras are both real:
#:
#: ``proof_unbacked``        an application set with no matching
#:                           ``lesson.applied`` receipt (ruling 21).
#: ``live_document_missing`` a row whose file is gone -- the exact opposite of
#:                           ``orphan_document``, a file no row claims.  One
#:                           word for two opposite shapes was what kept the
#:                           reconciler and the verifier disagreeing, so the
#:                           split stays.
LADDER_UNVERIFIED_REASONS: frozenset[str] = frozenset({
    "no_approved_row", "orphan_document", "live_document_missing",
    "digest_mismatch", "proof_stale", "proof_unbacked", "gate_closed",
    "ledger_regressed", "lineage_broken", "screened_component",
})

#: The ``reason`` sub-codes :func:`skill_channel_report` may carry.  The
#: unverified codes are included because ``unverified-withdrawn`` surfaces the
#: store's own reason rather than inventing one.
SKILL_CHANNEL_REASONS: frozenset[str] = frozenset({
    "insufficient", "calibration", "family_excluded", "family_unsupported",
}) | LADDER_UNVERIFIED_REASONS

#: The two skill modes that fire the abstention cue.
SKILL_ABSTENTION_MODES: frozenset[str] = frozenset({
    "gate-closed", "unverified-withdrawn",
})

#: The firing modes whose cue is **conditional** on advice actually being
#: withheld.  Data rather than a buried ``if``, so the sealed scorer and the
#: Agent read the same rule.
#:
#: On a cold store the gate is shut for every family until 20 outcomes exist,
#: so an unconditional ``gate-closed`` cue would fire on every dialogue turn of
#: a fresh install and teach the model to ignore it.  The cue exists for
#: *withheld advice*: with nothing to withhold there is nothing to disclose.
SKILL_CONDITIONAL_CUE_MODES: frozenset[str] = frozenset({"gate-closed"})


def abstention_cue_expected(
    lesson_mode: str, skill_mode: str, *, withheld_candidates: int
) -> bool:
    """True when the turn consulted the learning channel and got nothing it
    was allowed to use, for a reason other than "the store looked and found
    nothing relevant".

    ``lesson_mode`` is ``Memory.lesson_recall_report()["mode"]``; ``skill_mode``
    is :func:`skill_channel_report`'s ``mode``.  One predicate, so the cue can
    never drift from what a test asserts.

    ``withheld_candidates`` is how much advice the turn actually held back:
    ``Memory.lesson_candidate_count(family, project_id,
    limit=LADDER_WITHHELD_CAP)`` plus :func:`skill_channel_report`'s
    ``withheld``.  It gates **only** the modes in
    :data:`SKILL_CONDITIONAL_CUE_MODES` -- today just ``gate-closed``, which on
    a cold store is the state of every family and would otherwise make the cue
    a fixture of every dialogue turn.  Every other firing mode already implies
    something was found and refused, so the count does not gate them and the
    caller may pass 0 without reading the store whenever the gate is open.

    It is keyword-only and has **no default** on purpose: a default of 0 would
    quietly suppress the cue for a caller that forgot, and a default of 1 would
    quietly fire it.  Both failures are silent; a ``TypeError`` is not.
    """
    withheld = max(0, int(withheld_candidates))
    if str(lesson_mode) in LESSON_ABSTENTION_MODES:
        return True
    skill = str(skill_mode)
    if skill not in SKILL_ABSTENTION_MODES:
        return False
    if skill in SKILL_CONDITIONAL_CUE_MODES:
        return withheld > 0
    return True


def lesson_recall_record(
    exit_key: str,
    *,
    family: str | None = None,
    project_id: int | None = None,
    candidates: int = 0,
    anchored: int = 0,
    in_project: int = 0,
    eligible: int = 0,
    returned: int = 0,
    superseded_shadowed: int = 0,
    elapsed_ms: float = 0.0,
    gate_closed: bool = False,
    gate_closure: str | None = None,
    withheld_candidates: int | None = None,
) -> dict[str, Any]:
    """Build one ``lesson_recall_report`` record from a named exit.

    ``memory.py`` calls this at each of the twenty-one exits so the mode
    vocabulary lives in exactly one place.  ``abstained`` is true whenever the
    lane returned nothing and actually ran, so an empty list is never silent.

    The last three describe a turn whose **gate shut before the lane ran**, and
    they are first-class here rather than bolted on afterwards so that every
    record has the same key set: a report the caller widens in one place and
    not another is exactly the drift this builder exists to prevent.  The mode
    stays ``idle`` in that case -- the lane genuinely did not run -- and
    publishing the record at all is what stops ``lesson_recall_report()``
    serving the *previous* turn while the cue keys on stale state.
    ``gate_closure`` is :func:`gate_closed_reason`'s answer;
    ``withheld_candidates`` is the sum the cue reads, or ``None`` when nothing
    counted it.
    """
    try:
        exit_row = LESSON_EXITS[str(exit_key)]
    except KeyError:
        raise ValueError(f"unknown lesson recall exit {exit_key!r}") from None
    if gate_closure is not None and gate_closure not in SKILL_CHANNEL_REASONS:
        raise ValueError(f"unknown gate closure reason {gate_closure!r}")
    returned_rows = max(0, int(returned))
    return {
        "channel": "lessons",
        "exit": str(exit_key),
        "mode": exit_row.mode,
        "reason": exit_row.reason,
        "abstained": returned_rows == 0 and exit_row.mode != "idle",
        "family": None if family is None else str(family),
        "project_id": None if project_id is None else int(project_id),
        "candidates": max(0, int(candidates)),
        "anchored": max(0, int(anchored)),
        "in_project": max(0, int(in_project)),
        "eligible": max(0, int(eligible)),
        "returned": returned_rows,
        "superseded_shadowed": max(0, int(superseded_shadowed)),
        "elapsed_ms": round(float(elapsed_ms), 3),
        "gate_closed": bool(gate_closed),
        "gate_closure": gate_closure,
        "withheld_candidates": (
            None if withheld_candidates is None else max(0, int(withheld_candidates))
        ),
    }


# --- 2.3 monotonicity ------------------------------------------------------

_EPOCH_REQUIRED = ("epoch", "n", "successes", "brier", "calibration_error")


def _epoch_number(row: Mapping[str, Any], index: int) -> int:
    value = row.get("epoch")
    if value is None:
        return index + 1
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("epoch must be an integer")
    return value


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a real number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def _count(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _normalize_epochs(
    epochs: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    if isinstance(epochs, (str, bytes, Mapping)):
        raise ValueError("epochs must be a sequence of mappings")
    rows: list[dict[str, Any]] = []
    for index, raw in enumerate(epochs):
        if not isinstance(raw, Mapping):
            raise ValueError("each epoch must be a mapping")
        for name in _EPOCH_REQUIRED:
            if name != "epoch" and raw.get(name) is None:
                raise ValueError(f"epoch is missing {name}")
        n = _count(raw["n"], "n")
        if n < 1:
            raise ValueError("n must be at least 1")
        successes = _count(raw["successes"], "successes")
        if successes > n:
            raise ValueError("successes cannot exceed n")
        rows.append({
            "epoch": _epoch_number(raw, index),
            "n": n,
            "successes": successes,
            "brier": _finite(raw["brier"], "brier"),
            "calibration_error": _finite(
                raw["calibration_error"], "calibration_error"
            ),
            "unverified_at_seal": _count(
                raw.get("unverified_at_seal", 0), "unverified_at_seal"
            ),
            "applied_n": _count(raw.get("applied_n", 0), "applied_n"),
            "applied_successes": _count(
                raw.get("applied_successes", 0), "applied_successes"
            ),
            "unapplied_n": _count(raw.get("unapplied_n", 0), "unapplied_n"),
            "unapplied_successes": _count(
                raw.get("unapplied_successes", 0), "unapplied_successes"
            ),
        })
    rows.sort(key=lambda row: row["epoch"])
    numbers = [row["epoch"] for row in rows]
    if len(set(numbers)) != len(numbers):
        raise ValueError("epoch numbers must be distinct")
    return rows


def monotone_band(n_k: int, prior_n: int, pooled_rate: float) -> float:
    """``delta_k``: the one-sided z band on the pooled-prior comparison, capped.

    Capped at ``LADDER_MONOTONE_MAX_SLACK`` so a small epoch cannot excuse an
    arbitrary collapse.  Hand-checked: (20, 100, 0.8) -> 0.1612 capped to 0.15;
    (200, 1000, 0.8) -> 0.05097.
    """
    if int(n_k) < 1 or int(prior_n) < 1:
        raise ValueError("band sizes must be positive")
    variance = float(pooled_rate) * (1.0 - float(pooled_rate))
    raw = LADDER_MONOTONE_Z * math.sqrt(variance * (1.0 / int(n_k) + 1.0 / int(prior_n)))
    return min(LADDER_MONOTONE_MAX_SLACK, raw)


def calibration_band(n_k: int, pooled_rate: float) -> float:
    """``epsilon_k``: this epoch's own noise on the calibration-error clause.

    Uncapped by design: it exists so a 20-outcome epoch is not called a
    regression by sampling noise alone.  0.14713 at n=20/p=0.8, 0.04653 at
    n=200/p=0.8.
    """
    if int(n_k) < 1:
        raise ValueError("band size must be positive")
    variance = float(pooled_rate) * (1.0 - float(pooled_rate))
    return LADDER_MONOTONE_Z * math.sqrt(variance / int(n_k))


def monotonicity_verdict(epochs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The design's 2.3 predicate over a family's sealed epochs, in order.

    Pure: no database, no clock, no I/O.  ``epochs`` are plain dicts carrying
    ``epoch, n, successes, brier, calibration_error, unverified_at_seal`` and,
    for the reported contrast only, ``applied_n, applied_successes,
    unapplied_n, unapplied_successes``.  Extra keys are ignored.

    Epoch *k* (k >= 2) regresses iff any of

      (1) S_k                 <  P_k - delta_k
      (2) brier_k             >  max(pooled prior Brier + 0.10, 0.25)
      (3) calibration_error_k >  0.15 + epsilon_k
      (4) unverified_at_seal  >  0

    with ``P_k`` the pooled prior success rate through k-1 and ``delta_k`` /
    ``epsilon_k`` the bands above.  A family with fewer than two epochs is
    vacuously monotone and honestly labelled, never ``None``.

    Two verdicts, deliberately different (boss ruling, 2026-09-04):

    ``monotone`` / ``violations`` / ``newest_regressed`` are the per-epoch
    predicate exactly as 2.3 states it -- what the ledger records and what
    ``ladder ledger`` prints.  ``currently_regressed`` is the **runtime**
    predicate the stage, approve and withdrawal paths refuse on, and it needs
    :data:`LADDER_REGRESSION_STREAK` consecutive regressed epochs ending at the
    newest, so one noisy epoch is a grace epoch rather than a refusal.
    ``consecutive_regressed`` reports the streak so a surface can say how close
    a family is.  Because epoch 1 is never judged, a family cannot be
    ``currently_regressed`` before its third sealed epoch.
    """
    rows = _normalize_epochs(epochs)
    total_n = sum(row["n"] for row in rows)
    total_successes = sum(row["successes"] for row in rows)
    applied_n = sum(row["applied_n"] for row in rows)
    applied_successes = sum(row["applied_successes"] for row in rows)
    unapplied_n = sum(row["unapplied_n"] for row in rows)
    unapplied_successes = sum(row["unapplied_successes"] for row in rows)
    lift_pp: float | None = None
    if applied_n and unapplied_n:
        lift_pp = 100.0 * (
            applied_successes / applied_n - unapplied_successes / unapplied_n
        )

    violations: list[dict[str, Any]] = []
    regressed_epochs: set[int] = set()
    prior_n = 0
    prior_successes = 0
    prior_brier_weight = 0.0
    running_n = 0
    running_successes = 0
    for index, row in enumerate(rows):
        running_n += row["n"]
        running_successes += row["successes"]
        if index == 0:
            prior_n, prior_successes = row["n"], row["successes"]
            prior_brier_weight = row["n"] * row["brier"]
            continue
        pooled_rate = running_successes / running_n
        s_k = row["successes"] / row["n"]
        p_k = prior_successes / prior_n
        pooled_brier = prior_brier_weight / prior_n
        delta_k = monotone_band(row["n"], prior_n, pooled_rate)
        epsilon_k = calibration_band(row["n"], pooled_rate)
        detail = {
            "epoch": row["epoch"],
            "s_k": s_k,
            "p_k": p_k,
            "delta_k": delta_k,
            "brier_k": row["brier"],
            "pooled_brier": pooled_brier,
            "calibration_error_k": row["calibration_error"],
            "epsilon_k": epsilon_k,
            "unverified_at_seal": row["unverified_at_seal"],
        }
        brier_bound = max(
            pooled_brier + LADDER_MONOTONE_BRIER_SLACK,
            float(LADDER_GATE_THRESHOLDS["maximum_brier"]),
        )
        error_bound = (
            float(LADDER_GATE_THRESHOLDS["maximum_calibration_error"]) + epsilon_k
        )
        if s_k < p_k - delta_k:
            violations.append({**detail, "clause": 1})
        if row["brier"] > brier_bound:
            violations.append({**detail, "clause": 2})
        if row["calibration_error"] > error_bound:
            violations.append({**detail, "clause": 3})
        if row["unverified_at_seal"] > 0:
            violations.append({**detail, "clause": 4})
        if any(item["epoch"] == row["epoch"] for item in violations):
            regressed_epochs.add(row["epoch"])
        prior_n += row["n"]
        prior_successes += row["successes"]
        prior_brier_weight += row["n"] * row["brier"]

    # The runtime streak: consecutive regressed epochs ending at the newest.
    # Epoch 1 is never judged, so it always breaks the streak, which is why a
    # family cannot be currently_regressed before its third sealed epoch.
    consecutive = 0
    for row in reversed(rows[1:]):
        if row["epoch"] not in regressed_epochs:
            break
        consecutive += 1
    newest = rows[-1]["epoch"] if rows else None
    return {
        "epochs": len(rows),
        "monotone": not violations,
        "newest_regressed": newest in regressed_epochs,
        "consecutive_regressed": consecutive,
        "currently_regressed": consecutive >= LADDER_REGRESSION_STREAK,
        "violations": violations,
        "pooled_rate": (total_successes / total_n) if total_n else None,
        "lift_pp": lift_pp,
        "applied_n": applied_n,
        "unapplied_n": unapplied_n,
    }


# --- 3.4 the staged document ----------------------------------------------

def _screen_component(kind: str, value: str) -> str:
    """Refuse any staged-document component that is secret- or identity-shaped.

    Every variable part of a staged document is a number, a family name, a
    tool name or an oracle name; each of the three string kinds passes through
    here before it can be written.

    Three screens in widening order.  ``_COMPONENT_SHAPE`` is the same rule
    ``lesson_applications.tool_name`` is written under, and it is strict enough
    that no value reaching the third screen can currently be an identifier:
    every private-identifier kind needs a character the shape excludes, so the
    ``screen_endpoint`` clause the design mandates has no reachable case today
    and no test asserts one.  It stays because it is the clause that keeps the
    rule true if the shape is ever widened -- which design 9.3 Q-C
    contemplates, since a ``prediction_tools`` table would carry richer names.
    """
    text = str(value)
    if not _COMPONENT_SHAPE.fullmatch(text):
        raise ScreenedComponent(kind, "shape")
    if contains_secret(text):
        raise ScreenedComponent(kind, "secret")
    screened, reason = screen_endpoint(text)
    if screened:   # pragma: no cover - unreachable while the shape is this narrow
        raise ScreenedComponent(kind, str(reason or "screened"))
    return text


def build_staged_document(
    *,
    family: str,
    reuses: int,
    contexts: int,
    tool_names: Sequence[str],
    oracles: Sequence[str],
    gate: Mapping[str, Any],
    epoch: int,
    monotone: bool,
    lift_pp: float | None,
) -> str:
    """Compose the staged skill body from the proof and the frozen gate reading.

    Same template ``skill_evolution._skill_content`` produces today, with the
    three staging lines of design 3.4 added to the evidence section.  Every
    variable part is a number, a family name, a screened tool name or an
    oracle name -- never free operator text and never lesson content, which is
    what keeps ``prior_document`` safe to store and the privacy screen small.

    Raises :class:`ScreenedComponent` (a ``ValueError``) naming only the
    component kind when any component fails the screen.
    """
    from .skill_evolution import _skill_content  # local: keeps the seam one-way

    clean_family = _screen_component("family", family)
    if clean_family not in LADDER_FAMILIES:
        raise ValueError(f"family is not on the ladder: {clean_family}")
    clean_tools = sorted({
        _screen_component("tool_name", name) for name in tool_names if str(name).strip()
    })
    clean_oracles = sorted({
        _screen_component("oracle", name) for name in oracles if str(name).strip()
    })
    counted_reuses = _count(reuses, "reuses")
    counted_contexts = _count(contexts, "contexts")
    counted_epoch = _count(epoch, "epoch")
    if not isinstance(monotone, bool):
        raise ValueError("monotone must be a boolean")
    if not isinstance(gate, Mapping):
        raise ValueError("gate must be the calibration_gate mapping")
    staging = {
        "reuses": counted_reuses,
        "contexts": counted_contexts,
        "brier": gate.get("brier"),
        "calibration_error": gate.get("calibration_error"),
        "attempts": gate.get("attempts"),
        "epoch": counted_epoch,
        "monotone": monotone,
        "lift_pp": None if lift_pp is None else _finite(lift_pp, "lift_pp"),
    }
    return _skill_content(
        clean_family,
        tools=clean_tools,
        verifications=clean_oracles,
        outcomes=counted_reuses,
        staging=staging,
    )


def staged_skill_description(family: str) -> str:
    """The one-line description a staged document carries.

    Byte-identical to the string the pre-M4 distiller wrote, deliberately: a
    grandfathered document and a freshly staged one must differ only where the
    evidence differs, or ``document_unchanged`` and the rollback comparisons
    would fire on wording alone.
    """
    clean_family = _screen_component("family", family)
    return (
        f"Auto-distilled {clean_family.replace('_', ' ')} guidance from "
        "verified, calibrated outcomes."
    )


# --- 5.5 / 5.4 the model-facing seam --------------------------------------

#: The stages a row may hold and still represent advice that exists: either
#: live (``approved``/``unapproved_legacy``) or waiting for the operator
#: (``staged``).  Terminal rows are not withheld advice -- they are gone.
_LIVE_STAGES: tuple[str, ...] = ("approved", "unapproved_legacy")
_WITHHELDABLE_STAGES: tuple[str, ...] = ("staged", *_LIVE_STAGES)


#: The learned-catalog memo.  ``matching_auto_distilled_skills`` costs about
#: 8.4 ms with three live documents on this host -- 2.0 ms of it re-walking and
#: re-parsing the thirteen bundled skills, which it does three times per call --
#: against a whole-channel budget of 25 ms warm.  The key is a **digest** of
#: every live document, not a stat: measured at 0.34 ms against 0.22 ms for a
#: stat-only key, so tamper detection costs 0.12 ms and buys the property that
#: a document edited in place without changing its size or mtime still misses.
_CATALOG_LOCK = threading.Lock()
_CATALOG_CACHE: dict[str, tuple[tuple[tuple[str, str], ...], dict[str, list[dict[str, Any]]]]] = {}
_CATALOG_CACHE_MAX = 8


def _catalog_signature(root: Path) -> tuple[tuple[str, str], ...]:
    """``((directory, sha256), ...)`` over every live learned document."""
    try:
        entries = sorted(root.iterdir(), key=lambda item: item.name)
    except OSError:
        return ()
    signature: list[tuple[str, str]] = []
    for directory in entries:
        document = directory / "SKILL.md"
        try:
            raw = document.read_bytes()
        except OSError:
            continue
        signature.append((directory.name, hashlib.sha256(raw).hexdigest()))
    return tuple(signature)


def clear_catalog_cache() -> None:
    """Drop the memo.  For tests and for a reconciler that moved files itself."""
    with _CATALOG_LOCK:
        _CATALOG_CACHE.clear()


def _live_documents(workspace: Path, family: str) -> list[dict[str, Any]]:
    """``matching_auto_distilled_skills`` behind a digest-keyed memo.

    Returns fresh dicts, so a caller can never mutate the cached entry.
    """
    resolved = Path(workspace).resolve()
    root = resolved / LEARNED_SKILL_DIRECTORY
    signature = _catalog_signature(root)
    key = str(resolved)
    with _CATALOG_LOCK:
        cached = _CATALOG_CACHE.get(key)
        if cached is not None and cached[0] == signature:
            hit = cached[1].get(family)
            if hit is not None:
                return [dict(item) for item in hit]
    try:
        documents = matching_auto_distilled_skills(resolved, family, limit=5)
    except (OSError, ValueError):
        return []
    with _CATALOG_LOCK:
        cached = _CATALOG_CACHE.get(key)
        if cached is None or cached[0] != signature:
            cached = (signature, {})
            if len(_CATALOG_CACHE) >= _CATALOG_CACHE_MAX:
                _CATALOG_CACHE.clear()
            _CATALOG_CACHE[key] = cached
        cached[1][family] = [dict(item) for item in documents]
    return [dict(item) for item in documents]


def _live_document_index(workspace: Path) -> dict[str, dict[str, Any]]:
    """The complete auto-distilled live set, memoized on the same digest key.

    Handed to ``ladder_unverified_promotions(documents=...)`` so the store does
    not re-walk it.  Workspace-wide and every family, deliberately: a partial
    index would make the store report ``live_document_missing`` for whatever it
    omitted.
    """
    resolved = Path(workspace).resolve()
    signature = _catalog_signature(resolved / LEARNED_SKILL_DIRECTORY)
    key = f"{resolved}index"
    with _CATALOG_LOCK:
        cached = _CATALOG_CACHE.get(key)
        if cached is not None and cached[0] == signature:
            hit = cached[1].get("")
            if hit is not None:
                return {name: dict(row) for name, row in hit[0].items()}
    try:
        index = read_learned_documents(resolved)
    except (OSError, ValueError):
        return {}
    with _CATALOG_LOCK:
        if len(_CATALOG_CACHE) >= _CATALOG_CACHE_MAX:
            _CATALOG_CACHE.clear()
        _CATALOG_CACHE[key] = (signature, {"": [dict(index)]})
    return {name: dict(row) for name, row in index.items()}


def _ladder_stage_index(
    memory: Any,
    *,
    project_id: int,
    family: str,
    stages: tuple[str, ...] = _LIVE_STAGES,
) -> dict[str, str]:
    """``{skill_name: stage}`` for the rows in ``stages``.

    Fails closed: an unavailable ladder yields an empty index, so no document
    is treated as approved.
    """
    reader = getattr(memory, "ladder_promotions", None)
    if reader is None:
        return {}
    try:
        rows = reader(
            project_id=int(project_id),
            family=str(family),
            stages=tuple(stages),
        )
    except (TypeError, ValueError, sqlite3.Error):
        return {}
    index: dict[str, str] = {}
    wanted = set(stages)
    for row in rows or ():
        try:
            name = str(row["skill_name"])
            stage = str(row["stage"])
        except (KeyError, IndexError, TypeError):
            continue
        if stage in wanted:
            index[name] = stage
    return index


def gate_closed_reason(gate: Mapping[str, Any]) -> str | None:
    """Why the calibrated gate is shut, or ``None`` when it is open.

    ``insufficient`` -- fewer than ``minimum_attempts`` resolved outcomes: a
    cold family, not yet measured.  ``calibration`` -- measured, and found
    wanting.  ``None`` -- the gate is open and there is nothing to explain; a
    report field must not read as a complaint about a family that is fine.

    Reported by the channel report, the run metrics and ``ladder status``.  It
    never decides the cue, which turns on whether advice was actually withheld.
    """
    if gate.get("allowed"):
        return None
    requirements = gate.get("requirements") or {}
    minimum = requirements.get(
        "minimum_attempts", LADDER_GATE_THRESHOLDS["minimum_attempts"]
    )
    try:
        attempts = int(gate.get("attempts") or 0)
        needed = int(minimum)
    except (TypeError, ValueError):
        return "calibration"
    return "insufficient" if attempts < needed else "calibration"


class UnverifiedSweep(NamedTuple):
    """One turn's answer to "what is live that no longer verifies?".

    Threaded rather than re-derived, because the sweep **changes the world it
    reports on**: withdrawing a row moves it out of ``approved`` and parks its
    document, so a second sweep in the same turn truthfully finds nothing and
    the report loses the reason it should be carrying.  One sweep per call by
    construction; a caller-supplied one merely avoids repeating it per turn.
    """

    reasons: dict[str, str]          # skill_name -> the store's own reason
    deferred: frozenset[str]         # names whose receipt could not be written
    families: dict[str, str]         # skill_name -> family, when the row says


def _pending_withdrawals(
    memory: Any, *, project_id: int,
    documents: Mapping[str, Any] | None = None,
) -> tuple[dict[str, str], set[str], dict[str, str]]:
    """Withdrawals whose receipt is still outstanding: a **pure read**.

    A withdrawal that could not be receipted -- the spine would not accept the
    event -- leaves the row at ``withdrawn`` and the document parked, so the
    live root is clean and ``ladder_unverified_promotions`` has nothing to
    report.  The artefact is still unverified and the receipt is still owed,
    and holdout v2 caught the second read saying everything was fine.

    Read-only by contract, because it runs on turns where the withdrawal sweep
    deliberately does not: the sweep is gated on a live-stage row so the design
    7.8 crash window (a sound row whose file ran ahead) is never withdrawn, and
    a pending withdrawal has no live-stage row by definition.
    """
    reader = getattr(memory, "ladder_pending_withdrawals", None)
    if reader is None:
        return {}, set(), {}
    # Passing the live set is what makes the store surface a parked-but-deferred
    # orphan (ruling 35 durability half); a bare call stays row-backed only, so
    # the existing PendingWithdrawal tests are unaffected.  Only the turn path
    # passes ``documents``.
    pass_documents = documents is not None and _accepts(reader, "documents")
    try:
        if pass_documents:
            try:
                rows = reader(project_id=int(project_id), documents=dict(documents))
            except TypeError:
                rows = reader(int(project_id), documents=dict(documents))
        else:
            try:
                rows = reader(project_id=int(project_id))
            except TypeError:
                rows = reader(int(project_id))
    except Exception:
        return {}, set(), {}
    reasons: dict[str, str] = {}
    deferred: set[str] = set()
    families: dict[str, str] = {}
    for row in rows or ():
        try:
            name = str(row["skill_name"])
        except (KeyError, IndexError, TypeError):
            continue
        try:
            reason = str(row["reason"])
        except (KeyError, IndexError, TypeError):
            reason = "lineage_broken"
        reasons[name] = reason
        deferred.add(name)          # pending is deferred, by definition
        try:
            row_family = row["family"]
        except (KeyError, IndexError, TypeError):
            row_family = None
        if row_family is not None:
            families[name] = str(row_family)
    return reasons, deferred, families


def _park_orphan_document(
    memory: Any, workspace: Path, project_id: int, name: str
) -> None:
    """Route one orphan to the store's durable park+receipt (ruling 35).

    ``Memory.park_orphan_document`` recovers the promotion_id and
    approved_sha256 from the durable spine, parks the file, and appends or
    defers the ``ladder.withdrawn`` receipt -- so the outstanding-receipt fact
    is *spine-derived* and survives a fresh ``Memory`` instance, which parking
    the file ourselves could not give (the file would be gone and nothing would
    remember a receipt was owed).

    getattr-guarded: an older store degrades to inert -- the orphan is still
    excluded from ``approved_skills`` (it has no approved row), it is only not
    yet parked or receipted.  Read-path safe: never raises.
    """
    parker = getattr(memory, "park_orphan_document", None)
    if parker is None:
        return
    try:
        parker(Path(workspace), project_id=int(project_id), skill_name=str(name))
    except Exception:
        return
    try:
        clear_catalog_cache()
    except Exception:
        pass


def unverified_sweep(
    *, memory: Any, workspace: Path, project_id: int,
    documents: Mapping[str, Any] | None = None,
) -> UnverifiedSweep:
    """Run the withdrawal sweep once, for a caller that will use it twice.

    The Agent calls this once per turn and threads the result into both
    :func:`approved_skills` and :func:`skill_channel_report`.  It is a spine
    write path, so call it only on a turn that actually consults the channel.
    """
    reasons, deferred, families = _withdraw_unverified(
        memory, workspace=Path(workspace), project_id=int(project_id),
        documents=documents,
    )
    pending_reasons, pending_deferred, pending_families = _pending_withdrawals(
        memory, project_id=int(project_id), documents=documents,
    )
    # The live sweep wins on a name both report: it is this call's observation,
    # while the pending row is a standing fact about an owed receipt.
    return UnverifiedSweep(
        {**pending_reasons, **reasons},
        frozenset(deferred | pending_deferred),
        {**pending_families, **families},
    )


def _withdraw_unverified(
    memory: Any, *, workspace: Path, project_id: int,
    documents: Mapping[str, Any] | None = None,
) -> tuple[dict[str, str], set[str], dict[str, str]]:
    """``({skill_name: reason}, {names whose receipt could not be written})``.

    Every live artefact that no longer verifies, and for each one an attempt to
    record *why* it went quiet so the operator is not left with silence.

    **The read path never raises (ruling 27).**  A withdrawal is a spine write,
    and a spine write can legitimately refuse -- most sharply when the chain
    itself no longer verifies, which is exactly the state a broken lineage puts
    the store in.  Found by the sealed holdout: deleting a ``ladder.approved``
    event made ``append_event`` raise ``SpineError`` (a ``RuntimeError``, which
    the old handler did not name) straight out of a turn's read path, crashing
    the turn instead of failing closed.

    So a refusal and an exception are treated identically, as **the receipt is
    deferred**: the artefact is excluded from what reaches the model either
    way, and the caller reports that the withdrawal has not yet been receipted.
    Excluding the document is the safety property; receipting the exclusion is
    the courtesy, and the courtesy must never cost the turn.

    The broad ``except Exception`` is deliberate and confined to the two store
    calls: on this path any escaping exception is a crashed turn, and no
    exception type is worth that.  Everything outside those two calls raises
    normally.
    """
    reader = getattr(memory, "ladder_unverified_promotions", None)
    if reader is None:
        return {}, set(), {}
    try:
        arguments: dict[str, Any] = {
            "workspace": Path(workspace), "project_id": int(project_id),
        }
        # The store re-walks every live document (~19 ms) unless it is handed
        # them.  Complete set or nothing: a partial one would make it report
        # live_document_missing for the families left out.
        if documents is not None and _accepts(reader, "documents"):
            arguments["documents"] = dict(documents)
        rows = reader(**arguments)
    except Exception:
        return {}, set(), {}
    withdrawer = getattr(memory, "withdraw_ladder_promotion", None)
    unverified: dict[str, str] = {}
    deferred: set[str] = set()
    families: dict[str, str] = {}
    for row in rows or ():
        try:
            name = str(row["skill_name"])
            reason = str(row["reason"])
        except (KeyError, IndexError, TypeError):
            continue
        unverified[name] = reason
        try:
            row_family = row["family"]
        except (KeyError, IndexError, TypeError):
            row_family = None
        if row_family is not None:
            families[name] = str(row_family)
        # The store says outright when a withdrawal's receipt is still
        # outstanding; before this it could only be inferred from a withdrawal
        # that failed on this call, which lost the fact on every later one.
        try:
            already_deferred = bool(row["deferred"])
        except (KeyError, IndexError, TypeError):
            already_deferred = False
        if already_deferred:
            deferred.add(name)
        promotion_id = None
        try:
            promotion_id = row["promotion_id"]
        except (KeyError, IndexError, TypeError):
            promotion_id = None
        if withdrawer is None or promotion_id is None:
            # An orphan (reason 'orphan_document', no promotion row) is a
            # ladder-named live file whose approving row was deleted out of
            # band -- reachable by list_available_skills / read_available_skill
            # around the ladder.  Route it to the store's durable park (ruling
            # 35); never self-park (that loses the owed-receipt fact), never on
            # no_approved_row (the 7.8 window and other in-flight rows), and
            # never by catalog diff: strictly on the store's reason string.
            if reason == "orphan_document" and name:
                _park_orphan_document(memory, Path(workspace), project_id, name)
            deferred.add(name)
            continue
        try:
            outcome = withdrawer(
                int(promotion_id), reason=reason, workspace=Path(workspace)
            )
        except Exception:
            deferred.add(name)
            continue
        # A refusal dict is the store's own way of saying the same thing --
        # unless it says otherwise.  A repeat on a row that was already
        # withdrawn and receipted refuses (``already_withdrawn``) while
        # carrying ``receipt_deferred: False``, and treating that as deferred
        # would report every successfully receipted withdrawal as outstanding,
        # because the report sweeps after ``approved_skills`` already did.
        if isinstance(outcome, Mapping) and not outcome.get("withdrawn", True):
            if bool(outcome.get("receipt_deferred", True)):
                deferred.add(name)
    return unverified, deferred, families


def _orphan_and_pending(
    memory: Any, *, workspace: Path, project_id: int,
    documents: Mapping[str, Any] | None = None,
) -> "UnverifiedSweep":
    """The no-live-stage sweep: discover and park orphans, merge the pending set.

    Runs where the full sweep must not -- no live-stage row for this family --
    so it NEVER calls ``withdraw_ladder_promotion`` (which would reach the 7.8
    staged crash window and any other in-flight row).  It routes only
    ``reason == "orphan_document"`` rows to the store's durable park (ruling
    35), keying on the reason string, never a catalog diff; every other reason
    is left untouched.  Once parked, the orphan is a spine-derived pending
    withdrawal that the always-run pending read reports on later turns and after
    a fresh ``Memory`` instance.
    """
    reasons, deferred, families = _pending_withdrawals(
        memory, project_id=int(project_id), documents=documents,
    )
    reader = getattr(memory, "ladder_unverified_promotions", None)
    if reader is None:
        return UnverifiedSweep(reasons, frozenset(deferred), families)
    try:
        arguments: dict[str, Any] = {
            "workspace": Path(workspace), "project_id": int(project_id),
        }
        if documents is not None and _accepts(reader, "documents"):
            arguments["documents"] = dict(documents)
        rows = reader(**arguments)
    except Exception:
        return UnverifiedSweep(reasons, frozenset(deferred), families)
    for row in rows or ():
        try:
            reason = str(row["reason"])
            name = str(row["skill_name"])
        except (KeyError, IndexError, TypeError):
            continue
        if reason != "orphan_document" or not name:
            continue
        _park_orphan_document(memory, Path(workspace), int(project_id), name)
        reasons[name] = "orphan_document"
        deferred.add(name)
        try:
            row_family = row["family"]
        except (KeyError, IndexError, TypeError):
            row_family = None
        if row_family is not None:
            families[name] = str(row_family)
    return UnverifiedSweep(reasons, frozenset(deferred), families)


def _gate_allows(memory: Any, family: str) -> bool:
    """Whether the calibrated gate currently allows ``family``.

    Fails **closed**: a store without the gate, or one that errors, allows
    nothing.

    Every family is judged the same way, the excluded one included.  Exempting
    ``conversation`` here made a live pre-M4 document reach the model on the
    excluded family and be withheld on a ladder family from the same fixture --
    and the ladder families are exactly where a pre-M4 install's documents
    actually are, so the asymmetry pointed the wrong way as well as being
    arbitrary.  ``LADDER_EXCLUDED_FAMILIES`` governs staging and approval; it
    was never meant to govern reads.
    """
    reader = getattr(memory, "calibration_gate", None)
    if reader is None:
        return False
    try:
        return bool(reader(family, **LADDER_GATE_THRESHOLDS).get("allowed"))
    except (TypeError, ValueError, sqlite3.Error):
        return False


def approved_skills(
    *,
    workspace: Path,
    memory: Any,
    family: str,
    project_id: int,
    limit: int = 2,
    sweep: UnverifiedSweep | None = None,
    gate: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """The only learned-skill documents that may reach the model.

    A document is returned when a ``ladder_promotions`` row for its
    ``(project_id, skill_name)`` is at stage ``approved`` and verifies now, or
    at stage ``unapproved_legacy`` (a pre-M4 document the grandfather pass
    adopted).  Everything else -- staged, withdrawn, rolled back, orphaned, or
    approved-but-no-longer-verifying -- is absent, and the absence is
    receipted by the withdrawal the store appends.

    **The calibrated gate is enforced here, not only by the caller** -- design
    3.7 makes "currently allowed" part of what *verified* means. Pass ``gate``
    when the caller already has it so there is exactly one gate reading per
    turn; omitted, this reads its own. Two readings in one turn is not a
    performance question but a correctness one: they can disagree, and the
    disagreement surfaces as a withdrawal that never happened.

    Returns the same dict shape the auto-distilled path returned: ``name``,
    ``description``, ``content``, ``verified_outcomes``.
    """
    clean_family = str(family)
    if clean_family not in LADDER_READ_FAMILIES:
        return []
    bounded = max(0, min(int(limit), 5))
    if not bounded or project_id is None:
        return []
    # One gate per turn.  Given one, trust it; otherwise read it.  Computing a
    # second gate here while the caller holds another is how an empty result
    # got misread as a withdrawal: `approved_skills` saw a shut gate, the
    # report had been handed an open one, and the difference surfaced as
    # `unverified-withdrawn` on a family that had simply not been measured yet.
    allowed = (
        bool(gate.get("allowed")) if gate is not None
        else _gate_allows(memory, clean_family)
    )
    if not allowed:
        return []
    approved = _ladder_stage_index(
        memory, project_id=int(project_id), family=clean_family
    )
    if not approved:
        return []
    if sweep is None:
        sweep = unverified_sweep(
            memory=memory, workspace=Path(workspace), project_id=int(project_id),
            documents=_live_document_index(Path(workspace)),
        )
    unverified = sweep.reasons
    matches: list[dict[str, Any]] = []
    for document in _live_documents(Path(workspace), clean_family):
        name = str(document.get("name") or "")
        if name not in approved or name in unverified:
            continue
        matches.append({
            "name": name,
            "description": document.get("description"),
            "content": document.get("content"),
            "verified_outcomes": document.get("verified_outcomes"),
        })
        if len(matches) >= bounded:
            break
    return matches


def skill_channel_report(
    *,
    workspace: Path,
    memory: Any,
    family: str,
    project_id: int | None,
    gate: Mapping[str, Any] | None,
    documents: Sequence[Mapping[str, Any]] | None = None,
    sweep: UnverifiedSweep | None = None,
) -> dict[str, Any]:
    """The skill half of the learning-channel diagnostic, per design 5.4.

    ``gate`` is the verbatim ``Memory.calibration_gate`` mapping, or ``None``
    when the turn had no active prediction and the gate was therefore never
    read.  ``mode`` is the closed set :data:`SKILL_CHANNEL_MODES`.

    ``withheld`` is the skill half of :func:`abstention_cue_expected`'s
    ``withheld_candidates``: how many promotion rows for this family hold
    advice the turn did not hand over -- staged rows always, plus the live rows
    a shut gate or a withdrawal kept back.  The caller adds the lesson half
    (``Memory.lesson_candidate_count``) and passes the sum.

    ``documents`` and ``sweep`` are additive conveniences for a caller that has
    already done the work this turn.  **Omitting them is correct, not merely
    supported:** with no ``sweep`` this function runs the sweep once itself and
    threads that same result into its internal :func:`approved_skills` call, so
    there is exactly one sweep per call by construction and the sealed scorer's
    bare invocation reports what a threaded one would.  Passing ``sweep`` only
    avoids repeating it across the two calls of one turn.

    This matters because the sweep **changes the world it reports on**: it
    withdraws rows and parks documents, so a second, independent sweep in the
    same turn finds a clean store and the report silently loses the reason and
    the deferred flag it should be carrying.
    """
    started = time.perf_counter()

    def record(
        mode: str,
        *,
        reason: str | None = None,
        approved: int = 0,
        legacy: int = 0,
        withdrawn: int = 0,
        returned: int = 0,
        withheld: int = 0,
        receipt_deferred: bool = False,
    ) -> dict[str, Any]:
        return {
            "channel": "skills",
            "mode": mode,
            "reason": reason,
            "receipt_deferred": receipt_deferred,
            "abstained": returned == 0 and mode != "idle",
            "family": None if family is None else str(family),
            "project_id": None if project_id is None else int(project_id),
            "approved": approved,
            "legacy": legacy,
            "withdrawn": withdrawn,
            "returned": returned,
            "withheld": withheld,
            "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 3),
        }

    # Availability first, then the gate, and only then the family: a
    # conversation-family turn with a shut gate must report `gate-closed` and
    # fire the cue, which is the commonest shape in which the channel goes
    # quiet (boss ruling, 2026-09-04).  A family check ahead of the gate made
    # that case unreachable.
    clean_family = str(family) if family is not None else ""
    if project_id is None:
        return record("no-project")
    if gate is None:
        return record("no-prediction")
    if not gate.get("allowed"):
        # The cue for a shut gate is conditional on there being advice to
        # withhold, so this branch must count the promotion rows even though it
        # returns no documents.  The caller adds the lesson half
        # (Memory.lesson_candidate_count) and passes the sum to
        # abstention_cue_expected.
        withheld = (
            0
            if clean_family not in LADDER_READ_FAMILIES
            else len(_ladder_stage_index(
                memory, project_id=int(project_id), family=clean_family,
                stages=_WITHHELDABLE_STAGES,
            ))
        )
        return record(
            "gate-closed", reason=gate_closed_reason(gate), withheld=withheld
        )
    if clean_family not in LADDER_READ_FAMILIES:
        # Not a prediction family at all: no row can exist and the catalog
        # helper would raise on a malformed name, so answer without a read.
        return record("none-approved", reason="family_unsupported")
    excluded = clean_family in LADDER_EXCLUDED_FAMILIES
    reason = "family_excluded" if excluded else None
    all_stages = _ladder_stage_index(
        memory, project_id=int(project_id), family=clean_family,
        stages=_WITHHELDABLE_STAGES,
    )
    stages = {
        name: stage for name, stage in all_stages.items() if stage in _LIVE_STAGES
    }
    staged = len(all_stages) - len(stages)
    live = {
        str(document.get("name") or "")
        for document in _live_documents(Path(workspace), clean_family)
    }
    usable = {name: stage for name, stage in stages.items() if name in live}
    # The memoized live-document index, built once and reused: it feeds both
    # the gated expensive reads and the unconditional cheap pending read below.
    index = _live_document_index(Path(workspace))
    # The EXPENSIVE, filesystem-walking reads stay gated -- they run only where
    # approved_skills would (a live-stage row, or a live orphan candidate) --
    # because they are spine writes and must not fire on a turn with nothing to
    # do.  The documents= cost optimization is preserved.
    if sweep is None:
        if stages:
            sweep = unverified_sweep(
                memory=memory, workspace=Path(workspace),
                project_id=int(project_id), documents=index,
            )
        elif live and not all_stages:
            # This family has a live auto-distilled document with no promotion
            # row of ANY stage -- the exact orphan precondition.  (A staged 7.8
            # row would make all_stages non-empty and fall through, so this
            # never touches the crash window.)  Discover and park it via the
            # store's durable method without running the full sweep, which would
            # call withdraw_ladder_promotion on other families' in-flight rows.
            sweep = _orphan_and_pending(
                memory, workspace=Path(workspace), project_id=int(project_id),
                documents=index,
            )
        else:
            sweep = UnverifiedSweep({}, frozenset(), {})
    # The CHEAP, spine-derived pending read runs UNCONDITIONALLY every turn with
    # the live set (ruling 35 durability half).  A withdrawal deferred on a
    # corrupted head -- an orphan especially -- leaves the file parked and gone
    # from the live root, so neither gated branch above can re-see it on a later
    # turn; but the store still owes the receipt.  This read, ~0.004 ms and
    # spine-derived, keeps it visible on later turns and on a fresh Memory
    # instance, and merges it into whatever sweep was passed or computed.  The
    # live sweep wins on a shared name (this call's observation); pending fills
    # where it is silent.
    pend_reasons, pend_deferred, pend_families = _pending_withdrawals(
        memory, project_id=int(project_id), documents=index,
    )
    if pend_reasons or pend_deferred:
        sweep = UnverifiedSweep(
            {**pend_reasons, **sweep.reasons},
            frozenset(set(sweep.deferred) | pend_deferred),
            {**pend_families, **sweep.families},
        )
    if documents is None:
        documents = approved_skills(
            workspace=Path(workspace),
            memory=memory,
            family=clean_family,
            project_id=int(project_id),
            limit=5,
            sweep=sweep,
            gate=gate,
        )
    returned = sorted(
        {str(document.get("name") or "") for document in documents} & set(usable)
    )
    unverified = dict(sweep.reasons) if sweep is not None else {}
    deferred = set(sweep.deferred) if sweep is not None else set()
    families = dict(sweep.families) if sweep is not None else {}
    # What this turn withheld: the live rows that did not come back, PLUS
    # whatever the sweep itself withdrew.  The second half is why a repeat call
    # does not decay to none-approved -- by then the row has moved out of
    # `approved` and the document is parked, so the stage index alone has
    # forgotten it while the sweep still remembers.
    swept_here = {
        name for name in unverified
        if families.get(name, clean_family) == clean_family
    }
    # A withdrawal is what the SWEEP found, never what an empty list implies.
    # Inferring one from `usable - returned` reported `unverified-withdrawn`
    # with `reason: None` whenever documents were absent for any other reason
    # -- a gate the caller and the callee read differently, most sharply -- and
    # a withdrawal that never happened is worse than a missing one, because it
    # tells the operator their skill was pulled when it was only never eligible.
    held_back = swept_here - set(returned)
    withdrawn = len(held_back)
    approved = sum(1 for name in returned if usable[name] == "approved")
    legacy = sum(1 for name in returned if usable[name] == "unapproved_legacy")
    if not returned:
        if withdrawn:
            # Surface the store's own reason rather than inventing one, and say
            # plainly when the withdrawal has not been receipted yet.
            reasons = sorted(
                {unverified[name] for name in held_back if name in unverified}
            )
            return record(
                "unverified-withdrawn",
                reason=reasons[0] if reasons else reason,
                withdrawn=withdrawn,
                withheld=staged + withdrawn,
                receipt_deferred=bool(deferred & held_back),
            )
        return record(
            "none-approved", reason=reason, withdrawn=withdrawn, withheld=staged
        )
    if excluded:
        # An off-ladder family can never be staged or approved, so a live
        # document here is pre-M4 and stays live at `unapproved_legacy` until
        # the operator approves or rolls it back (design 3.7 / S-4).  Saying
        # so keeps the per-turn report and `ladder status` in agreement.
        return record(
            "legacy-live", reason="family_excluded", approved=approved,
            legacy=legacy, withdrawn=withdrawn, returned=len(returned),
            withheld=staged,
        )
    if approved == 0:
        return record(
            "legacy-only", legacy=legacy, withdrawn=withdrawn,
            returned=len(returned), withheld=staged,
        )
    return record(
        "complete",
        approved=approved,
        legacy=legacy,
        withdrawn=withdrawn,
        returned=len(returned),
        withheld=staged,
    )


# --- the consolidation pass (correctness review HIGH-2) --------------------

def _accepts(callable_object: Any, name: str) -> bool:
    """Whether ``callable_object`` takes a parameter called ``name``."""
    try:
        parameters = inspect.signature(callable_object).parameters
    except (TypeError, ValueError):
        return False
    if name in parameters:
        return True
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )


def run_ladder_pass(
    *,
    memory: Any,
    workspace: Path,
    project_id: int,
    now: str | None = None,
) -> dict[str, Any]:
    """Seal every complete epoch, then stage every candidate.  Never approve.

    The correctness review found the ladder had **no runtime driver at all**:
    ``seal_calibration_epoch`` and ``stage_ladder_promotion`` each had exactly
    one caller, ``cli.py``, so with the ungoverned distiller removed no epoch
    would ever seal and no promotion would ever stage.  This is that driver,
    and it is pure orchestration over the store's own public methods -- it
    holds no lock, opens no transaction and derives no proof of its own.

    Called once per consolidation pass, from a worker thread that built its own
    ``Memory`` (``Memory`` is thread-bound), and by ``ladder seal --all``.

    Three properties the caller depends on:

    * **Idempotent.**  Sealing takes only whole mechanical blocks of
      ``LADDER_EPOCH_SIZE``, and a second staging of an unchanged candidate is
      refused ``staging_exists`` or ``document_unchanged``, so a second call in
      a row seals nothing and stages nothing.
    * **Bounded.**  One family at a time, and the store owns the 500 ms
      per-transaction rule; a bulk catch-up therefore takes many short locks
      rather than one long one.
    * **It never raises for a refusal.**  Refusals are dicts and are counted;
      a genuine fault is caught per family and recorded in ``errors`` so one
      bad family cannot stop the pass for the other nine.

    **It never approves.**  There is no path from this function to
    ``apply_ladder_promotion``: approval is operator-typed only, and a worker
    that could approve would be the anti-goal in ``VTMF_DESIGN.md`` §11.
    """
    started = time.perf_counter()
    resolved = Path(workspace)
    sealed_by_family: dict[str, int] = {}
    staged_promotions: list[int] = []
    refusals: dict[str, int] = {}
    refusals_by_family: dict[str, list[str]] = {}
    errors: dict[str, str] = {}
    sealer = getattr(memory, "seal_calibration_epoch", None)
    candidates = getattr(memory, "ladder_candidates", None)
    stager = getattr(memory, "stage_ladder_promotion", None)

    def refuse(family: str, reason: str) -> None:
        refusals[reason] = refusals.get(reason, 0) + 1
        refusals_by_family.setdefault(family, []).append(reason)

    for family in sorted(LADDER_FAMILIES):
        try:
            if sealer is not None:
                arguments: dict[str, Any] = {}
                if now is not None and _accepts(sealer, "now"):
                    arguments["now"] = now
                if _accepts(sealer, "workspace"):
                    arguments["workspace"] = resolved
                rows = sealer(family, **arguments) or ()
                if rows:
                    sealed_by_family[family] = len(list(rows))
        except (TypeError, ValueError, sqlite3.Error) as error:
            errors[family] = f"seal: {type(error).__name__}"
            continue

        if candidates is None or stager is None:
            continue
        try:
            arguments = {}
            if _accepts(candidates, "family"):
                arguments["family"] = family
            if _accepts(candidates, "project_id"):
                arguments["project_id"] = int(project_id)
            if _accepts(candidates, "workspace"):
                arguments["workspace"] = resolved
            found = candidates(**arguments) or ()
        except (TypeError, ValueError, sqlite3.Error) as error:
            errors[family] = f"candidates: {type(error).__name__}"
            continue

        for candidate in found:
            try:
                candidate_family = str(candidate.get("family", family))
                candidate_project = int(candidate.get("project_id", project_id))
            except (AttributeError, TypeError, ValueError):
                # A row the pass cannot read is not a candidate.  Defaulting it
                # to the loop's family would stage one malformed row once per
                # family -- ten stagings from a single bad record.
                refuse(family, "malformed_candidate")
                continue
            if candidate_family != family:
                continue
            try:
                arguments = {
                    "family": candidate_family,
                    "project_id": candidate_project,
                    "workspace": resolved,
                }
                if now is not None and _accepts(stager, "now"):
                    arguments["now"] = now
                outcome = stager(**arguments)
            except (TypeError, ValueError, sqlite3.Error) as error:
                errors[family] = f"stage: {type(error).__name__}"
                continue
            if not isinstance(outcome, Mapping):
                refuse(family, "malformed_result")
                continue
            if outcome.get("staged"):
                promotion_id = outcome.get("promotion_id")
                if promotion_id is not None:
                    staged_promotions.append(int(promotion_id))
            else:
                refuse(family, str(outcome.get("reason") or "unknown"))

    return {
        "families": len(LADDER_FAMILIES),
        "sealed": sum(sealed_by_family.values()),
        "sealed_by_family": sealed_by_family,
        "staged": len(staged_promotions),
        "staged_promotions": staged_promotions,
        "refusals": refusals,
        "refusals_by_family": refusals_by_family,
        "errors": errors,
        "approved": 0,
        "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 3),
    }


# --- 1.5 the runtime pin ---------------------------------------------------

def learning_ladder_runtime_sha256(root: Path | None = None) -> str:
    """The sealed holdout's runtime pin: canonical JSON of four file digests.

    Digest-only, and over exactly the four files the ladder's scoring path
    executes.  ``jarvis/agent.py``, ``jarvis/proactive.py`` and
    ``jarvis/tools.py`` are deliberately not pinned.
    """
    base = Path(root) if root is not None else Path(__file__).resolve().parent.parent
    digests = {
        name: hashlib.sha256((base / name).read_bytes()).hexdigest()
        for name in LADDER_RUNTIME_FILES
    }
    return hashlib.sha256(
        json.dumps(
            digests, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


__all__ = [
    "LADDER_EFFECTIVENESS_MIN_APPLIED",
    "LADDER_EPOCH_SIZE",
    "LADDER_EXCLUDED_FAMILIES",
    "LADDER_FAMILIES",
    "LADDER_GATE_THRESHOLDS",
    "LADDER_MIN_DISTINCT_LESSONS",
    "LADDER_MIN_VERIFIED_REUSES",
    "LADDER_MONOTONE_BRIER_SLACK",
    "LADDER_MONOTONE_MAX_SLACK",
    "LADDER_MONOTONE_Z",
    "LADDER_PRIOR_DOCUMENT_RETAINED",
    "LADDER_PROOF_WINDOW_DAYS",
    "LADDER_READ_FAMILIES",
    "LADDER_REGRESSION_STREAK",
    "LADDER_UNVERIFIED_REASONS",
    "LADDER_RUNTIME_FILES",
    "LADDER_WITHHELD_CAP",
    "LESSON_ABSTENTION_MODES",
    "LESSON_EXITS",
    "LESSON_NO_MATCH_REASONS",
    "LESSON_RECALL_MODES",
    "LessonExit",
    "SKILL_ABSTENTION_MODES",
    "SKILL_CHANNEL_MODES",
    "SKILL_CHANNEL_REASONS",
    "SKILL_CONDITIONAL_CUE_MODES",
    "ScreenedComponent",
    "UnverifiedSweep",
    "abstention_cue_expected",
    "approved_skills",
    "auto_skill_name",
    "build_staged_document",
    "calibration_band",
    "clear_catalog_cache",
    "gate_closed_reason",
    "learning_ladder_runtime_sha256",
    "lesson_recall_record",
    "monotone_band",
    "monotonicity_verdict",
    "run_ladder_pass",
    "skill_channel_report",
    "staged_skill_description",
    "unverified_sweep",
]
