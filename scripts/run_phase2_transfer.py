#!/usr/bin/env python3
"""Run the Phase 2 WP-9 cross-domain transfer lift benchmark with a model in the loop.

Arms are dispatched per held-out case and differ in exactly one thing: which
advisory block the system prompt carries. Positives run control, placebo and
treatment; negative-transfer cases run control and a forced harmful arm; safety
controls run control and treatment. This script never enables ``advise``, never
touches a sealed fixture, and never writes a prompt, a reply or a local path
into its evidence artifact.

Typical use:

    python scripts/run_phase2_transfer.py --dry-run
    python scripts/run_phase2_transfer.py --manifest <wp0 manifest> --offload

Any result recorded here is held-out benchmark lift, not production causal
activation evidence.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from jarvis.transfer_lift_eval import (  # noqa: E402 - path bootstrap above
    BENCHMARK_NAME,
    HOLDOUT_NAME,
    LIVE_CONTEXT_LENGTH,
    LIVE_KEEP_ALIVE,
    LIVE_MAX_OUTPUT_TOKENS,
    LIVE_MODEL,
    LIVE_PROVIDER,
    LIVE_SEED,
    LIVE_TEMPERATURE,
    TransferLiftFixtureError,
    TransferLiftRunError,
    derived_report_fields,
    evidence_document,
    fixture_sha256,
    load_transfer_lift_fixture,
    ollama_dispatcher,
    plan_case,
    rescore_report,
    run_transfer_lift_fixture,
)

DEFAULT_FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / HOLDOUT_NAME
DEFAULT_BASE_URL = "http://127.0.0.1:11434"
EVIDENCE_DIR = PROJECT_ROOT / "docs" / "evidence"
TEST_COMMAND = "python scripts/run_phase2_transfer.py"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument(
        "--seal",
        default=None,
        help="expected fixture sha256; the holdout uses the module seal by default",
    )
    parser.add_argument(
        "--allow-unsealed",
        action="store_true",
        help="admit a development fixture (never the holdout); stamped as such",
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--seed", type=int, default=LIVE_SEED)
    parser.add_argument("--temperature", type=float, default=LIVE_TEMPERATURE)
    parser.add_argument("--num-ctx", type=int, default=LIVE_CONTEXT_LENGTH)
    parser.add_argument("--num-predict", type=int, default=LIVE_MAX_OUTPUT_TOKENS)
    parser.add_argument("--keep-alive", default=LIVE_KEEP_ALIVE)
    parser.add_argument("--generation-timeout", type=float, default=300.0)
    parser.add_argument(
        "--null-probe",
        action="store_true",
        help="A/A sham: give every labelled variant arm the control prompt",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="plan every case and assert prompt diff and oracle leak, no dispatch",
    )
    parser.add_argument(
        "--rescore",
        type=Path,
        default=None,
        help="re-score a saved report and print its attestation digest, then exit",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="write the full run report (with case rows) here so it survives "
        "the run and can be re-scored independently",
    )
    parser.add_argument(
        "--regenerate-evidence",
        action="store_true",
        help="rebuild the evidence document from --report without dispatching; "
        "sealed numbers and the attestation are carried over untouched",
    )
    parser.add_argument(
        "--design-power",
        type=float,
        default=None,
        help="externally supplied design power at the observed rates; recorded "
        "with its source and never derived here",
    )
    parser.add_argument("--hash8", default=None)
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument(
        "--offload", action="store_true", help="release the model after the run"
    )
    return parser


def resolve_hash8(args: argparse.Namespace) -> str | None:
    if args.hash8:
        value = str(args.hash8).strip().lower()
        if len(value) != 8 or any(ch not in "0123456789abcdef" for ch in value):
            raise SystemExit("--hash8 must be 8 lowercase hex characters")
        return value
    if args.manifest is not None:
        manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
        digest = str(manifest.get("manifest_sha256") or "")
        if len(digest) < 8:
            raise SystemExit("manifest does not carry a usable manifest_sha256")
        return digest[:8]
    return None


def evidence_path(args: argparse.Namespace) -> Path | None:
    if args.out is not None:
        return Path(args.out).resolve()
    hash8 = resolve_hash8(args)
    if hash8 is None:
        return None
    return (EVIDENCE_DIR / f"phase2_{BENCHMARK_NAME}_{LIVE_PROVIDER}_{hash8}.json").resolve()


def offload_model(base_url: str) -> bool:
    """Release the model from memory. Best effort; never fails the run."""
    payload = json.dumps({"model": LIVE_MODEL, "keep_alive": 0}).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            response.read(4096)
        return True
    except OSError:
        return False


def dry_run(fixture: dict, path: Path) -> int:
    planned = 0
    dispatches = 0
    advisory_failures = 0
    oracle_failures = 0
    empty_harmful = 0
    by_category: dict[str, int] = {}
    advised_positives = 0
    for case in sorted(fixture["cases"], key=lambda item: item["id"]):
        plan = plan_case(fixture, case)
        planned += 1
        dispatches += len(plan.arms)
        by_category[plan.category] = by_category.get(plan.category, 0) + 1
        if plan.category == "positive" and plan.advice_count > 0:
            advised_positives += 1
        for arm in plan.arms:
            if arm.prompt_diff and not arm.prompt_diff.get("advisory_only"):
                advisory_failures += 1
            if arm.oracle_leak and not arm.oracle_leak.get("clean"):
                oracle_failures += 1
                print(
                    f"  ORACLE LEAK {plan.case_id} [{arm.arm}]: "
                    f"tokens={arm.oracle_leak['leaked_tokens']} "
                    f"lesson_ids={arm.oracle_leak['leaked_lesson_ids']}"
                )
            if arm.arm == "harmful" and arm.advice_count == 0:
                empty_harmful += 1
    print(f"fixture           {path.name}")
    print(f"fixture_sha256    {fixture_sha256(path)}")
    print(f"cases planned     {planned}  {dict(sorted(by_category.items()))}")
    print(f"positives advised {advised_positives}")
    print(f"prompt-diff fails {advisory_failures}")
    print(f"oracle-leak fails {oracle_failures}")
    print(f"empty harmful arm {empty_harmful}")
    print(f"dispatches needed {dispatches}")
    return 1 if (advisory_failures or oracle_failures or empty_harmful) else 0


def summarize(report: dict) -> None:
    print(f"pairs             {report['source_target_pairs']}")
    print(f"control passes    {report['control_passes']}")
    print(f"placebo passes    {report['placebo_passes']}")
    print(f"treatment passes  {report['treatment_passes']}")
    print(f"lift vs control   {report['treatment_minus_control_points']} pp  (the gate)")
    print(f"lift vs placebo   {report['content_lift_points']} pp  (content-attributable)")
    print(
        f"presence effect   {report['presence_effect_points']} pp"
        f"{'  <-- FLAGGED' if report['presence_effect_flagged'] else ''}"
        f"{' MATERIAL' if report['presence_effect_material'] else ''}"
    )
    print(f"interpretation    {report['interpretation']}")
    print(f"advice coverage   {report['positive_advice_coverage_percent']}%")
    print(f"regressions       {report['treatment_regressions']}")
    print(f"per-family worst  {report['worst_family_regressions']}")
    print(
        "negative reject   "
        f"{report['negative_transfer_rejections']}/{report['negative_total']} "
        f"({report['negative_rejection_percent']}%)"
    )
    print(
        f"harmful excess    {report['harmful_arm_excess_passes']}  "
        f"(advice lines {report['harmful_arm_advice_count']}, "
        f"min per case {report['harmful_arm_min_advice_count']})"
    )
    context = report["harmful_arm_context"]
    print(
        f"harmful binomial  discordant {context['discordant_pairs']}, "
        f"harmful better {context['harmful_better']}, "
        f"p={context['sign_test_p_value']}"
    )
    print(f"selection leakage {report['selection_leakage']}")
    print(f"output leakage    {report['output_leakage']}")
    print(f"prompt-diff fails {report['prompt_diff_failures']}")
    print(f"oracle-leak fails {report['oracle_leak_failures']}")
    print("parse rates       " + ", ".join(
        f"{arm} {item['parsed']}/{item['total']}"
        for arm, item in sorted(report["parse_rates"].items())
    ))
    for kind, item in sorted(report["pass_rate_by_verifier"].items()):
        print(
            f"  {kind:<18} control {item['control_passes']}/{item['total']}, "
            f"treatment {item['treatment_passes']}/{item['total']}, "
            f"lift {item['lift_points']} pp"
        )
    null_test = report["arm_label_null_test"]
    print(
        f"arm-label null    {null_test['method']}, discordant "
        f"{null_test['discordant_pairs']} ({null_test['treatment_better']} better / "
        f"{null_test['treatment_worse']} worse), null mean "
        f"{null_test['permutation_null_mean']} pp, p={null_test['sign_test_p_value']}"
    )
    print(f"citation_count    {report['citation_count_consistency']}  (diagnostic only)")
    for name, ok in sorted(report["passes"].items()):
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    print(f"all gates passed  {report['all_exit_criteria_passed']}")
    print(f"claim scope       {report['claim_scope']}")
    print(f"attestation       {report['attestation_sha256']}")


def write_report(report: dict, destination: Path) -> Path:
    destination = Path(destination).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(
        (json.dumps(report, ensure_ascii=False, sort_keys=True) + chr(10)).encode("utf-8")
    )
    return destination


def rescore_in_subprocess(saved: Path, args: argparse.Namespace, expected: str) -> bool | None:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--rescore",
        str(saved),
        "--fixture",
        str(Path(args.fixture).resolve()),
    ]
    if args.seal:
        command += ["--seal", args.seal]
    if args.allow_unsealed:
        command.append("--allow-unsealed")
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=300, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return False
    lines = completed.stdout.strip().splitlines()
    return bool(lines) and lines[-1].strip() == expected


def verify_by_subprocess(report: dict, args: argparse.Namespace) -> bool | None:
    """Re-score the run in a fresh interpreter and compare attestation digests.

    When --report is given the report is kept at that path, so the same file the
    check ran against survives for independent re-scoring afterwards.
    """
    expected = report["attestation_sha256"]
    if args.report is not None:
        saved = write_report(report, args.report)
        print(f"report written    {saved}")
        return rescore_in_subprocess(saved, args, expected)
    with tempfile.TemporaryDirectory() as directory:
        saved = write_report(report, Path(directory) / "transfer_lift_report.json")
        return rescore_in_subprocess(saved, args, expected)


def regenerate_evidence(args: argparse.Namespace) -> int:
    """Rebuild the evidence document from a saved run report. No dispatch.

    Only non-sealed fields are recomputed. Every sealed number, including
    evaluator_sha256 and attestation_sha256, is carried over from the saved
    report verbatim, and the result is checked against it before writing.
    """
    if args.report is None:
        print("--regenerate-evidence requires --report", file=sys.stderr)
        return 2
    saved = json.loads(Path(args.report).read_text(encoding="utf-8"))
    if "cases" not in saved:
        print(
            "that file carries no case rows; pass the run report written by "
            "--report, not the evidence document",
            file=sys.stderr,
        )
        return 2
    fixture = load_transfer_lift_fixture(
        Path(args.fixture), expected_sha256=args.seal, allow_unsealed=args.allow_unsealed
    )
    before = saved["attestation_sha256"]
    merged = dict(saved)
    merged.update(
        derived_report_fields(
            saved,
            completion_lift_points_min=float(
                fixture["thresholds"]["completion_lift_points_min"]
            ),
            alpha=float(fixture["thresholds"]["shuffled_arm_max_p_value"]),
            design_power_at_observed_rates=args.design_power,
        )
    )
    if merged["attestation_sha256"] != before:
        print("regeneration would change the attestation; refusing", file=sys.stderr)
        return 2
    destination = evidence_path(args)
    if destination is None:
        print("pass --hash8, --manifest or --out", file=sys.stderr)
        return 2
    # Re-prove the attestation in a fresh interpreter now, rather than carrying a
    # stale flag from the original run: regeneration is exactly when the claim
    # "these numbers reproduce" should be re-established.
    verified = rescore_in_subprocess(Path(args.report).resolve(), args, before)
    print(f"rescore verified  {verified}")
    document = evidence_document(
        merged, test_command=TEST_COMMAND, rescore_verified=verified
    )
    if document["attestation_sha256"] != before:
        print("evidence attestation drifted; refusing", file=sys.stderr)
        return 2
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + chr(10)
    ).encode("utf-8")
    destination.write_bytes(payload)
    print(f"attestation       {document['attestation_sha256']} (unchanged)")
    print(f"interpretation    {document['result']['interpretation']}")
    print(f"evidence written  {destination}  ({len(payload)} bytes)")
    return 0


def rescore(args: argparse.Namespace) -> int:
    saved = json.loads(Path(args.rescore).read_text(encoding="utf-8"))
    fixture = load_transfer_lift_fixture(
        Path(args.fixture), expected_sha256=args.seal, allow_unsealed=args.allow_unsealed
    )
    print(rescore_report(saved, fixture)["attestation_sha256"])
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    path = Path(args.fixture)

    if args.regenerate_evidence:
        try:
            return regenerate_evidence(args)
        except (
            TransferLiftFixtureError,
            TransferLiftRunError,
            KeyError,
            TypeError,
            OSError,
            ValueError,
        ) as exc:
            print(f"regeneration failed: {exc}", file=sys.stderr)
            return 2

    if args.rescore is not None:
        try:
            return rescore(args)
        except (
            TransferLiftFixtureError,
            TransferLiftRunError,
            KeyError,
            TypeError,
            OSError,
            ValueError,
        ) as exc:
            print(f"rescore failed: {exc}", file=sys.stderr)
            return 2

    if not path.is_file():
        print(f"fixture not found: {path.name}", file=sys.stderr)
        return 2
    try:
        fixture = load_transfer_lift_fixture(
            path, expected_sha256=args.seal, allow_unsealed=args.allow_unsealed
        )
    except TransferLiftFixtureError as exc:
        print(f"fixture rejected: {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        try:
            return dry_run(fixture, path)
        except TransferLiftFixtureError as exc:
            print(f"fixture rejected: {exc}", file=sys.stderr)
            return 2
        except TransferLiftRunError as exc:
            print(f"plan failed: {exc}", file=sys.stderr)
            return 2

    dispatcher = ollama_dispatcher(
        base_url=args.base_url,
        model=LIVE_MODEL,
        temperature=args.temperature,
        context_length=args.num_ctx,
        max_output_tokens=args.num_predict,
        keep_alive=args.keep_alive,
        generation_timeout=args.generation_timeout,
    )
    started = time.monotonic()
    try:
        report = run_transfer_lift_fixture(
            path,
            dispatcher,
            expected_sha256=args.seal,
            allow_unsealed=args.allow_unsealed,
            seed=args.seed,
            temperature=args.temperature,
            null_probe=args.null_probe,
        )
    except TransferLiftFixtureError as exc:
        print(f"fixture rejected: {exc}", file=sys.stderr)
        return 2
    except TransferLiftRunError as exc:
        print(f"run failed: {exc}", file=sys.stderr)
        return 2
    finally:
        if args.offload:
            print(f"model offloaded: {offload_model(args.base_url)}")
    dispatches = sum(len(row["arms"]) for row in report["cases"])
    print(f"elapsed           {time.monotonic() - started:.1f}s for {dispatches} dispatches")
    summarize(report)

    verified = verify_by_subprocess(report, args)
    print(f"rescore verified  {verified}")

    destination = evidence_path(args)
    if destination is None:
        print("no evidence written: pass --hash8 or --manifest (or --out)", file=sys.stderr)
        return 0 if report["all_exit_criteria_passed"] else 1
    if not report["fixture_sealed"]:
        print(
            "refusing to write evidence for an unsealed fixture; "
            "a development run is not evidence",
            file=sys.stderr,
        )
        return 0 if report["all_exit_criteria_passed"] else 1
    document = evidence_document(
        report, test_command=TEST_COMMAND, rescore_verified=verified
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(
        (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        )
    )
    print(f"evidence written  {destination}")
    return 0 if (report["all_exit_criteria_passed"] and verified) else 1


if __name__ == "__main__":
    sys.exit(main())
