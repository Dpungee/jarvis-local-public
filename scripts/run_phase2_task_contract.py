#!/usr/bin/env python3
"""Record roadmap Phase 2 routing evidence from the frozen TaskContract holdout.

The run resolves all 66 sealed holdout cases against one exact, attesting
``provider:model`` reference, one case per fresh conversation, with no tools,
no memory, no workspace, and no fallback.  It then writes a single evidence
artifact holding the prompt-free receipt, the scorer's own numbers, and an
explicit pass/fail against each roadmap gate threshold it can speak to:

  gate 1  broad routing                        route_accuracy          >= 0.90
          (and every lane in route_by_lane     lane accuracy           >= 0.90)
  gate 3  material-ambiguity clarification     ambiguity_recall        >= 0.90
  gate 4  unnecessary clarification            specified_false_positive_rate
                                                                       <= 0.10

Nothing here modifies, rescores, or reinterprets the sealed fixture or the
scorer.  The gate rows read scorer fields; the fixture's own exit criteria are
reported alongside them, unedited.

The artifact follows ``docs/EVALUATION.md``: base identity, configuration
class, exact command, platform as OS name and Python version only, provider
and version, model, pass/fail, and known limitations.  It carries no prompt,
no model output, no case text, no username, no local path, and no hostname.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from jarvis.task_contract_benchmark import (  # noqa: E402
    BenchmarkProviderError,
    LiveTaskContractRun,
    _exact_model_reference,
    build_exact_model_benchmark_client,
    run_live_task_contract_benchmark,
)
from jarvis.task_contract_eval import load_task_contract_holdout  # noqa: E402


DEFAULT_FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "task_contract_holdout_v2.json"
DEFAULT_MODEL = "ollama:qwen3.5:9b"
EVIDENCE_DIR = PROJECT_ROOT / "docs" / "evidence"

# Each row names the roadmap gate bullet, the scorer field it is read from, and
# the roadmap's verbatim threshold.  The thresholds are the roadmap's, restated
# here so the artifact records what was compared; they are not read from the
# fixture, whose criteria are separately reported and in places stricter.
GATES: tuple[dict[str, Any], ...] = (
    {
        "gate": 1,
        "name": "broad routing accuracy",
        "metric_field": "route_accuracy",
        "comparator": ">=",
        "threshold": 0.90,
    },
    {
        "gate": 1,
        "name": "broad routing accuracy, every lane",
        "metric_field": "route_by_lane",
        "comparator": ">=",
        "threshold": 0.90,
    },
    {
        "gate": 3,
        "name": "material-ambiguity clarification recall",
        "metric_field": "ambiguity_recall",
        "comparator": ">=",
        "threshold": 0.90,
    },
    {
        "gate": 4,
        "name": "unnecessary clarification rate",
        "metric_field": "specified_false_positive_rate",
        "comparator": "<=",
        "threshold": 0.10,
    },
)

# Switches whose observed value describes the configuration class.  Only names
# on this list are read, and only values that look like a plain switch token
# are recorded, so no credential or path can reach the artifact through here.
_CONFIGURATION_SWITCHES = (
    "JARVIS_CLOUD_ENABLED",
    "JARVIS_OLLAMA_ENABLED",
    "JARVIS_EXTERNAL_ACCESS",
    "JARVIS_EXECUTION_MODE",
    "JARVIS_COMPUTER_ACCESS",
)
_SWITCH_VALUE_RE = re.compile(r"\A[A-Za-z0-9_.:-]{1,32}\Z")

KNOWN_LIMITATIONS = (
    "One run, one model, one host. Benchmark numbers are historical "
    "observations, not permanent guarantees, and must be refreshed after any "
    "code, model, provider, or hardware change.",
    "Resolver-level evidence only. This run scores the contract the resolver "
    "produced; it does not execute any workflow and says nothing about "
    "end-to-end completion.",
    "Gate 3 has a denominator of 11 material-ambiguity cases in this fixture, "
    "so a single miss lands at 0.909 and two miss the 0.90 threshold. The "
    "denominator is too thin to carry the gate on its own.",
    "The fixture's own exit criteria are in places stricter than the roadmap "
    "gates and are reported separately, unedited. A run can miss a fixture "
    "criterion while clearing the roadmap gate it corresponds to.",
    "Decoding is pinned to temperature 0.0, seed 0, think disabled, and an "
    "8192-token context. Results are not a claim about the model under other "
    "decoding settings.",
    "Runs are not bit-reproducible. Even at temperature 0.0 with a fixed seed, "
    "the local runtime does not guarantee identical output, and a case "
    "observed to resolve on one run can be rejected on the next. A single run "
    "bounds the resolver's behaviour; it does not pin it.",
    "The scorer refuses to score a partial set. If any case fails to yield a "
    "parseable contract, no routing, ambiguity, or false-positive number is "
    "produced at all and every gate row reports no observation. A gate row "
    "reading 'not scored' means the run was incomplete, not that the metric "
    "was measured and missed.",
    "The provider version is the value the provider reported at run time; it "
    "is recorded, not independently verified.",
    "The receipt's per-case model_attestation conflates attestation with "
    "contract acceptance: a case whose response was attested on the wire but "
    "whose contract was then rejected records 'not_observed', and "
    "exact_model_only is therefore false whenever any case is rejected for a "
    "reason unrelated to attestation. Read the attestation result from "
    "model_unattested and model_mismatch, which count only genuine "
    "attestation failures. Separating attestation into its own axis is a "
    "recorded follow-up for the module owner.",
    "A rejected case is not a parse failure. The observed rejections come "
    "from post-parse reconciliation - reconcile_task_contract_continuation "
    "(task_contract.py:1502) and the _validate_consistency check (:877) it "
    "reaches through bind_provided_material_continuation - which run after "
    "parse_task_contract has already accepted the payload.",
)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class BaseIdentityError(RuntimeError):
    """The run cannot be anchored to a verified base, so it records nothing.

    Raised before any provider call and reported by ``main`` the same way every
    other refusal is: a message on stderr and exit status 2.
    """


def _base_identity(manifest_path: Path | None, declared_sha256: str | None) -> dict[str, Any]:
    """Identify the exact tree state this evidence describes.

    Phase 2 is uncommitted, so the base identity is the WP-0 file manifest, not
    a commit. The manifest records its own hash; recomputing it here makes the
    identity checkable rather than merely asserted.
    """
    if manifest_path is not None:
        manifest = json.loads(manifest_path.read_bytes().decode("utf-8"))
        recorded = str(manifest.get("manifest_sha256") or "")
        recomputed = hashlib.sha256(
            _canonical_json(manifest["files"]).encode("utf-8")
        ).hexdigest()
        if not recorded or recorded != recomputed:
            raise BaseIdentityError(
                "the base manifest's recorded self-hash does not verify; refusing "
                "to record evidence against an unidentified base"
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
            "manifest_sha256": declared_sha256,
            "manifest_sha256_verified": False,
            "manifest_file_count": None,
            "base_commit": None,
            "branch": None,
            "committed": False,
        }
    raise BaseIdentityError(
        "evidence requires a base identity: pass --base-manifest or "
        "--base-manifest-sha256"
    )


def _configuration_class() -> dict[str, Any]:
    switches: dict[str, str] = {}
    for name in _CONFIGURATION_SWITCHES:
        raw = os.environ.get(name)
        if raw is None:
            switches[name] = "unset"
        elif _SWITCH_VALUE_RE.fullmatch(raw):
            switches[name] = raw
        else:
            switches[name] = "other"
    return {
        "summary": (
            "local attesting provider only; no tools, no memory, no workspace, "
            "no training, no fallback; one fresh conversation per case"
        ),
        "switches": switches,
    }


def _platform_identity() -> dict[str, str]:
    # OS name and Python version only. Never the node name, never a path.
    return {
        "os": platform.system(),
        "python": platform.python_version(),
    }


def _lane_rows(metrics: dict[str, Any], threshold: float) -> dict[str, Any]:
    rows: dict[str, Any] = {}
    for lane, values in sorted((metrics.get("route_by_lane") or {}).items()):
        accuracy = values.get("accuracy")
        rows[lane] = {
            "correct": values.get("correct"),
            "total": values.get("total"),
            "accuracy": accuracy,
            "passed": bool(accuracy is not None and accuracy >= threshold),
        }
    return rows


def _gate_rows(metrics: dict[str, Any] | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for spec in GATES:
        row = dict(spec)
        if metrics is None:
            row["observed"] = None
            row["passed"] = False
            row["note"] = "not scored: the run did not resolve all cases"
            rows.append(row)
            continue
        if spec["metric_field"] == "route_by_lane":
            lanes = _lane_rows(metrics, float(spec["threshold"]))
            observed = min(
                (values["accuracy"] for values in lanes.values() if values["accuracy"] is not None),
                default=None,
            )
            row["observed"] = observed
            row["observed_is"] = "worst lane accuracy"
            row["per_lane"] = lanes
            row["passed"] = bool(lanes) and all(v["passed"] for v in lanes.values())
        else:
            observed = metrics.get(spec["metric_field"])
            row["observed"] = observed
            if observed is None:
                row["passed"] = False
            elif spec["comparator"] == ">=":
                row["passed"] = bool(observed >= spec["threshold"])
            else:
                row["passed"] = bool(observed <= spec["threshold"])
        rows.append(row)
    return rows


def _rate(numerator: int, denominator: int) -> float | None:
    """A rate, or None when nothing was observed to divide by."""
    if denominator <= 0:
        return None
    return round(numerator / denominator, 6)


def _case_observations(
    receipt: dict[str, Any],
    fixture: dict[str, Any],
    metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Raw per-case counts read straight off the prompt-free receipt.

    These are **not** the scorer's metrics and are not a substitute for them.
    The scorer refuses to score a partial prediction set, and when it does the
    gate rows report no observation at all. This block exists so an incomplete
    run stays diagnosable: which cases the resolver rejected, and how the cases
    that did resolve compared with the fixture's expectations. Read a gate from
    `gates`; read this only to understand a run.

    Every rate here is stated over the cases that actually resolved, with the
    denominator named alongside it. A case the resolver rejected produced no
    clarification decision at all, so counting it as a correct one would
    flatter the run and counting it as a wrong one would invent a verdict the
    model never gave.

    Nothing here is derived from a metric the scorer declined to compute; every
    number is a count of receipt booleans.
    """
    expected = {str(case["id"]): case["expected"] for case in fixture["cases"]}
    cases = {str(case["id"]): case for case in receipt["cases"]}

    unresolved: dict[str, list[str]] = {}
    failed_checks: dict[str, list[str]] = {}
    by_lane: dict[str, dict[str, int]] = {}
    ambiguous_missed: list[str] = []
    specified_unnecessary: list[str] = []
    ambiguous_unresolved: list[str] = []
    specified_unresolved: list[str] = []
    ambiguous_correct = 0
    specified_correct = 0
    ambiguous_total = 0
    specified_total = 0

    for case_id, case in cases.items():
        case_expected = expected.get(case_id, {})
        lane = str(case_expected.get("lane") or "unknown")
        counts = by_lane.setdefault(
            lane, {"total": 0, "resolved": 0, "lane_correct": 0}
        )
        counts["total"] += 1
        wants_clarification = case_expected.get("clarification") is True
        if wants_clarification:
            ambiguous_total += 1
        else:
            specified_total += 1
        if case["status"] != "resolved":
            unresolved.setdefault(case["status"], []).append(case_id)
            # No contract, so no clarification decision to score either way.
            if wants_clarification:
                ambiguous_unresolved.append(case_id)
            else:
                specified_unresolved.append(case_id)
            continue
        counts["resolved"] += 1
        checks = case.get("checks") or {}
        if checks.get("lane") is True:
            counts["lane_correct"] += 1
        for name, passed in checks.items():
            if passed is False:
                failed_checks.setdefault(name, []).append(case_id)
        if checks.get("clarification") is True:
            if wants_clarification:
                ambiguous_correct += 1
            else:
                specified_correct += 1
        elif wants_clarification:
            ambiguous_missed.append(case_id)
        else:
            specified_unnecessary.append(case_id)

    return {
        "note": (
            "raw receipt counts, not scorer metrics; "
            + (
                "the scorer also produced metrics for this run - read gates "
                "and scorer_metrics for the graded numbers"
                if metrics is not None
                else "the scorer produced no metric for this run"
            )
        ),
        "unresolved_case_ids_by_status": {
            status: sorted(ids) for status, ids in sorted(unresolved.items())
        },
        "failed_check_case_ids": {
            name: sorted(ids) for name, ids in sorted(failed_checks.items())
        },
        "failed_check_counts": {
            name: len(ids) for name, ids in sorted(failed_checks.items())
        },
        "by_expected_lane": {
            lane: dict(counts) for lane, counts in sorted(by_lane.items())
        },
        "clarification": {
            "denominator": (
                "rates are over resolved cases only; fixture totals and "
                "unresolved counts are given so the gap is explicit"
            ),
            "ambiguous_total": ambiguous_total,
            "ambiguous_resolved": ambiguous_total - len(ambiguous_unresolved),
            "ambiguous_unresolved": len(ambiguous_unresolved),
            "ambiguous_unresolved_case_ids": sorted(ambiguous_unresolved),
            "ambiguous_correct": ambiguous_correct,
            "ambiguous_missed_case_ids": sorted(ambiguous_missed),
            "ambiguity_recall_over_resolved": _rate(
                ambiguous_correct, ambiguous_total - len(ambiguous_unresolved)
            ),
            "specified_total": specified_total,
            "specified_resolved": specified_total - len(specified_unresolved),
            "specified_unresolved": len(specified_unresolved),
            "specified_unresolved_case_ids": sorted(specified_unresolved),
            "specified_correct": specified_correct,
            "specified_unnecessary": len(specified_unnecessary),
            "specified_unnecessary_case_ids": sorted(specified_unnecessary),
            "unnecessary_clarification_rate_over_resolved": _rate(
                len(specified_unnecessary), specified_total - len(specified_unresolved)
            ),
        },
    }


def _command_line(args: argparse.Namespace) -> str:
    """Reconstruct the reproducing command without echoing any local path.

    Every flag that was actually passed appears here.  Defaults are omitted so
    the recorded line reproduces the run verbatim; a path a caller supplied is
    replaced by a placeholder, because the evidence policy forbids recording
    local paths and a path is never needed to reproduce anything.
    """
    parts = ["python scripts/run_phase2_task_contract.py", f"--model {args.model}"]
    if args.fixture.resolve() != DEFAULT_FIXTURE.resolve():
        parts.append("--fixture <fixture>")
    if args.out is not None:
        parts.append("--out <artifact path>")
    if args.base_manifest is not None:
        parts.append("--base-manifest <phase 2 base manifest>")
    if args.base_manifest_sha256 is not None:
        parts.append(f"--base-manifest-sha256 {args.base_manifest_sha256}")
    if args.base_url is not None:
        parts.append("--base-url <provider base url>")
    parts.append("--allow-live")
    return " ".join(parts)


def build_evidence(
    *,
    receipt: dict[str, Any],
    fixture: dict[str, Any],
    args: argparse.Namespace,
    provider: str,
    provider_version: str | None,
    provider_model: str,
    base_identity: dict[str, Any],
    wall_seconds: float,
) -> dict[str, Any]:
    metrics = receipt["summary"]["contract_metrics"]
    gates = _gate_rows(metrics)
    summary = receipt["summary"]
    return {
        "schema_version": 1,
        "report": "phase2_task_contract_resolver",
        "roadmap_phase": 2,
        "roadmap_gate_bullets": [1, 3, 4],
        "created_at": receipt["created_at"],
        "base": base_identity,
        "configuration_class": _configuration_class(),
        "command": _command_line(args),
        "platform": _platform_identity(),
        "provider": {"name": provider, "version": provider_version},
        "model": {"requested": receipt["requested_model"], "served": provider_model},
        "fixture": {
            "name": str(fixture.get("name") or ""),
            "sha256": receipt["fixture_sha256"],
            "case_count": summary["case_count"],
            "exit_criteria": dict(fixture["exit_criteria"]),
        },
        "gates": gates,
        "gates_passed": all(row["passed"] for row in gates),
        "per_lane": _lane_rows(metrics or {}, 0.90),
        "case_observations": _case_observations(receipt, fixture, metrics),
        "scorer_metrics": metrics,
        "fixture_exit_criteria_result": (
            {
                "passes": metrics.get("passes"),
                "all_contract_exit_criteria_passed": metrics.get(
                    "all_contract_exit_criteria_passed"
                ),
            }
            if metrics is not None
            else None
        ),
        "timing": {
            "p50_latency_ms": summary["p50_latency_ms"],
            "p95_latency_ms": summary["p95_latency_ms"],
            "total_latency_ms": summary["total_latency_ms"],
            "wall_seconds": round(wall_seconds, 3),
        },
        "attestation": {
            "exact_model_only": receipt["exact_model_only"],
            "model_attestation_required": receipt["model_attestation_required"],
            "provider_model_attestation": receipt["provider_model_attestation"],
            "resolved": summary["resolved"],
            "contract_rejected": summary["contract_rejected"],
            "provider_error": summary["provider_error"],
            "model_mismatch": summary["model_mismatch"],
            "model_unattested": summary["model_unattested"],
        },
        "known_limitations": list(KNOWN_LIMITATIONS),
        "receipt": receipt,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run the frozen TaskContract resolver holdout live and record "
            "roadmap Phase 2 gate evidence."
        )
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help="exact provider:model reference (default: %(default)s)",
    )
    parser.add_argument(
        "--fixture",
        type=Path,
        default=DEFAULT_FIXTURE,
        help="sealed holdout fixture to run",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="evidence artifact path (default: docs/evidence/phase2_task_contract_resolver_<provider>_<hash8>.json)",
    )
    parser.add_argument(
        "--base-manifest",
        type=Path,
        default=None,
        help="Phase 2 base file manifest; its recorded self-hash is verified",
    )
    parser.add_argument(
        "--base-manifest-sha256",
        default=None,
        help="declare the base manifest hash without reading the manifest file",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="provider base URL for a locally served model",
    )
    parser.add_argument(
        "--allow-live",
        action="store_true",
        help="required; without it no provider call is made",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite an existing artifact instead of refusing",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.allow_live:
        print(
            "refusing to run: this benchmark spends real provider calls and "
            "requires --allow-live",
            file=sys.stderr,
        )
        return 2

    try:
        base_identity = _base_identity(args.base_manifest, args.base_manifest_sha256)
    except BaseIdentityError as error:
        print(f"refusing to run: {error}", file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError) as error:
        print(f"refusing to run: unreadable base manifest ({type(error).__name__})", file=sys.stderr)
        return 2
    hash8 = str(base_identity["manifest_sha256"])[:8]

    # Resolve the destination without constructing a provider: even provider
    # discovery performs network I/O and must follow the overwrite preflight.
    try:
        _requested, provider, _model = _exact_model_reference(args.model)
    except (BenchmarkProviderError, ValueError) as error:
        print(f"refusing to run: {error}", file=sys.stderr)
        return 2
    out_path = args.out or (
        EVIDENCE_DIR / f"phase2_task_contract_resolver_{provider}_{hash8}.json"
    )
    if out_path.exists() and not args.force:
        print(
            f"refusing to run: {out_path.name} already exists; recorded evidence "
            "is not overwritten by accident - pass --force to replace it, or "
            "--out to write elsewhere",
            file=sys.stderr,
        )
        return 2

    try:
        adapter = build_exact_model_benchmark_client(
            args.model,
            base_url=args.base_url,
        )
    except BenchmarkProviderError as error:
        print(f"refusing to run: {error}", file=sys.stderr)
        return 2
    except ValueError as error:
        print(f"refusing to run: {error}", file=sys.stderr)
        return 2

    # A reference without a provider prefix resolves silently - a bare
    # "qwen3.5:9b" becomes ollama - and the default artifact name is derived
    # from the provider that resolution picked. Say out loud what was resolved,
    # before any call is spent, so a surprise never lands in a committed file.
    print(
        f"resolved provider: {adapter.provider}  provider_model: "
        f"{adapter.provider_model}  (from {adapter.requested_model})",
        file=sys.stderr,
    )

    fixture = load_task_contract_holdout(args.fixture)
    started = time.perf_counter()
    run = run_live_task_contract_benchmark(
        args.fixture,
        client=adapter,
        model=adapter.requested_model,
        allow_live=True,
        return_predictions=True,
    )
    wall_seconds = time.perf_counter() - started
    assert isinstance(run, LiveTaskContractRun)
    # Predictions quote operator text.  They exist for an in-process caller and
    # are deliberately dropped here: only the prompt-free receipt is written.
    receipt = run.receipt

    evidence = build_evidence(
        receipt=receipt,
        fixture=fixture,
        args=args,
        provider=adapter.provider,
        provider_version=adapter.provider_version,
        provider_model=adapter.provider_model,
        base_identity=base_identity,
        wall_seconds=wall_seconds,
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(
        (json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        )
    )

    summary = receipt["summary"]
    print(f"cases            : {summary['case_count']}")
    print(f"resolved         : {summary['resolved']}")
    print(f"contract_rejected: {summary['contract_rejected']}")
    print(f"provider_error   : {summary['provider_error']}")
    print(f"model_unattested : {summary['model_unattested']}")
    print(f"exact_model_only : {receipt['exact_model_only']}")
    for row in evidence["gates"]:
        status = "PASS" if row["passed"] else "FAIL"
        print(
            f"gate {row['gate']} {status}  {row['name']}: "
            f"{row['observed']} {row['comparator']} {row['threshold']}"
        )
    print(f"wall seconds     : {evidence['timing']['wall_seconds']}")
    try:
        shown = out_path.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        # An artifact written outside the repository has no repo-relative name,
        # and its absolute path is exactly what evidence output must not print.
        shown = out_path.name
    print(f"artifact         : {shown}")
    return 0 if evidence["gates_passed"] else 1


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
