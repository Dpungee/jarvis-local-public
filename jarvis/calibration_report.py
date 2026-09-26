"""Read-only calibration reporting over the production initiative gate.

Sandbox rows are not the operator's rows. ``Memory.competence`` counts only
``interactive``, ``worker`` and ``proactive`` outcomes from the database it is
given, so a report produced from an isolated ``JARVIS_DATA`` describes that
sandbox and can never move the operator's initiative gate.

Every threshold, verdict and reason string here comes from the production code
path. :func:`jarvis.proactive.calibrated_meta_gate` is called exactly as
:func:`jarvis.proactive.initiative_eligibility` calls it, so the per-family
verdict in this report is the same object the runtime uses to grant authority.
This module deliberately re-implements no gate rule and no scoring formula: it
does not compute a Brier score, a calibration error, a success rate or a drift
signal of its own. It also never writes: every call it makes is a ``SELECT``
through an already-open :class:`~jarvis.memory.Memory`, and
:func:`read_only_memory` opens a database file through SQLite's read-only URI
mode and reports from a throwaway copy so a report can never migrate, upgrade or
touch the file it was pointed at.
"""
from __future__ import annotations

import json
import platform
import shutil
import sqlite3
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .memory import Memory
from .memory_spine import KEY_SIDECAR_SUFFIX
from .proactive import (
    META_GATE_MAX_BRIER,
    META_GATE_MAX_CALIBRATION_ERROR,
    META_GATE_MIN_ATTEMPTS,
    calibrated_meta_gate,
    initiative_eligibility,
)

REPORT_VERSION = 1

#: The production gate function whose verdict this report republishes verbatim.
GATE_SOURCE = "jarvis.proactive.calibrated_meta_gate"

#: Thresholds are read from ``jarvis/proactive.py`` and never redefined here.
THRESHOLD_SOURCE = "jarvis/proactive.py"

#: ``Memory.competence`` restricts its aggregate to these origins. The constant
#: mirrors that SQL so a report can name what it counted; the mirror itself is
#: proved by ``tests/test_calibration_report.py``, which resolves one prediction
#: per origin in ``Memory.PREDICTION_ORIGINS`` and asserts the counted set.
#: ``practice`` is excluded by design: calibration must not be manufacturable by
#: a practice harness, and routing around that exclusion would weaken the gate.
COUNTED_PREDICTION_ORIGINS = frozenset({"interactive", "worker", "proactive"})

#: Mirrors of ``Memory.drift_report``'s own defaults, so this report describes the
#: same window the runtime's drift check uses. ``tests/test_calibration_report.py``
#: reads that signature and fails if the two ever part company.
DEFAULT_DRIFT_WINDOW = 30
DEFAULT_DRIFT_BASELINE = 90
DEFAULT_DRIFT_MINIMUM_SAMPLES = 10

_TOP_FAILURE_LIMIT = 3


def excluded_prediction_origins(memory: Memory) -> list[str]:
    """Return the prediction origins ``competence()`` deliberately ignores."""
    return sorted(set(memory.PREDICTION_ORIGINS) - COUNTED_PREDICTION_ORIGINS)


def _competence_rows(memory: Memory) -> dict[str, dict[str, Any]]:
    return {str(row["family"]): dict(row) for row in memory.competence()}


def _drift_by_family(findings: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(item["family"]): dict(item) for item in findings}


def family_report(
    memory: Memory,
    family: str,
    *,
    competence_rows: dict[str, dict[str, Any]] | None = None,
    drift_by_family: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return one family's production gate verdict plus its supporting counts.

    ``allowed``, ``attempts``, ``brier``, ``mean_predicted``, ``observed_success``,
    ``calibration_error``, ``evidence_applicable``, ``evidence_rate``,
    ``requirements``, ``reasons``, ``authority`` and ``never_authorizes`` are the
    fields :func:`jarvis.proactive.calibrated_meta_gate` returned, unmodified.
    """
    gate = dict(calibrated_meta_gate(memory, family))
    rows = _competence_rows(memory) if competence_rows is None else competence_rows
    row = rows.get(family) or {}
    mean_steps = row.get("mean_steps")
    max_steps = row.get("max_steps")
    top_failures = (
        [
            {"failure_class": str(item["failure_class"]), "n": int(item["n"])}
            for item in memory.failure_histogram(family, limit=_TOP_FAILURE_LIMIT)
        ]
        if int(gate.get("attempts") or 0)
        else []
    )
    drift = (drift_by_family or {}).get(family)
    gate.update({
        "mean_steps": None if mean_steps is None else float(mean_steps),
        "max_steps": None if max_steps is None else int(max_steps),
        "top_failures": top_failures,
        "drift_signals": list(drift["signals"]) if drift else [],
    })
    return gate


def calibration_report(
    memory: Memory,
    *,
    families: Sequence[str] | None = None,
    drift_window: int = DEFAULT_DRIFT_WINDOW,
    drift_baseline: int = DEFAULT_DRIFT_BASELINE,
    drift_minimum_samples: int = DEFAULT_DRIFT_MINIMUM_SAMPLES,
) -> dict[str, Any]:
    """Build the whole read-only calibration picture for one database.

    ``families`` defaults to every family in ``Memory.PREDICTION_FAMILIES``, which
    is the same set :func:`jarvis.proactive.initiative_eligibility` iterates when
    it counts calibrated families.
    """
    known = set(memory.PREDICTION_FAMILIES)
    selected = sorted(known) if families is None else [str(item) for item in families]
    unknown = [item for item in selected if item not in known]
    if unknown:
        raise ValueError(f"Unknown task family: {sorted(unknown)[0]}")

    competence_rows = _competence_rows(memory)
    drift_findings = [
        dict(item)
        for item in memory.drift_report(
            window=drift_window,
            baseline=drift_baseline,
            minimum_samples=drift_minimum_samples,
        )
    ]
    drift_index = _drift_by_family(drift_findings)
    families_report = [
        family_report(
            memory,
            family,
            competence_rows=competence_rows,
            drift_by_family=drift_index,
        )
        for family in selected
    ]
    calibrated = [item["family"] for item in families_report if item["allowed"]]
    resolved = sum(int(row.get("attempts") or 0) for row in competence_rows.values())
    return {
        "report_version": REPORT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "gate_source": GATE_SOURCE,
        "threshold_source": THRESHOLD_SOURCE,
        "thresholds": {
            "minimum_attempts": META_GATE_MIN_ATTEMPTS,
            "maximum_brier": META_GATE_MAX_BRIER,
            "maximum_calibration_error": META_GATE_MAX_CALIBRATION_ERROR,
        },
        "counted_origins": sorted(COUNTED_PREDICTION_ORIGINS),
        "excluded_origins": excluded_prediction_origins(memory),
        "families": families_report,
        "calibrated_families": calibrated,
        "calibrated_family_count": len(calibrated),
        "families_with_outcomes": sorted(
            item["family"] for item in families_report if int(item["attempts"] or 0)
        ),
        "resolved_outcomes_counted": resolved,
        "open_predictions": int(memory.open_prediction_count()),
        "calibration_bins": [dict(item) for item in memory.calibration(10)],
        "drift": {
            "window": int(drift_window),
            "baseline": int(drift_baseline),
            "minimum_samples": int(drift_minimum_samples),
            "families_with_signals": sorted(drift_index),
            "findings": drift_findings,
        },
    }


def initiative_snapshot(config: Any, memory: Memory) -> dict[str, Any]:
    """Return the production Tier 0/Tier 1 verdict without reinterpreting it.

    This is the authority for "at least 3 calibrated families": the requirement
    lives in :func:`jarvis.proactive.initiative_eligibility` and is reported here
    only by delegation.
    """
    return dict(initiative_eligibility(config, memory))


def _format_number(value: Any, spec: str = ".3f") -> str:
    if value is None:
        return "n/a"
    return format(float(value), spec)


def format_report(report: dict[str, Any]) -> str:
    """Render the report as plain text, including every unmet gate reason."""
    lines: list[str] = []
    lines.append(
        "Calibration report (read-only). Sandbox rows are not the operator's rows: "
        "this describes only the database it was pointed at."
    )
    thresholds = report["thresholds"]
    lines.append(
        f"Gate: {report['gate_source']} "
        f"(attempts >= {thresholds['minimum_attempts']}, "
        f"Brier <= {thresholds['maximum_brier']:.2f}, "
        f"calibration error <= {thresholds['maximum_calibration_error']:.2f}; "
        f"thresholds from {report['threshold_source']})"
    )
    lines.append(
        "Counted origins: "
        + ", ".join(report["counted_origins"])
        + " | excluded: "
        + ", ".join(report["excluded_origins"])
    )
    lines.append("")
    header = (
        f"{'family':20}{'n':>5}{'brier':>8}{'predicted':>11}{'observed':>10}"
        f"{'cal.err':>9}{'evidence':>10}{'steps':>8}  gate"
    )
    lines.append(header)
    for item in report["families"]:
        evidence = (
            "n/a"
            if not int(item["evidence_applicable"] or 0)
            else _format_number(item["evidence_rate"], ".2f")
        )
        lines.append(
            f"{item['family']:20}{int(item['attempts'] or 0):>5}"
            f"{_format_number(item['brier']):>8}"
            f"{_format_number(item['mean_predicted']):>11}"
            f"{_format_number(item['observed_success']):>10}"
            f"{_format_number(item['calibration_error']):>9}"
            f"{evidence:>10}"
            f"{_format_number(item['mean_steps'], '.1f'):>8}"
            f"  {'PASS' if item['allowed'] else 'BLOCKED'}"
        )
        for reason in item["reasons"]:
            lines.append(f"{'':22}- {reason}")
        if item["top_failures"]:
            summary = ", ".join(
                f"{entry['failure_class']}x{entry['n']}" for entry in item["top_failures"]
            )
            lines.append(f"{'':22}top failures: {summary}")
        for signal in item["drift_signals"]:
            lines.append(f"{'':22}drift: {signal['signal']}")
    lines.append("")
    lines.append(
        f"Calibrated families: {report['calibrated_family_count']}"
        + (
            " (" + ", ".join(report["calibrated_families"]) + ")"
            if report["calibrated_families"]
            else ""
        )
    )
    lines.append(
        f"Resolved outcomes counted: {report['resolved_outcomes_counted']}; "
        f"open predictions: {report['open_predictions']}"
    )
    drift = report["drift"]
    lines.append(
        "Drift signals: "
        + (", ".join(drift["families_with_signals"]) if drift["families_with_signals"] else "none")
        + f" (window={drift['window']}, baseline={drift['baseline']}, "
        f"minimum_samples={drift['minimum_samples']})"
    )
    lines.append(
        "The Tier 1 family requirement is enforced by "
        "jarvis.proactive.initiative_eligibility; run `python -m jarvis initiative` "
        "against the runtime that owns the gate."
    )
    return "\n".join(lines)


def evidence_platform() -> dict[str, str]:
    """Return the only platform facts an evidence artifact may carry."""
    return {"os": platform.system(), "python": platform.python_version()}


def evidence_filename(benchmark: str, provider: str, manifest_sha256: str) -> str:
    """Return the Phase 2 evidence filename for one benchmark and provider."""
    token = str(manifest_sha256).strip()
    if len(token) < 8:
        raise ValueError("manifest_sha256 must be at least 8 characters")
    safe = [str(benchmark).strip(), str(provider).strip()]
    for part in safe:
        if not part or not part.replace("_", "").replace("-", "").isalnum():
            raise ValueError("benchmark and provider must be alphanumeric tokens")
    return f"phase2_{safe[0]}_{safe[1]}_{token[:8]}.json"


def evidence_document(
    report: dict[str, Any],
    *,
    benchmark: str,
    provider: dict[str, Any] | str,
    base: dict[str, Any] | str,
    configuration_class: dict[str, Any] | str,
    command: str,
    result: str,
    known_limitations: Sequence[str],
    model: dict[str, Any] | str | None = None,
    run: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Wrap a report in the fields ``docs/EVALUATION.md`` requires.

    The top-level key names match the Phase 2 evidence artifacts the other
    packages write, so one collation pass can read them all. No prompt, reply,
    log line, path, username or hostname is carried: the platform block is the
    OS name and the Python version only.
    """
    document: dict[str, Any] = {
        "benchmark": str(benchmark),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "base": base if isinstance(base, dict) else {"manifest_sha256": str(base)},
        "provider": provider if isinstance(provider, dict) else {"name": str(provider)},
        "configuration_class": (
            configuration_class
            if isinstance(configuration_class, dict)
            else {"summary": str(configuration_class)}
        ),
        "command": str(command),
        "platform": evidence_platform(),
        "result": str(result),
        "known_limitations": [str(item) for item in known_limitations],
        "sandbox_disclaimer": (
            "These rows were produced in an isolated JARVIS_DATA sandbox. They are "
            "not the operator's rows and they do not move the operator's initiative "
            "gate, which reads his own database."
        ),
        "report": report,
    }
    if model is not None:
        document["model"] = model if isinstance(model, dict) else {"requested": str(model)}
    if run:
        document["run"] = run
    return document


def write_evidence(
    document: dict[str, Any],
    destination: Path,
    *,
    force: bool = False,
) -> Path:
    """Write one evidence artifact as UTF-8 JSON with LF line endings.

    A recorded result is evidence, not a scratch file: an existing artifact is
    never replaced unless the caller says so. The guard lives here rather than
    only in the command layer, so no other caller can route around it.
    """
    path = Path(destination)
    if path.exists() and not force:
        raise FileExistsError(
            f"{path.name} already exists; pass force=True to replace a recorded "
            "evidence artifact"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=False)
    path.write_bytes((payload + "\n").encode("utf-8"))
    return path


@contextmanager
def read_only_memory(
    path: Path | str,
    *,
    scratch_dir: Path | str | None = None,
) -> Iterator[Memory]:
    """Open a throwaway copy of ``path`` so the source is never written.

    The source is opened through SQLite's ``mode=ro`` URI and copied with the
    backup API, which is WAL-safe. ``Memory`` then opens the copy, so any schema
    migration it performs happens on the copy and the database file's bytes are
    unchanged. Any ``-wal``/``-shm`` sidecar the read itself materialised beside
    the source is removed again; one that was already there is left alone.
    """
    source = Path(path).expanduser()
    if not source.is_file():
        raise FileNotFoundError(f"No database at {source.name}")
    holder = tempfile.mkdtemp(
        prefix="jarvis-calibration-",
        dir=None if scratch_dir is None else str(scratch_dir),
    )
    try:
        copy = Path(holder) / "calibration-copy.db"
        _copy_database(source, copy)
        # A modern snapshot must retain its existing keyed audit identity. Never
        # generate a replacement key for an established spine or touch the source.
        source_key = Path(str(source) + KEY_SIDECAR_SUFFIX)
        if source_key.is_file():
            copied_key = Path(str(copy) + KEY_SIDECAR_SUFFIX)
            shutil.copyfile(source_key, copied_key)
            copied_key.chmod(0o600)
        with Memory(copy) as memory:
            yield memory
    finally:
        shutil.rmtree(holder, ignore_errors=True)


def _copy_database(source: Path, destination: Path) -> None:
    """Copy a database without ever opening the source for writing.

    The backup API through a ``mode=ro`` URI is preferred because it is WAL-safe
    and consistent. A read-only connection cannot create the ``-shm`` file a live
    WAL database needs, so when that fails the sidecars are copied byte for byte
    instead and the copy is recovered on open. Neither path writes the source
    database file. SQLite may still materialise a ``-shm``/``-wal`` sidecar beside
    the source while reading it, so any sidecar that did not exist beforehand is
    removed again; one that was already there belongs to a live writer and is left
    strictly alone.
    """
    sidecars = {
        suffix: source.with_name(source.name + suffix)
        for suffix in ("-wal", "-shm")
    }
    existed = {suffix: path.exists() for suffix, path in sidecars.items()}
    try:
        _backup_or_copy(source, destination, sidecars)
    finally:
        for suffix, path in sidecars.items():
            if existed[suffix]:
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError:
                # A live writer may have claimed it in the meantime. Never fight it.
                pass


def _backup_or_copy(
    source: Path,
    destination: Path,
    sidecars: dict[str, Path],
) -> None:
    try:
        reader = sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        reader = None
    if reader is not None:
        try:
            writer = sqlite3.connect(str(destination))
            try:
                reader.backup(writer)
            finally:
                writer.close()
            return
        except sqlite3.Error:
            destination.unlink(missing_ok=True)
        finally:
            reader.close()
    shutil.copyfile(source, destination)
    for suffix, sidecar in sidecars.items():
        if sidecar.is_file():
            shutil.copyfile(sidecar, destination.with_name(destination.name + suffix))
