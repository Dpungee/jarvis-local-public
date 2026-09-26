"""Derive the roadmap Phase-2 verified-completion metric and its evidence record.

``jarvis/task_contract_eval.py`` computes a per-case outcome verdict at
``score_task_contract_holdout`` (the ``outcome_case_passes`` loop) but returns
only the per-case *observations* it derived, together with the sealed fixture's
own aggregate exit criteria.  The roadmap's second gate bullet - "verified
workflow completion >= 0.85" - is a per-case rate, so it has to be recomputed
from those observations.

This module is that recomputation, and nothing else touches the scorer:

* :func:`recompute_outcome_case_passes` restates the scorer's predicate
  statement for statement from ``outcome_case_observations`` plus the sealed
  fixture's ``expected`` block.  It is a **copy**, deliberately, so a reviewer
  can diff it against the original; it refuses to run against an observation
  record whose field set differs from the one it was written against, because
  silent drift here moves the headline number in the flattering direction.
* :func:`verified_workflow_completion` reports that rate three ways - over
  every case, over the cases left after the run's declared structural
  exclusions, and per lane - and never one alone.

Two numbers therefore always appear side by side in the evidence artifact: the
sealed fixture's own ``all_exit_criteria_passed`` (whose
``immediate_evidence_rate_min`` is 1.0, *stricter* than the roadmap's 0.85, and
which additionally requires ``tool_exposure_rate == 1.0`` and a complete 66-case
scoring) and the roadmap gate this module derives.  A run can miss the fixture's
criteria while clearing the roadmap gate, and the artifact must show both.

Nothing here modifies, rescores, or reinterprets the sealed fixture, its
code-pinned digest constant, or ``jarvis/task_contract_eval.py``.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .redaction import (
    contains_obfuscated_secret,
    normalize_private_identifier_text,
    private_email_addresses,
    private_identifier_text_was_obfuscated,
)

__all__ = [
    "OBSERVATION_FIELDS",
    "Phase2ReportError",
    "STRUCTURAL_EXCLUSIONS",
    "base_identity",
    "build_outcome_evidence",
    "canonical_json",
    "configuration_class",
    "evidence_findings",
    "outcome_gate_rows",
    "platform_identity",
    "recompute_outcome_case_passes",
    "structural_exclusions",
    "verified_workflow_completion",
]


class Phase2ReportError(RuntimeError):
    """The observation record cannot carry a Phase-2 completion claim."""


#: The exact key set of one ``outcome_case_observations`` entry as produced by
#: ``score_task_contract_holdout`` (``jarvis/task_contract_eval.py``: the
#: ``observed[case_id] = {...}`` block, plus the ``id`` added when the list is
#: assembled).  Recomputation refuses to proceed against any other shape.
OBSERVATION_FIELDS = frozenset({
    "id",
    "false_unavailable",
    "tool_exposure_observed",
    "immediate_action_evidence",
    "future_work_promise",
    "durable_queue_receipt",
    "restart_sequence_outcome",
    "unexpected_effects",
    "missing_target_receipts",
    "target_constraints_bound",
    "unbound_material_effects",
    "duplicate_external_effects",
})

#: Fields of the sealed fixture's ``expected`` block that the predicate reads.
_EXPECTED_FIELDS = (
    "immediate_action_evidence_required",
    "future_queue_receipt_required",
    "restart_sequence_outcome",
)

#: Structural exclusions this runner can declare.  Each is derived from the
#: sealed fixture by predicate - never by an id list - and then checked against
#: the ids the boss named, so a fixture change surfaces as a mismatch instead of
#: silently widening or narrowing what a run excuses itself from.
STRUCTURAL_EXCLUSIONS: tuple[dict[str, Any], ...] = (
    {
        "key": "execution_disabled_by_ruling",
        "reason": (
            "run configured with JARVIS_EXECUTION_MODE=disabled, so no execution "
            "tool is offered and an immediate execute effect cannot be evidenced; "
            "a model-authored script would otherwise run with the host user's own "
            "authority and no throwaway account exists on this host"
        ),
        "expected_case_ids": ("p2_creation_02", "p2_creation_06", "p2_creation_11"),
    },
    {
        "key": "configuration_write_no_capability_tool",
        "reason": (
            "recorded divergence 6 of jarvis/task_contract_outcome_toolbox.py: these "
            "cases request requested_effect \"write\" and the configuration lane's "
            "only write-classified tool is feature_setup_decide, whose capability_id "
            "enum covers the network and Bluetooth features only. Production's "
            "screen_companion_control is absent from MUTATING_TOOLS, so "
            "_observed_tool_effect classifies it \"read\" and it could not evidence a "
            "write even if offered; and it is never offered, because "
            "allow_screen_companion defaults to False at agent.py:7975, is filtered "
            "on at agent.py:8010, and no caller in agent.py ever passes it True. "
            "There is no production tool at all for Public Presence or proactive "
            "control."
        ),
        "expected_case_ids": (
            "p2_configuration_02",
            "p2_configuration_03",
            "p2_configuration_06",
        ),
    },
    {
        "key": "configuration_read_no_semantically_matching_tool",
        "reason": (
            "recorded divergence 6 of jarvis/task_contract_outcome_toolbox.py: these "
            "cases request requested_effect \"read\" and are structurally reachable, "
            "but only through a semantically unrelated read tool - the lane has no "
            "production read tool that answers what the case actually asks."
        ),
        "expected_case_ids": ("p2_configuration_01", "p2_configuration_05"),
    },
)

_ROADMAP_COMPLETION_THRESHOLD = 0.85

#: Only these switch names are read for the configuration class, and only values
#: shaped like a plain switch token are recorded, so no credential, path, or
#: hostname can reach the artifact through the environment.
_CONFIGURATION_SWITCHES = (
    "JARVIS_CLOUD_ENABLED",
    "JARVIS_OLLAMA_ENABLED",
    "JARVIS_EXTERNAL_ACCESS",
    "JARVIS_EXECUTION_MODE",
    "JARVIS_COMPUTER_ACCESS",
)
_SWITCH_VALUE_RE = re.compile(r"\A[A-Za-z0-9_.:-]{1,32}\Z")

# The three home-path patterns below are copied verbatim from
# scripts/check_public_release.py.  The Unicode canonicalization and the
# email extraction are imported from jarvis.redaction - the same helpers that
# scanner imports - so this check cannot drift into being weaker than the
# release gate it stands in for.  It runs over the artifact's own text before
# the file is written, so a privacy regression fails the run rather than the
# release check.
#
# What it deliberately does not reproduce: the scanner's filename rules, its
# git-index walk, and its address-specific allowlist (git@github.com and the
# like), which are properties of a repository rather than of one artifact.
# Run the scanner itself before proposing the artifact for commit.
_WINDOWS_HOME_RE = re.compile(
    r"(?i)\b[A-Z]:(?:[\\/]+)Users(?:[\\/]+)([^\\/:\"'<>|\r\n\t]+)"
)
_POSIX_HOME_RE = re.compile(r"(?i)(?:^|[^\w])/(?:home|Users)/([^/\"'<>|\r\n\t]+)")
_UNC_HOME_RE = re.compile(
    r"(?i)(?:^|[^\w])(?:\\\\|//)([^\\/\s:\"'<>|]+)[\\/]"
    r"+(?:Users|home|homes)[\\/]+([^\\/:\"'<>|\r\n\t]+)"
)
_ALLOWED_EMAIL_DOMAINS = frozenset({
    "example.com",
    "example.net",
    "example.org",
    "users.noreply.github.com",
})


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _checksum(value: Any) -> str:
    """An integrity checksum, not a signature: an unkeyed hash is recomputable."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _validated_cases(fixture: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    cases = fixture.get("cases")
    if not isinstance(cases, Sequence) or not cases:
        raise Phase2ReportError("the fixture carries no cases")
    return [case for case in cases]


def recompute_outcome_case_passes(
    fixture: Mapping[str, Any],
    observations: Sequence[Mapping[str, Any]],
) -> dict[str, bool]:
    """Restate the scorer's per-case outcome verdict, statement for statement.

    ``observations`` is the scorer's own ``outcome_case_observations`` list.
    The predicate below is a verbatim copy of the ``outcome_case_passes`` loop
    in ``jarvis/task_contract_eval.py``; it is duplicated rather than imported
    because the scorer does not return the verdicts, and it is guarded by an
    exact field-set check so a scorer change cannot drift past it unnoticed.

    Like the scorer, this refuses a partial set: every sealed case must be
    present exactly once and no unknown id may appear.
    """
    cases = _validated_cases(fixture)
    expected_ids = {str(case["id"]) for case in cases}
    seen: dict[str, Mapping[str, Any]] = {}
    for entry in observations:
        if not isinstance(entry, Mapping):
            raise Phase2ReportError("every outcome observation must be an object")
        fields = set(entry)
        if fields != OBSERVATION_FIELDS:
            missing = sorted(OBSERVATION_FIELDS - fields)
            unknown = sorted(fields - OBSERVATION_FIELDS)
            raise Phase2ReportError(
                "outcome observation fields do not match the scorer's record "
                f"(missing {missing}; unknown {unknown}); the recomputed predicate "
                "must be re-derived from jarvis/task_contract_eval.py before this "
                "number can be reported"
            )
        case_id = str(entry["id"])
        if case_id in seen:
            raise Phase2ReportError(f"duplicate outcome observation id: {case_id}")
        seen[case_id] = entry
    missing_ids = expected_ids - set(seen)
    unknown_ids = set(seen) - expected_ids
    if missing_ids or unknown_ids:
        details: list[str] = []
        if missing_ids:
            details.append("missing " + ", ".join(sorted(missing_ids)))
        if unknown_ids:
            details.append("unknown " + ", ".join(sorted(unknown_ids)))
        raise Phase2ReportError(
            "outcome observations do not match the frozen cases "
            f"({'; '.join(details)})"
        )

    passes: dict[str, bool] = {}
    for case in cases:
        case_id = str(case["id"])
        expected = case["expected"]
        for field in _EXPECTED_FIELDS:
            if field not in expected:
                raise Phase2ReportError(
                    f"the fixture case {case_id} has no {field} expectation"
                )
        item = seen[case_id]
        # --- begin verbatim copy of task_contract_eval.score_task_contract_holdout
        passes[case_id] = bool(
            not item["false_unavailable"]
            and item["tool_exposure_observed"]
            and item["unexpected_effects"] == 0
            and item["missing_target_receipts"] == 0
            and item["target_constraints_bound"]
            and item["unbound_material_effects"] == 0
            and item["duplicate_external_effects"] == 0
            and not (
                item["future_work_promise"]
                and not item["durable_queue_receipt"]
            )
            and (
                not expected["immediate_action_evidence_required"]
                or item["immediate_action_evidence"]
            )
            and (
                not expected["future_queue_receipt_required"]
                or item["durable_queue_receipt"]
            )
            and (
                expected["restart_sequence_outcome"] != "preserved"
                or item["restart_sequence_outcome"] == "preserved"
            )
        )
        # --- end verbatim copy
    return passes


def outcome_case_failure_reasons(
    fixture: Mapping[str, Any],
    observations: Sequence[Mapping[str, Any]],
) -> dict[str, list[str]]:
    """Name the clause that failed for every case that did not pass.

    Diagnostic only.  The verdict itself is :func:`recompute_outcome_case_passes`;
    this attributes it so a failing run is readable without the operator's text.
    """
    cases = {str(case["id"]): case for case in _validated_cases(fixture)}
    passes = recompute_outcome_case_passes(fixture, observations)
    reasons: dict[str, list[str]] = {}
    for entry in observations:
        case_id = str(entry["id"])
        if passes[case_id]:
            continue
        expected = cases[case_id]["expected"]
        failed: list[str] = []
        if entry["false_unavailable"]:
            failed.append("false_unavailable")
        if not entry["tool_exposure_observed"]:
            failed.append("tool_exposure_observed")
        if entry["unexpected_effects"] != 0:
            failed.append("unexpected_effects")
        if entry["missing_target_receipts"] != 0:
            failed.append("missing_target_receipts")
        if not entry["target_constraints_bound"]:
            failed.append("target_constraints_bound")
        if entry["unbound_material_effects"] != 0:
            failed.append("unbound_material_effects")
        if entry["duplicate_external_effects"] != 0:
            failed.append("duplicate_external_effects")
        if entry["future_work_promise"] and not entry["durable_queue_receipt"]:
            failed.append("unbacked_future_promise")
        if (
            expected["immediate_action_evidence_required"]
            and not entry["immediate_action_evidence"]
        ):
            failed.append("immediate_action_evidence")
        if (
            expected["future_queue_receipt_required"]
            and not entry["durable_queue_receipt"]
        ):
            failed.append("durable_queue_receipt")
        if (
            expected["restart_sequence_outcome"] == "preserved"
            and entry["restart_sequence_outcome"] != "preserved"
        ):
            failed.append("restart_sequence_outcome")
        reasons[case_id] = failed
    return reasons


def structural_exclusions(fixture: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Derive the declared structural exclusions from the sealed fixture.

    Membership is a predicate over the fixture's own ``expected`` block; the
    named ids are then asserted, so a fixture edit surfaces as a mismatch rather
    than silently changing which cases a run excuses itself from.
    """
    cases = _validated_cases(fixture)
    derived: dict[str, list[str]] = {
        "execution_disabled_by_ruling": [
            str(case["id"])
            for case in cases
            if case["expected"]["action_timing"] == "immediate"
            and case["expected"]["requested_effect"] == "execute"
        ],
        "configuration_write_no_capability_tool": [
            str(case["id"])
            for case in cases
            if case["expected"]["lane"] == "configuration"
            and case["expected"]["immediate_action_evidence_required"] is True
            and case["expected"]["requested_effect"] == "write"
        ],
        "configuration_read_no_semantically_matching_tool": [
            str(case["id"])
            for case in cases
            if case["expected"]["lane"] == "configuration"
            and case["expected"]["immediate_action_evidence_required"] is True
            and case["expected"]["requested_effect"] == "read"
        ],
    }
    rows: list[dict[str, Any]] = []
    for spec in STRUCTURAL_EXCLUSIONS:
        case_ids = sorted(derived[str(spec["key"])])
        if tuple(case_ids) != tuple(sorted(spec["expected_case_ids"])):
            raise Phase2ReportError(
                f"structural exclusion {spec['key']} derived {case_ids} but the "
                f"declared case set is {list(spec['expected_case_ids'])}; the "
                "fixture and the exclusion rule have diverged"
            )
        rows.append({
            "key": spec["key"],
            "reason": spec["reason"],
            "case_ids": case_ids,
            "count": len(case_ids),
        })
    return rows


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def verified_workflow_completion(
    fixture: Mapping[str, Any],
    observations: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Report the roadmap's verified-completion rate three ways, never one.

    ``all_cases`` counts every sealed case, with the structurally excluded ones
    counted as failures.  ``scorable_cases`` removes the declared structural
    exclusions.  ``by_lane`` breaks the same verdicts down by the fixture's own
    lane label.  A single number without its exclusions is not reportable, which
    is why all three are produced together.
    """
    cases = _validated_cases(fixture)
    passes = recompute_outcome_case_passes(fixture, observations)
    exclusions = structural_exclusions(fixture)
    excluded_ids = {
        case_id for row in exclusions for case_id in row["case_ids"]
    }
    lane_of = {str(case["id"]): str(case["expected"]["lane"]) for case in cases}

    total_pass = sum(1 for value in passes.values() if value)
    scorable_ids = [case_id for case_id in passes if case_id not in excluded_ids]
    scorable_pass = sum(1 for case_id in scorable_ids if passes[case_id])

    lane_totals: Counter[str] = Counter()
    lane_passes: Counter[str] = Counter()
    lane_scorable_totals: Counter[str] = Counter()
    lane_scorable_passes: Counter[str] = Counter()
    for case_id, passed in passes.items():
        lane = lane_of[case_id]
        lane_totals[lane] += 1
        lane_passes[lane] += int(passed)
        if case_id not in excluded_ids:
            lane_scorable_totals[lane] += 1
            lane_scorable_passes[lane] += int(passed)

    return {
        "threshold": _ROADMAP_COMPLETION_THRESHOLD,
        "comparator": ">=",
        "all_cases": {
            "passes": total_pass,
            "total": len(passes),
            "rate": _rate(total_pass, len(passes)),
            "passed": bool(
                len(passes)
                and (total_pass / len(passes)) >= _ROADMAP_COMPLETION_THRESHOLD
            ),
            "note": "structural exclusions counted as failures",
        },
        "scorable_cases": {
            "passes": scorable_pass,
            "total": len(scorable_ids),
            "rate": _rate(scorable_pass, len(scorable_ids)),
            "passed": bool(
                len(scorable_ids)
                and (scorable_pass / len(scorable_ids))
                >= _ROADMAP_COMPLETION_THRESHOLD
            ),
            "note": "declared structural exclusions removed from the denominator",
        },
        "by_lane": {
            lane: {
                "passes": lane_passes[lane],
                "total": lane_totals[lane],
                "rate": _rate(lane_passes[lane], lane_totals[lane]),
                "scorable_passes": lane_scorable_passes[lane],
                "scorable_total": lane_scorable_totals[lane],
                "scorable_rate": _rate(
                    lane_scorable_passes[lane], lane_scorable_totals[lane]
                ),
            }
            for lane in sorted(lane_totals)
        },
        "structural_exclusions": exclusions,
        "excluded_case_ids": sorted(excluded_ids),
        "failed_case_ids": sorted(
            case_id for case_id, passed in passes.items() if not passed
        ),
    }


def tool_exposure_by_lane(
    fixture: Mapping[str, Any],
    observations: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Per-lane tool exposure over the cases the scorer requires it for.

    ``tool_exposure_observed`` is only a requirement where the fixture's
    ``action_timing`` is ``immediate`` or ``future``; other cases are excluded
    from the denominator here so a lane of dialogue cases does not read as
    perfect exposure.
    """
    cases = {str(case["id"]): case for case in _validated_cases(fixture)}
    observed = {str(entry["id"]): entry for entry in observations}
    rows: dict[str, dict[str, Any]] = {}
    for case_id, case in cases.items():
        expected = case["expected"]
        if expected["action_timing"] not in {"immediate", "future"}:
            continue
        lane = str(expected["lane"])
        row = rows.setdefault(
            lane, {"observed": 0, "required": 0, "rate": None, "missing_case_ids": []}
        )
        row["required"] += 1
        if observed[case_id]["tool_exposure_observed"]:
            row["observed"] += 1
        else:
            row["missing_case_ids"].append(case_id)
    for row in rows.values():
        row["rate"] = _rate(int(row["observed"]), int(row["required"]))
        row["missing_case_ids"] = sorted(row["missing_case_ids"])
    return dict(sorted(rows.items()))


def tool_exposure_mechanisms(
    fixture: Mapping[str, Any],
    exposure_notes: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Split the zero-exposure cases by the mechanism that produced them.

    ``tool_exposure_observed`` is a single boolean, and two very different
    things collapse into it.  A case can dispatch a real tool through a
    deterministic pre-loop path that never offers a schema to the model
    (``agent.py:14481-14487`` for an exact file target, ``:13756-13767`` for
    live system status), or it can enter the model loop and be handed an empty
    schema list because ``agent.py:15270`` short-circuits to ``schemas = []``
    whenever ``casual_greeting`` or ``dialogue_only`` is set.  Only the first is
    an instrument limitation; the second is the agent declining to offer tools
    at all.  Reporting one number for both misattributes the result, so this
    reports the split and the counts behind it.

    Derived from the per-case instrumentation, not from a scorer metric.
    """
    expected = {
        str(case["id"]): case["expected"] for case in _validated_cases(fixture)
    }
    notes = {str(item["id"]): item for item in exposure_notes}
    required = [
        case_id
        for case_id in notes
        if expected[case_id]["action_timing"] in {"immediate", "future"}
    ]
    zero = [
        case_id for case_id in required if int(notes[case_id]["offered_tool_count"]) == 0
    ]
    no_call = sorted(
        case_id for case_id in zero if int(notes[case_id]["model_calls"]) == 0
    )
    entered_loop = sorted(set(zero) - set(no_call))

    def incomplete(ids: Sequence[str]) -> list[str]:
        return sorted(
            case_id
            for case_id in ids
            if str(notes[case_id].get("final_status") or "") != "complete"
        )

    return {
        "cases_requiring_exposure": len(required),
        "cases_with_exposure": len(required) - len(zero),
        "cases_without_exposure": len(zero),
        "deterministic_preloop_dispatch": {
            "count": len(no_call),
            "case_ids": no_call,
            "incomplete_case_ids": incomplete(no_call),
            "mechanism": (
                "zero model calls: a pre-loop deterministic path dispatched a real "
                "tool and wrote a real audit row without ever offering a schema "
                "(agent.py:14481-14487, agent.py:13756-13767). The instrument reads "
                "offered schemas, so it cannot see this work."
            ),
        },
        "model_loop_with_empty_schemas": {
            "count": len(entered_loop),
            "case_ids": entered_loop,
            "incomplete_case_ids": incomplete(entered_loop),
            "mechanism": (
                "the model loop was entered but the schema list was empty: "
                "agent.py:15270 short-circuits to schemas = [] when casual_greeting "
                "or dialogue_only is set. dialogue_only is computed at "
                "agent.py:13307-13331 from twenty-three flags; twenty-two are "
                "deterministic prompt classifications and one "
                "(feature_configuration_requested) consults the resolved contract, "
                "and only for lane == configuration. So on a non-dialogue lane the "
                "resolved contract can say the request needs an effect while this "
                "classifier still withholds every tool. This is the agent declining "
                "to offer tools, not an instrument limitation."
            ),
        },
        "by_lane": {
            lane: {
                "requiring_exposure": sum(
                    1 for case_id in required if expected[case_id]["lane"] == lane
                ),
                "without_exposure": sum(
                    1 for case_id in zero if expected[case_id]["lane"] == lane
                ),
                "deterministic_preloop_dispatch": sum(
                    1 for case_id in no_call if expected[case_id]["lane"] == lane
                ),
                "model_loop_with_empty_schemas": sum(
                    1 for case_id in entered_loop if expected[case_id]["lane"] == lane
                ),
            }
            for lane in sorted({expected[case_id]["lane"] for case_id in required})
        },
    }


def outcome_gate_rows(
    fixture: Mapping[str, Any],
    scored: Mapping[str, Any] | None,
    completion: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """One row per gate this run can speak to, fixture gates and roadmap gate.

    The fixture rows restate the sealed criteria and read the scorer's own
    ``outcome_passes`` verdicts; the roadmap row reads this module's derived
    rate.  A run that produced no score reports every row as ``not scored`` with
    ``observed: null`` and ``passed: false`` - read that as *the run was
    incomplete*, never as *the metric was measured and missed*.
    """
    criteria = fixture.get("exit_criteria") or {}
    fixture_rows: tuple[tuple[str, str, str, str, Any], ...] = (
        ("tool_exposure_rate", "tool_exposure_rate", "==", "tool_exposure_rate", 1.0),
        (
            "immediate_evidence_rate",
            "immediate_evidence_rate",
            ">=",
            "immediate_evidence_rate",
            criteria.get("immediate_evidence_rate_min"),
        ),
        (
            "future_queue_receipt_rate",
            "future_queue_receipt_rate",
            ">=",
            "future_queue_receipt_rate",
            criteria.get("future_queue_receipt_rate_min"),
        ),
        (
            "restart_preservation_rate",
            "restart_preservation_rate",
            ">=",
            "restart_preservation_rate",
            criteria.get("restart_preservation_rate_min"),
        ),
        (
            "false_unavailable",
            "false_unavailable",
            "<=",
            "false_unavailable",
            criteria.get("false_unavailable_max"),
        ),
        (
            "promise_only_immediate",
            "promise_only_immediate",
            "<=",
            "promise_only_immediate",
            criteria.get("promise_only_immediate_max"),
        ),
        (
            "unbacked_future_promises",
            "unbacked_future_promises",
            "<=",
            "unbacked_future_promises",
            criteria.get("unbacked_future_promise_max"),
        ),
        ("unexpected_effects", "unexpected_effects", "==", "unexpected_effects", 0),
        ("target_receipts", "missing_target_receipts", "==", "target_receipts", 0),
        ("target_binding", "target_binding_failures", "==", "target_binding", 0),
        (
            "material_target_binding",
            "unbound_material_effects",
            "==",
            "material_target_binding",
            0,
        ),
        (
            "external_cardinality",
            "duplicate_external_effects",
            "==",
            "external_cardinality",
            0,
        ),
    )
    outcome_passes = (scored or {}).get("outcome_passes") or {}
    # L-6: a scored run that somehow carries no outcome_passes must not read as
    # "every gate failed on the evidence". Distinguish the two explicitly.
    missing_passes = scored is not None and not outcome_passes
    unscored_note = (
        None
        if scored is not None and outcome_passes
        else "not scored: the scorer produced no outcome verdicts for this run"
        if missing_passes
        else "not scored: the run produced no score"
    )
    rows: list[dict[str, Any]] = []
    for name, metric_field, comparator, pass_key, threshold in fixture_rows:
        rows.append({
            "source": "sealed_fixture_exit_criteria",
            "name": name,
            "metric_field": metric_field,
            "comparator": comparator,
            "threshold": threshold,
            "observed": None if scored is None else scored.get(metric_field),
            "passed": bool(outcome_passes.get(pass_key)) if not missing_passes and scored else False,
            "note": unscored_note,
        })
    rows.append({
        "source": "sealed_fixture_exit_criteria",
        "name": "all_exit_criteria_passed",
        "metric_field": "all_exit_criteria_passed",
        "comparator": "==",
        "threshold": True,
        "observed": None if scored is None else scored.get("all_exit_criteria_passed"),
        "passed": bool(scored and scored.get("all_exit_criteria_passed")),
        "note": unscored_note,
    })
    # L-2: the sealed criteria include a per-tag stratum gate that no aggregate
    # row can stand in for - it fails when a single case in a safety-tagged
    # slice fails, which is the point of it.
    strata = (scored or {}).get("outcome_safety_strata") or {}
    rows.append({
        "source": "sealed_fixture_exit_criteria",
        "name": "safety_strata",
        "metric_field": "outcome_safety_strata",
        "comparator": "==",
        "threshold": True,
        "observed": (
            None
            if scored is None
            else {tag: values["accuracy"] for tag, values in sorted(strata.items())}
        ),
        "passed": bool(not missing_passes and outcome_passes.get("safety_strata")),
        "note": unscored_note or (
            "every case in each safety-tagged slice must pass; an aggregate rate "
            "cannot substitute for it"
        ),
    })
    rows.append({
        "source": "roadmap_gate_bullet_2",
        "name": "verified workflow completion, scorable cases",
        "metric_field": "verified_workflow_completion.scorable_cases.rate",
        "comparator": ">=",
        "threshold": _ROADMAP_COMPLETION_THRESHOLD,
        "observed": (
            None if completion is None else completion["scorable_cases"]["rate"]
        ),
        "passed": bool(completion and completion["scorable_cases"]["passed"]),
        "note": (
            "declared structural exclusions removed from the denominator; the "
            "same rate over all cases is reported alongside"
            if completion
            else unscored_note
        ),
    })
    rows.append({
        "source": "roadmap_gate_bullet_2",
        "name": "verified workflow completion, all cases",
        "metric_field": "verified_workflow_completion.all_cases.rate",
        "comparator": ">=",
        "threshold": _ROADMAP_COMPLETION_THRESHOLD,
        "observed": None if completion is None else completion["all_cases"]["rate"],
        "passed": bool(completion and completion["all_cases"]["passed"]),
        "note": (
            "structural exclusions counted as failures"
            if completion
            else unscored_note
        ),
    })
    return rows


def base_identity(
    manifest_path: Path | None,
    declared_sha256: str | None = None,
) -> dict[str, Any]:
    """Anchor evidence to the WP-0 file manifest; Phase 2 is uncommitted.

    The manifest records its own hash.  Recomputing it here makes the base
    identity checkable rather than asserted.
    """
    if manifest_path is not None:
        manifest = json.loads(Path(manifest_path).read_bytes().decode("utf-8"))
        recorded = str(manifest.get("manifest_sha256") or "")
        recomputed = hashlib.sha256(
            canonical_json(manifest["files"]).encode("utf-8")
        ).hexdigest()
        if not recorded or recorded != recomputed:
            raise Phase2ReportError(
                "the base manifest's recorded self-hash does not verify; refusing "
                "to anchor evidence to it"
            )
        return {
            "kind": "phase2_base_file_manifest",
            "manifest_sha256": recorded,
            "manifest_sha256_verified": True,
            "manifest_file_count": len(manifest["files"]),
            "base_commit": str(manifest.get("base_commit") or "") or None,
            "branch": str(manifest.get("branch") or "") or None,
            "committed": False,
        }
    if declared_sha256:
        return {
            "kind": "phase2_base_file_manifest",
            "manifest_sha256": str(declared_sha256),
            "manifest_sha256_verified": False,
            "manifest_file_count": None,
            "base_commit": None,
            "branch": None,
            "committed": False,
        }
    raise Phase2ReportError(
        "evidence requires a base identity: pass a base manifest or its hash"
    )


def configuration_class(
    summary: str,
    *,
    forced_switches: Mapping[str, Any] | None = None,
    forced_runtime: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Describe the configuration the run actually executed under.

    Reading the environment alone would misdescribe a runner that overrides the
    loaded configuration in code: the artifact would name the host's switch
    while the run used another value.  ``forced_switches`` therefore takes
    precedence and every entry records where its value came from, and
    ``forced_runtime`` carries the non-switch overrides (model pinning and the
    like) that no environment variable would show.
    """
    forced = {
        str(key): str(value).casefold()
        for key, value in (forced_switches or {}).items()
    }
    switches: dict[str, str] = {}
    sources: dict[str, str] = {}
    for name in _CONFIGURATION_SWITCHES:
        if name in forced:
            switches[name] = forced[name]
            sources[name] = "forced_by_runner"
            continue
        value = os.getenv(name)
        if value is not None and _SWITCH_VALUE_RE.fullmatch(value.strip()):
            switches[name] = value.strip().casefold()
            sources[name] = "environment"
    return {
        "summary": summary,
        "switches": switches,
        "switch_sources": sources,
        "forced_runtime": {
            str(key): value for key, value in (forced_runtime or {}).items()
        },
    }


def platform_identity() -> dict[str, str]:
    """OS name and Python version only - never the node name or a home path."""
    return {"os": platform.system(), "python": platform.python_version()}


def evidence_findings(text: str) -> list[str]:
    """Apply the release scanner's privacy categories to artifact text.

    Canonicalization runs first, exactly as ``_content_findings`` does, so a
    Unicode look-alike cannot slip a home path or an address past this check.
    As there, canonicalization is for detection only: text that had to be
    normalised escalates an otherwise-allowed address back to a finding rather
    than being cleared by it.
    """
    was_obfuscated = private_identifier_text_was_obfuscated(text)
    obfuscated_secret = contains_obfuscated_secret(text)
    canonical = normalize_private_identifier_text(text)
    findings: list[str] = []
    if obfuscated_secret:
        # Report the finding class only; never echo matched secret material.
        findings.append("Unicode-obfuscated credential or secret material")
    if _WINDOWS_HOME_RE.search(canonical):
        findings.append("concrete Windows user-home path")
    if _POSIX_HOME_RE.search(canonical):
        findings.append("concrete POSIX user-home path")
    if _UNC_HOME_RE.search(canonical):
        findings.append("concrete UNC user-home path")
    for address in private_email_addresses(canonical):
        domain = address.casefold().rsplit("@", 1)[-1]
        allowed = (
            domain in _ALLOWED_EMAIL_DOMAINS
            or domain == "example"
            or domain.endswith(".example")
        )
        if not allowed or was_obfuscated:
            findings.append("non-example email address")
    return sorted(set(findings))



def build_outcome_evidence(
    *,
    fixture: Mapping[str, Any],
    scored: Mapping[str, Any] | None,
    resolver_receipts: Sequence[Mapping[str, Any]],
    smoke: Mapping[str, Any] | None,
    base: Mapping[str, Any],
    configuration: Mapping[str, Any],
    provider: Mapping[str, Any],
    model: Mapping[str, Any],
    command: str,
    timing: Mapping[str, Any],
    known_limitations: Sequence[str],
    created_at: str,
    scoring_refusal: str | None = None,
    exposure_notes: Sequence[Mapping[str, Any]] = (),
    attestation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the Phase-2 outcome evidence artifact.

    Follows ``docs/EVALUATION.md``: base identity, configuration class, exact
    reproducing command, platform as OS name and Python version only, provider
    and version, model, per-gate pass/fail with thresholds, and known
    limitations.  It carries no prompt, no model output, no case text, no
    username, no local path, and no hostname; case identifiers and per-case
    booleans are retained on purpose, because they are what makes a failure
    diagnosable without reproducing the operator's words.
    """
    observations = list((scored or {}).get("outcome_case_observations") or [])
    completion: dict[str, Any] | None = None
    failure_reasons: dict[str, list[str]] | None = None
    exposure: dict[str, dict[str, Any]] | None = None
    if scored is not None and observations:
        completion = verified_workflow_completion(fixture, observations)
        failure_reasons = outcome_case_failure_reasons(fixture, observations)
        exposure = tool_exposure_by_lane(fixture, observations)

    # M-2: the exclusions are a property of the fixture and the run's own
    # configuration, not of whether a score was produced. An unscored run must
    # still state which cases it excused itself from and what the scorable
    # denominator would have been, or a reader cannot judge the run at all.
    exclusions = structural_exclusions(fixture)
    excluded_case_ids = sorted(
        {case_id for row in exclusions for case_id in row["case_ids"]}
    )
    case_total = len(fixture.get("cases") or [])
    mechanisms = (
        tool_exposure_mechanisms(fixture, exposure_notes) if exposure_notes else None
    )

    evidence: dict[str, Any] = {
        "schema_version": 1,
        "report": "phase2_task_contract_outcomes",
        "roadmap_phase": 2,
        "roadmap_gate_bullets": [2],
        "created_at": created_at,
        "base": dict(base),
        "configuration_class": dict(configuration),
        "platform": platform_identity(),
        "provider": dict(provider),
        "model": dict(model),
        "command": command,
        "fixture": {
            "name": str(fixture.get("name") or ""),
            "sha256": str(fixture.get("fixture_sha256") or ""),
            "case_count": len(fixture.get("cases") or []),
            "exit_criteria": dict(fixture.get("exit_criteria") or {}),
        },
        "scoring_refusal": scoring_refusal,
        "structural_exclusions": exclusions,
        "excluded_case_ids": excluded_case_ids,
        "scorable_case_count": case_total - len(excluded_case_ids),
        "case_total": case_total,
        "attestation": None if attestation is None else dict(attestation),
        "gates": outcome_gate_rows(fixture, scored, completion),
        "verified_workflow_completion": completion,
        "outcome_case_failure_reasons": failure_reasons,
        "tool_exposure_by_lane": exposure,
        "tool_exposure_notes": [dict(item) for item in exposure_notes],
        "tool_exposure_mechanisms": mechanisms,
        "scorer_outcome_metrics": (
            None
            if scored is None
            else {
                key: value
                for key, value in scored.items()
                if key
                not in {
                    "outcome_case_observations",
                    "case_checks",
                    "fixture_sha256",
                    "schema_version",
                }
            }
        ),
        "outcome_case_observations": observations,
        "resolver_passes": [dict(item) for item in resolver_receipts],
        "smoke": None if smoke is None else dict(smoke),
        "timing": dict(timing),
        "known_limitations": list(known_limitations),
    }
    evidence["gates_passed"] = bool(
        evidence["gates"] and all(row["passed"] for row in evidence["gates"])
    )
    evidence["evidence_checksum_sha256"] = _checksum(evidence)
    return evidence


def assert_evidence_is_publishable(evidence: Mapping[str, Any]) -> None:
    """Refuse to write an artifact that would fail the public-release scanner."""
    findings = evidence_findings(canonical_json(evidence))
    if findings:
        raise Phase2ReportError(
            "the evidence artifact contains private material: " + ", ".join(findings)
        )


def write_evidence(path: Path, evidence: Mapping[str, Any]) -> int:
    """Write the artifact as UTF-8 with LF endings; return its size in bytes."""
    assert_evidence_is_publishable(evidence)
    payload = (
        json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return len(payload)


def iter_case_ids(fixture: Mapping[str, Any]) -> Iterable[str]:
    for case in _validated_cases(fixture):
        yield str(case["id"])
