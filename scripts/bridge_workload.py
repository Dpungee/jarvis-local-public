#!/usr/bin/env python3
"""Workload validation for the replay bridge's proposed ingest budgets.

::

    python scripts/bridge_workload.py all --out results.json
    python scripts/bridge_workload.py skew
    python scripts/bridge_workload.py worker
    python scripts/bridge_workload.py budgets
    python scripts/bridge_workload.py status_cost

`bridge_benchmark.py` measured the bridge in isolation against a uniformly
replicated history.  That answered "how fast can a batch commit" and left three
questions open, each of which turns out to matter more than the first:

* **Skew.**  Replication gives every room the same number of messages and every
  agent the same amount of activity.  Real histories do not, and the projection
  tables are indexed on exactly the columns that skew concentrates.
* **Scheduling.**  ``max_events / poll_seconds`` is a ceiling that assumes the
  worker is idle.  The worker is a task executor first: it runs one bridge pass
  per cycle, and a cycle lasts as long as whatever task it claimed.  The ceiling
  is therefore an upper bound on a condition that may rarely hold.
* **The budgets themselves.**  A recommendation derived from continuous-ingestion
  contention is not validated by that measurement -- continuous ingestion is a
  different workload from the one the budget produces.

Nothing here activates the bridge, changes a shipped default, or touches a live
database.  Every store is a temporary file.  Test-only overrides are passed as
arguments to ``ingest_once``, which has always accepted them; the module
constants are not modified.

**On noisy runs.**  Every repeat is retained and reported.  Exclusions use one
rule, declared before the runs and applied mechanically -- a modified z-score on
the median absolute deviation -- and both the full and the filtered statistics
appear in the output.  Dropping a run because it looked wrong is how a benchmark
becomes an argument.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))

from bridge_benchmark import (                      # noqa: E402
    _environment, _store_pragmas, _summary, list_reader,
)
from jarvis import memory_bridge as mb              # noqa: E402
from jarvis.memory import Memory, now_iso           # noqa: E402

WORKLOAD_VERSION = 1

#: The shipped values, and the values the performance handoff proposed.  Both
#: are applied as call arguments only; neither constant is edited.
CURRENT_BUDGET = {"max_events": 500, "batch_size": 100, "lease_seconds": 120}
PROPOSED_BUDGET = {"max_events": 2500, "batch_size": 100, "lease_seconds": 30}


# ---------------------------------------------------------------------------
# Statistics that keep their outliers
# ---------------------------------------------------------------------------

#: Modified z-score threshold.  Declared here, before any run, so that which
#: repeats count as noise is a property of the rule rather than of the results.
OUTLIER_Z = 3.5


def _outliers(values: Sequence[float]) -> list[int]:
    """Indices of outlying repeats by modified z-score on the MAD.

    The median absolute deviation rather than the standard deviation because a
    single very slow run inflates the standard deviation enough to hide itself.
    Returns an empty list when the MAD is zero -- with no spread there is no
    basis to call anything anomalous.
    """
    if len(values) < 3:
        return []
    median = statistics.median(values)
    deviations = [abs(value - median) for value in values]
    mad = statistics.median(deviations)
    if mad == 0:
        return []
    return [
        index for index, value in enumerate(values)
        if abs(0.6745 * (value - median) / mad) > OUTLIER_Z
    ]


def _repeat_stats(values: Sequence[float], *, unit: str) -> dict[str, Any]:
    """Per-repeat aggregates, reported with and without labelled outliers."""
    indices = _outliers(values)
    kept = [value for index, value in enumerate(values) if index not in indices]
    def describe(sample):
        if not sample:
            return None
        return {
            "n": len(sample),
            "min": round(min(sample), 4),
            "median": round(statistics.median(sample), 4),
            "max": round(max(sample), 4),
            "spread_ratio": (
                round(max(sample) / min(sample), 3) if min(sample) > 0 else None
            ),
        }
    return {
        "unit": unit,
        "runs": [round(value, 4) for value in values],
        "all_runs": describe(values),
        "excluding_outliers": describe(kept),
        "outlier_indices": indices,
        "outlier_rule": f"modified z-score on MAD > {OUTLIER_Z}; runs retained above",
    }


# ---------------------------------------------------------------------------
# Skewed histories, built through the real runtime
# ---------------------------------------------------------------------------

SHAPES = ("uniform", "hot_room", "long_thread", "uneven_agents",
          "wide_fanout", "room_churn")


def _build_shape(path: Path, shape: str, messages: int) -> tuple[list, dict[str, Any]]:
    """Drive the real runtime into one distribution, then replay it.

    Built through the runtime's own API rather than replicated, because
    replication is exactly what destroys skew: remapping identifiers per cycle
    turns one hot room into many cold ones.  The store API is used directly for
    reply chains -- the bound-agent wrapper does not expose
    ``reply_to_message_id`` (see the handoff's defect note).
    """
    from jarvis import multi_agent_runtime as runtime

    store = runtime.MultiAgentRuntimeStore(path)
    try:
        agent_count = {"uneven_agents": 10, "wide_fanout": 8}.get(shape, 4)
        agents = []
        for index in range(agent_count):
            record = store.create_agent(
                display_name=f"Agent{index}", role="bench",
                specialties=("bench",), idempotency_key=f"a{index}",
            )
            store.bind_agent(record.agent_id).start(idempotency_key=f"s{index}")
            agents.append(record.agent_id)

        room_count = {"wide_fanout": max(2, messages // 8)}.get(shape, 1)
        if shape in {"uniform", "uneven_agents"}:
            # uneven_agents varies the *agent* distribution, so it gets the same
            # room count as uniform: one skewed axis at a time, or the result
            # cannot be attributed to either.
            room_count = max(2, agent_count)
        rooms = []
        for index in range(room_count):
            room = store.bind_agent(agents[index % len(agents)]).create_room(
                f"room{index}", idempotency_key=f"r{index}"
            )
            rooms.append(room.room_id)
            for member in agents:
                if member == agents[index % len(agents)]:
                    continue
                store.bind_agent(agents[index % len(agents)]).invite(
                    room.room_id, member, idempotency_key=f"i{index}-{member[:8]}"
                )
                store.bind_agent(member).join_room(
                    room.room_id, idempotency_key=f"j{index}-{member[:8]}"
                )

        if shape == "room_churn":
            # Membership transitions rather than messages.  This is the shape
            # that grows ``memory_bridge_room_members`` for one room, which is
            # what ``room_membership_intervals`` has to walk -- a read path M3
            # and M4 will depend on and that nothing has measured.
            for cycle in range(messages // 2):
                member = agents[1 + cycle % (agent_count - 1)]
                store.bind_agent(member).leave_room(
                    rooms[0], idempotency_key=f"lv{cycle}"
                )
                store.bind_agent(agents[0]).invite(
                    rooms[0], member, idempotency_key=f"iv{cycle}"
                )
                store.bind_agent(member).join_room(
                    rooms[0], idempotency_key=f"jn{cycle}"
                )

        previous_message: str | None = None
        for index in range(0 if shape == "room_churn" else messages):
            if shape == "hot_room" or shape == "long_thread":
                room_id = rooms[0]
            elif shape == "wide_fanout":
                room_id = rooms[index % len(rooms)]
            else:
                room_id = rooms[index % len(rooms)]
            if shape == "uneven_agents":
                # One agent produces ninety percent of the traffic.
                sender = agents[0] if index % 10 else agents[1 + index % (agent_count - 1)]
            else:
                sender = agents[index % len(agents)]
            reply_to = previous_message if shape == "long_thread" else None
            record = store.send_room_message(
                room_id=room_id, sender_id=sender,
                body=f"message {index}", reply_to_message_id=reply_to,
                idempotency_key=f"m{index}",
            )
            previous_message = record.message_id

        events = []
        after_sequence, after_event_id = 0, None
        while True:
            batch = store.replay_events(
                after_sequence=after_sequence, after_event_id=after_event_id,
                limit=1000,
            )
            if not batch:
                break
            events.extend(batch)
            after_sequence, after_event_id = batch[-1].sequence, batch[-1].event_id

        membership_rows = sum(
            1 for event in events
            if event.event_type in {"room.agent_joined", "room.agent_left",
                                    "room.agent_invited"}
        )
        per_room: dict[str, int] = {}
        per_agent: dict[str, int] = {}
        for event in events:
            if event.event_type == "room.message_sent":
                record = event.payload.get("record", {})
                per_room[record.get("room_id")] = per_room.get(record.get("room_id"), 0) + 1
                per_agent[record.get("sender_id")] = per_agent.get(record.get("sender_id"), 0) + 1
        descriptor = {
            "shape": shape,
            "events": len(events),
            "agents": agent_count,
            "rooms": room_count,
            "messages": messages,
            "max_messages_per_room": max(per_room.values()) if per_room else 0,
            "max_messages_per_agent": max(per_agent.values()) if per_agent else 0,
            "room_concentration": (
                round(max(per_room.values()) / sum(per_room.values()), 3)
                if per_room else None
            ),
            "agent_concentration": (
                round(max(per_agent.values()) / sum(per_agent.values()), 3)
                if per_agent else None
            ),
            "hottest_room": (
                max(per_room, key=per_room.get) if per_room else rooms[0]
            ),
            "membership_events": membership_rows,
        }
        return events, descriptor
    finally:
        store.close()


def _plain_events(events: Sequence[Any]) -> list[dict[str, Any]]:
    from bridge_benchmark import _envelope
    return [_envelope(event) for event in events]


def scenario_skew(root: Path, *, messages: int, repeats: int) -> dict[str, Any]:
    """Does entity skew change ingest cost, or only the shape of the output?"""
    rows = []
    for shape in SHAPES:
        events, descriptor = _build_shape(root / f"skew-{shape}.db", shape, messages)
        plain = _plain_events(events)
        per_batch_runs: list[float] = []
        rate_runs: list[float] = []
        interval_runs: list[float] = []
        status_runs: list[float] = []
        for repeat in range(repeats):
            store_path = root / f"skew-{shape}-{repeat}.db"
            with Memory(store_path) as memory:
                timings: list[float] = []
                started = time.perf_counter()
                consumed = 0
                while consumed < len(plain):
                    cursor = mb.read_cursor(memory.db)
                    start = int(cursor["last_sequence"])
                    batch = plain[start:start + 100]
                    if not batch:
                        break
                    mark = time.perf_counter()
                    with memory._immediate_transaction():
                        result = mb.import_batch(
                            memory.db, batch, now=now_iso(),
                            expected_sequence=start,
                            expected_event_id=cursor["last_event_id"],
                        )
                    timings.append(time.perf_counter() - mark)
                    if result["status"] != "active":
                        raise SystemExit(f"{shape} halted: {result}")
                    consumed += len(batch)
                elapsed = time.perf_counter() - started
                per_batch_runs.append(_summary(timings)["p99_ms"])
                rate_runs.append(len(plain) / elapsed)

                # The derived read paths M3/M4 will use.  Not on the ingest
                # path today, which is exactly why their cost under skew is
                # worth knowing before something depends on it.
                generation = mb.active_generation(memory.db)
                if descriptor["hottest_room"]:
                    mark = time.perf_counter()
                    mb.room_membership_intervals(
                        memory.db, generation, descriptor["hottest_room"]
                    )
                    interval_runs.append((time.perf_counter() - mark) * 1000)
                mark = time.perf_counter()
                mb.bridge_status(memory.db, now=now_iso())
                status_runs.append((time.perf_counter() - mark) * 1000)
            for suffix in ("", "-wal", "-shm", ".memory-spine.key"):
                Path(str(store_path) + suffix).unlink(missing_ok=True)
        rows.append({
            "descriptor": descriptor,
            "batch_p99_ms": _repeat_stats(per_batch_runs, unit="ms"),
            "events_per_second": _repeat_stats(rate_runs, unit="events/s"),
            "hot_room_interval_query_ms": (
                _repeat_stats(interval_runs, unit="ms") if interval_runs else None
            ),
            "bridge_status_ms": _repeat_stats(status_runs, unit="ms"),
        })
    return {"scenario": "skew", "messages_per_shape": messages, "results": rows}


# ---------------------------------------------------------------------------
# The actual worker cycle
# ---------------------------------------------------------------------------

class _FakeHeartbeat:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.lost = False

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None


class _FakeResult(str):
    def __new__(cls, content: str = "done"):
        value = str.__new__(cls, content)
        value.status = "complete"
        value.reason = None
        value.retryable = False
        value.waiting_for_approval = False
        value.approval_id = None
        return value


def _worker_condition(
    root: Path, *, duration: float, cycles: int, stream: list[dict[str, Any]]
) -> dict[str, Any]:
    """One worker-load condition, in its own scope.

    A function rather than a loop body so every closure below binds a parameter
    instead of a loop variable -- the late-binding bug this shape prevents is
    quiet, and a harness that reports the wrong condition's numbers is worse
    than one that crashes.
    """
    from jarvis import cli

    home = root / f"worker-home-{duration}"
    (home / "data").mkdir(parents=True, exist_ok=True)
    (home / "workspace").mkdir(parents=True, exist_ok=True)
    keys = ("JARVIS_DATA", "JARVIS_WORKSPACE", "JARVIS_MEMORY_BRIDGE",
            "JARVIS_MEMORY_BRIDGE_RUNTIME_DB", "JARVIS_OLLAMA_ENABLED")
    previous_env = {key: os.environ.get(key) for key in keys}
    os.environ.update({
        "JARVIS_DATA": str(home / "data"),
        "JARVIS_WORKSPACE": str(home / "workspace"),
        # Deliberately NOT "worker": the harness supplies its own reader, so no
        # runtime store is opened and no shipped default is exercised.
        "JARVIS_MEMORY_BRIDGE": "disabled",
        "JARVIS_OLLAMA_ENABLED": "false",
    })
    marks: list[float] = []
    sleeps: list[float] = []
    actual_sleeps: list[float] = []
    try:
        memory = Memory(home / "data" / "jarvis.db")
        queued = 0
        if duration > 0:
            for index in range(cycles):
                memory.add_task(f"benchmark task {index}")
                queued += 1

        def reader(after_sequence: int, after_event_id: Any, limit: int) -> Any:
            del after_event_id
            begin = int(after_sequence)
            return stream[begin:begin + int(limit)]

        def patched_pass(config: Any, mem: Any, worker_id: str) -> Any:
            mark = time.perf_counter()
            result = mb.ingest_once(
                mem.db, reader, now=now_iso(), owner=worker_id,
                transaction=mem._immediate_transaction, clock=now_iso,
                **CURRENT_BUDGET,
            )
            marks.append(time.perf_counter() - mark)
            return result

        class _Agent:
            def run(self, *args: Any, **kwargs: Any) -> Any:
                time.sleep(duration)
                return _FakeResult()

        def record_sleep(seconds: float) -> None:
            sleeps.append(seconds)
            # The real worker would sleep the whole poll interval; the harness
            # shortens it so a twelve-cycle run does not take a minute.  Both
            # the requested and the actual sleep are recorded, so the real
            # cadence at any poll interval is reconstructible rather than
            # implied -- the measured gap in an idle run is an artefact of this
            # shortening and must not be read as a cadence.
            capped = min(seconds, 0.05)
            actual_sleeps.append(capped)
            time.sleep(capped)

        original_pass = cli._run_bridge_pass
        cli._run_bridge_pass = patched_pass
        captured = io.StringIO()
        started = time.perf_counter()
        try:
            # The worker prints; capturing keeps the harness's JSON clean and
            # gives a second measurement for free -- how many lines a real loop
            # emits per cycle, which is the throttle working or not working.
            with contextlib.redirect_stdout(captured):
                cli.worker(
                    1,
                    max_cycles=cycles,
                    sleep=record_sleep,
                    memory_factory=lambda *args, **kwargs: memory,
                    agent_factory=lambda *args, **kwargs: _Agent(),
                    heartbeat_factory=_FakeHeartbeat,
                    manage_process_lock=False,
                    status_heartbeat=False,
                )
        finally:
            cli._run_bridge_pass = original_pass
        wall = time.perf_counter() - started
        # The worker closes the store it was handed, so reopen to read state.
        with Memory(home / "data" / "jarvis.db") as reopened:
            ingested = int(mb.read_cursor(reopened.db)["last_sequence"])
        printed = [
            line for line in captured.getvalue().splitlines()
            if line.strip().startswith("Bridge")
        ]
    finally:
        for key, value in previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    requested = sum(sleeps)
    work_seconds = wall - sum(actual_sleeps)

    def modelled_gap(poll: float) -> float | None:
        """Mean seconds between bridge passes at a given poll interval.

        Work time is measured; sleep time is arithmetic.  Separating them is
        what lets one short run describe the shipped five-second poll without
        the run having to take five seconds per cycle.
        """
        if not marks:
            return None
        return round((work_seconds + len(sleeps) * poll) / len(marks), 4)

    return {
        "task_seconds": duration,
        "queued_tasks": queued,
        "cycles_requested": cycles,
        "bridge_passes": len(marks),
        "wall_seconds": round(wall, 3),
        "measured_gap_seconds_harness_clock": (
            round(wall / len(marks), 4) if marks else None
        ),
        "bridge_time_seconds": round(sum(marks), 4),
        "bridge_share_of_wall": round(sum(marks) / wall, 4) if wall else None,
        "work_seconds_excluding_sleep": round(work_seconds, 4),
        "sleep_requests": len(sleeps),
        "harness_actual_sleep_seconds": round(sum(actual_sleeps), 3),
        "real_worker_sleep_would_be_seconds": round(requested, 3),
        "modelled_gap_at_poll_1s": modelled_gap(1.0),
        "modelled_gap_at_poll_5s": modelled_gap(5.0),
        "modelled_events_per_second_at_poll_5s": (
            round(CURRENT_BUDGET["max_events"] / modelled_gap(5.0), 1)
            if modelled_gap(5.0) else None
        ),
        "events_ingested": ingested,
        "events_per_pass": round(ingested / len(marks), 1) if marks else None,
        "bridge_pass_ms": _summary(marks),
        "bridge_lines_printed": len(printed),
        "bridge_lines_per_pass": (
            round(len(printed) / len(marks), 3) if marks else None
        ),
    }


def scenario_worker(
    root: Path, *, cycles: int, task_seconds: Sequence[float], events: int
) -> dict[str, Any]:
    """Measure the real worker loop, not an idealised one.

    The worker is a task executor that happens to run a bridge pass at the top
    of each cycle.  When it claims a task, the next pass waits for that task to
    finish -- so the interval between bridge passes is the *cycle* time, and the
    cycle time is whatever the workload makes it.  ``max_events / poll_seconds``
    describes the idle case and only the idle case.

    Wall-clock rates are not reported here: the harness shortens the poll sleep
    so a run finishes in seconds, which would flatter any events-per-second
    figure.  What the run does establish -- events per pass, passes per cycle,
    and the share of a cycle the bridge occupies -- does not depend on how long
    the worker sleeps.
    """
    from bridge_benchmark import SyntheticHistory, _runtime_template

    template = _runtime_template(root / "worker-template.db")
    history = SyntheticHistory(template)
    stream = [history.event(sequence) for sequence in range(1, events + 1)]

    rows = [
        _worker_condition(root, duration=duration, cycles=cycles, stream=stream)
        for duration in task_seconds
    ]
    return {
        "scenario": "worker",
        "note": (
            "the bridge runs once per worker cycle; a cycle lasts as long as the "
            "task the worker claimed, so cadence is set by the workload, not by "
            "the poll interval"
        ),
        "results": rows,
    }


# ---------------------------------------------------------------------------
# Current versus proposed budgets, under burst arrivals
# ---------------------------------------------------------------------------

_FOREGROUND_CHILD = """
import json, sys, time
sys.path.insert(0, "__REPO_ROOT__")
from jarvis.memory import Memory

path, seconds = sys.argv[1:3]
samples = []
memory = Memory(path)
try:
    deadline = time.perf_counter() + float(seconds)
    index = 0
    while time.perf_counter() < deadline:
        mark = time.perf_counter()
        memory.remember_verified(
            "workload fact %d about deployment" % index, "fact", "operator",
            origin="explicit_operator_memory", actor="operator", permission="bench",
        )
        samples.append(time.perf_counter() - mark)
        index += 1
finally:
    memory.close()
print(json.dumps({"samples": samples}))
"""


def scenario_budgets(
    root: Path, *, seconds: float, poll_seconds: float, burst_multiple: int,
    base_rate: int, repeats: int,
) -> dict[str, Any]:
    """A/B the two budgets under bursty arrivals with a live foreground writer.

    Bursty rather than uniform because a uniform arrival rate below the ceiling
    never exercises the thing a per-pass bound is for.  The foreground writer is
    a separate process doing ordinary memory writes throughout, so the latency
    figures belong to *this* duty cycle rather than to the continuous-ingestion
    measurement, which is a different workload and is not evidence about this one.
    """
    from bridge_benchmark import SyntheticHistory, _runtime_template
    template = _runtime_template(root / "budget-template.db")

    rows = []
    for label, budget in (("current", CURRENT_BUDGET), ("proposed", PROPOSED_BUDGET)):
        catchup_runs: list[float] = []
        backlog_runs: list[float] = []
        p95_runs: list[float] = []
        p99_runs: list[float] = []
        for repeat in range(repeats):
            path = root / f"budget-{label}-{repeat}.db"
            history = SyntheticHistory(template)
            with Memory(path) as memory:
                child = subprocess.Popen(
                    [sys.executable, "-c",
                     _FOREGROUND_CHILD.replace(
                         "__REPO_ROOT__", str(REPO_ROOT).replace(chr(92), "/")),
                     str(path), str(seconds)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
                time.sleep(0.3)
                started = time.perf_counter()
                backlog_series: list[int] = []
                available = 0
                burst_at = seconds / 2
                in_burst = False
                while True:
                    offset = time.perf_counter() - started
                    if offset >= seconds:
                        break
                    # A single burst halfway through: base rate, then a spike.
                    in_burst = burst_at <= offset < burst_at + 1.0
                    rate = base_rate * (burst_multiple if in_burst else 1)
                    available += int(rate * poll_seconds)
                    mark = time.perf_counter()
                    mb.ingest_once(
                        memory.db, history.reader(head=available), now=now_iso(),
                        owner=f"bench-{label}",
                        transaction=memory._immediate_transaction,
                        clock=now_iso, **budget,
                    )
                    cursor = int(mb.read_cursor(memory.db)["last_sequence"])
                    backlog_series.append(max(0, available - cursor))
                    remaining = poll_seconds - (time.perf_counter() - mark)
                    if remaining > 0:
                        time.sleep(remaining)
                # Catch-up: stop arrivals, keep polling until drained.
                drain_started = time.perf_counter()
                while True:
                    cursor = int(mb.read_cursor(memory.db)["last_sequence"])
                    if cursor >= available:
                        break
                    if time.perf_counter() - drain_started > seconds * 4:
                        break
                    mb.ingest_once(
                        memory.db, history.reader(head=available), now=now_iso(),
                        owner=f"bench-{label}",
                        transaction=memory._immediate_transaction,
                        clock=now_iso, **budget,
                    )
                    time.sleep(poll_seconds)
                catchup = time.perf_counter() - drain_started
                out, err = child.communicate(timeout=seconds * 6 + 60)
                if child.returncode != 0:
                    raise SystemExit(f"foreground child failed: {err[-2000:]}")
                samples = json.loads(out.strip())["samples"]
            for suffix in ("", "-wal", "-shm", ".memory-spine.key"):
                Path(str(path) + suffix).unlink(missing_ok=True)
            summary = _summary(samples)
            p95_runs.append(summary["p95_ms"])
            p99_runs.append(summary["p99_ms"])
            backlog_runs.append(float(max(backlog_series) if backlog_series else 0))
            catchup_runs.append(catchup)
        rows.append({
            "budget": label,
            "settings": dict(budget),
            "arrival": {
                "base_events_per_second": base_rate,
                "burst_multiple": burst_multiple,
                "burst_seconds": 1.0,
                "poll_seconds": poll_seconds,
                "window_seconds": seconds,
            },
            "foreground_p95_ms": _repeat_stats(p95_runs, unit="ms"),
            "foreground_p99_ms": _repeat_stats(p99_runs, unit="ms"),
            "peak_backlog_events": _repeat_stats(backlog_runs, unit="events"),
            "catchup_seconds": _repeat_stats(catchup_runs, unit="s"),
        })
    return {"scenario": "budgets", "results": rows}


# ---------------------------------------------------------------------------
# Lease recovery
# ---------------------------------------------------------------------------

_LEASE_CRASH_CHILD = """
import os, sys
sys.path.insert(0, "__REPO_ROOT__")
from jarvis import memory_bridge as mb
from jarvis.memory import Memory, now_iso

path, seconds = sys.argv[1:3]
memory = Memory(path)
with memory._immediate_transaction():
    mb.acquire_ingest_lease(
        memory.db, owner="doomed", now=now_iso(), lease_seconds=int(seconds)
    )
os._exit(0)
"""


def scenario_lease_recovery(root: Path, *, lease_seconds: Sequence[int]) -> dict[str, Any]:
    """How long a dead holder blocks ingestion, for each candidate lease.

    Measured by simulated clock rather than by waiting: the lease compares ISO
    timestamps the caller supplies, so a 120-second recovery can be observed
    without spending 120 seconds.  What is measured is the *decision boundary* --
    the point at which a replacement is allowed in -- not the wall clock.
    """
    rows = []
    for duration in lease_seconds:
        path = root / f"lease-{duration}.db"
        with Memory(path):
            pass
        completed = subprocess.run(
            [sys.executable, "-c",
             _LEASE_CRASH_CHILD.replace(
                 "__REPO_ROOT__", str(REPO_ROOT).replace(chr(92), "/")),
             str(path), str(duration)],
            capture_output=True, text=True, timeout=120,
        )
        if completed.returncode != 0:
            raise SystemExit(f"crash child failed: {completed.stderr[-2000:]}")
        base = now_iso()
        reclaimed_at = None
        with Memory(path) as memory:
            held = mb.lease_state(memory.db)["held"]
            for offset in range(0, int(duration) + 30):
                stamp = (
                    datetime.fromisoformat(base) + timedelta(seconds=offset)
                ).isoformat()
                with memory._immediate_transaction():
                    token = mb.acquire_ingest_lease(
                        memory.db, owner="replacement", now=stamp,
                        lease_seconds=int(duration),
                    )
                if token is not None:
                    reclaimed_at = offset
                    break
        rows.append({
            "lease_seconds": duration,
            "lease_held_after_crash": bool(held),
            "reclaim_possible_after_seconds": reclaimed_at,
            "blocked_seconds": reclaimed_at,
        })
    return {
        "scenario": "lease_recovery",
        "note": (
            "a dead holder blocks ingestion for the full lease period; the lease "
            "is released on expiry, not on process death"
        ),
        "results": rows,
    }


# ---------------------------------------------------------------------------
# Derived read paths, which nothing on the ingest path exercises
# ---------------------------------------------------------------------------

def scenario_interval_cost(
    root: Path, *, transition_counts: Sequence[int], repeats: int
) -> dict[str, Any]:
    """How ``room_membership_intervals`` grows with one room's churn.

    Nothing calls it today -- it is the derived half of the contract, waiting
    for M3 and M4 -- which is exactly why its shape is worth knowing now.  It
    walks every membership transition for a room to fold them into half-open
    intervals, so a long-lived room with constant joining and leaving is its
    worst case, and a long-lived room is the normal case.
    """
    rows = []
    for transitions in transition_counts:
        path = root / f"interval-{transitions}.db"
        events, descriptor = _build_shape(path, "room_churn", transitions)
        plain = _plain_events(events)
        store_path = root / f"interval-store-{transitions}.db"
        with Memory(store_path) as memory:
            mb.ingest_once(
                memory.db, list_reader(plain), now=now_iso(), owner="interval-bench",
                transaction=memory._immediate_transaction, clock=now_iso,
                max_events=len(plain) + 10, batch_size=250,
            )
            generation = mb.active_generation(memory.db)
            room_id = descriptor["hottest_room"]
            member_rows = int(memory.db.execute(
                "SELECT COUNT(*) FROM memory_bridge_room_members WHERE room_id=?",
                (room_id,),
            ).fetchone()[0])
            samples = []
            for _ in range(repeats):
                mark = time.perf_counter()
                intervals = mb.room_membership_intervals(
                    memory.db, generation, room_id
                )
                samples.append(time.perf_counter() - mark)
        for suffix in ("", "-wal", "-shm", ".memory-spine.key"):
            Path(str(store_path) + suffix).unlink(missing_ok=True)
        rows.append({
            "requested_transitions": transitions,
            "membership_rows_for_room": member_rows,
            "intervals_returned": len(intervals),
            "room_membership_intervals": _summary(samples),
        })
    return {
        "scenario": "interval_cost",
        "note": (
            "derived read path; not on the ingest path today, and not exposed "
            "to any agent surface"
        ),
        "results": rows,
    }


# ---------------------------------------------------------------------------
# Operator surface cost as the store grows
# ---------------------------------------------------------------------------

def scenario_status_cost(root: Path, *, sizes: Sequence[int], repeats: int) -> dict[str, Any]:
    """Does ``jarvis bridge status`` stay usable as projections accumulate?

    It counts every projection table.  An operator surface whose cost grows with
    the data it reports on is a surface that stops being used at the point it
    matters most, so the growth is worth knowing before activation rather than
    after.
    """
    from bridge_benchmark import SyntheticHistory, _runtime_template
    template = _runtime_template(root / "status-template.db")
    history = SyntheticHistory(template)

    rows = []
    for size in sizes:
        path = root / f"status-{size}.db"
        stream = [history.event(sequence) for sequence in range(1, size + 1)]
        with Memory(path) as memory:
            mb.ingest_once(
                memory.db, list_reader(stream), now=now_iso(), owner="status-bench",
                transaction=memory._immediate_transaction, clock=now_iso,
                max_events=size + 10, batch_size=250,
            )
            projected = sum(
                int(memory.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in mb.BRIDGE_PROJECTION_TABLES
            )
            samples = []
            for _ in range(repeats):
                mark = time.perf_counter()
                mb.bridge_status(memory.db, now=now_iso())
                samples.append(time.perf_counter() - mark)
        for suffix in ("", "-wal", "-shm", ".memory-spine.key"):
            Path(str(path) + suffix).unlink(missing_ok=True)
        rows.append({
            "runtime_events": size,
            "projected_rows": projected,
            "bridge_status": _summary(samples),
        })
    return {"scenario": "status_cost", "results": rows}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "scenario",
        choices=["all", "skew", "worker", "budgets", "lease_recovery",
                 "status_cost", "interval_cost"],
    )
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--messages", type=int, default=3000)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--seconds", type=float, default=8.0)
    parser.add_argument("--cycles", type=int, default=12)
    args = parser.parse_args(argv)

    report: dict[str, Any] = {
        "environment": {**_environment(), "workload_version": WORKLOAD_VERSION},
        "store_pragmas": None,
        "budgets_compared": {"current": CURRENT_BUDGET, "proposed": PROPOSED_BUDGET},
        "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "scenarios": [],
    }
    with tempfile.TemporaryDirectory(prefix="jxwork-") as temporary:
        root = Path(temporary)
        report["store_pragmas"] = _store_pragmas(root)
        wanted = (
            ["skew", "worker", "budgets", "lease_recovery", "status_cost",
             "interval_cost"]
            if args.scenario == "all" else [args.scenario]
        )
        for name in wanted:
            print(f"[workload] {name} ...", flush=True)
            if name == "skew":
                report["scenarios"].append(scenario_skew(
                    root, messages=args.messages, repeats=args.repeats
                ))
            elif name == "worker":
                report["scenarios"].append(scenario_worker(
                    root, cycles=args.cycles, task_seconds=(0.0, 0.25, 2.0),
                    events=20000,
                ))
            elif name == "budgets":
                report["scenarios"].append(scenario_budgets(
                    root, seconds=args.seconds, poll_seconds=0.5,
                    burst_multiple=20, base_rate=100, repeats=max(3, args.repeats),
                ))
            elif name == "lease_recovery":
                report["scenarios"].append(scenario_lease_recovery(
                    root, lease_seconds=(120, 30, 15)
                ))
            elif name == "interval_cost":
                report["scenarios"].append(scenario_interval_cost(
                    root, transition_counts=(300, 1200, 4800), repeats=7
                ))
            elif name == "status_cost":
                report["scenarios"].append(scenario_status_cost(
                    root, sizes=(1000, 5000, 20000, 60000), repeats=7
                ))
    rendered = json.dumps(report, indent=2, sort_keys=False)
    if args.out:
        args.out.write_bytes(rendered.encode("utf-8"))
        print(f"[workload] wrote {args.out.name}")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
