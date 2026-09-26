"""Ingest health, the bounded incident ledger, and operator reporting.

Three properties are load-bearing here, and each of them is a requirement from
the assignment rather than a nicety:

* **A failure is never rendered as healthy.**  A pass that could not persist a
  halt is not a halt; a pass that lost its lease ingested nothing; a cursor that
  is ``active`` while the last pass failed is not a running bridge.
* **Diagnostics are bounded.**  The incident ledger is keyed on a closed code
  set, so it cannot grow with the number of failures, and worker output is
  throttled so a permanent condition is not restated every poll forever.
* **Diagnostics are sanitized.**  Everything written here goes through the same
  ``_safe_detail`` screen a halt reason does.

Every store is a disposable temporary file.  The bridge is never activated.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from types import SimpleNamespace
from unittest.mock import patch

from jarvis import cli, memory_bridge as mb
from jarvis.memory import Memory, now_iso

from tests.test_memory_bridge import agent_record, event
from tests.test_memory_bridge_corrections import PRIVATE_PROJECT, _later


class HealthCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="jxhealth-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.path = self.root / "memory.db"
        self.memory = Memory(self.path)
        self.addCleanup(self.memory.close)
        self.db = self.memory.db

    def _history(self, count: int = 6) -> list[dict]:
        events = [event(1, "agent.created", agent_record(1))]
        for index in range(2, count + 1):
            events.append(
                event(index, "agent.running", agent_record(1, lifecycle="RUNNING"))
            )
        return events

    def _reader(self, events):
        def read(after_sequence, after_event_id, limit):
            del after_event_id
            start = int(after_sequence)
            return events[start:start + int(limit)]
        return read

    def _ingest(self, reader, **kwargs):
        kwargs.setdefault("now", now_iso())
        kwargs.setdefault("owner", "worker-a")
        return mb.ingest_once(
            self.db, reader,
            transaction=self.memory._immediate_transaction, **kwargs
        )

    def _health(self) -> dict:
        return mb.bridge_health(self.db, now=now_iso())

    def _count(self, table: str) -> int:
        return int(self.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    def _drive_worker_reporting(
        self, reader, *, polls: int, status: str | None = None, **kwargs
    ) -> list[str]:
        """Run N real passes and collect exactly what a worker would print.

        Mirrors the worker loop's own bookkeeping -- previous status plus a
        consecutive-repeat counter -- so the throttle is exercised through the
        contract the worker actually uses rather than through a convenient
        stand-in.
        """
        printed: list[str] = []
        previous: str | None = None
        repeats = 0
        for _ in range(polls):
            summary = self._ingest(reader, **kwargs)
            if status is not None:
                self.assertEqual(summary["status"], status, summary)
            repeats = repeats + 1 if summary["status"] == previous else 0
            printed.extend(
                cli._bridge_pass_report(self.memory, summary, previous, repeats)
            )
            previous = str(summary["status"])
        return printed


# ---------------------------------------------------------------------------
# Schema and vocabulary
# ---------------------------------------------------------------------------

class VocabularyTests(HealthCase):
    def test_the_health_tables_exist_and_are_owned_by_the_bridge(self) -> None:
        for table in ("memory_bridge_health", "memory_bridge_incidents"):
            self.assertIn(table, mb.BRIDGE_CONTROL_TABLES)
            self.assertIsNotNone(
                self.db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone(),
                table,
            )

    def test_incident_codes_are_exactly_the_unhealthy_statuses(self) -> None:
        self.assertEqual(
            mb.INCIDENT_CODES, mb.BRIDGE_PASS_STATUSES - mb.HEALTHY_PASS_STATUSES
        )
        # The failures the review named must each be their own code.
        for code in ("halted", "halt_not_recorded", "lease_lost",
                     "reader_unavailable", "reader_error"):
            self.assertIn(code, mb.INCIDENT_CODES, code)
        # And the outcomes that are not failures must not be.
        for code in ("ok", "idle", "bounded", "lease_held_elsewhere"):
            self.assertNotIn(code, mb.INCIDENT_CODES, code)

    def test_an_unknown_status_is_refused(self) -> None:
        with self.memory._immediate_transaction():
            with self.assertRaises(mb.BridgeError):
                mb.record_pass(self.db, status="probably_fine", now=now_iso())


# ---------------------------------------------------------------------------
# A failure is never healthy
# ---------------------------------------------------------------------------

class FailureIsNeverHealthyTests(HealthCase):
    def test_a_reader_failure_records_an_incident_and_is_not_healthy(self) -> None:
        def read(after_sequence, after_event_id, limit):
            raise mb.ReaderFailure("the store is busy")

        summary = self._ingest(read)
        self.assertEqual(summary["status"], "reader_unavailable")
        health = self._health()
        self.assertFalse(health["healthy"])
        self.assertTrue(health["last_pass_failed"])
        self.assertEqual(health["last_pass_status"], "reader_unavailable")
        self.assertEqual(health["consecutive_failures"], 1)
        codes = [item["code"] for item in health["incidents"]]
        self.assertEqual(codes, ["reader_unavailable"])

    def test_an_unpersisted_halt_is_never_recorded_as_a_durable_halt(self) -> None:
        """The distinction the review insisted on, carried into diagnostics.

        ``halt_not_recorded`` means the bridge hit a structural failure and could
        not write the halt.  Recording that as ``halted`` would send an operator
        to clear a flag that was never set, and leave the real condition -- a
        worker whose halt write is being refused -- unnamed.
        """
        outer = self

        def read(after_sequence, after_event_id, limit):
            with outer.memory._immediate_transaction():
                mb.import_batch(
                    outer.db, outer._history(1), now=now_iso()
                )
            raise mb.ReaderFailure(
                "history is unusable", code="reader_history_invalid"
            )

        summary = self._ingest(read)
        self.assertEqual(summary["status"], "halt_not_recorded")
        health = self._health()
        self.assertEqual(health["last_pass_status"], "halt_not_recorded")
        self.assertFalse(health["healthy"])
        self.assertFalse(health["halted"], "the consumer was not actually halted")
        codes = [item["code"] for item in health["incidents"]]
        self.assertIn("halt_not_recorded", codes)
        self.assertNotIn("halted", codes)

    def test_a_durable_halt_is_recorded_as_halted_and_never_as_healthy(self) -> None:
        self._ingest(self._reader([event(2, "agent.created", agent_record(1))]))
        health = self._health()
        self.assertTrue(health["halted"])
        self.assertFalse(health["healthy"])
        self.assertEqual(health["last_pass_status"], "halted")

    def test_a_lost_lease_is_a_failure_not_a_quiet_success(self) -> None:
        outer = self
        events = self._history(4)
        seen: list[int] = []

        def read(after_sequence, after_event_id, limit):
            seen.append(int(after_sequence))
            if len(seen) == 2:
                with outer.memory._immediate_transaction():
                    mb.acquire_ingest_lease(
                        outer.db, owner="replacement", now=_later(121)
                    )
            start = int(after_sequence)
            return events[start:start + int(limit)]

        summary = self._ingest(read, batch_size=2)
        self.assertEqual(summary["status"], "lease_lost")
        health = self._health()
        self.assertFalse(health["healthy"])
        self.assertIn("lease_lost", [item["code"] for item in health["incidents"]])

    def test_a_cursor_that_is_active_after_a_failed_pass_is_still_not_healthy(self) -> None:
        """The single-field trap: `status == active` is not `healthy`."""
        def read(after_sequence, after_event_id, limit):
            raise RuntimeError("unclassified")

        self._ingest(read)
        report = mb.bridge_status(self.db, now=now_iso())
        self.assertEqual(report["status"], "active")
        self.assertFalse(report["health"]["healthy"])

    def test_recovery_clears_the_consecutive_failure_count(self) -> None:
        def failing(after_sequence, after_event_id, limit):
            raise mb.ReaderFailure("busy")

        self._ingest(failing)
        self._ingest(failing)
        self.assertEqual(self._health()["consecutive_failures"], 2)
        self._ingest(self._reader(self._history(4)))
        health = self._health()
        self.assertEqual(health["consecutive_failures"], 0)
        self.assertTrue(health["healthy"])
        # The incident history is kept: recovery is not amnesia.
        self.assertIn(
            "reader_unavailable", [item["code"] for item in health["incidents"]]
        )


# ---------------------------------------------------------------------------
# Bounded diagnostics
# ---------------------------------------------------------------------------

class BoundedDiagnosticsTests(HealthCase):
    def test_a_flood_of_one_failure_stays_one_row(self) -> None:
        """The table is bounded by its key space, not by a retention policy."""
        def failing(after_sequence, after_event_id, limit):
            raise mb.ReaderFailure("busy")

        for _ in range(40):
            self._ingest(failing)
        self.assertEqual(self._count("memory_bridge_incidents"), 1)
        self.assertEqual(self._count("memory_bridge_health"), 1)
        incident = self._health()["incidents"][0]
        self.assertEqual(incident["code"], "reader_unavailable")
        self.assertEqual(incident["occurrences"], 40)
        self.assertNotEqual(incident["first_seen"], None)

    def test_the_ledger_can_never_exceed_the_closed_code_set(self) -> None:
        for code in sorted(mb.INCIDENT_CODES):
            with self.memory._immediate_transaction():
                mb._record_incident(
                    self.db, code=code, now=now_iso(),
                    consumer=mb.DEFAULT_CONSUMER, detail=None, cursor_sequence=0,
                )
                mb._record_incident(
                    self.db, code=code, now=now_iso(),
                    consumer=mb.DEFAULT_CONSUMER, detail=None, cursor_sequence=0,
                )
        self.assertEqual(
            self._count("memory_bridge_incidents"), len(mb.INCIDENT_CODES)
        )

    def test_quiet_healthy_passes_do_not_rewrite_the_health_row(self) -> None:
        """A five-second poll must not commit a write forever while idle."""
        empty = self._reader([])
        self._ingest(empty)
        first = self.db.execute(
            "SELECT updated_at FROM memory_bridge_health"
        ).fetchone()[0]
        for _ in range(10):
            self._ingest(empty)
        second = self.db.execute(
            "SELECT updated_at FROM memory_bridge_health"
        ).fetchone()[0]
        self.assertEqual(first, second, "an idle pass rewrote the health row")

    def test_the_heartbeat_still_refreshes_an_idle_row_eventually(self) -> None:
        self._ingest(self._reader([]))
        with self.memory._immediate_transaction():
            wrote = mb.record_pass(
                self.db, status="idle", now=_later(3600), heartbeat_seconds=60
            )
        self.assertTrue(wrote, "the heartbeat never refreshed a stale health row")

    def test_a_failure_is_always_written_even_when_repeated(self) -> None:
        def failing(after_sequence, after_event_id, limit):
            raise mb.ReaderFailure("busy")

        self._ingest(failing)
        first = self.db.execute(
            "SELECT updated_at, consecutive_failures FROM memory_bridge_health"
        ).fetchone()
        self._ingest(failing)
        second = self.db.execute(
            "SELECT updated_at, consecutive_failures FROM memory_bridge_health"
        ).fetchone()
        self.assertEqual(int(second[1]), int(first[1]) + 1)

    def test_report_throttling_says_it_once_then_escalates(self) -> None:
        """Say it when it starts, then on powers of two.  Never every poll."""
        self.assertTrue(mb.should_report_pass(None, "halted", 1))
        self.assertTrue(mb.should_report_pass("ok", "halted", 1))
        printed = [
            count for count in range(1, 200)
            if mb.should_report_pass("halted", "halted", count)
        ]
        self.assertEqual(printed, [1, 2, 4, 8, 16, 32, 64, 128])
        # A change of condition always reports, whatever the count.
        self.assertTrue(mb.should_report_pass("halted", "reader_error", 37))

    def test_the_worker_does_not_restate_a_halt_every_poll(self) -> None:
        """Forty polls against a halted bridge, counting what a worker prints.

        The old worker printed on every one of them.  The rule here is the
        escalating one, so forty polls produce a handful of lines: the onset and
        then powers of two.
        """
        reader = self._reader([event(2, "agent.created", agent_record(1))])
        printed = self._drive_worker_reporting(reader, polls=40, status="halted")
        self.assertTrue(printed)
        self.assertIn("HALTED", printed[0])
        # 1, 2, 4, 8, 16, 32 -> six lines for forty polls, not forty.
        self.assertEqual(len(printed), 6, printed)

    def test_a_permanently_bounded_bridge_does_not_print_every_poll(self) -> None:
        """The status most likely to repeat forever was the one with no throttle.

        Found by driving the real worker loop rather than by reading the code:
        twelve cycles produced twelve identical "still waiting" lines.  A halt
        is an incident somebody clears; being merely behind is the ordinary
        steady state under a small per-pass bound, so ``bounded`` is precisely
        the outcome that must not restate itself on every cycle.
        """
        events = self._history(400)
        printed = self._drive_worker_reporting(
            self._reader(events), polls=32, status="bounded",
            max_events=8, batch_size=4,
        )
        self.assertTrue(printed)
        self.assertIn("still waiting", printed[0])
        # 1, 2, 4, 8, 16, 32 -> six lines for thirty-two polls, not thirty-two.
        self.assertEqual(len(printed), 6, printed)

    def test_every_repeated_outcome_is_throttled_not_just_failures(self) -> None:
        """One rule for all of them, so a new status cannot arrive unthrottled."""
        for status in sorted(mb.BRIDGE_PASS_STATUSES):
            lines = [
                line
                for repeats in range(1, 20)
                for line in cli._bridge_pass_report(
                    self.memory, {"status": status}, status, repeats
                )
            ]
            # Nineteen consecutive repeats may print at most the powers of two
            # within them: 2, 4, 8, 16.
            self.assertLessEqual(len(lines), 4, f"{status} printed {len(lines)}")

    def test_the_worker_reports_a_reader_failure_that_used_to_be_silent(self) -> None:
        for status in ("reader_unavailable", "reader_error", "lease_lost",
                       "halt_not_recorded", "runtime_unavailable", "error"):
            lines = cli._bridge_pass_report(self.memory, {"status": status}, None, 0)
            self.assertTrue(lines, f"{status} printed nothing at all")
            # A failure must never wear the success line's shape.
            self.assertFalse(lines[0].startswith("Bridge ingested"), status)

    def test_status_never_calls_a_failing_bridge_plainly_active(self) -> None:
        """The header is the first thing an operator reads, so it must not lie."""
        def failing(after_sequence, after_event_id, limit):
            raise mb.ReaderFailure("busy")

        self._ingest(failing)
        report = mb.bridge_status(self.db, now=now_iso())
        self.assertEqual(report["status"], "active")
        self.assertEqual(cli._bridge_status_state(report), "ACTIVE, DEGRADED")

    def test_status_says_active_when_the_bridge_really_is(self) -> None:
        self._ingest(self._reader(self._history(4)))
        report = mb.bridge_status(self.db, now=now_iso())
        self.assertEqual(cli._bridge_status_state(report), "ACTIVE")

    def test_status_says_halted_when_halted(self) -> None:
        self._ingest(self._reader([event(2, "agent.created", agent_record(1))]))
        report = mb.bridge_status(self.db, now=now_iso())
        self.assertEqual(cli._bridge_status_state(report), "HALTED")

    def test_ingestion_yields_to_an_active_foreground_request(self) -> None:
        """The yield already existed; ingestion was outside it.

        The bridge pass runs at the top of the worker cycle, before the
        foreground check further down.  With a foreground request active the
        cycle becomes a one-second loop, so an ungated bridge would take the
        memory write lock most often exactly while somebody is waiting on it --
        the opposite of what the yield is for.
        """
        config = SimpleNamespace(
            memory_bridge="worker",
            memory_bridge_runtime_db=str(self.root / "runtime.db"),
            data_dir=self.root,
        )
        (self.root / "runtime.db").write_bytes(b"")
        self.memory._bridge_ready = True
        with patch.object(cli, "_runtime_state", return_value="running"):
            with patch.object(cli, "_foreground_request_active", return_value=True):
                result = cli._run_bridge_pass(config, self.memory, "worker-a")
        self.assertEqual(result["status"], "foreground_yield")
        self.assertEqual(mb.read_cursor(self.db)["last_sequence"], 0)
        health = self._health()
        self.assertEqual(health["last_pass_status"], "foreground_yield")
        # A yield is not a failure: it must not be an incident and must not
        # make the bridge look unhealthy.
        self.assertTrue(health["healthy"])
        self.assertEqual(health["incidents"], [])
        self.assertNotIn("foreground_yield", mb.INCIDENT_CODES)

    def test_a_quiet_foreground_does_not_block_ingestion(self) -> None:
        """The gate must be a yield, not an off switch."""
        config = SimpleNamespace(
            memory_bridge="worker",
            memory_bridge_runtime_db=str(self.root / "absent.db"),
            data_dir=self.root,
        )
        self.memory._bridge_ready = True
        with patch.object(cli, "_runtime_state", return_value="running"):
            with patch.object(cli, "_foreground_request_active", return_value=False):
                result = cli._run_bridge_pass(config, self.memory, "worker-a")
        # The runtime store is absent, so the pass gets that far and stops for
        # that reason -- not for the yield.
        self.assertEqual(result["status"], "runtime_unavailable")

    def test_every_healthy_outcome_except_bounded_stays_quiet(self) -> None:
        """Derived from the vocabulary, so a new status cannot arrive noisy."""
        for status in sorted(mb.HEALTHY_PASS_STATUSES):
            if status == "bounded":
                continue
            self.assertEqual(
                cli._bridge_pass_report(self.memory, {"status": status}, None, 0), [],
                status,
            )

    def test_every_failing_outcome_says_something(self) -> None:
        for status in sorted(mb.INCIDENT_CODES):
            self.assertTrue(
                cli._bridge_pass_report(self.memory, {"status": status}, None, 0),
                f"{status} printed nothing",
            )


# ---------------------------------------------------------------------------
# Backlog
# ---------------------------------------------------------------------------

class BacklogTests(HealthCase):
    def test_a_drained_stream_reports_caught_up(self) -> None:
        self._ingest(self._reader(self._history(6)))
        health = self._health()
        self.assertTrue(health["caught_up"])
        self.assertTrue(health["healthy"])

    def test_a_bounded_pass_with_work_left_reports_a_lower_bound(self) -> None:
        """`bounded` alone says nothing; the probe turns it into a fact."""
        events = self._history(30)
        summary = self._ingest(self._reader(events), max_events=6, batch_size=3)
        self.assertEqual(summary["status"], "bounded")
        health = self._health()
        self.assertFalse(health["caught_up"])
        self.assertGreaterEqual(health["behind_at_least"], 1)

    def test_a_bounded_pass_that_finished_the_stream_reports_caught_up(self) -> None:
        events = self._history(6)
        summary = self._ingest(self._reader(events), max_events=6, batch_size=3)
        self.assertEqual(summary["status"], "bounded")
        self.assertTrue(
            self._health()["caught_up"],
            "a pass that consumed the whole stream still claimed a backlog",
        )

    def test_a_probe_failure_leaves_the_backlog_unknown_not_wrong(self) -> None:
        events = self._history(30)
        calls = {"n": 0}

        def read(after_sequence, after_event_id, limit):
            calls["n"] += 1
            start = int(after_sequence)
            batch = events[start:start + int(limit)]
            if start >= 6:                     # the probe, after the bound
                raise mb.ReaderFailure("busy on the probe")
            return batch

        summary = self._ingest(read, max_events=6, batch_size=3)
        self.assertEqual(summary["status"], "bounded", summary)
        health = self._health()
        self.assertIsNone(health["caught_up"])
        self.assertEqual(health["last_pass_status"], "bounded")

    def test_a_failing_pass_does_not_erase_the_last_backlog_measurement(self) -> None:
        """Losing the measurement while failing loses it exactly when it matters."""
        self._ingest(self._reader(self._history(30)), max_events=6, batch_size=3)
        measured = self._health()
        self.assertFalse(measured["caught_up"])
        self.assertGreaterEqual(measured["behind_at_least"], 1)
        observed_at = measured["observed_at"]

        def failing(after_sequence, after_event_id, limit):
            raise mb.ReaderFailure("busy")

        for _ in range(3):
            self._ingest(failing)
        after = self._health()
        self.assertFalse(after["healthy"])
        self.assertIs(after["caught_up"], False, "the backlog verdict was erased")
        self.assertEqual(after["behind_at_least"], measured["behind_at_least"])
        self.assertEqual(
            after["observed_at"], observed_at,
            "a pass that measured nothing claimed a fresh observation",
        )

    def test_a_bounded_pass_never_claims_catch_up_it_did_not_measure(self) -> None:
        """The reassuring sentence is the one that must be earned.

        A bounded pass whose backlog probe failed knows nothing about what is
        left.  Saying "it caught up on this pass" there is the same class of
        error as calling a failing bridge healthy, and it is easier to make,
        because "bounded" reads like a benign outcome.
        """
        events = self._history(30)

        def read(after_sequence, after_event_id, limit):
            start = int(after_sequence)
            if start >= 6:                       # the probe, after the bound
                raise mb.ReaderFailure("busy on the probe")
            return events[start:start + int(limit)]

        summary = self._ingest(read, max_events=6, batch_size=3)
        self.assertEqual(summary["status"], "bounded")
        self.assertIsNone(self._health()["caught_up"])
        lines = cli._bridge_pass_report(self.memory, summary, None, 0)
        self.assertTrue(lines)
        joined = " ".join(lines).lower()
        self.assertIn("could not be measured", joined)
        self.assertNotIn("caught up", joined)

    def test_a_bounded_pass_that_measured_catch_up_may_say_so(self) -> None:
        summary = self._ingest(self._reader(self._history(6)), max_events=6,
                               batch_size=3)
        self.assertEqual(summary["status"], "bounded")
        self.assertTrue(self._health()["caught_up"])
        lines = cli._bridge_pass_report(self.memory, summary, None, 0)
        self.assertTrue(any("caught up" in line.lower() for line in lines))

    def test_a_bounded_pass_with_a_measured_backlog_reports_it(self) -> None:
        summary = self._ingest(self._reader(self._history(30)), max_events=6,
                               batch_size=3)
        lines = cli._bridge_pass_report(self.memory, summary, None, 0)
        self.assertTrue(any("still waiting" in line for line in lines))

    def test_status_renders_backlog_as_a_lower_bound(self) -> None:
        self._ingest(self._reader(self._history(30)), max_events=6, batch_size=3)
        lines = cli._bridge_health_lines(self._health())
        joined = "\n".join(lines)
        self.assertIn("behind by at least", joined)
        self.assertIn("ingest health", joined)

    def test_status_says_unknown_before_any_pass(self) -> None:
        lines = cli._bridge_health_lines(self._health())
        self.assertTrue(any("no pass recorded yet" in line for line in lines))


# ---------------------------------------------------------------------------
# Sanitization
# ---------------------------------------------------------------------------

class SanitizedDiagnosticsTests(HealthCase):
    def _blob(self) -> str:
        chunks = [json.dumps(mb.bridge_status(self.db, now=now_iso()), default=str)]
        for table in ("memory_bridge_health", "memory_bridge_incidents",
                      "memory_bridge_cursor"):
            for row in self.db.execute(f"SELECT * FROM {table}"):
                chunks.append("|".join("" if v is None else str(v) for v in row))
        return "\n".join(chunks)

    def test_no_runtime_text_reaches_the_incident_ledger(self) -> None:
        events = [
            event(1, "agent.created", agent_record(project=PRIVATE_PROJECT),
                  project_id=PRIVATE_PROJECT)
        ]
        self._ingest(self._reader(events))
        blob = self._blob()
        self.assertNotIn(PRIVATE_PROJECT, blob)
        self.assertNotIn("private personality text", blob)
        self.assertIn("unmapped_project", blob)

    def test_an_adapter_message_never_reaches_the_ledger(self) -> None:
        def read(after_sequence, after_event_id, limit):
            raise mb.ReaderFailure(
                f"connection to host failed while reading {PRIVATE_PROJECT}"
            )

        self._ingest(read)
        self.assertNotIn(PRIVATE_PROJECT, self._blob())
        self.assertIn("reader_unavailable", self._blob())

    def test_incident_detail_is_safe_detail_output(self) -> None:
        with self.memory._immediate_transaction():
            mb._record_incident(
                self.db, code="reader_error", now=now_iso(),
                consumer=mb.DEFAULT_CONSUMER,
                detail={"project": PRIVATE_PROJECT, "at": 7},
                cursor_sequence=7,
            )
        detail = self.db.execute(
            "SELECT detail FROM memory_bridge_incidents"
        ).fetchone()[0]
        self.assertIn("at=7", detail)
        self.assertIn("project=redacted", detail)
        self.assertNotIn(PRIVATE_PROJECT, detail)

    def test_clearing_incidents_does_not_clear_a_halt(self) -> None:
        self._ingest(self._reader([event(2, "agent.created", agent_record(1))]))
        self.assertTrue(self._health()["halted"])
        with self.memory._immediate_transaction():
            cleared = mb.clear_incidents(self.db)
        self.assertGreaterEqual(cleared, 1)
        self.assertEqual(self._count("memory_bridge_incidents"), 0)
        self.assertTrue(
            mb.read_cursor(self.db)["status"] == "halted",
            "clearing the ledger resumed the bridge",
        )
        self.assertFalse(self._health()["healthy"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
