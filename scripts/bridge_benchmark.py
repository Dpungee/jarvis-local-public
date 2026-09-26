#!/usr/bin/env python3
"""Performance harness for the runtime->memory replay bridge.

::

    python scripts/bridge_benchmark.py all --out results.json
    python scripts/bridge_benchmark.py throughput [--events N] [--repeats R]
    python scripts/bridge_benchmark.py contention
    python scripts/bridge_benchmark.py backlog
    python scripts/bridge_benchmark.py lease
    python scripts/bridge_benchmark.py shape

This is a **development harness**, not product surface.  It lives under
``scripts/`` deliberately: ``self_diagnosis.runtime_manifest_sha256`` hashes
every ``jarvis/**/*.py`` and ``tests/**/*.py``, so a benchmark that moved into
either would make every sealed fixture depend on it.

Nothing here activates the bridge.  Every store is a temporary file created and
deleted by the run; no configuration is written, no live database is opened, and
the harness never imports a provider or touches the network.

**On the synthetic histories.**  A hand-written event stream measures whatever
its author imagined.  So the workload is built by driving the *real* runtime once
to produce a short history covering every projected family, then replicating that
template with fresh identifiers to whatever length a scenario needs.  Shape
fidelity is therefore a property of the construction rather than a claim, and
``shape`` prints the template's event-type histogram so a reader can check what
was actually measured.  What replication does *not* reproduce is a real store's
entity skew -- one room with ten thousand messages, one agent in four hundred
rooms -- and every number here should be read with that limitation in mind.

**On the environment block.**  It records the OS family, interpreter and SQLite
versions and the logical CPU count, and deliberately records no hostname, user,
absolute path, network address or device identifier: the repository's guidance
forbids machine and network data in artifacts, and a benchmark result is an
artifact like any other.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jarvis import memory_bridge as mb                      # noqa: E402
from jarvis.memory import Memory, now_iso                   # noqa: E402

HARNESS_VERSION = 1


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def _percentile(values: Sequence[float], quantile: float) -> float | None:
    """Nearest-rank percentile.

    Nearest-rank rather than interpolated because every value here is an
    observed latency: reporting a number no run actually produced would be a
    small lie in exactly the place these results are meant to be trusted.
    """
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(quantile * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def _summary(samples: Sequence[float]) -> dict[str, Any]:
    if not samples:
        return {"n": 0}
    return {
        "n": len(samples),
        "min_ms": round(min(samples) * 1000, 4),
        "p50_ms": round(_percentile(samples, 0.50) * 1000, 4),
        "p95_ms": round(_percentile(samples, 0.95) * 1000, 4),
        "p99_ms": round(_percentile(samples, 0.99) * 1000, 4),
        "max_ms": round(max(samples) * 1000, 4),
        "mean_ms": round(statistics.fmean(samples) * 1000, 4),
        "total_s": round(sum(samples), 4),
    }


def _store_pragmas(root: Path) -> dict[str, Any]:
    """The durability settings every number below was produced under.

    These dominate write latency.  ``synchronous=FULL`` in WAL means each batch
    commit fsyncs, so a result measured under ``NORMAL`` would be a different
    and much flattering benchmark of a system nobody runs.
    """
    path = root / "pragma-probe.db"
    with Memory(path) as memory:
        values = {
            name: memory.db.execute(f"PRAGMA {name}").fetchone()[0]
            for name in ("journal_mode", "synchronous", "busy_timeout",
                         "secure_delete", "foreign_keys")
        }
    return values


def _environment() -> dict[str, Any]:
    """Reproducibility facts only.  No host, user, path or network identity."""
    return {
        "harness_version": HARNESS_VERSION,
        "bridge_schema_version": mb.BRIDGE_SCHEMA_VERSION,
        "reducer_version": mb.REDUCER_VERSION,
        "os_family": platform.system(),
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "logical_cpus": os.cpu_count(),
        "identifiers_recorded": False,
    }


# ---------------------------------------------------------------------------
# Synthetic history built from a real runtime template
# ---------------------------------------------------------------------------

def _runtime_template(root: Path) -> list[dict[str, Any]]:
    """Drive the real runtime once; return its events as plain envelopes.

    Every projected family is exercised.  The runtime module is read and driven
    through its own public API; nothing here modifies it.
    """
    from jarvis import multi_agent_runtime as runtime

    store = runtime.MultiAgentRuntimeStore(root / "template-runtime.db")
    try:
        forge = store.create_agent(
            display_name="Forge", role="implementation",
            specialties=("coding",), idempotency_key="tpl-a",
        )
        sentry = store.create_agent(
            display_name="Sentry", role="security",
            specialties=("security",), idempotency_key="tpl-b",
        )
        first = store.bind_agent(forge.agent_id)
        second = store.bind_agent(sentry.agent_id)
        first.start(idempotency_key="tpl-s1")
        second.start(idempotency_key="tpl-s2")
        first.send_message(
            sentry.agent_id, "can you review the token locking?",
            idempotency_key="tpl-dm",
        )
        room = first.create_room("concurrency review", idempotency_key="tpl-room")
        first.invite(room.room_id, sentry.agent_id, idempotency_key="tpl-invite")
        second.join_room(room.room_id, idempotency_key="tpl-join")
        first.send_room_message(
            room.room_id, "here is the race", idempotency_key="tpl-rm"
        )
        task = first.create_task(
            "fix the race", "token refresh double-writes", idempotency_key="tpl-task"
        )
        first.delegate_task(
            task.task_id, sentry.agent_id, "security review",
            idempotency_key="tpl-dlg",
        )
        second.update_task(
            task.task_id, runtime.TaskStatus.RUNNING, idempotency_key="tpl-run"
        )
        # The task was delegated above, so the delegate is now its owner and
        # the runtime refuses an artifact shared by anyone else.
        second.share_artifact(
            "design note", media_type="text/markdown",
            uri="file:///tmp/design.md", sha256="c" * 64, size_bytes=2048,
            task_id=task.task_id, idempotency_key="tpl-art",
        )
        request = first.request_collaboration(
            sentry.agent_id, runtime.CollaborationKind.REVIEW,
            "please review", idempotency_key="tpl-req",
        )
        second.respond_to_collaboration(
            request.request_id, "looks fine", idempotency_key="tpl-rsp"
        )
        second.update_task(
            task.task_id, runtime.TaskStatus.COMPLETED,
            idempotency_key="tpl-done", result="locking corrected",
        )
        second.leave_room(room.room_id, idempotency_key="tpl-leave")
        first.request_capability(
            "net.fetch", scope=runtime.RuntimeScope.GLOBAL, scope_id=None,
            reason="fetch the spec", idempotency_key="tpl-cap",
        )

        # The remaining projected families.  A workload that omits them measures
        # the cheap half of the reducer table and calls it throughput.
        for suffix, status in (
            ("blocked", runtime.TaskStatus.BLOCKED),
            ("failed", runtime.TaskStatus.FAILED),
            ("cancelled", runtime.TaskStatus.CANCELLED),
        ):
            extra = first.create_task(
                f"task {suffix}", "state coverage",
                idempotency_key=f"tpl-task-{suffix}",
            )
            # OPEN -> FAILED is not a legal runtime transition; the terminal
            # states are reached through RUNNING.
            first.update_task(
                extra.task_id, runtime.TaskStatus.RUNNING,
                idempotency_key=f"tpl-{suffix}-run",
            )
            first.update_task(
                extra.task_id, status, idempotency_key=f"tpl-{suffix}"
            )

        # A broadcast collaboration is the only path that emits an explicit
        # close: a targeted response closes its request implicitly.  HELP is
        # the broadcast kind, and the runtime refuses a target agent on it.
        broadcast = first.request_collaboration(
            None, runtime.CollaborationKind.HELP,
            "anyone free to review?", idempotency_key="tpl-broadcast",
        )
        first.close_collaboration(broadcast.request_id, idempotency_key="tpl-close")

        spare = store.create_agent(
            display_name="Spare", role="analysis", specialties=("analysis",),
            idempotency_key="tpl-c",
        )
        store.update_agent_model(
            spare.agent_id, actor_id="owner", model_provider="claude-cli",
            model_name="claude-sonnet-4-5", idempotency_key="tpl-model",
        )
        store.update_agent_policy(
            spare.agent_id, actor_id="owner",
            autonomy=runtime.AgentAutonomy.NORMAL,
            authority=runtime.AgentAuthority.STANDARD,
            idempotency_key="tpl-policy",
        )
        third = store.bind_agent(spare.agent_id)
        third.start(idempotency_key="tpl-s3")
        third.pause(idempotency_key="tpl-pause")
        third.stop(idempotency_key="tpl-stop")

        events = store.replay_events(after_sequence=0, after_event_id=None, limit=500)
        return [_envelope(event) for event in events]
    finally:
        store.close()


_ENVELOPE_KEYS = (
    "sequence", "event_id", "event_type", "schema_version", "occurred_at",
    "actor_id", "project_id", "scope_kind", "scope_id", "subject_kind",
    "subject_id", "idempotency_key", "command_sha256", "payload", "result_kind",
    "result_id",
)


def _plain(value: Any) -> Any:
    return getattr(value, "value", value)


def _envelope(event: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in _ENVELOPE_KEYS:
        if key == "scope_kind":
            out[key] = _plain(getattr(event, "scope_kind", None)
                              or getattr(event, "scope", None))
        else:
            out[key] = _plain(getattr(event, key, None))
    out["payload"] = json.loads(json.dumps(out["payload"], default=str))
    return out


def _fresh_id(prefix: str, seed: str) -> str:
    return f"{prefix}_{hashlib.sha256(seed.encode()).hexdigest()[:32]}"


def _remap(value: Any, mapping: dict[str, str], cycle: int) -> Any:
    """Rewrite every opaque runtime identifier to a fresh one for this cycle."""
    if isinstance(value, str) and mb._ID_RE.match(value):
        if value not in mapping:
            prefix = value.split("_", 1)[0]
            mapping[value] = _fresh_id(prefix, f"{cycle}:{value}")
        return mapping[value]
    if isinstance(value, dict):
        return {key: _remap(item, mapping, cycle) for key, item in value.items()}
    if isinstance(value, list):
        return [_remap(item, mapping, cycle) for item in value]
    return value


class SyntheticHistory:
    """An arbitrarily long contiguous history, generated on demand.

    Materializing the stream was the first design and it made the contention
    scenario dishonest: a list big enough to keep a writer busy for six seconds
    is a hundred-megabyte file, so the list was small, the child drained it in
    under a second, and the "during ingestion" foreground numbers were really
    baseline numbers with a short disturbance in them.

    Generating on demand fixes that and costs nothing: event *n* is a pure
    function of the template and *n*, so a reader can be asked for any window
    and two processes agree without sharing anything but the template.
    """

    def __init__(self, template: Sequence[dict[str, Any]]) -> None:
        self.template = list(template)
        self.span = len(self.template)
        self._cycle_maps: dict[int, dict[str, str]] = {}

    def _mapping(self, cycle: int) -> dict[str, str]:
        mapping = self._cycle_maps.get(cycle)
        if mapping is None:
            if len(self._cycle_maps) > 64:      # bounded cache, not a leak
                self._cycle_maps.clear()
            mapping = {}
            self._cycle_maps[cycle] = mapping
        return mapping

    def event(self, sequence: int) -> dict[str, Any]:
        cycle, index = divmod(int(sequence) - 1, self.span)
        source = self.template[index]
        event = _remap(
            json.loads(json.dumps(source)), self._mapping(cycle), cycle
        )
        event["sequence"] = int(sequence)
        event["event_id"] = _fresh_id("evt", f"{cycle}:{index}:{sequence}")
        event["idempotency_key"] = f"bench-{sequence}"
        record = event.get("payload", {}).get("record")
        if isinstance(record, dict):
            canonical = json.dumps(
                record, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            )
            event["payload"]["record_sha256"] = hashlib.sha256(
                canonical.encode("utf-8")
            ).hexdigest()
        return event

    def window(self, after_sequence: int, limit: int, head: int | None = None) -> list:
        """Events ``(after_sequence, after_sequence + limit]``, capped at ``head``.

        ``head`` is how the backlog scenario models arrival: the runtime has
        only produced so many events yet, and the follower cannot read past it.
        """
        start = int(after_sequence) + 1
        stop = start + int(limit)
        if head is not None:
            stop = min(stop, int(head) + 1)
        return [self.event(sequence) for sequence in range(start, max(start, stop))]

    def reader(self, head: int | None = None) -> Callable[..., Any]:
        """A reader over this history, optionally capped at an arrival head.

        ``head`` is a plain value rather than a callback: the backlog scenario
        builds a fresh reader each poll, and a closure over its loop variable
        would be a late-binding bug waiting for the first caller who keeps the
        reader around.
        """
        def read(after_sequence: int, after_event_id: str | None, limit: int) -> Any:
            del after_event_id
            return self.window(after_sequence, limit, head)
        return read


def synthetic_history(
    template: Sequence[dict[str, Any]], count: int
) -> list[dict[str, Any]]:
    """``count`` contiguous events as a list, for scenarios that want one."""
    history = SyntheticHistory(template)
    return [history.event(sequence) for sequence in range(1, int(count) + 1)]


def list_reader(events: Sequence[dict[str, Any]]) -> Callable[..., Any]:
    def read(after_sequence: int, after_event_id: str | None, limit: int) -> Any:
        del after_event_id
        start = int(after_sequence)
        return events[start:start + int(limit)]
    return read


# ---------------------------------------------------------------------------
# Scenario A -- throughput and per-batch latency
# ---------------------------------------------------------------------------

def scenario_throughput(
    template: Sequence[dict[str, Any]],
    root: Path,
    *,
    events: int,
    repeats: int,
    batch_sizes: Sequence[int],
) -> dict[str, Any]:
    """Per-batch latency and sustained events/second at several batch sizes.

    Each repeat gets a fresh store, because a store that already holds a
    million projection rows measures index growth as well as batch cost and the
    two would be indistinguishable in one number.
    """
    history = synthetic_history(template, events)
    rows = []
    for batch_size in batch_sizes:
        per_batch: list[float] = []
        wall_times: list[float] = []
        for repeat in range(repeats):
            path = root / f"tp-{batch_size}-{repeat}.db"
            with Memory(path) as memory:
                timings: list[float] = []

                def timed_reader(after_sequence, after_event_id, limit, _t=timings):
                    return list_reader(history)(after_sequence, after_event_id, limit)

                started = time.perf_counter()
                consumed = 0
                while consumed < len(history):
                    cursor = mb.read_cursor(memory.db)
                    start_sequence = int(cursor["last_sequence"])
                    batch = history[start_sequence:start_sequence + batch_size]
                    if not batch:
                        break
                    mark = time.perf_counter()
                    with memory._immediate_transaction():
                        result = mb.import_batch(
                            memory.db, batch, now=now_iso(),
                            expected_sequence=start_sequence,
                            expected_event_id=cursor["last_event_id"],
                        )
                    timings.append(time.perf_counter() - mark)
                    if result["status"] != "active":
                        raise SystemExit(f"benchmark halted: {result}")
                    consumed += len(batch)
                elapsed = time.perf_counter() - started
                per_batch.extend(timings)
                wall_times.append(elapsed)
            path.unlink(missing_ok=True)
            for suffix in ("-wal", "-shm", ".memory-spine.key"):
                Path(str(path) + suffix).unlink(missing_ok=True)
        total_events = len(history) * repeats
        total_wall = sum(wall_times)
        rows.append({
            "batch_size": batch_size,
            "events_per_run": len(history),
            "repeats": repeats,
            "batch_latency": _summary(per_batch),
            "events_per_second": round(total_events / total_wall, 1) if total_wall else None,
            "per_event_us": round(total_wall / total_events * 1_000_000, 2)
            if total_events else None,
        })
    return {"scenario": "throughput", "results": rows}


# ---------------------------------------------------------------------------
# Scenario B -- write contention with a foreground workload
# ---------------------------------------------------------------------------

#: Child sources are substituted with ``str.replace``, never ``str.format``:
#: they contain set literals and ``format`` would read those braces as fields.
_INGEST_CHILD = """
import json, sys, time
sys.path.insert(0, "__REPO_ROOT__")
sys.path.insert(0, "__REPO_ROOT__/scripts")
from bridge_benchmark import SyntheticHistory
from jarvis import memory_bridge as mb
from jarvis.memory import Memory, now_iso

path, template_path, batch_size, seconds = sys.argv[1:5]
template = json.loads(open(template_path, encoding="utf-8").read())
history = SyntheticHistory(template)
read = history.reader()
deadline = time.perf_counter() + float(seconds)
passes = 0
halted = None
memory = Memory(path)
try:
    while time.perf_counter() < deadline:
        result = mb.ingest_once(
            memory.db, read, now=now_iso(), owner="bench-ingest",
            transaction=memory._immediate_transaction,
            batch_size=int(batch_size), max_events=int(batch_size) * 4,
            clock=now_iso,
        )
        passes += 1
        if result["status"] == "halted":
            halted = result["halt_code"]
            break
    ingested = int(mb.read_cursor(memory.db)["last_sequence"])
finally:
    memory.close()
print(json.dumps({"passes": passes, "ingested": ingested, "halted": halted}))
"""


def _foreground_samples(memory: Memory, seconds: float) -> list[float]:
    """Latency of an ordinary memory write, the operation ingestion competes with."""
    samples: list[float] = []
    deadline = time.perf_counter() + seconds
    index = 0
    while time.perf_counter() < deadline:
        mark = time.perf_counter()
        memory.remember_verified(
            f"benchmark fact {index} about deployment", "fact", "operator",
            origin="explicit_operator_memory", actor="operator", permission="bench",
        )
        samples.append(time.perf_counter() - mark)
        index += 1
    return samples


def scenario_contention(
    template: Sequence[dict[str, Any]],
    root: Path,
    *,
    seconds: float,
    batch_sizes: Sequence[int],
    events: int,
) -> dict[str, Any]:
    """Foreground write latency with and without a second process ingesting.

    Two OS processes on one store, because that is the shape that matters: the
    bridge holds ``BEGIN IMMEDIATE`` for the length of a batch and every other
    writer waits.  A same-process measurement would hide the lock behind the
    GIL and report a contention cost that nobody experiences.
    """
    del events
    template_path = root / "contention-template.json"
    template_path.write_text(json.dumps(list(template)), encoding="utf-8")

    baseline_path = root / "contention-baseline.db"
    with Memory(baseline_path) as memory:
        baseline = _foreground_samples(memory, seconds)

    rows = []
    for batch_size in batch_sizes:
        path = root / f"contention-{batch_size}.db"
        with Memory(path) as memory:
            child = subprocess.Popen(
                [sys.executable, "-c",
                 _INGEST_CHILD.replace(
                     "__REPO_ROOT__", str(REPO_ROOT).replace(chr(92), "/")),
                 str(path), str(template_path), str(batch_size), str(seconds)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            time.sleep(0.4)                       # let the child open and start
            during = _foreground_samples(memory, seconds)
            out, err = child.communicate(timeout=seconds * 6 + 60)
            if child.returncode != 0:
                raise SystemExit(f"ingest child failed: {err[-2000:]}")
            child_report = json.loads(out.strip())
            cursor = mb.read_cursor(memory.db)
        ingested = int(cursor["last_sequence"])
        rows.append({
            "batch_size": batch_size,
            "ingest_passes": int(child_report["passes"]),
            "ingested_events": ingested,
            "ingest_events_per_second": round(ingested / seconds, 1),
            "ingest_halted": child_report["halted"],
            "foreground_during": _summary(during),
        })
    return {
        "scenario": "contention",
        "foreground_operation": "Memory.remember_verified",
        "foreground_baseline": _summary(baseline),
        "results": rows,
    }


# ---------------------------------------------------------------------------
# Scenario C -- backlog growth and catch-up
# ---------------------------------------------------------------------------

def scenario_backlog(
    template: Sequence[dict[str, Any]],
    root: Path,
    *,
    arrival_rates: Sequence[int],
    seconds: float,
    poll_seconds: float,
    max_events: int,
    batch_size: int,
) -> dict[str, Any]:
    """Does a bounded worker pass keep up with a sustained arrival rate?

    The runtime's head advances on a wall clock while the consumer runs one
    bounded pass per poll interval, exactly as the worker loop does.  Backlog is
    sampled after every pass, so a rate that cannot be sustained shows as a
    rising series rather than as a single averaged number that hides it.
    """
    rows = []
    for rate in arrival_rates:
        history = SyntheticHistory(template)
        path = root / f"backlog-{rate}.db"
        backlog_series: list[int] = []
        pass_latency: list[float] = []
        with Memory(path) as memory:
            started = time.perf_counter()
            available = 0
            passes = 0
            while True:
                now_offset = time.perf_counter() - started
                if now_offset >= seconds:
                    break
                available = int(rate * now_offset)
                mark = time.perf_counter()
                mb.ingest_once(
                    memory.db, history.reader(head=available),
                    now=now_iso(), owner="bench-backlog",
                    transaction=memory._immediate_transaction,
                    max_events=max_events, batch_size=batch_size, clock=now_iso,
                )
                pass_latency.append(time.perf_counter() - mark)
                passes += 1
                cursor = int(mb.read_cursor(memory.db)["last_sequence"])
                backlog_series.append(max(0, available - cursor))
                remaining = poll_seconds - (time.perf_counter() - mark)
                if remaining > 0:
                    time.sleep(remaining)
            final_cursor = int(mb.read_cursor(memory.db)["last_sequence"])
        # Catch-up phase: stop arrivals, keep polling, measure drain.
        drain_started = time.perf_counter()
        with Memory(path) as memory:
            while True:
                cursor = int(mb.read_cursor(memory.db)["last_sequence"])
                if cursor >= available or time.perf_counter() - drain_started > seconds * 3:
                    break
                mb.ingest_once(
                    memory.db, history.reader(head=available),
                    now=now_iso(), owner="bench-backlog",
                    transaction=memory._immediate_transaction,
                    max_events=max_events, batch_size=batch_size, clock=now_iso,
                )
            drained = int(mb.read_cursor(memory.db)["last_sequence"])
        drain_seconds = time.perf_counter() - drain_started
        half = len(backlog_series) // 2 or 1
        rows.append({
            "arrival_events_per_second": rate,
            "window_seconds": seconds,
            "poll_seconds": poll_seconds,
            "max_events_per_pass": max_events,
            "batch_size": batch_size,
            "passes": passes,
            "events_offered": available,
            "events_ingested_in_window": final_cursor,
            "backlog_first_half_mean": round(
                statistics.fmean(backlog_series[:half]), 1) if backlog_series else None,
            "backlog_second_half_mean": round(
                statistics.fmean(backlog_series[half:]), 1)
            if backlog_series[half:] else None,
            "backlog_final": backlog_series[-1] if backlog_series else None,
            "backlog_max": max(backlog_series) if backlog_series else None,
            "sustained": bool(
                backlog_series and backlog_series[-1] <= max(1, rate * 0.5)
            ),
            "pass_latency": _summary(pass_latency),
            "drain_seconds": round(drain_seconds, 3),
            "drained_to": drained,
        })
    return {"scenario": "backlog", "results": rows}


# ---------------------------------------------------------------------------
# Scenario D -- lease renewal and takeover
# ---------------------------------------------------------------------------

_WORKER_CHILD = """
import json, sys, time
sys.path.insert(0, "__REPO_ROOT__")
from jarvis import memory_bridge as mb
from jarvis.memory import Memory, now_iso

path, history_path, owner, batch_size, lease_seconds = sys.argv[1:6]
events = json.loads(open(history_path, encoding="utf-8").read())
memory = Memory(path)
outcomes = []
try:
    for _ in range(200):
        def read(after_sequence, after_event_id, limit):
            start = int(after_sequence)
            return events[start:start + int(limit)]
        result = mb.ingest_once(
            memory.db, read, now=now_iso(), owner=owner,
            transaction=memory._immediate_transaction,
            batch_size=int(batch_size), max_events=int(batch_size) * 2,
            lease_seconds=int(lease_seconds), clock=now_iso,
        )
        outcomes.append(result["status"])
        if result["status"] in {"idle", "halted"}:
            break
finally:
    memory.close()
print(json.dumps({"outcomes": outcomes}))
"""


def scenario_lease(
    template: Sequence[dict[str, Any]],
    root: Path,
    *,
    events: int,
    workers: int,
    batch_size: int,
    lease_seconds: int,
) -> dict[str, Any]:
    """Renewal under a deliberately slow batch, then concurrent workers."""
    history = synthetic_history(template, events)
    history_path = root / "lease-history.json"
    history_path.write_text(json.dumps(history), encoding="utf-8")

    # D1: a pass slower than its own lease must survive by renewal.
    slow_path = root / "lease-slow.db"
    renewals: dict[str, Any]
    with Memory(slow_path) as memory:
        delay = 0.35
        def slow_reader(after_sequence, after_event_id, limit):
            time.sleep(delay)
            start = int(after_sequence)
            return history[start:start + int(limit)]

        mark = time.perf_counter()
        result = mb.ingest_once(
            memory.db, slow_reader, now=now_iso(), owner="slow-worker",
            transaction=memory._immediate_transaction,
            batch_size=5, max_events=40, lease_seconds=1, clock=now_iso,
        )
        elapsed = time.perf_counter() - mark
        renewals = {
            "reader_delay_s": delay,
            "lease_seconds": 1,
            "pass_seconds": round(elapsed, 3),
            "pass_exceeded_lease": elapsed > 1.0,
            "status": result["status"],
            "lease": result["lease"],
            "events": result["events"],
            "cursor": int(mb.read_cursor(memory.db)["last_sequence"]),
        }

    # D2: concurrent workers on one store.
    concurrent_path = root / "lease-concurrent.db"
    with Memory(concurrent_path):
        pass
    children = [
        subprocess.Popen(
            [sys.executable, "-c", _WORKER_CHILD.replace("__REPO_ROOT__", str(REPO_ROOT).replace(chr(92), "/")),
             str(concurrent_path), str(history_path), f"worker-{index}",
             str(batch_size), str(lease_seconds)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for index in range(workers)
    ]
    started = time.perf_counter()
    outcomes: list[list[str]] = []
    for child in children:
        out, err = child.communicate(timeout=600)
        if child.returncode != 0:
            raise SystemExit(f"worker failed: {err[-2000:]}")
        outcomes.append(json.loads(out.strip())["outcomes"])
    wall = time.perf_counter() - started
    with Memory(concurrent_path) as memory:
        cursor = mb.read_cursor(memory.db)
        projected = sum(
            int(memory.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in mb.BRIDGE_PROJECTION_TABLES
        )
        duplicates = int(memory.db.execute(
            """SELECT COUNT(*) FROM (
                   SELECT runtime_event_id, item_key, COUNT(*) AS c
                   FROM memory_bridge_agent_events
                   GROUP BY runtime_event_id, item_key HAVING c > 1)"""
        ).fetchone()[0])
    flat = [status for run in outcomes for status in run]
    return {
        "scenario": "lease",
        "renewal_under_slow_batches": renewals,
        "concurrent": {
            "workers": workers,
            "events_offered": len(history),
            # Wall time includes interpreter startup for each child, so it is
            # not a throughput measurement and is not reported as one.
            "wall_seconds_including_process_start": round(wall, 3),
            "cursor": int(cursor["last_sequence"]),
            "cursor_status": str(cursor["status"]),
            "projected_rows": projected,
            "duplicate_projection_rows": duplicates,
            "status_counts": {
                status: flat.count(status) for status in sorted(set(flat))
            },
            "note": (
                "correctness measure, not a throughput measure: interpreter "
                "startup dominates the wall time at these history sizes"
            ),
        },
    }


# ---------------------------------------------------------------------------
# Scenario E -- the real replay reader
# ---------------------------------------------------------------------------

def scenario_reader(root: Path, *, runtime_events: int, repeats: int) -> dict[str, Any]:
    """Latency of the real ``replay_events`` call against a real runtime store.

    This is the one cost the memory side cannot control and the one the lease
    budget actually has to cover: renewal happens once per batch, so the lease
    must outlast a single reader call plus a single import, not a whole pass.
    Measuring it is the difference between a lease budget and a guess.

    The store is driven through the runtime's own API, read-only afterwards.
    """
    from jarvis import multi_agent_runtime as runtime

    path = root / "reader-runtime.db"
    store = runtime.MultiAgentRuntimeStore(path)
    built = 0
    try:
        first = store.create_agent(
            display_name="Reader", role="bench", specialties=("bench",),
            idempotency_key="rd-a",
        )
        second = store.create_agent(
            display_name="Peer", role="bench", specialties=("bench",),
            idempotency_key="rd-b",
        )
        actor = store.bind_agent(first.agent_id)
        actor.start(idempotency_key="rd-s1")
        store.bind_agent(second.agent_id).start(idempotency_key="rd-s2")
        index = 0
        while built < runtime_events:
            actor.send_message(
                second.agent_id, f"message {index}", idempotency_key=f"rd-m{index}"
            )
            index += 1
            built = int(store.db.execute(
                "SELECT COUNT(*) FROM runtime_events"
            ).fetchone()[0])
        rows = []
        for limit in (25, 100, 250, 500):
            samples: list[float] = []
            for _ in range(repeats):
                after_sequence, after_event_id = 0, None
                while True:
                    mark = time.perf_counter()
                    batch = store.replay_events(
                        after_sequence=after_sequence,
                        after_event_id=after_event_id, limit=limit,
                    )
                    samples.append(time.perf_counter() - mark)
                    if not batch:
                        break
                    after_sequence = batch[-1].sequence
                    after_event_id = batch[-1].event_id
            rows.append({"limit": limit, "call_latency": _summary(samples)})
        return {
            "scenario": "reader",
            "runtime_events": built,
            "repeats": repeats,
            "results": rows,
            "note": (
                "replay_events verifies every event content digest, so its cost "
                "scales with the batch limit and is paid before any memory write"
            ),
        }
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Shape attestation
# ---------------------------------------------------------------------------

def _generation_cost(template: Sequence[dict[str, Any]], count: int = 4000) -> dict[str, Any]:
    """How long the harness itself takes to produce one event.

    Worth measuring rather than waving at.  The scenarios that generate events
    inside the timed window -- contention and backlog -- are paying this per
    event, so their ingest rates are not comparable with ``throughput``, which
    builds its list before the clock starts.  Quantifying it is the difference
    between a caveat and an excuse.
    """
    history = SyntheticHistory(template)
    mark = time.perf_counter()
    for sequence in range(1, count + 1):
        history.event(sequence)
    elapsed = time.perf_counter() - mark
    return {
        "events": count,
        "per_event_us": round(elapsed / count * 1_000_000, 2),
        "events_per_second": round(count / elapsed, 1),
    }


def scenario_shape(template: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """What the synthetic workload actually contains, and how it was built."""
    histogram: dict[str, int] = {}
    for event in template:
        histogram[str(event["event_type"])] = histogram.get(
            str(event["event_type"]), 0
        ) + 1
    projected = sorted(set(histogram) & set(mb.PROJECTED_EVENTS))
    ignored = sorted(set(histogram) & set(mb.IGNORED_EVENTS))
    return {
        "scenario": "shape",
        "source": "real multi_agent_runtime history, replicated with fresh identifiers",
        "template_events": len(template),
        "event_type_histogram": dict(sorted(histogram.items())),
        "projected_families_covered": projected,
        "ignored_families_covered": ignored,
        "projected_families_absent": sorted(
            set(mb.PROJECTED_EVENTS) - set(histogram)
        ),
        "harness_generation_cost": _generation_cost(template),
        "limitation": (
            "replication gives uniform entity distribution; a real store's skew "
            "(one very large room, one agent in many rooms) is not reproduced"
        ),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "scenario",
        choices=["all", "throughput", "contention", "backlog", "lease", "reader",
                 "shape"],
    )
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--events", type=int, default=2000)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--seconds", type=float, default=6.0)
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args(argv)

    report: dict[str, Any] = {
        "environment": _environment(),
        "store_pragmas": None,
        "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "scenarios": [],
    }
    with tempfile.TemporaryDirectory(prefix="jxbench-") as temporary:
        root = Path(temporary)
        report["store_pragmas"] = _store_pragmas(root)
        template = _runtime_template(root)
        wanted = (
            ["shape", "throughput", "reader", "contention", "backlog", "lease"]
            if args.scenario == "all" else [args.scenario]
        )
        for name in wanted:
            print(f"[bench] {name} ...", flush=True)
            if name == "shape":
                report["scenarios"].append(scenario_shape(template))
            elif name == "throughput":
                report["scenarios"].append(scenario_throughput(
                    template, root, events=args.events, repeats=args.repeats,
                    batch_sizes=(1, 10, 25, 50, 100, 250, 500),
                ))
            elif name == "contention":
                report["scenarios"].append(scenario_contention(
                    template, root, seconds=args.seconds,
                    batch_sizes=(25, 100, 500), events=args.events * 5,
                ))
            elif name == "backlog":
                report["scenarios"].append(scenario_backlog(
                    template, root, # 500 events per pass every 0.25 s caps the drain at
                    # 2,000/s, so the last two rates are there to show the
                    # bound biting rather than to flatter it.
                    arrival_rates=(50, 200, 800, 2000, 3000, 5000),
                    seconds=args.seconds, poll_seconds=0.25,
                    max_events=500, batch_size=100,
                ))
            elif name == "reader":
                report["scenarios"].append(scenario_reader(
                    root, runtime_events=5000, repeats=2,
                ))
            elif name == "lease":
                report["scenarios"].append(scenario_lease(
                    template, root, events=args.events, workers=args.workers,
                    batch_size=50, lease_seconds=5,
                ))
    rendered = json.dumps(report, indent=2, sort_keys=False)
    if args.out:
        args.out.write_bytes(rendered.encode("utf-8"))
        print(f"[bench] wrote {args.out.name}")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
