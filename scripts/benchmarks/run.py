#!/usr/bin/env python3
"""``python scripts/benchmarks/run.py`` -- the public benchmark CLI.

::

    python scripts/benchmarks/run.py list
    python scripts/benchmarks/run.py fetch <dataset> [--cache DIR]
    python scripts/benchmarks/run.py run <benchmark> --config PATH [--n N] [--smoke] [--resume]
    python scripts/benchmarks/run.py report <path> [--markdown]

There is deliberately **no** ``jarvis benchmark`` subcommand: this package is
not in the shipped wheel, does not belong in the offline-capable product
surface, and must stay out of ``self_diagnosis.runtime_manifest_sha256``'s
candidate set.

``--smoke`` runs 25 cases and writes only a ``*.smoke.jsonl``; the report writer
and the markdown renderer both refuse a smoke tier, so a smoke can never become
a published number.  Nor can a fake-provider run: its model id is outside every
allowed prefix, and the report writer refuses on that too.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

if __package__ in (None, ""):  # pragma: no cover - direct-script execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "scripts.benchmarks"

from . import RUNNER_VERSION, cache, locomo, longmemeval, report, ruler_style, scoring, synthetic
from .driver import (
    DEFAULT_ALLOWED_MODEL_PREFIXES,
    Case,
    DriverError,
    Instance,
    battery_model,
    make_runner,
    reconfigure_stdout,
)

SMOKE_CASES = 25
DEFAULT_REPORT_DIR = Path("reports") / "benchmarks"
DATASET_BENCHMARKS = {
    "longmemeval_s": "longmemeval_s",
    "longmemeval_oracle": "longmemeval_oracle",
    "locomo10": "locomo10",
}
GENERATED = ("ruler_style", synthetic.LONGMEMEVAL_SHAPE, synthetic.LOCOMO_SHAPE)
BENCHMARKS = tuple(DATASET_BENCHMARKS) + GENERATED

NON_COMMERCIAL_DECLARATION = (
    "non-commercial: the operator's use of Jarvis is personal and non-commercial"
)


class UsageError(RuntimeError):
    """A refusal the CLI reports as a non-zero exit rather than a traceback."""

    def __init__(self, message: str, *, code: str = "usage") -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def default_config(benchmark: str) -> dict[str, Any]:
    """The published config's shape, with everything this host can fill in."""

    root = cache.repository_root()
    return {
        "benchmark": benchmark,
        "runner_version": RUNNER_VERSION,
        "dataset": {"name": DATASET_BENCHMARKS.get(benchmark), "sha256": None},
        "n_cases": None,
        "sampling": {"strategy": "stratified", "key": "question_type", "seed": 20260904},
        "jarvis": {"schema": None, "spine_schema": None, "graph_schema": None},
        "runtime": {
            "JARVIS_COMPACTION_ENABLED": False,
            "JARVIS_MEMORY_EMBEDDINGS": "disabled",
            "JARVIS_CONTEXT_LENGTH": None,
            # The control arm's own window. It is separate from the agent's on
            # purpose: sharing one value makes the top of the RULER-style grid
            # undeliverable, and raising the agent's window to fit the grid
            # would change what the jarvis arm measures. Both are published.
            "JARVIS_DIRECT_CONTEXT_LENGTH": None,
        },
        "model": {
            "answer": battery_model(),
            "judge": None,
            "judge_prompt_sha256": scoring.judge_prompt_sha256(),
            # L-9: a judged column that does not pin its decoding parameters is
            # not reproducible even in principle.
            "judge_temperature": scoring.JUDGE_TEMPERATURE,
            "judge_seed": scoring.JUDGE_SEED,
            "allowed_model_prefixes": list(DEFAULT_ALLOWED_MODEL_PREFIXES),
        },
        "ingestion": "transcript",
        "fresh_conversation_per_case": True,
        "provider": "jarvis",
        "use_declaration": NON_COMMERCIAL_DECLARATION,
        "host": {"os": sys.platform, "python": sys.version.split()[0]},
        "compaction_available": (root / "jarvis" / "memory_compaction.py").exists(),
    }


def _merge(base: dict[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    for key, value in overlay.items():
        # A leading underscore marks a template comment; it documents the file
        # for the operator and must not reach the hashed, published config.
        if str(key).startswith("_"):
            continue
        if isinstance(value, Mapping) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value
    return base


def load_config(path: Path | None, benchmark: str) -> dict[str, Any]:
    """Read a config file over the defaults, then re-stamp the derived fields."""

    config = default_config(benchmark)
    if path is not None:
        try:
            overlay = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise UsageError(f"could not read config {path}: {exc}", code="bad_config") from exc
        if not isinstance(overlay, dict):
            raise UsageError(f"{path} must hold a JSON object", code="bad_config")
        _merge(config, overlay)
    config["benchmark"] = benchmark
    config["runner_version"] = RUNNER_VERSION
    # Derived, never taken from the file: a config that lied about the judge
    # prompt would publish a hash that does not describe the prompt we sent.
    config["model"]["judge_prompt_sha256"] = scoring.judge_prompt_sha256()
    config["model"]["judge_temperature"] = scoring.JUDGE_TEMPERATURE
    config["model"]["judge_seed"] = scoring.JUDGE_SEED
    config["use_declaration"] = NON_COMMERCIAL_DECLARATION
    return config


def _runtime_int(runtime: Mapping[str, Any], key: str) -> int | None:
    raw = runtime.get(key)
    try:
        return int(raw) if raw else None
    except (TypeError, ValueError):
        raise UsageError(f"{key} must be an integer, not {raw!r}", code="bad_config") from None


def runtime_settings(config: Mapping[str, Any]) -> tuple[int | None, str, int | None]:
    """The runtime block the run must actually apply, not merely publish."""

    runtime = config.get("runtime") or {}
    return (
        _runtime_int(runtime, "JARVIS_CONTEXT_LENGTH"),
        str(runtime.get("JARVIS_MEMORY_EMBEDDINGS") or "disabled"),
        _runtime_int(runtime, "JARVIS_DIRECT_CONTEXT_LENGTH"),
    )


# ---------------------------------------------------------------------------
# The judge column
# ---------------------------------------------------------------------------


def fake_judge(prompt: str) -> str:
    """A deterministic judge for smoke runs.  Never produces a published number."""

    reference = ""
    answer = ""
    for line in prompt.splitlines():
        if line.startswith("Reference answer:"):
            reference = line.split(":", 1)[1].strip()
        elif line.startswith("Answer to grade:"):
            answer = line.split(":", 1)[1].strip()
    if scoring.is_abstention(answer):
        return "VERDICT: ABSTAINED"
    if reference and scoring.contains_answer(answer, reference):
        return "VERDICT: CORRECT"
    return "VERDICT: INCORRECT"


def make_judge(provider: str, model: str) -> Callable[[str], str]:
    """``fake`` for smoke; otherwise a tool-free provider call, no store attached."""

    if provider == "fake":
        return fake_judge

    def _judge(prompt: str) -> str:  # pragma: no cover - live provider
        from dataclasses import replace as _replace

        from jarvis.config import Config
        from jarvis.model_client import build_model_client

        config = _replace(Config.load(), claude_cli_enabled=True)
        client = build_model_client(config)
        try:
            response = client.chat(
                [{"role": "user", "content": prompt}],
                [],
                model,
                temperature=scoring.JUDGE_TEMPERATURE,
                seed=scoring.JUDGE_SEED,
            )
        finally:
            closer = getattr(client, "close", None)
            if callable(closer):
                closer()
        return str(response.get("content", ""))

    return _judge


# ---------------------------------------------------------------------------
# Materialising a benchmark
# ---------------------------------------------------------------------------


def _dataset_handle(benchmark: str, config: Mapping[str, Any], args: argparse.Namespace, *, scored: bool):
    overrides = dict(config.get("dataset") or {})
    spec = cache.spec_for(DATASET_BENCHMARKS[benchmark], overrides=overrides)
    return cache.ensure_dataset(
        spec,
        cache_dir=getattr(args, "cache", None),
        allow_fetch=False,
        scored=scored,
    )


def materialise(
    benchmark: str,
    config: Mapping[str, Any],
    args: argparse.Namespace,
    *,
    scored: bool,
) -> tuple[list[tuple[Instance, Case]], dict[str, Any], str]:
    """Return the ordered (instance, case) work list, the dataset block, group key."""

    seed = int((config.get("sampling") or {}).get("seed", 20260904))
    limit = args.n if args.n is not None else config.get("n_cases")
    if args.smoke:
        limit = SMOKE_CASES

    if benchmark in DATASET_BENCHMARKS:
        handle = _dataset_handle(benchmark, config, args, scored=scored)
        if benchmark == "locomo10":
            instances = locomo.load(handle.path)
            print(f"locomo: {locomo.question_count(instances)} questions in the file")
            pairs = locomo.stratified_cases(instances, n=limit, seed=seed)
            return pairs, handle.as_config(), locomo.GROUP_KEY
        instances = longmemeval.load(handle.path)
        chosen = longmemeval.stratified_sample(instances, n=limit, seed=seed)
        return list(longmemeval.iter_cases(chosen)), handle.as_config(), longmemeval.GROUP_KEY

    if benchmark == synthetic.LONGMEMEVAL_SHAPE:
        instances = synthetic.longmemeval_shape(n=limit or SMOKE_CASES, seed=seed)
        return (
            list(longmemeval.iter_cases(instances)),
            {"name": benchmark, "generated": True, "seed": seed},
            longmemeval.GROUP_KEY,
        )
    if benchmark == synthetic.LOCOMO_SHAPE:
        instances = synthetic.locomo_shape(seed=seed)
        pairs = locomo.stratified_cases(instances, n=limit, seed=seed)
        return pairs, {"name": benchmark, "generated": True, "seed": seed}, locomo.GROUP_KEY
    raise UsageError(f"{benchmark} is not materialised through this path", code="unknown_benchmark")


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def cmd_list(args: argparse.Namespace) -> int:
    print("datasets (fetched at run time, never vendored):")
    for name, spec in sorted(cache.DATASETS.items()):
        pinned = "pinned" if spec.sha256 else "UNPINNED"
        size = f"{spec.bytes:,}" if spec.bytes else "unknown"
        print(
            f"  {name:<20} {spec.licence:<12} {spec.licence_class:<10} "
            f"{size:>15} bytes  {pinned}"
        )
        if spec.notes:
            print(f"      {spec.notes}")
    print("\ngenerated benchmarks (no network, no external corpus):")
    for name in GENERATED:
        print(f"  {name}")
    print(f"\ncache directory:    {cache.default_cache_dir()}")
    print(f"declared use:       {NON_COMMERCIAL_DECLARATION}")
    print(f"judge prompt sha256 {scoring.judge_prompt_sha256()}")
    print(
        f"judge decoding      temperature={scoring.JUDGE_TEMPERATURE} "
        f"seed={scoring.JUDGE_SEED}"
    )
    if cache.commercial_use_declared():
        print(
            f"  NOTE: {cache.COMMERCIAL_USE_ENV} is set, so every restricted "
            "dataset above refuses to be fetched or read."
        )
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    spec = cache.spec_for(args.dataset)
    handle = cache.ensure_dataset(
        spec, cache_dir=args.cache, allow_fetch=True, scored=False
    )
    print(f"{handle.name}: {handle.path}")
    print(f"  bytes          {handle.bytes:,}")
    print(f"  sha256         {handle.sha256}")
    print(f"  licence        {handle.licence} ({handle.licence_class})")
    print(f"  licence sha256 {handle.licence_sha256 or 'not fetched'}")
    if not handle.pinned:
        print(
            "  NOT PINNED: write the sha256 above into the config's "
            "dataset.sha256 before scoring a run."
        )
    if handle.licence_drift:
        # M-1: the licence is re-fetched on every fetch, so this compares the
        # upstream text with what was cached rather than the cache with itself.
        print(
            "  WARNING: the licence text has moved since the cached copy: "
            f"{handle.previous_licence_sha256} -> {handle.licence_sha256}"
        )

    scan = cache.scan_cached_dataset_for_leakage(handle)
    print(f"  leakage scan   {scan.summary()}")
    if not scan.clean:
        print("  LEAKAGE CHECK FAILED:")
        for finding in scan.findings:
            print(f"    {finding}")
        return 3
    print("  leakage check  PASS")
    print(
        "\nRecord this line in docs/BENCHMARKS.md (standing block):\n"
        f"  Leakage check last run against a fetched dataset: {report.utc_date()} "
        f"({handle.name}, {scan.summary()}, no findings)."
    )
    return 0


def _judge_for(args: argparse.Namespace, config: Mapping[str, Any]) -> Callable[[str], str] | None:
    if not args.judge:
        return None
    model = (config.get("model") or {}).get("judge") or (config.get("model") or {}).get("answer")
    return make_judge(args.provider, str(model))


def _check_tier(args: argparse.Namespace, config: Mapping[str, Any], tier: str) -> None:
    """L-5: the tiering is a gate, not a label.

    ``--tier full`` and an ``--n`` above the config's own ``n_cases`` are the
    two ways an operator spends hours of subscription quota by accident, so
    both need the intent stated out loud.
    """

    if tier == "full" and not getattr(args, "confirm_full", False):
        raise UsageError(
            "the full tier runs the whole dataset -- hours of wall clock and "
            "millions of tokens on the operator's subscription. Pass "
            "--confirm-full to say you mean it, and run one benchmark at a time",
            code="full_tier_unconfirmed",
        )
    declared = config.get("n_cases")
    requested = getattr(args, "n", None)
    if (
        requested is not None
        and isinstance(declared, int)
        and requested > declared
        and not getattr(args, "confirm_full", False)
    ):
        raise UsageError(
            f"--n {requested} exceeds the config's n_cases ({declared}); pass "
            "--confirm-full to override the budget the config declared",
            code="n_exceeds_config",
        )


def cmd_run(args: argparse.Namespace) -> int:
    benchmark = args.benchmark
    if benchmark not in BENCHMARKS:
        raise UsageError(
            f"unknown benchmark {benchmark!r}; known: {', '.join(BENCHMARKS)}",
            code="unknown_benchmark",
        )
    config = load_config(args.config, benchmark)
    config["provider"] = args.provider
    tier = report.SMOKE_TIER if args.smoke else str(args.tier)
    config["tier"] = tier
    _check_tier(args, config, tier)
    if args.judge:
        config["model"]["judge"] = (config.get("model") or {}).get("judge") or config["model"]["answer"]

    if benchmark == "ruler_style":
        return _run_ruler(args, config, tier)

    pairs, dataset_block, group_key = materialise(
        benchmark, config, args, scored=tier != report.SMOKE_TIER
    )
    config["dataset"] = dataset_block
    config["n_cases"] = len(pairs)
    digest = report.config_sha256(config)
    out_dir = Path(args.out) if args.out else DEFAULT_REPORT_DIR
    suffix = ".smoke.jsonl" if tier == report.SMOKE_TIER else ".jsonl"
    jsonl = out_dir / f"{benchmark}-{digest[:8]}{suffix}"
    done = report.completed_case_ids(jsonl) if args.resume else set()
    if not args.resume and jsonl.exists():
        raise UsageError(
            f"{jsonl} already exists; pass --resume to continue it or remove it",
            code="jsonl_exists",
        )

    judge_fn = _judge_for(args, config)
    context_length, embeddings, direct_length = runtime_settings(config)
    runner = make_runner(
        args.provider,
        model=config["model"]["answer"],
        compaction_enabled=bool(config["runtime"].get("JARVIS_COMPACTION_ENABLED")),
        context_length=context_length,
        embeddings=embeddings,
        direct_context_length=direct_length,
    )
    started = report.utc_now()
    scorer = locomo if group_key == locomo.GROUP_KEY else longmemeval
    current: str | None = None
    scored_rows = 0
    try:
        for instance, case in pairs:
            if case.case_id in done:
                continue
            if instance.instance_id != current:
                runner.ingest(instance)
                current = instance.instance_id
            outcome = runner.ask(case)
            verdict = scoring.judge_case(case.question, case.gold, outcome.reply, judge_fn)
            row = scorer.score_row(
                instance, case, outcome, judge_verdict=verdict, benchmark=benchmark
            )
            report.append_case(jsonl, row)
            scored_rows += 1
            print(f"  [{scored_rows}/{len(pairs)}] {case.case_id} det={row['det']}")
    finally:
        runner.close()

    return _finish(args, config, benchmark, jsonl, tier, started, group_key, out_dir, digest)


def _round_robin(items: Sequence[Any], *, key: Callable[[Any], str]) -> list[Any]:
    """Interleave a task-major list so any prefix of it covers every task."""

    buckets: dict[str, list[Any]] = {}
    for item in items:
        buckets.setdefault(key(item), []).append(item)
    order = sorted(buckets)
    woven: list[Any] = []
    deepest = max((len(bucket) for bucket in buckets.values()), default=0)
    for index in range(deepest):
        for name in order:
            bucket = buckets[name]
            if index < len(bucket):
                woven.append(bucket[index])
    return woven


def _run_ruler(args: argparse.Namespace, config: dict[str, Any], tier: str) -> int:
    seed = int((config.get("sampling") or {}).get("seed", 20260904))
    per_cell = 1 if args.smoke else int(args.samples_per_cell)
    lengths = tuple(int(value) for value in args.lengths) if args.lengths else ruler_style.DEFAULT_LENGTHS
    samples = ruler_style.generate(
        lengths=lengths, samples_per_cell=per_cell, seed=seed
    )
    if args.smoke:
        # ``generate`` is task-major, so a prefix slice covered niah_single and
        # nothing else. Take a round-robin across tasks instead, so 25 units of
        # smoke exercise all six shapes.
        samples = _round_robin(samples, key=lambda item: item.task)[:SMOKE_CASES]
    elif args.n is not None:
        samples = samples[: int(args.n)]
    arms = tuple(args.arms) if args.arms else ruler_style.ARMS
    config["dataset"] = {
        "name": "ruler_style",
        "generated": True,
        "seed": seed,
        "lengths": list(lengths),
        "depths": list(ruler_style.DEPTHS),
        "samples_per_cell": per_cell,
        "arms": list(arms),
        "length_units": "approximate tokens = characters / 4 (no tokenizer is shipped)",
    }
    config["n_cases"] = len(samples) * len(arms)
    digest = report.config_sha256(config)
    out_dir = Path(args.out) if args.out else DEFAULT_REPORT_DIR
    suffix = ".smoke.jsonl" if tier == report.SMOKE_TIER else ".jsonl"
    jsonl = out_dir / f"ruler_style-{digest[:8]}{suffix}"
    done = report.completed_case_ids(jsonl) if args.resume else set()
    if not args.resume and jsonl.exists():
        raise UsageError(
            f"{jsonl} already exists; pass --resume to continue it or remove it",
            code="jsonl_exists",
        )

    context_length, embeddings, direct_length = runtime_settings(config)
    if direct_length is None:
        # Size the control arm to the grid rather than to the agent: a
        # 32K-token haystack cannot be delivered into a 32K window, and the
        # top row of the default grid would be reported not-delivered for a
        # reason that is an artefact of the configuration, not a finding.
        direct_length = max(lengths) * 2
    config["runtime"]["JARVIS_DIRECT_CONTEXT_LENGTH"] = direct_length
    runner = make_runner(
        args.provider,
        model=config["model"]["answer"],
        compaction_enabled=bool(config["runtime"].get("JARVIS_COMPACTION_ENABLED")),
        context_length=context_length,
        embeddings=embeddings,
        direct_context_length=direct_length,
    )
    started = report.utc_now()
    # A smoke is 25 units of model work in total, not 25 per arm.
    ceiling = SMOKE_CASES if args.smoke else len(samples) * len(arms)
    total = min(len(samples) * len(arms), ceiling)
    index = 0
    try:
        for sample in samples:
            for arm in arms:
                if index >= total:
                    break
                index += 1
                marker = f"{sample.case.case_id}|{arm}"
                if marker in done:
                    continue
                if arm == "jarvis":
                    runner.ingest(ruler_style.as_instance(sample))
                    outcome = runner.ask(sample.case)
                else:
                    # The direct arm still needs a live store for the agent to
                    # run in; it simply has no ingested history to recall from.
                    runner.ingest(
                        Instance(
                            instance_id=f"{sample.case.case_id}|direct",
                            sessions=(),
                            cases=(sample.case,),
                        )
                    )
                    outcome = runner.ask_direct(sample.case, sample.context)
                row = ruler_style.score_row(sample, outcome, arm=arm)
                row["case_id"] = marker
                report.append_case(jsonl, row)
                print(f"  [{index}/{total}] {marker} det={row['det']}")
    finally:
        runner.close()
    return _finish(
        args, config, "ruler_style", jsonl, tier, started, ruler_style.GROUP_KEY, out_dir, digest
    )


def _finish(
    args: argparse.Namespace,
    config: Mapping[str, Any],
    benchmark: str,
    jsonl: Path,
    tier: str,
    started: str,
    group_key: str,
    out_dir: Path,
    digest: str,
) -> int:
    # M-4: a file that scores a case twice states an n that is not the number
    # of cases and a rate weighted by whichever cases were written twice. Two
    # concurrent --resume processes against one path produce exactly that.
    rows = report.read_cases(jsonl, allow_duplicates=tier == report.SMOKE_TIER)
    print(f"\n{len(rows)} case(s) scored; per-case output at {jsonl}")
    if tier == report.SMOKE_TIER:
        print(
            "smoke tier: no report is written, by design. Its numbers are not "
            "publishable and report --markdown refuses to render them."
        )
        summary = report.aggregate(rows, group_key=group_key)
        print(json.dumps(summary["overall"], indent=2))
        return 0
    built = report.build_report(
        benchmark=benchmark,
        config=config,
        rows=rows,
        tier=tier,
        root=cache.repository_root(),
        started_utc=started,
        limitations=list(args.limitation or []),
        resumed_from=str(jsonl) if args.resume else None,
        group_key=group_key,
        command=command_line(),
    )
    target = out_dir / report.report_filename(benchmark, built["finished_utc"][:10], digest)
    written = report.write_report(target, built)
    print(f"report written to {written}")
    return 0


def command_line() -> str:
    """Rule 5's "the exact command", recorded beside every published number."""

    return " ".join(sys.argv)


def cmd_report(args: argparse.Namespace) -> int:
    try:
        payload = json.loads(Path(args.path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UsageError(f"could not read {args.path}: {exc}", code="bad_report") from exc
    report.require_report(payload)
    if args.markdown:
        print(report.render_markdown(payload))
    else:
        print(json.dumps(payload.get("aggregate", {}), indent=2))
    return 0


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python scripts/benchmarks/run.py",
        description=(
            "Public benchmark runner. These numbers demonstrate; they never gate. "
            "The release authority is the sealed one-use holdout set."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    listing = sub.add_parser("list", help="show the dataset registry and the cache location")
    listing.set_defaults(handler=cmd_list)

    fetch = sub.add_parser("fetch", help="fetch one dataset into the cache and verify it")
    fetch.add_argument("dataset", choices=sorted(cache.DATASETS))
    fetch.add_argument("--cache", default=None, help="cache directory (must be outside the repository)")
    fetch.set_defaults(handler=cmd_fetch)

    run = sub.add_parser("run", help="run one benchmark")
    run.add_argument("benchmark", choices=list(BENCHMARKS))
    run.add_argument("--config", type=Path, default=None)
    run.add_argument("--n", type=int, default=None, help="case count (a subset is published as a subset)")
    run.add_argument("--smoke", action="store_true", help=f"{SMOKE_CASES} cases, no report")
    run.add_argument("--resume", action="store_true", help="continue an existing per-case JSONL")
    run.add_argument("--tier", choices=["subset", "full"], default="subset")
    run.add_argument(
        "--confirm-full",
        action="store_true",
        dest="confirm_full",
        help="required for --tier full, or for an --n above the config's n_cases",
    )
    run.add_argument("--provider", choices=["jarvis", "fake"], default="jarvis")
    run.add_argument("--judge", action="store_true", help="add the judged column beside the deterministic one")
    run.add_argument("--cache", default=None)
    run.add_argument("--out", type=Path, default=None)
    run.add_argument("--limitation", action="append", default=[])
    run.add_argument("--arms", nargs="*", choices=list(ruler_style.ARMS), default=None)
    run.add_argument("--lengths", nargs="*", type=int, default=None)
    run.add_argument("--samples-per-cell", type=int, default=20, dest="samples_per_cell")
    run.set_defaults(handler=cmd_run)

    rendering = sub.add_parser("report", help="summarise or render a written report")
    rendering.add_argument("path", type=Path)
    rendering.add_argument("--markdown", action="store_true")
    rendering.set_defaults(handler=cmd_report)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    reconfigure_stdout()
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        return int(args.handler(args))
    except (UsageError, cache.DatasetError, DriverError, report.ReportError, ruler_style.RulerError) as exc:
        print(f"refused ({exc.code}): {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
