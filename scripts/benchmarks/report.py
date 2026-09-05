"""Config hashing, resumable per-case output, and the published report.

Four refusals live here, and each of them is a rule from the design made
structural rather than advisory:

* **A row may not carry case text.**  Every key comes from a closed set and
  every string value is at most :data:`MAX_ROW_STRING_CHARS` characters.  That
  is what lets LoCoMo -- CC BY-NC 4.0, commercial use prohibited -- be measured
  and published without redistributing one word of it.
* **The local model never scores.**  A report whose observed models fall
  outside the config's ``allowed_model_prefixes`` is refused, which is also
  what stops a fake-provider smoke from ever becoming a published number.
* **A smoke is not a report.**  ``tier == "smoke"`` refuses both the report
  write and the markdown render.
* **A partial run is published as partial.**  ``n`` is whatever was actually
  scored; nothing is extrapolated and a subset never wears the benchmark's
  plain name.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

MAX_ROW_STRING_CHARS = 64
SMOKE_TIER = "smoke"
TIERS = ("smoke", "subset", "full")
REPORT_SCHEMA = 1

# The closed key set of a per-case row.  Ids, enums, booleans and numbers only.
ROW_KEYS = frozenset(
    {
        "case_id",
        "instance_id",
        "benchmark",
        "type",
        "category",
        "arm",
        "task",
        "length",
        "depth",
        "qa_index",
        "sample_id",
        "det",
        "em",
        "f1",
        "judge",
        "abstained",
        "gold_abstention",
        "latency_ms",
        "prompt_tokens",
        "completion_tokens",
        "tool_calls",
        "status",
        "model",
        "model_reported",
        "asserted",
        "delivered_fraction",
        "prompt_chars",
        "error_code",
    }
)

# Every published row must attest which model produced it: "the local model
# never scores" is only structural if a row cannot omit the evidence.
REQUIRED_ROW_KEYS = frozenset({"case_id", "model"})


class ReportError(RuntimeError):
    """A closed-reason refusal from the report layer."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def canonical_json(value: Any) -> str:
    """The one serialisation two implementations cannot disagree about."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def config_sha256(config: Mapping[str, Any]) -> str:
    """The config hash carried by every published row."""

    return hashlib.sha256(canonical_json(config).encode("utf-8")).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def git_state(root: Path) -> tuple[str, bool]:
    """The exact commit a number was measured at, and whether the tree was dirty."""

    def _git(*args: str) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        ).stdout.decode("utf-8", errors="replace").strip()

    try:
        commit = _git("rev-parse", "HEAD")
        dirty = bool(_git("status", "--porcelain"))
    except (OSError, subprocess.CalledProcessError):
        return "unknown", True
    return commit, dirty


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------


def validate_row(row: Mapping[str, Any], *, require_model: bool = False) -> None:
    """Refuse a row that could carry benchmark case text, or that attests nothing."""

    if require_model:
        missing = sorted(key for key in REQUIRED_ROW_KEYS if not row.get(key))
        if missing:
            raise ReportError(
                f"a published row must carry {', '.join(REQUIRED_ROW_KEYS)}; "
                f"missing or empty: {', '.join(missing)}",
                code="row_attests_nothing",
            )
    for key, value in row.items():
        if key not in ROW_KEYS:
            raise ReportError(
                f"report rows carry a closed key set; {key!r} is not in it",
                code="row_key_not_allowed",
            )
        if isinstance(value, str) and len(value) > MAX_ROW_STRING_CHARS:
            raise ReportError(
                f"row field {key!r} is {len(value)} characters; a report row may not "
                f"carry case text (limit {MAX_ROW_STRING_CHARS})",
                code="row_carries_case_text",
            )
        if isinstance(value, (list, tuple, dict)):
            raise ReportError(
                f"row field {key!r} is a container; rows carry scalars only",
                code="row_not_scalar",
            )


MAX_LIMITATION_CHARS = 200
MAX_LIMITATIONS = 12


def validate_limitations(limitations: Sequence[str]) -> list[str]:
    """Bound the one free-text channel an operator can put into a report.

    ``--limitation`` and any key merged from a config file land verbatim in the
    published JSON and in the rendered markdown, and neither passes through
    ``validate_row``.  It is operator-supplied, so no dataset can drive it --
    but it is the only place a LoCoMo question could reach a published file by
    accident, and a superlative here would sit beside a number.
    """

    cleaned: list[str] = []
    for item in limitations:
        text = " ".join(str(item).split())
        if not text:
            continue
        if len(text) > MAX_LIMITATION_CHARS:
            raise ReportError(
                f"a limitation is {len(text)} characters; keep it under "
                f"{MAX_LIMITATION_CHARS} so it cannot carry case text",
                code="limitation_too_long",
            )
        found = banned_claim_findings(text)
        if found:
            raise ReportError(
                f"a limitation carries a forbidden claim ({', '.join(found)})",
                code="limitation_banned_claim",
            )
        cleaned.append(text)
    if len(cleaned) > MAX_LIMITATIONS:
        raise ReportError(
            f"{len(cleaned)} limitations; keep it under {MAX_LIMITATIONS}",
            code="too_many_limitations",
        )
    return cleaned


def append_case(jsonl_path: Path, row: Mapping[str, Any]) -> None:
    """Resumable per-case output.

    A run that dies at case 412 of 600 still yields ``n=412``, which the
    honest-reporting rules require be published as partial rather than lost.
    """

    validate_row(row)
    path = Path(jsonl_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(canonical_json(dict(row)) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def read_cases(jsonl_path: Path, *, allow_duplicates: bool = True) -> list[dict[str, Any]]:
    """Read a per-case JSONL back, skipping a torn final line.

    ``allow_duplicates=False`` refuses a file that scores the same ``case_id``
    twice, naming the line number.  Two ``--resume`` processes against one path
    each read ``done`` at start and then both write the remainder, and a report
    built from that file states an ``n`` that is not the number of cases and a
    rate weighted by whichever cases happened to be written twice.
    """

    path = Path(jsonl_path)
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            # A run killed mid-write leaves at most one torn line; dropping it
            # is the resumable contract, not data loss.
            continue
        if not isinstance(parsed, dict):
            continue
        case_id = str(parsed.get("case_id") or "")
        if case_id:
            if case_id in seen and not allow_duplicates:
                raise ReportError(
                    f"{path} scores {case_id!r} twice (first at line {seen[case_id]}, "
                    f"again at line {number}); a duplicated case double-counts n "
                    "and every rate. Remove the duplicate or re-run the benchmark",
                    code="duplicate_case_id",
                )
            seen.setdefault(case_id, number)
        rows.append(parsed)
    return rows


def completed_case_ids(jsonl_path: Path) -> set[str]:
    """Which cases ``--resume`` may skip."""

    return {str(row["case_id"]) for row in read_cases(jsonl_path) if row.get("case_id")}


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _group(rows: Sequence[Mapping[str, Any]], key: str) -> dict[str, list[Mapping[str, Any]]]:
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        value = row.get(key)
        if value is None:
            continue
        grouped.setdefault(str(value), []).append(row)
    return grouped


def aggregate(rows: Sequence[Mapping[str, Any]], *, group_key: str = "type") -> dict[str, Any]:
    """Overall and per-group scores, latency, tokens, and the models observed."""

    from . import scoring

    def _cell(subset: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        judged = [row for row in subset if row.get("judge") in {"CORRECT", "INCORRECT", "ABSTAINED"}]
        return {
            "n": len(subset),
            "deterministic": scoring.rate(subset, "det"),
            "judge": (
                round(sum(1 for row in judged if row.get("judge") == "CORRECT") / len(judged), 4)
                if judged
                else None
            ),
            "em": scoring.mean([float(row["em"]) for row in subset if row.get("em") is not None]),
            "f1": scoring.mean([float(row["f1"]) for row in subset if row.get("f1") is not None]),
        }

    abstention_rows = [row for row in rows if row.get("gold_abstention")]
    judged_rows = [row for row in rows if row.get("judge") is not None]
    unparsed = sum(1 for row in judged_rows if row.get("judge") == "UNPARSED")
    delivery = [
        float(row["delivered_fraction"])
        for row in rows
        if row.get("delivered_fraction") is not None
    ]
    not_delivered = sum(1 for row in rows if row.get("status") == "context_exceeded")
    latencies = [float(row["latency_ms"]) for row in rows if row.get("latency_ms") is not None]
    prompts = [float(row["prompt_tokens"]) for row in rows if row.get("prompt_tokens") is not None]
    completions = [
        float(row["completion_tokens"]) for row in rows if row.get("completion_tokens") is not None
    ]
    return {
        "overall": _cell(rows),
        "by_group": {name: _cell(subset) for name, subset in sorted(_group(rows, group_key).items())},
        "group_key": group_key,
        "abstention": {
            "n": len(abstention_rows),
            "accuracy": scoring.rate(abstention_rows, "det"),
            # H-2: how often a reply declined *and* still stated a value. Left
            # visible rather than absorbed into the accuracy figure.
            "asserted_while_declining": sum(
                1 for row in abstention_rows if row.get("asserted")
            ),
        },
        "judge_reliability": {
            "n": len(judged_rows),
            "unparsed": unparsed,
            # A judge that failed on half the cases used to produce a
            # confident-looking number over the other half with no warning.
            "unparsed_rate": (
                round(unparsed / len(judged_rows), 4) if judged_rows else None
            ),
        },
        "delivery": {
            "n": len(delivery),
            "not_delivered": not_delivered,
            "delivered_fraction_p50": scoring.percentile(delivery, 0.50),
            "delivered_fraction_min": min(delivery) if delivery else None,
        },
        "latency_ms": {
            "p50": scoring.percentile(latencies, 0.50),
            "p95": scoring.percentile(latencies, 0.95),
        },
        "tokens_per_answer": {
            "prompt_p50": scoring.percentile(prompts, 0.50),
            "completion_p50": scoring.percentile(completions, 0.50),
        },
        "errors": sum(1 for row in rows if row.get("status") == "error"),
        "models_seen": sorted({str(row["model"]) for row in rows if row.get("model")}),
    }


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


def check_models(models: Iterable[str], allowed_prefixes: Sequence[str]) -> None:
    """Refuse to publish a number a disallowed model produced."""

    prefixes = tuple(allowed_prefixes)
    if not prefixes:
        raise ReportError(
            "the config must declare allowed_model_prefixes; an empty list would "
            "let any model produce a published number",
            code="allowed_prefixes_missing",
        )
    observed = [str(model) for model in models if str(model).strip()]
    if not observed:
        # M-3: no attestation is not compliance. "The local model never scores"
        # has to be structural, and a guard that treats silence as a pass is
        # weaker than the rule it enforces.
        raise ReportError(
            "refusing to write a report with no model evidence: not one row "
            "recorded which model produced it",
            code="models_unrecorded",
        )
    offenders = sorted(
        model for model in models if not any(str(model).startswith(prefix) for prefix in prefixes)
    )
    if offenders:
        raise ReportError(
            "refusing to write a report: "
            f"{', '.join(offenders)} are outside allowed_model_prefixes {list(prefixes)}",
            code="model_not_allowed",
        )


def report_filename(benchmark: str, date: str, config_hash: str) -> str:
    return f"{benchmark}-{date}-{config_hash[:8]}.json"


def build_report(
    *,
    benchmark: str,
    config: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    tier: str,
    root: Path,
    started_utc: str,
    finished_utc: str | None = None,
    limitations: Sequence[str] = (),
    resumed_from: str | None = None,
    group_key: str = "type",
    command: str | None = None,
) -> dict[str, Any]:
    """Assemble the report object without writing it."""

    if tier not in TIERS:
        raise ReportError(f"unknown tier {tier!r}; use one of {TIERS}", code="unknown_tier")
    for row in rows:
        validate_row(row, require_model=True)
    seen: set[str] = set()
    for row in rows:
        case_id = str(row.get("case_id") or "")
        if case_id in seen:
            raise ReportError(
                f"{case_id!r} appears twice in the rows handed to build_report; a "
                "duplicated case double-counts n and every rate",
                code="duplicate_case_id",
            )
        seen.add(case_id)
    limitations = validate_limitations(limitations)
    digest = config_sha256(config)
    summary = aggregate(rows, group_key=group_key)
    commit, dirty = git_state(root)
    date = (finished_utc or started_utc)[:10]
    return {
        "schema": REPORT_SCHEMA,
        "benchmark": benchmark,
        "run_id": f"{benchmark}-{date}-{digest[:8]}",
        "started_utc": started_utc,
        "finished_utc": finished_utc or utc_now(),
        "config": dict(config),
        "config_sha256": digest,
        "commit": commit,
        "dirty": dirty,
        "n": len(rows),
        "tier": tier,
        "resumed_from": resumed_from,
        # Honest-reporting rule 5 requires the exact command beside every
        # number. It is deliberately outside the hashed config, so re-running
        # the same configuration from a different path keeps the same hash.
        "command": command if command is not None else " ".join(sys.argv),
        "aggregate": summary,
        "rows": [dict(row) for row in rows],
        "limitations": list(limitations),
    }


def write_report(
    path: Path,
    report: Mapping[str, Any],
    *,
    allowed_prefixes: Sequence[str] | None = None,
) -> Path:
    """Write a report, or refuse with a code.  Never overwrites."""

    tier = str(report.get("tier"))
    if tier == SMOKE_TIER:
        raise ReportError(
            "a smoke run does not produce a report; its per-case JSONL is the "
            "only artefact, by design",
            code="smoke_is_not_a_report",
        )
    if not report.get("rows"):
        raise ReportError("refusing to write a report with no scored cases", code="no_cases")
    prefixes = allowed_prefixes
    if prefixes is None:
        model_block = report.get("config", {}).get("model", {})
        prefixes = model_block.get("allowed_model_prefixes", ())
    check_models(report.get("aggregate", {}).get("models_seen", ()), tuple(prefixes))
    target = Path(path)
    if target.exists():
        raise ReportError(
            f"{target} already exists; reports are append-only artefacts and are "
            "never overwritten",
            code="report_exists",
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return target


# Design section 3.9, which overrides VTMF section 7's own aspiration: what
# differentiates Jarvis is that its memory behaviour is measured and gated, not
# that the architecture is unprecedented.
BANNED_CLAIMS = ("best-in-class", "state of the art", "state-of-the-art", "beats ")


def banned_claim_findings(text: str) -> list[str]:
    """Every superlative the honest-reporting rules forbid, found in ``text``."""

    lowered = str(text).casefold()
    return [claim for claim in BANNED_CLAIMS if claim in lowered]


def require_report(payload: Mapping[str, Any]) -> None:
    """Refuse anything that is not a written report.

    A per-case JSONL is working output, not a publishable artefact, and a
    single-case one happens to parse as a JSON object -- so renaming it to
    ``.json`` must not get it rendered as a table row.
    """

    if (
        not isinstance(payload, Mapping)
        or payload.get("schema") != REPORT_SCHEMA
        or not payload.get("config_sha256")
    ):
        raise ReportError(
            "this file is not a benchmark report; per-case output is working "
            "material and is never publishable, whatever it is renamed to",
            code="not_a_report",
        )
    if str(payload.get("tier")) == SMOKE_TIER:
        raise ReportError(
            "refusing to treat a smoke run as a publishable result",
            code="smoke_is_not_a_report",
        )


def render_markdown(report: Mapping[str, Any]) -> str:
    """The append-only table row plus its provenance, for docs/BENCHMARKS.md."""

    require_report(report)
    config = report.get("config", {})
    dataset = config.get("dataset", {}) or {}
    model = config.get("model", {}) or {}
    summary = report.get("aggregate", {})
    overall = summary.get("overall", {})
    abstention = summary.get("abstention", {})
    latency = summary.get("latency_ms", {})
    tokens = summary.get("tokens_per_answer", {})

    def _cell(value: Any) -> str:
        if value is None:
            return "n/a"
        if isinstance(value, float):
            return f"{value:.3f}"
        return str(value)

    header = (
        "| date | benchmark | n | tier | model | commit | config hash | dataset sha256 "
        "| licence sha256 | deterministic | judge | abstention | p50 latency ms "
        "| prompt tokens/answer | report |\n"
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|\n"
    )
    row = "| {date} | {benchmark} | {n} | {tier} | {model} | {commit} | {config} | {data} | {lic} | {det} | {judge} | {abst} | {p50} | {tok} | {link} |\n".format(
        date=str(report.get("finished_utc", ""))[:10],
        benchmark=report.get("benchmark", ""),
        n=report.get("n", 0),
        tier=report.get("tier", ""),
        model=model.get("answer", "unknown"),
        commit=str(report.get("commit", "unknown"))[:12],
        config=str(report.get("config_sha256", ""))[:12],
        data=str(dataset.get("sha256") or "n/a")[:12],
        lic=str(dataset.get("licence_sha256") or "n/a")[:12],
        det=_cell(overall.get("deterministic")),
        judge=_cell(overall.get("judge")),
        abst=_cell(abstention.get("accuracy")),
        p50=_cell(latency.get("p50")),
        tok=_cell(tokens.get("prompt_p50")),
        link=f"`reports/benchmarks/{report_filename(str(report.get('benchmark')), str(report.get('finished_utc', ''))[:10], str(report.get('config_sha256', '')))}`",
    )
    notes = "".join(f"\n- limitation: {item}" for item in report.get("limitations", ()))
    command = report.get("command")
    if command:
        notes += f"\n- command: `{command}`"
    reliability = summary.get("judge_reliability") or {}
    if reliability.get("unparsed"):
        # A judged column with unparsed cells is a partial judged column, and
        # the denominator silently excluded them. Say so beside the number.
        notes += (
            f"\n- judge unparsed: {reliability['unparsed']} of {reliability.get('n')} "
            "judged cases; the judged column above excludes them"
        )
    delivery = summary.get("delivery") or {}
    if delivery.get("not_delivered"):
        notes += (
            f"\n- not delivered: {delivery['not_delivered']} cell(s) exceeded the "
            "configured context length and are scored as missing, not as wrong"
        )
    by_group = summary.get("by_group", {})
    breakdown = ""
    if by_group:
        breakdown = "\n\n<details><summary>per-{key} breakdown</summary>\n\n".format(
            key=summary.get("group_key", "type")
        )
        breakdown += "| group | n | deterministic | judge | em | f1 |\n|---|---|---|---|---|---|\n"
        for name, cell in by_group.items():
            breakdown += "| {n} | {count} | {det} | {judge} | {em} | {f1} |\n".format(
                n=name,
                count=cell.get("n", 0),
                det=_cell(cell.get("deterministic")),
                judge=_cell(cell.get("judge")),
                em=_cell(cell.get("em")),
                f1=_cell(cell.get("f1")),
            )
        breakdown += "\n</details>"
    return header + row + notes + breakdown + "\n"
