from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from jarvis import cli, memory_graph
from jarvis.cli import _run_graph
from jarvis.config import Config
from jarvis.memory import Memory


def _command(subject: str, predicate: str, value: str) -> str:
    return "Remember this project fact: " + json.dumps(
        {"subject": subject, "predicate": predicate, "value": value},
        ensure_ascii=False,
        separators=(",", ":"),
    )


GRAPH_TEST_TIME_BUDGET_MS = 5_000.0


def relax_graph_time_budget(test: unittest.TestCase) -> None:
    """Give the graph read a deadline a loaded test runner cannot trip.

    ``memory_graph.TIME_BUDGET_MS`` is 25 ms.  That is the right bound in
    production and ``Memory.graph_chains`` reads it per call, so a read that
    exceeds it correctly returns what it screened so far with mode
    ``budget-exceeded`` and its chains marked incomplete.  Under a full suite
    on a loaded host the same read can cross 25 ms for reasons that have
    nothing to do with the behaviour under test, and a test asserting a
    complete chain, a ``complete`` mode, or the absence of a ``not_recorded``
    cue then fails on the clock rather than on the product.

    Every test that asserts a graph answer calls this from ``setUp``; a test
    that exercises the budget itself must not, and would patch the constant
    the other way instead.  There is no such test in this file - the budget
    exit test lives in ``tests/test_memory_graph_integration.py``.
    """
    patcher = patch.object(
        memory_graph, "TIME_BUDGET_MS", GRAPH_TEST_TIME_BUDGET_MS
    )
    patcher.start()
    test.addCleanup(patcher.stop)


def _args(command: str, **flags: object) -> Namespace:
    fields: dict[str, object] = {"graph_command": command, "json": False}
    if command == "rebuild":
        fields["apply"] = False
        fields["yes"] = False
        fields["plan"] = None
    if command == "paths":
        fields["subject"] = ["Kestrel", "relay"]
        fields["project"] = 1
        fields["hops"] = 3
        fields["temporal"] = False
    fields.update(flags)
    return Namespace(**fields)


class GraphCliTests(unittest.TestCase):
    """``python -m jarvis graph`` at schema 48: status and verify print ids and
    counts only, rebuild carries the plan-token exit codes of
    ``spine rebuild-claims``, and paths shows the screened chains the agent
    would see."""

    def setUp(self) -> None:
        relax_graph_time_budget(self)
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.workspace = root / "workspace"
        self.data_dir = root / "data"
        self.workspace.mkdir()
        self.data_dir.mkdir()
        # The CLI opens data_dir / "jarvis.db".
        self.db_path = self.data_dir / "jarvis.db"
        memory = Memory(self.db_path)
        try:
            conversation = memory.new_conversation(project_id=1)
            for subject, predicate, value in (
                ("Kestrel relay", "deployed on host", "Harrier box"),
                ("Harrier box", "datacenter", "Fenwick"),
                ("Fenwick", "region", "Northgate"),
            ):
                memory.remember_explicit_project_claim(
                    conversation, 1, _command(subject, predicate, value)
                )
        finally:
            memory.close()
        self.config_patch = patch(
            "jarvis.cli.Config.load",
            return_value=replace(
                Config.load(),
                data_dir=self.data_dir,
                workspace=self.workspace,
                vault_dir=None,
            ),
        )
        self.config_patch.start()

    def tearDown(self) -> None:
        self.config_patch.stop()
        self.temp.cleanup()

    def _run(self, command: str, **flags: object) -> tuple[int, str]:
        output = io.StringIO()
        with redirect_stdout(output):
            code = _run_graph(_args(command, **flags))
        return code, output.getvalue()

    def _raw(self, sql: str, *params: object) -> None:
        raw = sqlite3.connect(str(self.db_path))
        try:
            raw.execute(sql, params)
            raw.commit()
        finally:
            raw.close()

    def _edge_count(self) -> int:
        raw = sqlite3.connect(str(self.db_path))
        try:
            return int(
                raw.execute("SELECT COUNT(*) FROM memory_graph_edges").fetchone()[0]
            )
        finally:
            raw.close()

    # --- the parser ---------------------------------------------------------

    def test_the_graph_deadline_is_relaxed_for_this_class(self) -> None:
        # See relax_graph_time_budget: a 25 ms deadline is the product bound,
        # not a property any assertion in this file is about.
        self.assertEqual(memory_graph.TIME_BUDGET_MS, GRAPH_TEST_TIME_BUDGET_MS)

    def test_status_does_not_pay_for_a_full_verification(self) -> None:
        """C-4: ``status`` is counts, and ``verify_graph`` compares every claim
        to its edge - seconds on a large store.  If status ever calls it again
        this fails instead of quietly costing the operator the wait."""
        with patch.object(
            Memory, "verify_graph", side_effect=AssertionError("status verified")
        ):
            code, text = self._run("status")
        self.assertEqual(code, 0, text)
        self.assertIn("Memory graph:", text)

    def test_the_dry_run_reports_the_expected_edge_count(self) -> None:
        # C-2: the report's key is edges_expected; reading a key that does not
        # exist printed "rebuilt edges 0" on a store with edges.
        self._raw("DELETE FROM memory_graph_edges WHERE claim_id=1")
        code, text = self._run("rebuild")
        self.assertEqual(code, 1, text)
        self.assertIn("live edges 2", text)
        self.assertIn("expected edges 3", text)
        self.assertNotIn("expected edges 0", text)

    def test_hops_outside_the_walk_is_a_usage_error(self) -> None:
        # C-3: silently widening --hops 0 to the full walk would print more
        # than the operator asked to see.
        for hops in (0, -1, 4, 99):
            with self.subTest(hops=hops):
                code, text = self._run("paths", hops=hops)
                self.assertEqual(code, 2, text)
                self.assertIn("--hops must be between 1 and 3", text)
                self.assertIn("nothing was read", text)
                self.assertNotIn("Kestrel relay", text)

    def test_every_hop_inside_the_walk_is_accepted(self) -> None:
        for hops in (1, 2, 3):
            with self.subTest(hops=hops):
                code, _text = self._run("paths", hops=hops)
                self.assertEqual(code, 0)

    def test_paths_names_what_it_could_not_identify(self) -> None:
        """Design 10.7 item 4: chains for the name that resolved, and a line
        for the one that did not.  A listing that answers for one name while
        silently ignoring another reads as a complete answer."""
        result = {
            "rows": [{
                "subject": "Kestrel relay", "predicate": "deployed on host",
                "value": "Harrier box", "status": "active", "chain": 1, "hop": 1,
            }],
            "overflow": [],
            "report": {
                "channel": "graph", "mode": "complete",
                "unresolved": ["Tarnworth mill"],
            },
        }
        with patch.object(Memory, "graph_chains", return_value=result):
            code, text = self._run("paths")
        self.assertEqual(code, 0, text)
        self.assertIn("Kestrel relay", text)
        self.assertIn("no stored fact identifies: Tarnworth mill", text)

    def test_paths_names_the_unidentified_even_with_no_chains(self) -> None:
        result = {
            "rows": [], "overflow": [],
            "report": {
                "channel": "graph", "mode": "no-start",
                "unresolved": ["Tarnworth mill"],
            },
        }
        with patch.object(Memory, "graph_chains", return_value=result):
            code, text = self._run("paths")
        self.assertEqual(code, 0, text)
        self.assertIn("No chain answers", text)
        self.assertIn("no stored fact identifies: Tarnworth mill", text)

    def test_paths_says_nothing_when_every_name_resolved(self) -> None:
        code, text = self._run("paths")
        self.assertEqual(code, 0, text)
        self.assertNotIn("no stored fact identifies", text)

    def test_parser_accepts_every_graph_subcommand(self) -> None:
        parser = cli._parser()
        status = parser.parse_args(["graph", "status", "--json"])
        self.assertEqual((status.command, status.graph_command), ("graph", "status"))
        self.assertTrue(status.json)
        verify = parser.parse_args(["graph", "verify"])
        self.assertEqual(verify.graph_command, "verify")
        rebuild = parser.parse_args(
            ["graph", "rebuild", "--apply", "--yes", "--plan", "abc123", "--json"]
        )
        self.assertEqual(rebuild.graph_command, "rebuild")
        self.assertTrue(rebuild.apply and rebuild.yes and rebuild.json)
        self.assertEqual(rebuild.plan, "abc123")
        paths = parser.parse_args(
            ["graph", "paths", "Kestrel", "relay", "--project", "2",
             "--hops", "2", "--temporal"]
        )
        self.assertEqual(paths.subject, ["Kestrel", "relay"])
        self.assertEqual((paths.project, paths.hops), (2, 2))
        self.assertTrue(paths.temporal)

    # --- status and verify --------------------------------------------------

    def test_status_reports_counts_and_the_three_exclusion_categories(self) -> None:
        code, text = self._run("status")
        self.assertEqual(code, 0, text)
        self.assertIn("Memory graph:", text)
        self.assertIn("edges", text)
        self.assertIn("reserved-predicate", text)
        self.assertIn("private-subject", text)
        self.assertIn("over-long-subject", text)

    def test_status_json_carries_the_category_object(self) -> None:
        code, text = self._run("status", json=True)
        self.assertEqual(code, 0, text)
        payload = json.loads(text)
        self.assertEqual(
            sorted(payload["excluded"]),
            ["excluded_predicate", "subject_private", "subject_too_long"],
        )
        self.assertGreaterEqual(payload["edges"], 3)

    def test_verify_is_ok_on_an_untouched_store(self) -> None:
        code, text = self._run("verify")
        self.assertEqual(code, 0, text)
        self.assertIn("Memory graph OK", text)

    def test_verify_exits_one_on_a_tamper_and_names_fields_not_values(self) -> None:
        self._raw(
            "UPDATE memory_graph_edges SET status='superseded' "
            "WHERE claim_id=(SELECT MIN(claim_id) FROM memory_graph_edges)"
        )
        code, text = self._run("verify")
        self.assertEqual(code, 1, text)
        self.assertIn("Memory graph FAILED", text)
        # Problem details name fields, never the stored text (the M-1 rule).
        for secret in ("Kestrel relay", "Harrier box", "Fenwick", "Northgate"):
            self.assertNotIn(secret, text)

    def test_verify_json_never_carries_a_stored_value(self) -> None:
        self._raw("DELETE FROM memory_graph_edges WHERE claim_id=1")
        code, text = self._run("verify", json=True)
        self.assertEqual(code, 1, text)
        for secret in ("Kestrel relay", "Harrier box", "Fenwick", "Northgate"):
            self.assertNotIn(secret, text)

    # --- rebuild ------------------------------------------------------------

    def test_dry_run_is_equivalent_on_an_untouched_store(self) -> None:
        code, text = self._run("rebuild")
        self.assertEqual(code, 0, text)
        self.assertIn("equivalent", text)

    def test_yes_without_apply_is_a_usage_error(self) -> None:
        code, text = self._run("rebuild", yes=True)
        self.assertEqual(code, 2, text)
        self.assertIn("--yes requires --apply", text)

    def test_plan_without_apply_yes_is_a_usage_error(self) -> None:
        code, text = self._run("rebuild", plan="abc123")
        self.assertEqual(code, 2, text)
        self.assertIn("--plan requires --apply --yes", text)

    def test_apply_without_yes_prints_the_plan_and_changes_nothing(self) -> None:
        before = self._edge_count()
        self._raw("DELETE FROM memory_graph_edges WHERE claim_id=1")
        code, text = self._run("rebuild", apply=True)
        self.assertEqual(code, 2, text)
        self.assertIn("Would change", text)
        self.assertIn("plan token", text)
        self.assertEqual(self._edge_count(), before - 1)

    def test_apply_yes_reconciles_and_the_dry_run_is_then_equivalent(self) -> None:
        before = self._edge_count()
        self._raw("DELETE FROM memory_graph_edges WHERE claim_id=1")
        code, text = self._run("rebuild", apply=True, yes=True)
        self.assertEqual(code, 0, text)
        self.assertIn("Graph projection rebuilt", text)
        self.assertEqual(self._edge_count(), before)
        code, text = self._run("rebuild")
        self.assertEqual(code, 0, text)
        self.assertIn("equivalent", text)

    def test_stale_plan_token_refuses_and_changes_nothing(self) -> None:
        self._raw("DELETE FROM memory_graph_edges WHERE claim_id=1")
        after_tamper = self._edge_count()
        code, text = self._run(
            "rebuild", apply=True, yes=True, plan="0123456789ab"
        )
        self.assertEqual(code, 1, text)
        self.assertIn("stale_plan", text)
        self.assertEqual(self._edge_count(), after_tamper)

    def test_plan_token_binds_apply_to_the_plan_the_operator_saw(self) -> None:
        self._raw("DELETE FROM memory_graph_edges WHERE claim_id=1")
        code, text = self._run("rebuild", apply=True)
        self.assertEqual(code, 2, text)
        token = ""
        for line in text.splitlines():
            if line.startswith("plan token:"):
                token = line.split(":", 1)[1].strip()
        self.assertTrue(token)
        code, text = self._run("rebuild", apply=True, yes=True, plan=token)
        self.assertEqual(code, 0, text)
        self.assertIn("Graph projection rebuilt", text)

    def test_rebuild_output_never_carries_a_stored_value(self) -> None:
        self._raw(
            "UPDATE memory_graph_edges SET confidence=0.1 "
            "WHERE claim_id=(SELECT MIN(claim_id) FROM memory_graph_edges)"
        )
        for flags in ({}, {"json": True}, {"apply": True}):
            with self.subTest(flags=flags):
                _code, text = self._run("rebuild", **flags)
                for secret in ("Kestrel relay", "Harrier box", "Fenwick", "Northgate"):
                    self.assertNotIn(secret, text)

    # --- paths --------------------------------------------------------------

    def test_paths_prints_the_chain_the_agent_would_see(self) -> None:
        code, text = self._run("paths")
        self.assertEqual(code, 0, text)
        self.assertIn("Kestrel relay", text)
        self.assertIn("Harrier box", text)
        self.assertIn("chain 1 hop 1", text)

    def test_paths_hops_bounds_what_is_printed(self) -> None:
        _code, deep = self._run("paths")
        _code, shallow = self._run("paths", hops=1)
        self.assertIn("hop 2", deep)
        self.assertNotIn("hop 2", shallow)

    def test_paths_on_an_unknown_subject_reports_the_mode(self) -> None:
        code, text = self._run("paths", subject=["Nightjar", "relay"])
        self.assertEqual(code, 0, text)
        self.assertIn("No chain answers", text)
        self.assertIn("no-start", text)

    def test_paths_json_carries_rows_overflow_and_report(self) -> None:
        code, text = self._run("paths", json=True)
        self.assertEqual(code, 0, text)
        payload = json.loads(text)
        self.assertEqual(sorted(payload), ["overflow", "report", "rows"])
        self.assertEqual(payload["report"]["channel"], "graph")

    def test_paths_stays_inside_the_named_project(self) -> None:
        memory = Memory(self.db_path)
        try:
            other = int(memory.add_project("other", "@projects/other"))
            conversation = memory.new_conversation(project_id=other)
            memory.remember_explicit_project_claim(
                conversation, other,
                _command("Kestrel relay", "deployed on host", "Talon box"),
            )
        finally:
            memory.close()
        _code, text = self._run("paths", project=1)
        self.assertNotIn("Talon box", text)


if __name__ == "__main__":
    unittest.main()
