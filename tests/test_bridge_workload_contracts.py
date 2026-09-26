"""Bounded fixture scenarios exercise SQLite, never models or private history."""
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import bridge_workload as workload
from test_bridge_benchmark_contracts import SyntheticClock


class WorkloadContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_outliers_retained_and_small_or_constant_sets(self):
        self.assertEqual(workload._outliers([1, 2]), [])
        self.assertEqual(workload._outliers([1, 1, 1, 100]), [])
        self.assertEqual(workload._outliers([1, 2, 3, 4, 100]), [4])
        report = workload._repeat_stats([1, 2, 3, 4, 100], unit="ms")
        self.assertEqual(report["outlier_indices"], [4])
        self.assertEqual(report["all_runs"]["n"], 5)
        self.assertEqual(report["excluding_outliers"]["n"], 4)
        self.assertIsNone(workload._repeat_stats([], unit="s")["all_runs"])

    def test_all_seeded_shapes_ingest(self):
        report = workload.scenario_skew(self.root, messages=4, repeats=1)
        self.assertEqual(report["scenario"], "skew")
        self.assertEqual(len(report["results"]), 6)
        shapes = {row["descriptor"]["shape"]: row["descriptor"] for row in report["results"]}
        self.assertEqual(set(shapes), set(workload.SHAPES))
        self.assertEqual(shapes["hot_room"]["room_concentration"], 1)
        self.assertEqual(shapes["long_thread"]["messages"], 4)
        self.assertEqual(shapes["uniform"]["room_concentration"], .25)
        self.assertGreater(shapes["room_churn"]["membership_events"], 0)

    def test_small_read_path_scenarios(self):
        intervals = workload.scenario_interval_cost(self.root, transition_counts=[4], repeats=2)
        self.assertEqual(len(intervals["results"]), 1)
        self.assertGreater(intervals["results"][0]["intervals_returned"], 0)
        self.assertEqual(intervals["results"][0]["room_membership_intervals"]["n"], 2)
        status = workload.scenario_status_cost(self.root, sizes=[5, 10], repeats=2)
        self.assertEqual([r["runtime_events"] for r in status["results"]], [5, 10])
        self.assertTrue(all(r["projected_rows"] > 0 for r in status["results"]))
        self.assertTrue(all(r["bridge_status"]["n"] == 2 for r in status["results"]))

    def test_worker_has_bounded_cycles_and_restores_bridge_callback(self):
        from jarvis import cli
        original = cli._run_bridge_pass
        report = workload.scenario_worker(self.root, cycles=2, task_seconds=[0, .001], events=8)
        self.assertIs(cli._run_bridge_pass, original)
        self.assertEqual(len(report["results"]), 2)
        for row in report["results"]:
            self.assertEqual(row["events_ingested"], 8)

    def test_expired_lease_recovery_uses_exact_simulated_boundary(self):
        stamp = "2026-01-01T00:00:00+00:00"

        def acquire_then_exit(command, **kwargs):
            self.assertEqual(kwargs["timeout"], 120)
            with workload.Memory(command[-2]) as memory:
                with memory._immediate_transaction():
                    token = workload.mb.acquire_ingest_lease(memory.db, owner="doomed", now=stamp, lease_seconds=int(command[-1]))
                self.assertIsNotNone(token)
            return SimpleNamespace(returncode=0, stderr="")

        with patch.object(workload, "now_iso", return_value=stamp), patch.object(workload.subprocess, "run", side_effect=acquire_then_exit):
            report = workload.scenario_lease_recovery(self.root, lease_seconds=[2, 5])
        self.assertEqual([r["blocked_seconds"] for r in report["results"]], [2, 5])
        self.assertTrue(all(r["lease_held_after_crash"] for r in report["results"]))

    def test_crash_child_failure_is_not_reported_as_recovery(self):
        with patch.object(workload.subprocess, "run", return_value=SimpleNamespace(returncode=1, stderr="synthetic failure")), self.assertRaisesRegex(SystemExit, "crash child failed"):
            workload.scenario_lease_recovery(self.root, lease_seconds=[2])

    def test_budget_settings_preserved_and_fixture_metrics_labelled(self):
        # Mock only the foreground process and clock, not SQLite ingestion.
        child = SimpleNamespace(returncode=0, communicate=lambda **kwargs: ('{"samples":[0.001,0.002]}', ""))
        current = dict(workload.CURRENT_BUDGET)
        proposed = dict(workload.PROPOSED_BUDGET)
        with patch.object(workload, "time", SyntheticClock()), patch.object(workload.subprocess, "Popen", return_value=child):
            report = workload.scenario_budgets(self.root, seconds=.2, poll_seconds=.05, burst_multiple=2, base_rate=20, repeats=1)
        self.assertEqual([r["settings"] for r in report["results"]], [current, proposed])
        self.assertEqual(workload.CURRENT_BUDGET, current)
        self.assertEqual(workload.PROPOSED_BUDGET, proposed)
        for row in report["results"]:
            self.assertEqual(row["peak_backlog_events"]["all_runs"]["max"], 0)
            self.assertEqual(row["foreground_p95_ms"]["all_runs"]["median"], 2)
        self.assertFalse(list(self.root.glob("budget-current-*")))


if __name__ == "__main__":
    unittest.main()
