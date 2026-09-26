"""Small synthetic harness contracts; timings are not performance acceptance."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import bridge_benchmark as bench


class SyntheticClock:
    def __init__(self):
        self.current = 0.0

    def perf_counter(self):
        self.current += .005
        return self.current

    def sleep(self, seconds):
        self.current += seconds


class BenchmarkContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.template = bench._runtime_template(self.root)

    def test_statistics_nearest_rank_and_empty(self):
        self.assertIsNone(bench._percentile([], .95))
        self.assertEqual(bench._summary([]), {"n": 0})
        self.assertEqual(bench._percentile([4, 1, 3, 2], .5), 2)
        self.assertEqual(bench._percentile([1, 2], -1), 1)
        self.assertEqual(bench._percentile([1, 2], 2), 2)
        result = bench._summary([.001, .003])
        self.assertEqual(result["mean_ms"], 2)
        self.assertEqual(result["total_s"], .004)

    def test_history_is_deterministic_bounded_and_detached(self):
        before = json.dumps(self.template, sort_keys=True)
        history = bench.SyntheticHistory(self.template)
        first = history.event(1)
        self.assertEqual(first, history.event(1))
        second_cycle = history.event(len(self.template) + 1)
        self.assertNotEqual(first["event_id"], second_cycle["event_id"])
        first["payload"]["test_marker"] = "synthetic"
        self.assertNotIn("test_marker", history.event(1)["payload"])
        self.assertEqual(before, json.dumps(self.template, sort_keys=True))
        for cycle in range(100):
            history._mapping(cycle)
        self.assertLessEqual(len(history._cycle_maps), 65)
        rows = history.window(2, 9, head=5)
        self.assertEqual([row["sequence"] for row in rows], [3, 4, 5])
        self.assertEqual(history.window(5, 2, head=3), [])
        self.assertEqual(history.reader(5)(after_sequence=2, after_event_id=None, limit=9), rows)
        self.assertEqual(bench.list_reader(rows)(after_sequence=1, after_event_id=None, limit=1), rows[1:2])

    def test_small_throughput_and_sqlite_reader(self):
        report = bench.scenario_throughput(self.template, self.root, events=9, repeats=1, batch_sizes=[2, 9])
        self.assertEqual([r["batch_latency"]["n"] for r in report["results"]], [5, 1])
        self.assertFalse(list(self.root.glob("tp-*")))
        reader = bench.scenario_reader(self.root, runtime_events=7, repeats=1)
        self.assertEqual(reader["runtime_events"], 7)
        self.assertEqual(len(reader["results"]), 4)
        self.assertTrue(all(row["call_latency"]["n"] >= 1 for row in reader["results"]))

    def test_invalid_event_halts_instead_of_advancing(self):
        template = json.loads(json.dumps(self.template))
        template[0]["schema_version"] = 999
        with self.assertRaises(SystemExit):
            bench.scenario_throughput(template, self.root, events=1, repeats=1, batch_sizes=[1])

    def test_backlog_bound_and_complete_drain(self):
        with patch.object(bench, "time", SyntheticClock()):
            report = bench.scenario_backlog(self.template, self.root, arrival_rates=[100], seconds=.3, poll_seconds=.05, max_events=1, batch_size=1)
        row = report["results"][0]
        self.assertGreater(row["backlog_final"], 0)
        self.assertEqual(row["drained_to"], row["events_offered"])
        self.assertLessEqual(row["events_ingested_in_window"], row["passes"])

    def test_contention_orchestration_and_failed_child(self):
        # Child metrics are fixtures, not measured concurrent-write evidence.
        child = SimpleNamespace(returncode=0, communicate=lambda **kwargs: ('{"passes":2,"halted":false}', ""))
        with patch.object(bench, "time", SyntheticClock()), patch.object(bench.subprocess, "Popen", return_value=child):
            report = bench.scenario_contention(self.template, self.root, events=8, seconds=.04, batch_sizes=[2])
        self.assertEqual(report["results"][0]["ingest_passes"], 2)
        self.assertFalse(report["results"][0]["ingest_halted"])
        self.assertGreater(report["foreground_baseline"]["n"], 0)
        child.returncode = 1
        with patch.object(bench, "time", SyntheticClock()), patch.object(bench.subprocess, "Popen", return_value=child), self.assertRaisesRegex(SystemExit, "ingest child failed"):
            bench.scenario_contention(self.template, self.root, events=8, seconds=.04, batch_sizes=[3])

    def test_synthetic_projection_does_not_promote_to_personal_memory(self):
        events = bench.synthetic_history(self.template, len(self.template) * 2)
        with bench.Memory(self.root / "projection.db") as memory:
            result = bench.mb.ingest_once(memory.db, bench.list_reader(events), now=bench.now_iso(), owner="test", transaction=memory._immediate_transaction, max_events=len(events), batch_size=10, clock=bench.now_iso)
            self.assertEqual(result["status"], "bounded")
            self.assertEqual(bench.mb.read_cursor(memory.db)["last_sequence"], len(events))
            for table in ("memories", "messages"):
                self.assertEqual(memory.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)

    def test_shape_and_environment_are_nonidentifying(self):
        report = bench.scenario_shape(self.template)
        self.assertEqual(report["scenario"], "shape")
        environment = bench._environment()
        self.assertFalse(environment["identifiers_recorded"])
        self.assertNotIn("hostname", environment)
        self.assertNotIn("user", environment)
        self.assertNotIn(str(self.root), json.dumps(environment))
        pragmas = bench._store_pragmas(self.root)
        self.assertEqual(str(pragmas["journal_mode"]).lower(), "wal")
        self.assertEqual(pragmas["foreign_keys"], 1)

    def test_cli_rejects_unknown_scenario_and_writes_json(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            bench.main(["not-a-scenario"])
        self.assertEqual(error.exception.code, 2)
        output = self.root / "report.json"
        with contextlib.redirect_stdout(io.StringIO()), patch.object(bench, "scenario_shape", return_value={"scenario": "shape"}):
            bench.main(["shape", "--out", str(output)])
        result = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(len(result["scenarios"]), 1)


if __name__ == "__main__":
    unittest.main()
