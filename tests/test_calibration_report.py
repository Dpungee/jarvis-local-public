"""The calibration report republishes the production gate and never writes.

These tests hold the WP-10 boundaries by execution: the report's verdict is the
gate's verdict field for field, ``practice`` outcomes stay excluded, the report
opens nothing for writing, no case is verified by the model's own claim, and the
sandbox harness cannot grant itself a capability or a non-local provider.
"""
from __future__ import annotations

import ast
import contextlib
import hashlib
import importlib.util
import inspect
import io
import json
import platform
import re
import sqlite3
import sys
import tempfile
import unittest
import unittest.mock
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from jarvis import calibration_report as calibration
from jarvis.memory import Memory
from jarvis.proactive import (
    META_GATE_MAX_BRIER,
    META_GATE_MAX_CALIBRATION_ERROR,
    META_GATE_MIN_ATTEMPTS,
)

ROOT = Path(__file__).resolve().parents[1]
_WRITE_STATEMENT = re.compile(
    r"^\s*(?:INSERT|UPDATE|DELETE|REPLACE|CREATE|DROP|ALTER|VACUUM|BEGIN|COMMIT"
    r"|SAVEPOINT|RELEASE|REINDEX)\b",
    re.I,
)


def _harness_module():
    """Load the script as a module. ``dataclass`` needs it in ``sys.modules``."""
    name = "run_phase2_calibration"
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "scripts" / "run_phase2_calibration.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


class _RecordingConnection:
    """Delegate to a real connection while recording every statement executed."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self.statements: list[str] = []

    def execute(self, sql, *args, **kwargs):
        self.statements.append(str(sql))
        return self._connection.execute(sql, *args, **kwargs)

    def executemany(self, sql, *args, **kwargs):
        self.statements.append(str(sql))
        return self._connection.executemany(sql, *args, **kwargs)

    def executescript(self, sql, *args, **kwargs):
        self.statements.append(str(sql))
        return self._connection.executescript(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._connection, name)


def _seed(
    memory: Memory,
    family: str,
    *,
    attempts: int,
    predicted: float,
    successes: int,
    origin: str = "interactive",
    evidence: bool | None = True,
    verification: str = "tool_success",
) -> list[int]:
    identifiers: list[int] = []
    for index in range(attempts):
        complete = index < successes
        prediction_id = memory.record_prediction(
            family=family,
            profile="fast",
            model="local-test-model",
            predicted_success=predicted,
            predicted_steps=2,
            predicted_verification=verification,
            basis="prior",
            origin=origin,
        )
        memory.resolve_prediction(
            prediction_id,
            actual_status="complete" if complete else "failed",
            actual_steps=2,
            evidence_ok=None if evidence is None else (evidence and complete),
            failure_class=None if complete else "unknown",
        )
        identifiers.append(prediction_id)
    return identifiers


class CalibrationReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.holder = tempfile.TemporaryDirectory()
        self.addCleanup(self.holder.cleanup)
        self.database = Path(self.holder.name) / "jarvis.db"

    def _memory(self) -> Memory:
        memory = Memory(self.database)
        self.addCleanup(memory.close)
        return memory

    def test_report_matches_the_production_gate_field_by_field(self) -> None:
        memory = self._memory()
        _seed(memory, "file_ops", attempts=24, predicted=0.85, successes=21)
        _seed(memory, "conversation", attempts=12, predicted=0.95, successes=6)
        report = calibration.calibration_report(memory)
        self.assertTrue(report["families"])
        for item in report["families"]:
            gate = memory.calibration_gate(
                item["family"],
                minimum_attempts=META_GATE_MIN_ATTEMPTS,
                maximum_brier=META_GATE_MAX_BRIER,
                maximum_calibration_error=META_GATE_MAX_CALIBRATION_ERROR,
            )
            for key, value in gate.items():
                self.assertEqual(item[key], value, f"{item['family']}.{key}")
            self.assertEqual(item["reasons"], gate["reasons"])

    def test_thresholds_are_read_from_proactive(self) -> None:
        report = calibration.calibration_report(self._memory())
        self.assertEqual(report["thresholds"], {
            "minimum_attempts": META_GATE_MIN_ATTEMPTS,
            "maximum_brier": META_GATE_MAX_BRIER,
            "maximum_calibration_error": META_GATE_MAX_CALIBRATION_ERROR,
        })
        self.assertEqual(report["gate_source"], "jarvis.proactive.calibrated_meta_gate")

    def test_only_the_counted_origins_reach_competence(self) -> None:
        """Prove the mirrored origin set instead of trusting the comment."""
        memory = self._memory()
        families = sorted(memory.PREDICTION_FAMILIES)
        origins = sorted(memory.PREDICTION_ORIGINS)
        self.assertGreaterEqual(len(families), len(origins))
        assignment = dict(zip(origins, families))
        for origin, family in assignment.items():
            _seed(memory, family, attempts=1, predicted=0.5, successes=1, origin=origin)
        counted = {
            origin
            for origin, family in assignment.items()
            if any(row["family"] == family for row in memory.competence())
        }
        self.assertEqual(counted, set(calibration.COUNTED_PREDICTION_ORIGINS))
        self.assertNotIn("practice", counted)
        self.assertIn("practice", calibration.excluded_prediction_origins(memory))

    def test_practice_outcomes_cannot_manufacture_a_calibrated_family(self) -> None:
        memory = self._memory()
        _seed(memory, "code_test", attempts=40, predicted=0.70, successes=40,
              origin="practice")
        report = calibration.calibration_report(memory)
        row = next(item for item in report["families"] if item["family"] == "code_test")
        self.assertEqual(row["attempts"], 0)
        self.assertFalse(row["allowed"])
        self.assertEqual(report["calibrated_family_count"], 0)

    def test_the_report_issues_no_write_statement(self) -> None:
        memory = self._memory()
        _seed(memory, "file_ops", attempts=22, predicted=0.85, successes=20)
        recorder = _RecordingConnection(memory.db)
        memory.db = recorder
        try:
            calibration.calibration_report(memory)
        finally:
            memory.db = recorder._connection
        self.assertTrue(recorder.statements)
        offending = [sql for sql in recorder.statements if _WRITE_STATEMENT.search(sql)]
        self.assertEqual(offending, [])

    def test_read_only_memory_leaves_the_source_bytes_untouched(self) -> None:
        memory = self._memory()
        _seed(memory, "file_ops", attempts=21, predicted=0.85, successes=19)
        expected = calibration.calibration_report(memory)
        memory.close()
        before = hashlib.sha256(self.database.read_bytes()).hexdigest()
        with calibration.read_only_memory(self.database, scratch_dir=self.holder.name) as copy:
            actual = calibration.calibration_report(copy)
        after = hashlib.sha256(self.database.read_bytes()).hexdigest()
        self.assertEqual(before, after)
        self.assertEqual(
            actual["resolved_outcomes_counted"], expected["resolved_outcomes_counted"]
        )
        self.assertEqual(
            [item["reasons"] for item in actual["families"]],
            [item["reasons"] for item in expected["families"]],
        )

    def test_read_only_copy_falls_back_without_writing_the_source(self) -> None:
        """A live WAL database that refuses a read-only open still reports."""
        memory = self._memory()
        _seed(memory, "file_ops", attempts=21, predicted=0.85, successes=19)
        memory.close()
        before = hashlib.sha256(self.database.read_bytes()).hexdigest()
        original = sqlite3.connect
        calls: list[str] = []

        def refuse_readonly(target, *args, **kwargs):
            # Refuse only the read-only open of the source, the way a live WAL
            # database does. Memory's own inspection of the copy must still work.
            if (
                kwargs.get("uri")
                and "mode=ro" in str(target)
                and "calibration-copy" not in str(target)
            ):
                calls.append(str(target))
                raise sqlite3.OperationalError("unable to open database file")
            return original(target, *args, **kwargs)

        with unittest.mock.patch.object(sqlite3, "connect", refuse_readonly):
            with calibration.read_only_memory(
                self.database, scratch_dir=self.holder.name
            ) as copy:
                report = calibration.calibration_report(copy)
        self.assertTrue(calls)
        self.assertEqual(report["resolved_outcomes_counted"], 21)
        self.assertEqual(
            hashlib.sha256(self.database.read_bytes()).hexdigest(), before
        )

    def test_read_only_memory_rejects_a_missing_database(self) -> None:
        with self.assertRaises(FileNotFoundError):
            with calibration.read_only_memory(Path(self.holder.name) / "absent.db"):
                pass

    def test_unknown_family_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            calibration.calibration_report(self._memory(), families=["not_a_family"])

    def test_drift_signals_reach_the_family_row(self) -> None:
        memory = self._memory()
        # Older baseline succeeds; the recent window collapses.
        _seed(memory, "file_ops", attempts=20, predicted=0.85, successes=20)
        _seed(memory, "file_ops", attempts=12, predicted=0.85, successes=0)
        report = calibration.calibration_report(
            memory, drift_window=12, drift_baseline=20, drift_minimum_samples=10
        )
        row = next(item for item in report["families"] if item["family"] == "file_ops")
        signals = {signal["signal"] for signal in row["drift_signals"]}
        self.assertIn("success_rate_drop", signals)
        self.assertIn("file_ops", report["drift"]["families_with_signals"])
        self.assertEqual(
            report["drift"]["findings"],
            [dict(item) for item in memory.drift_report(
                window=12, baseline=20, minimum_samples=10
            )],
        )

    def test_format_report_prints_every_unmet_reason(self) -> None:
        memory = self._memory()
        _seed(memory, "file_ops", attempts=5, predicted=0.85, successes=1)
        report = calibration.calibration_report(memory)
        text = calibration.format_report(report)
        row = next(item for item in report["families"] if item["family"] == "file_ops")
        self.assertTrue(row["reasons"])
        for reason in row["reasons"]:
            self.assertIn(reason, text)
        self.assertIn("Sandbox rows are not the operator's rows", text)
        self.assertIn("practice", text)

    def test_initiative_snapshot_only_delegates(self) -> None:
        """The 'at least 3 calibrated families' rule stays in proactive.py."""
        from jarvis import proactive

        memory = self._memory()
        _seed(memory, "file_ops", attempts=21, predicted=0.85, successes=19)
        config = SimpleNamespace(initiative="disabled", proactive_enabled=False)
        snapshot = calibration.initiative_snapshot(config, memory)
        self.assertEqual(snapshot, proactive.initiative_eligibility(config, memory))
        self.assertEqual(snapshot["required_calibrated_families"], 3)
        self.assertIn(
            "requires at least 3 calibrated families",
            " ".join(str(item) for item in snapshot["tier1_blockers"]),
        )
        report = calibration.calibration_report(memory)
        self.assertEqual(
            report["calibrated_families"], snapshot["calibrated_families"]
        )

    def test_top_failures_are_reported_for_families_with_outcomes(self) -> None:
        memory = self._memory()
        _seed(memory, "file_ops", attempts=6, predicted=0.85, successes=2)
        report = calibration.calibration_report(memory)
        row = next(item for item in report["families"] if item["family"] == "file_ops")
        self.assertEqual(row["top_failures"], [{"failure_class": "unknown", "n": 4}])
        empty = next(
            item for item in report["families"] if item["family"] == "external_publish"
        )
        self.assertEqual(empty["top_failures"], [])


class CalibrationBoundaryTests(unittest.TestCase):
    """H-1: a family exactly at the tolerance must not be refused by float noise."""

    def setUp(self) -> None:
        self.holder = tempfile.TemporaryDirectory()
        self.addCleanup(self.holder.cleanup)
        self.database = Path(self.holder.name) / "jarvis.db"

    def _gate(self, predicted: float, successes: int, attempts: int = 20) -> dict:
        database = Path(self.holder.name) / f"gate-{predicted}-{successes}.db"
        memory = Memory(database)
        self.addCleanup(memory.close)
        _seed(
            memory, "file_ops", attempts=attempts, predicted=predicted,
            successes=successes, evidence=None,
        )
        return memory.calibration_gate(
            "file_ops",
            minimum_attempts=META_GATE_MIN_ATTEMPTS,
            maximum_brier=META_GATE_MAX_BRIER,
            maximum_calibration_error=META_GATE_MAX_CALIBRATION_ERROR,
        )

    def test_predicted_085_against_observed_10_passes_at_n20(self) -> None:
        gate = self._gate(0.85, 20)
        self.assertEqual(gate["attempts"], 20)
        self.assertEqual(gate["mean_predicted"], 0.85)
        self.assertEqual(gate["observed_success"], 1.0)
        self.assertEqual(gate["calibration_error"], 0.15)
        self.assertTrue(gate["allowed"], gate["reasons"])
        self.assertEqual(gate["reasons"], [])

    def test_the_gate_never_prints_a_reason_that_contradicts_itself(self) -> None:
        """A refusal may not print a value at or below the printed tolerance."""
        for successes in range(21):
            for predicted in (0.55, 0.6, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 1.0):
                self._assert_no_contradiction(self._gate(predicted, successes))

    def _gate_at(self, predicted: float, successes: int, attempts: int = 20) -> dict:
        """Seed one family at an exact mean predicted value and read the gate."""
        database = Path(self.holder.name) / f"sweep-{predicted!r}-{successes}.db"
        memory = Memory(database)
        try:
            for index in range(attempts):
                prediction_id = memory.record_prediction(
                    family="file_ops",
                    profile="fast",
                    model="local-test-model",
                    predicted_success=predicted,
                    predicted_steps=2,
                    predicted_verification="not_applicable",
                    basis="prior",
                    origin="interactive",
                )
                memory.resolve_prediction(
                    prediction_id,
                    actual_status="complete" if index < successes else "failed",
                    actual_steps=2,
                    evidence_ok=None,
                    failure_class=None if index < successes else "unknown",
                )
            return memory.calibration_gate(
                "file_ops",
                minimum_attempts=META_GATE_MIN_ATTEMPTS,
                maximum_brier=META_GATE_MAX_BRIER,
                maximum_calibration_error=META_GATE_MAX_CALIBRATION_ERROR,
            )
        finally:
            memory.close()

    def test_display_rounding_never_prints_a_refusal_as_the_threshold(self) -> None:
        """A refused error must not display as the tolerance it exceeded.

        Sweeps predicted values at 3-decimal resolution and finer - k/200 and
        k/1000 shapes - against a full observed rate, which is where a stored
        value such as 0.84975 produces a true error of 0.15025 that three decimals
        rendered as "0.150".
        """
        checked = 0
        for numerator in range(100, 201):            # k/200: 0.500 .. 1.000
            gate = self._gate_at(numerator / 200, 20)
            checked += 1
            self._assert_no_contradiction(gate)
        for numerator in range(840, 861):            # k/1000: 0.840 .. 0.860
            gate = self._gate_at(numerator / 1000, 20)
            checked += 1
            self._assert_no_contradiction(gate)
        self.assertEqual(checked, 122)

    def test_the_reviewers_example_is_refused_and_reads_correctly(self) -> None:
        gate = self._gate_at(0.84975, 20)
        self.assertEqual(gate["observed_success"], 1.0)
        self.assertAlmostEqual(gate["calibration_error"], 0.15025, places=9)
        self.assertFalse(gate["allowed"])
        reason = next(
            item for item in gate["reasons"] if item.startswith("calibration error")
        )
        self.assertTrue(
            reason.endswith("is 0.1502") or reason.endswith("is 0.1503"), reason
        )
        # This is the value three decimals used to render as the tolerance.
        self.assertEqual(f"{gate['calibration_error']:.3f}", "0.150")
        self._assert_no_contradiction(gate)

    _CALIBRATION_REASON = re.compile(
        r"calibration error must be <= (?P<threshold>\d+\.\d+); is (?P<value>\d+\.\d+)$"
    )

    def _assert_no_contradiction(self, gate: dict) -> None:
        """A refusal must print a value that visibly exceeds the printed threshold.

        Reading only what the operator sees: if the displayed value parses to no
        more than the displayed tolerance, the message contradicts the verdict,
        whatever the stored float was.
        """
        for reason in gate["reasons"]:
            match = self._CALIBRATION_REASON.search(reason)
            if match is None:
                continue
            self.assertGreater(
                float(match.group("value")),
                float(match.group("threshold")),
                f"predicted={gate['mean_predicted']!r} "
                f"observed={gate['observed_success']!r}: {reason}",
            )

    def test_every_exact_boundary_pair_at_n20_passes(self) -> None:
        """The eleven gate-reachable pairs whose true error is exactly 0.15."""
        pairs = []
        for successes in range(21):
            observed = successes / 20
            if observed < 0.70:
                continue  # the gate's own success floor
            for offset in (Decimal("0.15"), Decimal("-0.15")):
                predicted = Decimal(successes) / Decimal(20) + offset
                if Decimal(0) <= predicted <= Decimal(1):
                    pairs.append((float(predicted), successes))
        pairs = sorted(set(pairs))
        self.assertEqual(len(pairs), 11)
        refused_before_the_fix = [
            (predicted, successes)
            for predicted, successes in pairs
            if abs(predicted - successes / 20) > META_GATE_MAX_CALIBRATION_ERROR
        ]
        self.assertTrue(refused_before_the_fix, "the float edge should still exist")
        for predicted, successes in pairs:
            gate = self._gate(predicted, successes)
            self.assertLessEqual(
                gate["calibration_error"], META_GATE_MAX_CALIBRATION_ERROR
            )
            self.assertTrue(
                gate["allowed"],
                f"predicted={predicted} observed={successes / 20}: {gate['reasons']}",
            )

    def test_a_genuinely_miscalibrated_family_is_still_refused(self) -> None:
        """Rounding to nine decimals cannot admit a real miss."""
        gate = self._gate(0.80, 20)  # observed 1.0, error 0.20
        self.assertFalse(gate["allowed"])
        self.assertIn(
            "calibration error must be <= 0.15; is 0.200",
            " ".join(gate["reasons"]),
        )

    def test_the_report_republishes_the_corrected_verdict(self) -> None:
        memory = Memory(self.database)
        self.addCleanup(memory.close)
        _seed(memory, "security_analysis", attempts=20, predicted=0.85,
              successes=20, evidence=None)
        report = calibration.calibration_report(memory)
        row = next(
            item for item in report["families"]
            if item["family"] == "security_analysis"
        )
        self.assertTrue(row["allowed"])
        self.assertIn("security_analysis", report["calibrated_families"])

class EvidenceDocumentTests(unittest.TestCase):
    def test_platform_is_the_os_name_and_python_version_only(self) -> None:
        block = calibration.evidence_platform()
        self.assertEqual(set(block), {"os", "python"})
        self.assertEqual(block["os"], platform.system())
        self.assertEqual(block["python"], platform.python_version())

    def _document(self) -> dict:
        return calibration.evidence_document(
            {"report_version": 1, "calibrated_family_count": 0,
             "resolved_outcomes_counted": 0},
            benchmark="calibration",
            provider={"name": "ollama", "version": "0.32.15"},
            base={"kind": "phase2_base_file_manifest",
                  "manifest_sha256": "6616688bfcc072ee", "committed": False},
            configuration_class={"summary": "isolated sandbox",
                                 "switches": {"JARVIS_EXECUTION_MODE": "disabled"}},
            command="python scripts/run_phase2_calibration.py sandbox --root <sandbox>",
            result="0 calibrated families",
            known_limitations=["sandbox only"],
            model={"requested": "qwen3.5:9b", "served": "qwen3.5:9b"},
        )

    def test_evidence_document_carries_no_host_or_home_path(self) -> None:
        document = self._document()
        payload = json.dumps(document)
        self.assertNotIn(platform.node(), payload)
        self.assertNotIn(str(Path.home()), payload)
        self.assertNotIn(str(Path.home().as_posix()), payload)
        self.assertIn("sandbox_disclaimer", document)
        self.assertIn("not the operator's rows", document["sandbox_disclaimer"])

    def test_evidence_document_uses_the_shared_phase2_field_names(self) -> None:
        """WP-13 collates every package's artifact; the key names must line up."""
        document = self._document()
        for key in (
            "benchmark", "created_at", "base", "provider", "configuration_class",
            "command", "platform", "result", "known_limitations", "model",
        ):
            self.assertIn(key, document)
        self.assertEqual(document["base"]["manifest_sha256"], "6616688bfcc072ee")
        self.assertIs(document["base"]["committed"], False)
        self.assertNotIn("run", document)

    def test_scalar_arguments_are_normalised_into_blocks(self) -> None:
        document = calibration.evidence_document(
            {},
            benchmark="calibration",
            provider="ollama",
            base="6616688bfcc072ee",
            configuration_class="isolated sandbox",
            command="cmd",
            result="none",
            known_limitations=(),
            model="qwen3.5:9b",
            run={"cases_run": 3},
        )
        self.assertEqual(document["provider"], {"name": "ollama"})
        self.assertEqual(document["base"], {"manifest_sha256": "6616688bfcc072ee"})
        self.assertEqual(
            document["configuration_class"], {"summary": "isolated sandbox"}
        )
        self.assertEqual(document["model"], {"requested": "qwen3.5:9b"})
        self.assertEqual(document["run"], {"cases_run": 3})

    def test_evidence_filename_uses_the_manifest_hash8(self) -> None:
        self.assertEqual(
            calibration.evidence_filename("calibration", "ollama", "6616688bfcc072ee"),
            "phase2_calibration_ollama_6616688b.json",
        )
        with self.assertRaises(ValueError):
            calibration.evidence_filename("calibration", "ollama", "short")
        with self.assertRaises(ValueError):
            calibration.evidence_filename("calib ration", "ollama", "6616688bfcc072ee")

    def test_write_evidence_uses_lf_line_endings(self) -> None:
        with tempfile.TemporaryDirectory() as holder:
            target = Path(holder) / "nested" / "evidence.json"
            calibration.write_evidence({"a": 1}, target)
            self.assertNotIn(b"\r\n", target.read_bytes())
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"a": 1})


class HarnessCaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = _harness_module()

    def test_case_plan_is_balanced_and_uniquely_identified(self) -> None:
        families = list(self.harness.OFFLINE_FAMILIES)
        cases = self.harness.build_cases(families, 20)
        self.assertEqual(len(cases), 60)
        self.assertEqual(len({case.case_id for case in cases}), 60)
        for family in families:
            self.assertEqual(
                sum(1 for case in cases if case.intended_family == family), 20
            )
        # Interleaved, so an interrupted run stays balanced across families.
        self.assertEqual(
            [case.intended_family for case in cases[: len(families)]], families
        )

    def test_every_case_names_a_known_verifier(self) -> None:
        known = set(self.harness.VERIFIERS)
        for case in self.harness.build_cases(self.harness.OFFLINE_FAMILIES, 20):
            self.assertIn(case.check, known, case.case_id)
            self.assertTrue(case.prompt.strip())

    def test_only_filter_selects_a_named_subset_in_plan_order(self) -> None:
        """Topping a family back up after a host failure must not reorder or invent."""
        harness = self.harness
        plan = harness.build_cases(harness.OFFLINE_FAMILIES, 20)
        wanted = {"conversation_pow_a", "file_ops_replace_02", "security_analysis_mtls"}
        selected = [case for case in plan if case.case_id in wanted]
        self.assertEqual(len(selected), 3)
        self.assertEqual(
            [case.case_id for case in selected],
            [case.case_id for case in plan if case.case_id in wanted],
        )
        # Every re-runnable case either seeds its own starting state or is graded
        # from the reply, so a repeat is never pre-satisfied by the first attempt.
        for case in selected:
            self.assertTrue(
                case.seed_files or case.check == "answer_has_token", case.case_id
            )

    def test_requesting_more_cases_than_authored_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.harness.build_cases(["file_ops"], 21)
        with self.assertRaises(ValueError):
            self.harness.build_cases(["code_test"], 1)

    def test_file_verifiers_check_the_filesystem_not_the_reply(self) -> None:
        harness = self.harness
        with tempfile.TemporaryDirectory() as holder:
            workspace = Path(holder)
            case = next(
                item for item in harness.file_ops_cases()
                if item.check == "file_has_line"
            )
            claim = "I have created the file exactly as requested. Task complete."
            self.assertFalse(harness.verify(case, workspace, claim))
            target = workspace / case.check_args["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(case.check_args["line"] + "\n", encoding="utf-8")
            self.assertTrue(harness.verify(case, workspace, ""))

    def test_absence_and_rename_verifiers(self) -> None:
        harness = self.harness
        with tempfile.TemporaryDirectory() as holder:
            workspace = Path(holder)
            absent = next(
                item for item in harness.file_ops_cases() if item.check == "file_absent"
            )
            stale = workspace / absent.check_args["path"]
            stale.write_text("x\n", encoding="utf-8")
            self.assertFalse(harness.verify(absent, workspace, "Deleted it."))
            stale.unlink()
            self.assertTrue(harness.verify(absent, workspace, ""))

            renamed = next(
                item for item in harness.file_ops_cases() if item.check == "renamed"
            )
            old = workspace / renamed.check_args["absent"]
            new = workspace / renamed.check_args["path"]
            old.write_text(renamed.check_args["line"] + "\n", encoding="utf-8")
            new.write_text(renamed.check_args["line"] + "\n", encoding="utf-8")
            self.assertFalse(harness.verify(renamed, workspace, ""))
            old.unlink()
            self.assertTrue(harness.verify(renamed, workspace, ""))

    def test_exact_line_verifier_rejects_extra_lines(self) -> None:
        """"Write only its first line" must not pass when the whole file is copied."""
        harness = self.harness
        case = next(
            item for item in harness.file_ops_cases()
            if item.case_id == "file_ops_first_line_01"
        )
        self.assertTrue(case.check_args.get("exact"))
        with tempfile.TemporaryDirectory() as holder:
            workspace = Path(holder)
            target = workspace / case.check_args["path"]
            wanted = case.check_args["lines"][0]
            target.write_text(f"{wanted}\ndelta-9014\nthird\n", encoding="utf-8")
            self.assertFalse(harness.verify(case, workspace, ""))
            target.write_text(f"{wanted}\n", encoding="utf-8")
            self.assertTrue(harness.verify(case, workspace, ""))

    def test_json_verifier_requires_the_exact_value(self) -> None:
        harness = self.harness
        with tempfile.TemporaryDirectory() as holder:
            workspace = Path(holder)
            case = next(
                item for item in harness.file_ops_cases() if item.check == "json_has_key"
            )
            target = workspace / case.check_args["path"]
            target.write_text("not json", encoding="utf-8")
            self.assertFalse(harness.verify(case, workspace, ""))
            target.write_text(json.dumps({"marker": "wrong"}), encoding="utf-8")
            self.assertFalse(harness.verify(case, workspace, ""))
            target.write_text(
                json.dumps({"marker": case.check_args["value"]}), encoding="utf-8"
            )
            self.assertTrue(harness.verify(case, workspace, ""))

    def test_answer_verifier_rejects_a_confident_claim_without_the_answer(self) -> None:
        harness = self.harness
        case = next(
            item for item in harness.conversation_cases()
            if item.check_args["expected"] == "391"
        )
        with tempfile.TemporaryDirectory() as holder:
            workspace = Path(holder)
            self.assertFalse(harness.verify(
                case, workspace, "I have calculated this correctly. Done."
            ))
            self.assertFalse(harness.verify(case, workspace, "The answer is 3910."))
            self.assertFalse(harness.verify(case, workspace, "The answer is 1391."))
            self.assertTrue(harness.verify(case, workspace, "17 x 23 = 391."))

    def test_conversation_answer_keys_are_computed_not_asserted(self) -> None:
        harness = self.harness
        keys = {
            case.case_id: case.check_args["expected"]
            for case in harness.conversation_cases()
        }
        self.assertEqual(keys["conversation_mul_a"], str(17 * 23))
        self.assertEqual(keys["conversation_sec_a"], str(45 * 60))
        self.assertEqual(len(set(keys)), 20)

    def test_security_cases_avoid_time_sensitive_research(self) -> None:
        from jarvis.security_expertise import (
            classify_security_expertise,
            requires_current_security_research,
        )

        for case in self.harness.security_analysis_cases():
            self.assertTrue(
                classify_security_expertise(case.prompt).active,
                f"{case.case_id} would not route to security_analysis",
            )
            self.assertFalse(
                requires_current_security_research(case.prompt),
                f"{case.case_id} would demand current sources",
            )

    def test_every_prompt_routes_to_its_intended_family(self) -> None:
        """The deterministic classifier must agree with each case's intent.

        This is the check that would have caught the first draft, where four
        ``file_ops`` shapes tripped ``_requires_coding`` and were measured as
        ``code_build``/``code_refactor`` instead. A TaskContract lane can still
        override the family at run time, which is why the sandbox records the
        observed family rather than assuming this one.
        """
        from jarvis.agent import _requires_coding, _task_family
        from jarvis.security_expertise import classify_security_expertise

        for case in self.harness.build_cases(self.harness.OFFLINE_FAMILIES, 20):
            coding = _requires_coding(case.prompt)
            family = _task_family(
                case.prompt,
                casual_greeting=False,
                learning_task=False,
                deep_research_task=False,
                requires_coding=coding,
                requires_web=False,
                allow_external_mutation=False,
                allow_computer_files=False,
                security_task=classify_security_expertise(case.prompt).active,
            )
            self.assertEqual(family, case.intended_family, case.case_id)

    def test_unknown_verifier_is_an_error_not_a_pass(self) -> None:
        harness = self.harness
        broken = harness.Case("x", "file_ops", "p", "no_such_check", {})
        with tempfile.TemporaryDirectory() as holder:
            with self.assertRaises(ValueError):
                harness.verify(broken, Path(holder), "")


class HarnessBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = _harness_module()

    def test_sandbox_environment_disables_every_optional_capability(self) -> None:
        with tempfile.TemporaryDirectory() as holder:
            environment = self.harness.sandbox_environment(
                Path(holder), self.harness.DEFAULT_MODEL
            )
        self.assertEqual(environment["JARVIS_EXECUTION_MODE"], "disabled")
        self.assertEqual(environment["JARVIS_COMPUTER_ACCESS"], "disabled")
        self.assertEqual(environment["JARVIS_EXTERNAL_ACCESS"], "disabled")
        self.assertEqual(environment["JARVIS_CLOUD_ENABLED"], "false")
        self.assertEqual(environment["JARVIS_OLLAMA_ENABLED"], "true")
        for key in (
            "JARVIS_MODEL", "JARVIS_FAST_MODEL", "JARVIS_REASONING_MODEL",
            "JARVIS_CODING_MODEL", "JARVIS_DEEP_MODEL", "JARVIS_LEARNING_MODEL",
        ):
            self.assertEqual(environment[key], self.harness.DEFAULT_MODEL)
        self.assertTrue(environment["JARVIS_DATA"].endswith("data"))
        self.assertTrue(environment["JARVIS_WORKSPACE"].endswith("workspace"))

    def test_sandbox_environment_refuses_a_non_local_provider(self) -> None:
        with tempfile.TemporaryDirectory() as holder:
            for model in ("anthropic:claude-sonnet-4-5", "openai:gpt-4o",
                          "claude-cli:claude-sonnet-4-5", "codex-cli:gpt-5.6-sol"):
                with self.assertRaises(ValueError, msg=model):
                    self.harness.sandbox_environment(Path(holder), model)
            # An ordinary Ollama name:tag reference is accepted.
            self.harness.sandbox_environment(Path(holder), "qwen3.5:9b")

    def test_a_case_refuses_to_run_outside_the_sandbox(self) -> None:
        harness = self.harness
        with tempfile.TemporaryDirectory() as holder:
            good = harness.sandbox_environment(Path(holder), harness.DEFAULT_MODEL)
        harness.assert_sandboxed(good)  # the sandbox environment is accepted
        for key, value in (
            ("JARVIS_DATA", ""),
            ("JARVIS_WORKSPACE", ""),
            ("JARVIS_EXECUTION_MODE", "trusted-host"),
            ("JARVIS_COMPUTER_ACCESS", "trusted-desktop"),
            ("JARVIS_EXTERNAL_ACCESS", "trusted-external"),
            ("JARVIS_CLOUD_ENABLED", "true"),
        ):
            broken = dict(good)
            broken[key] = value
            with self.assertRaises(SystemExit, msg=key):
                harness.assert_sandboxed(broken)
        with self.assertRaises(SystemExit):
            harness.assert_sandboxed({})

    def test_recorded_switches_name_no_path(self) -> None:
        switches = self.harness._sandbox_switches(None, self.harness.DEFAULT_MODEL)
        self.assertEqual(switches["JARVIS_EXECUTION_MODE"], "disabled")
        self.assertEqual(switches["JARVIS_CLOUD_ENABLED"], "false")
        self.assertNotIn("JARVIS_DATA", switches)
        self.assertNotIn("JARVIS_WORKSPACE", switches)
        joined = " ".join(switches.values())
        self.assertNotIn(str(Path.home()), joined)

    def test_base_block_reads_the_wp0_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as holder:
            manifest = Path(holder) / "manifest.json"
            manifest.write_text(json.dumps({
                "manifest_sha256": "6616688bfcc072ee",
                "base_commit": "35d5344",
                "branch": "claude/roadmap-phase1",
                "files": {"a": "b", "c": "d"},
            }), encoding="utf-8")
            block = self.harness._base_block(
                SimpleNamespace(manifest_file=str(manifest), manifest_sha256=None)
            )
        self.assertEqual(block["manifest_sha256"], "6616688bfcc072ee")
        self.assertEqual(block["manifest_file_count"], 2)
        self.assertIs(block["committed"], False)
        with self.assertRaises(SystemExit):
            self.harness._base_block(
                SimpleNamespace(manifest_file=None, manifest_sha256=None)
            )

    def test_prepare_root_refuses_the_repository(self) -> None:
        with self.assertRaises(ValueError):
            self.harness.prepare_root(ROOT)
        with self.assertRaises(ValueError):
            self.harness.prepare_root(ROOT / "workspace" / "sandbox")

    def test_harness_never_records_or_resolves_a_prediction(self) -> None:
        """The runtime owns the outcome rows; the harness only runs and reads.

        Checked on the parsed syntax tree, so prose in a docstring can neither
        pass nor fail this test: only a real call counts.
        """
        tree = ast.parse(
            (ROOT / "scripts" / "run_phase2_calibration.py").read_text(encoding="utf-8")
        )
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            self.assertNotIn(name, {"record_prediction", "resolve_prediction"})
            for keyword in node.keywords:
                if keyword.arg == "origin":
                    self.fail("the harness must never choose a prediction origin")

    def test_report_module_issues_no_write_sql(self) -> None:
        tree = ast.parse(
            (ROOT / "jarvis" / "calibration_report.py").read_text(encoding="utf-8")
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                self.assertIsNone(
                    _WRITE_STATEMENT.search(node.value),
                    f"write statement in a literal: {node.value[:60]!r}",
                )
            if isinstance(node, ast.Call):
                name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
                self.assertNotIn(
                    name,
                    {"record_prediction", "resolve_prediction", "executescript"},
                )

    def test_capability_requirements_cover_every_family(self) -> None:
        harness = self.harness
        self.assertEqual(
            set(harness.CAPABILITY_REQUIREMENTS), set(Memory.PREDICTION_FAMILIES)
        )
        for family in harness.OFFLINE_FAMILIES:
            self.assertIn(family, harness.CAPABILITY_REQUIREMENTS)
            self.assertIn(family, harness.CASE_BUILDERS)


class RetryAndMergeTests(unittest.TestCase):
    """M-2 and M-4: a retry must not double-count, and the merge must say so."""

    def setUp(self) -> None:
        self.harness = _harness_module()
        self.holder = tempfile.TemporaryDirectory()
        self.addCleanup(self.holder.cleanup)

    def _run_file(self, name: str, cases: list[dict]) -> Path:
        path = Path(self.holder.name) / name
        path.write_text(
            json.dumps({"model": "qwen3.5:9b", "per_family": 20,
                        "families_requested": ["file_ops"], "cases": cases}),
            encoding="utf-8",
        )
        return path

    def test_retry_selects_only_the_cases_that_recorded_nothing(self) -> None:
        path = self._run_file("run1.json", [
            {"case_id": "file_ops_create_line_01", "prediction_id": 1},
            {"case_id": "conversation_pow_a", "prediction_id": None,
             "error": "TimeoutExpired"},
            {"case_id": "security_analysis_mtls", "prediction_id": None,
             "error": "no result (exit 3221225794)"},
        ])
        self.assertEqual(
            self.harness.retryable_case_ids(path),
            ["conversation_pow_a", "security_analysis_mtls"],
        )

    def test_retry_refuses_a_run_where_everything_recorded(self) -> None:
        path = self._run_file("run2.json", [
            {"case_id": "file_ops_create_line_01", "prediction_id": 1},
        ])
        with self.assertRaises(ValueError) as caught:
            self.harness.retryable_case_ids(path)
        self.assertIn("nothing to retry", str(caught.exception))

    def test_retry_refuses_a_case_that_appears_twice_with_an_outcome(self) -> None:
        path = self._run_file("run3.json", [
            {"case_id": "conversation_pow_a", "prediction_id": None, "error": "x"},
            {"case_id": "conversation_pow_a", "prediction_id": 7},
        ])
        with self.assertRaises(ValueError) as caught:
            self.harness.retryable_case_ids(path)
        self.assertIn("double-count", str(caught.exception))

    def test_merge_replaces_the_lost_attempt_and_records_provenance(self) -> None:
        first = self._run_file("first.json", [
            {"case_id": "file_ops_create_line_01", "prediction_id": 1,
             "observed_family": "file_ops", "recorded_status": "complete",
             "independently_verified": True, "seconds": 5.0},
            {"case_id": "conversation_pow_a", "prediction_id": None,
             "observed_family": None, "recorded_status": None,
             "independently_verified": False, "seconds": 300.0,
             "error": "TimeoutExpired"},
        ])
        second = self._run_file("second.json", [
            {"case_id": "conversation_pow_a", "prediction_id": 9,
             "observed_family": "conversation", "recorded_status": "complete",
             "independently_verified": True, "seconds": 8.0},
        ])
        merged = self.harness.merge_runs([first, second])
        self.assertEqual(merged["cases_run"], 2)
        self.assertEqual(merged["cases_recorded"], 2)
        self.assertEqual(merged["retried_after_host_failure"], ["conversation_pow_a"])
        self.assertIn("TimeoutExpired", merged["retry_reason"])
        self.assertEqual(
            sorted(merged["by_observed_family"]), ["conversation", "file_ops"]
        )
        self.assertNotIn("unrecorded", merged["by_observed_family"])
        self.assertEqual(
            merged["agreement_is_an_upper_bound_for"],
            list(self.harness.EVIDENCE_FREE_FAMILIES),
        )
        self.assertTrue(any("merge-run" in step for step in merged["commands"]))
        self.assertTrue(any("--retry-from" in step for step in merged["commands"]))

    def test_merge_refuses_to_stack_two_recorded_attempts(self) -> None:
        first = self._run_file("a.json", [
            {"case_id": "file_ops_create_line_01", "prediction_id": 1},
        ])
        second = self._run_file("b.json", [
            {"case_id": "file_ops_create_line_01", "prediction_id": 2},
        ])
        with self.assertRaises(ValueError) as caught:
            self.harness.merge_runs([first, second])
        self.assertIn("double-count", str(caught.exception))

    def test_the_delta_check_accepts_one_row_per_recorded_case(self) -> None:
        expected = self.harness.assert_outcome_delta(
            {"conversation": 19, "file_ops": 20},
            {"conversation": 20, "file_ops": 20},
            [
                {"case_id": "conversation_pow_a", "observed_family": "conversation"},
                {"case_id": "conversation_avg_a", "observed_family": None},
            ],
        )
        self.assertEqual(expected, {"conversation": 1})

    def test_the_delta_check_catches_a_double_counted_retry(self) -> None:
        with self.assertRaises(RuntimeError) as caught:
            self.harness.assert_outcome_delta(
                {"conversation": 19},
                {"conversation": 21},
                [{"case_id": "conversation_pow_a",
                  "observed_family": "conversation"}],
            )
        message = str(caught.exception)
        self.assertIn("double-counted", message)
        self.assertIn("conversation", message)

    def test_the_delta_check_catches_a_row_that_never_arrived(self) -> None:
        with self.assertRaises(RuntimeError):
            self.harness.assert_outcome_delta(
                {"file_ops": 19},
                {"file_ops": 19},
                [{"case_id": "file_ops_copy_01", "observed_family": "file_ops"}],
            )

    def test_the_delta_check_catches_a_writer_outside_the_harness(self) -> None:
        with self.assertRaises(RuntimeError):
            self.harness.assert_outcome_delta(
                {"security_analysis": 5},
                {"security_analysis": 6},
                [],
            )

    def test_recorded_command_uses_the_merged_provenance(self) -> None:
        line = self.harness._recorded_command({"commands": ["step one", "step two"]})
        self.assertIn("step one", line)
        self.assertIn("step two", line)
        self.assertIn(
            "report --database <db>", self.harness._recorded_command(None)
        )


class TargetClearingTests(unittest.TestCase):
    """M-3: a repeat must not be pre-satisfied by the first attempt leftovers."""

    def setUp(self) -> None:
        self.harness = _harness_module()

    def test_every_file_case_declares_the_paths_its_verifier_reads(self) -> None:
        for case in self.harness.file_ops_cases():
            targets = self.harness.target_paths(case)
            self.assertTrue(targets, case.case_id)
            self.assertIn(str(case.check_args["path"]), targets)
            if case.check == "renamed":
                self.assertIn(str(case.check_args["absent"]), targets)

    def test_a_leftover_target_would_otherwise_pass_a_do_nothing_repeat(self) -> None:
        with tempfile.TemporaryDirectory() as holder:
            workspace = Path(holder)
            case = next(
                item for item in self.harness.file_ops_cases()
                if item.case_id == "file_ops_create_line_01"
            )
            target = workspace / case.check_args["path"]
            target.write_text(case.check_args["line"] + "\n", encoding="utf-8")
            # Left in place, a do-nothing repeat passes.
            self.assertTrue(self.harness.verify(case, workspace, ""))
            # Cleared the way run_one_case clears it, the repeat has to earn it.
            for relative in self.harness.target_paths(case):
                (workspace / relative).unlink(missing_ok=True)
            self.assertFalse(self.harness.verify(case, workspace, ""))

    @staticmethod
    def _write_success(case, workspace: Path) -> None:
        """Put the workspace into the state a successful first attempt leaves."""
        args = case.check_args
        target = workspace / str(args["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        if case.check == "file_absent":
            target.unlink(missing_ok=True)
            return
        if case.check == "renamed":
            (workspace / str(args["absent"])).unlink(missing_ok=True)
            target.write_text(str(args["line"]) + "\n", encoding="utf-8")
            return
        if case.check == "json_has_key":
            target.write_text(
                json.dumps({str(args["key"]): str(args["value"])}), encoding="utf-8"
            )
            return
        if case.check == "file_has_lines":
            target.write_text(
                "\n".join(str(item) for item in args["lines"]) + "\n", encoding="utf-8"
            )
            return
        target.write_text(str(args["line"]) + "\n", encoding="utf-8")

    def _do_nothing_repeat_passes(self, case, clear: bool) -> bool:
        """Would a repeat that does no work at all score as a pass?"""
        harness = self.harness
        with tempfile.TemporaryDirectory() as holder:
            workspace = Path(holder)
            self._write_success(case, workspace)
            if clear:
                for relative in harness.target_paths(case):
                    (workspace / relative).unlink(missing_ok=True)
            for relative, content in case.seed_files:
                seed = workspace / relative
                seed.parent.mkdir(parents=True, exist_ok=True)
                seed.write_bytes(content.encode("utf-8"))
            return harness.verify(case, workspace, "")

    def test_six_shapes_would_pass_a_do_nothing_repeat_without_clearing(self) -> None:
        at_risk = sorted(
            case.case_id
            for case in self.harness.file_ops_cases()
            if self._do_nothing_repeat_passes(case, clear=False)
        )
        shapes = sorted({case_id.rsplit("_", 1)[0] for case_id in at_risk})
        self.assertEqual(shapes, [
            "file_ops_copy",
            "file_ops_create_line",
            "file_ops_first_line",
            "file_ops_folder",
            "file_ops_json",
            "file_ops_three_lines",
        ])
        self.assertEqual(len(at_risk), 12)

    def test_clearing_the_targets_closes_every_one_of_them(self) -> None:
        still_free = [
            case.case_id
            for case in self.harness.file_ops_cases()
            if self._do_nothing_repeat_passes(case, clear=True)
        ]
        self.assertEqual(still_free, [])

    def test_run_one_case_clears_before_it_seeds(self) -> None:
        source = (ROOT / "scripts" / "run_phase2_calibration.py").read_text(
            encoding="utf-8"
        )
        body = source[source.index("def run_one_case("):]
        clear = body.index("unlink(missing_ok=True)")
        seed = body.index("for relative, content in case.seed_files:")
        self.assertLess(clear, seed, "targets must be cleared before seeding")


class AnswerVerifierTests(unittest.TestCase):
    """M-6: a denial next to the token must not read as a correct answer."""

    ADVERSARIAL = (
        ("391", True),
        ("17 x 23 = 391.", True),
        ("The answer is 391", True),
        ("No, it is 391.", True),
        ("The answer is 391. That is not 392.", True),
        ("Working: 17*23.\nThe answer is 391.", True),
        ("391.", True),
        ("The answer is not 391.", False),
        ("It isn't 391; it is 392.", False),
        ("391 is wrong. The answer is 392.", False),
        ("The result is 392, not 391.", False),
        ("I cannot compute 391 for you.", False),
        ("Some say 391 but that is incorrect.\nThe answer is 392.", False),
        ("Rather than 391, use 392.", False),
        ("391 is incorrect.", False),
        ("Never 391.", False),
        ("The answer is 3910.", False),
        ("The answer is 1391.", False),
        ("I have calculated this correctly. Done.", False),
    )

    def setUp(self) -> None:
        self.harness = _harness_module()

    def test_adversarial_replies(self) -> None:
        for reply, expected in self.ADVERSARIAL:
            with self.subTest(reply=reply):
                self.assertEqual(
                    self.harness._token_present(reply, "391"), expected
                )

    def test_negation_is_detected_for_word_and_dotted_answers(self) -> None:
        harness = self.harness
        self.assertTrue(harness._token_present("desserts", "desserts"))
        self.assertFalse(
            harness._token_present("The word is not desserts.", "desserts")
        )
        self.assertTrue(
            harness._token_present("255.255.255.192", "255.255.255.192")
        )
        self.assertFalse(
            harness._token_present(
                "It is not 255.255.255.192.", "255.255.255.192"
            )
        )

    def test_bare_no_is_not_a_denial(self) -> None:
        self.assertTrue(self.harness._token_present("No. The answer is 4.", "4"))

    def test_the_upper_bound_caveat_names_the_evidence_free_families(self) -> None:
        text = self.harness.UPPER_BOUND_LIMITATION
        for family in self.harness.EVIDENCE_FREE_FAMILIES:
            self.assertIn(family, text)
        self.assertIn("upper bound", text)

class SidecarAndDefaultsTests(unittest.TestCase):
    """M-5 and L-3: read nothing into existence, and mirror drift_report."""

    def setUp(self) -> None:
        self.holder = tempfile.TemporaryDirectory()
        self.addCleanup(self.holder.cleanup)
        self.database = Path(self.holder.name) / "jarvis.db"

    def _wal_database(self) -> None:
        memory = Memory(self.database)
        _seed(memory, "file_ops", attempts=21, predicted=0.85, successes=19)
        mode = memory.db.execute("PRAGMA journal_mode").fetchone()[0]
        memory.close()
        self.assertEqual(str(mode).casefold(), "wal")

    def _sidecars(self) -> list[str]:
        return sorted(
            path.name
            for path in Path(self.holder.name).iterdir()
            if path.name.startswith("jarvis.db-")
        )

    def test_reading_a_wal_database_leaves_no_new_sidecar(self) -> None:
        self._wal_database()
        before = self._sidecars()
        with calibration.read_only_memory(
            self.database, scratch_dir=self.holder.name
        ) as copy:
            report = calibration.calibration_report(copy)
        self.assertEqual(report["resolved_outcomes_counted"], 21)
        self.assertEqual(self._sidecars(), before)

    def test_a_sidecar_that_was_already_there_is_left_alone(self) -> None:
        self._wal_database()
        planted = self.database.with_name(self.database.name + "-wal")
        planted.write_bytes(b"")
        stamp = planted.stat().st_mtime_ns
        with calibration.read_only_memory(
            self.database, scratch_dir=self.holder.name
        ):
            pass
        self.assertTrue(planted.exists(), "a live writer's sidecar must survive")
        self.assertEqual(planted.stat().st_mtime_ns, stamp)

    def test_drift_defaults_mirror_the_runtime_signature(self) -> None:
        signature = inspect.signature(Memory.drift_report)
        self.assertEqual(
            signature.parameters["window"].default,
            calibration.DEFAULT_DRIFT_WINDOW,
        )
        self.assertEqual(
            signature.parameters["baseline"].default,
            calibration.DEFAULT_DRIFT_BASELINE,
        )
        self.assertEqual(
            signature.parameters["minimum_samples"].default,
            calibration.DEFAULT_DRIFT_MINIMUM_SAMPLES,
        )

    def test_the_report_uses_those_defaults(self) -> None:
        memory = Memory(self.database)
        self.addCleanup(memory.close)
        report = calibration.calibration_report(memory)
        self.assertEqual(report["drift"], {
            "window": calibration.DEFAULT_DRIFT_WINDOW,
            "baseline": calibration.DEFAULT_DRIFT_BASELINE,
            "minimum_samples": calibration.DEFAULT_DRIFT_MINIMUM_SAMPLES,
            "families_with_signals": [],
            "findings": [],
        })


class EvidenceOverwriteTests(unittest.TestCase):
    """N-1: a recorded result is evidence, not a scratch file."""

    def setUp(self) -> None:
        self.harness = _harness_module()
        self.holder = tempfile.TemporaryDirectory()
        self.addCleanup(self.holder.cleanup)

    def _args(self, destination: Path, force: bool) -> SimpleNamespace:
        return SimpleNamespace(
            json=False,
            evidence_out=str(destination),
            manifest_file=None,
            manifest_sha256="6616688bfcc072ee",
            provider="ollama",
            force=force,
            model="qwen3.5:9b",
        )

    def _emit(self, destination: Path, force: bool) -> None:
        database = Path(self.holder.name) / "stub.db"
        memory = Memory(database)
        try:
            report = calibration.calibration_report(memory)
        finally:
            memory.close()
        self.harness._emit_report(
            report,
            self._args(destination, force),
            command="cmd",
            configuration="cfg",
            limitations=["none"],
        )

    def test_an_existing_artifact_is_not_silently_replaced(self) -> None:
        target = Path(self.holder.name) / "evidence.json"
        target.write_text("{}", encoding="utf-8")
        with self.assertRaises(SystemExit) as caught:
            self._emit(target, force=False)
        self.assertIn("already exists", str(caught.exception))
        self.assertEqual(target.read_text(encoding="utf-8"), "{}")

    def test_force_replaces_it(self) -> None:
        target = Path(self.holder.name) / "evidence.json"
        target.write_text("{}", encoding="utf-8")
        self._emit(target, force=True)
        payload = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(payload["benchmark"], "calibration")

    def test_a_fresh_path_needs_no_force(self) -> None:
        target = Path(self.holder.name) / "fresh.json"
        self._emit(target, force=False)
        self.assertTrue(target.is_file())

class RefusalExitStatusTests(unittest.TestCase):
    """A refusal the caller can fix leaves cleanly, not as a traceback."""

    def setUp(self) -> None:
        self.harness = _harness_module()
        self.holder = tempfile.TemporaryDirectory()
        self.addCleanup(self.holder.cleanup)

    def _run_file(self, name: str, cases: list[dict]) -> Path:
        path = Path(self.holder.name) / name
        path.write_text(
            json.dumps({"model": "qwen3.5:9b", "per_family": 20,
                        "families_requested": ["file_ops"], "cases": cases}),
            encoding="utf-8",
        )
        return path

    def _main(self, argv: list[str]) -> tuple[int, str]:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            status = self.harness.main(argv)
        return status, stderr.getvalue()

    def test_a_run_with_nothing_to_retry_exits_two(self) -> None:
        run_file = self._run_file("complete.json", [
            {"case_id": "file_ops_create_line_01", "prediction_id": 1},
        ])
        root = Path(self.holder.name) / "sandbox"
        status, message = self._main([
            "sandbox", "--root", str(root), "--per-family", "20",
            "--retry-from", str(run_file),
        ])
        self.assertEqual(status, self.harness.REFUSED_EXIT)
        self.assertEqual(status, 2)
        self.assertIn("refused:", message)
        self.assertIn("nothing to retry", message)

    def test_an_unknown_case_id_exits_two(self) -> None:
        run_file = self._run_file("unknown.json", [
            {"case_id": "not_a_case_at_all", "prediction_id": None, "error": "x"},
        ])
        root = Path(self.holder.name) / "sandbox2"
        status, message = self._main([
            "sandbox", "--root", str(root), "--per-family", "20",
            "--retry-from", str(run_file),
        ])
        self.assertEqual(status, 2)
        self.assertIn("Unknown case id", message)

    def test_a_case_recorded_twice_exits_two(self) -> None:
        run_file = self._run_file("dupe.json", [
            {"case_id": "conversation_pow_a", "prediction_id": None, "error": "x"},
            {"case_id": "conversation_pow_a", "prediction_id": 4},
        ])
        root = Path(self.holder.name) / "sandbox3"
        status, message = self._main([
            "sandbox", "--root", str(root), "--per-family", "20",
            "--retry-from", str(run_file),
        ])
        self.assertEqual(status, 2)
        self.assertIn("double-count", message)

    def test_a_merge_that_would_stack_attempts_exits_two(self) -> None:
        first = self._run_file("m1.json", [
            {"case_id": "file_ops_create_line_01", "prediction_id": 1},
        ])
        second = self._run_file("m2.json", [
            {"case_id": "file_ops_create_line_01", "prediction_id": 2},
        ])
        status, message = self._main([
            "merge-run", "--run-file", str(first), "--run-file", str(second),
            "--out", str(Path(self.holder.name) / "merged.json"),
        ])
        self.assertEqual(status, 2)
        self.assertIn("double-count", message)
        self.assertFalse((Path(self.holder.name) / "merged.json").exists())

    def test_a_refusal_never_reaches_the_model(self) -> None:
        """The refusal happens before any case process is started."""
        run_file = self._run_file("early.json", [
            {"case_id": "file_ops_create_line_01", "prediction_id": 1},
        ])
        root = Path(self.holder.name) / "sandbox4"
        with unittest.mock.patch.object(
            self.harness.subprocess, "run",
            side_effect=AssertionError("no case may be started"),
        ):
            status, _ = self._main([
                "sandbox", "--root", str(root), "--per-family", "20",
                "--retry-from", str(run_file),
            ])
        self.assertEqual(status, 2)


class WriteEvidenceGuardTests(unittest.TestCase):
    """The overwrite guard lives in write_evidence, not only in the command."""

    def setUp(self) -> None:
        self.holder = tempfile.TemporaryDirectory()
        self.addCleanup(self.holder.cleanup)

    def test_an_existing_artifact_is_refused(self) -> None:
        target = Path(self.holder.name) / "evidence.json"
        target.write_text('{"kept": true}', encoding="utf-8")
        with self.assertRaises(FileExistsError) as caught:
            calibration.write_evidence({"replaced": True}, target)
        self.assertIn("already exists", str(caught.exception))
        self.assertEqual(
            json.loads(target.read_text(encoding="utf-8")), {"kept": True}
        )

    def test_force_replaces_it(self) -> None:
        target = Path(self.holder.name) / "evidence.json"
        target.write_text('{"kept": true}', encoding="utf-8")
        calibration.write_evidence({"replaced": True}, target, force=True)
        self.assertEqual(
            json.loads(target.read_text(encoding="utf-8")), {"replaced": True}
        )

    def test_a_fresh_path_is_written_with_lf_endings(self) -> None:
        target = Path(self.holder.name) / "nested" / "evidence.json"
        written = calibration.write_evidence({"a": 1}, target)
        self.assertEqual(written, target)
        self.assertNotIn(b"\r\n", target.read_bytes())

    def test_a_directory_in_the_way_is_not_clobbered(self) -> None:
        target = Path(self.holder.name) / "adirectory"
        target.mkdir()
        with self.assertRaises(FileExistsError):
            calibration.write_evidence({"a": 1}, target)

class RunbookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runbook = ROOT / "docs" / "CALIBRATION_RUNBOOK.md"
        self.text = self.runbook.read_text(encoding="utf-8")

    def test_runbook_exists_and_leads_with_the_sandbox_disclaimer(self) -> None:
        first = next(
            line for line in self.text.splitlines() if line.strip() and not line.startswith("#")
        )
        self.assertIn("not the operator", first)

    def test_runbook_names_only_commands_the_cli_defines(self) -> None:
        commands = set(re.findall(r"python -m jarvis ([a-z-]+)", self.text))
        self.assertTrue(commands)
        cli = (ROOT / "jarvis" / "cli.py").read_text(encoding="utf-8")
        defined = set(re.findall(r'add_parser\(\s*"([a-z-]+)"', cli))
        self.assertEqual(commands - defined, set())

    def test_runbook_states_the_practice_exclusion(self) -> None:
        self.assertIn("practice", self.text)
        self.assertIn("interactive", self.text)

    def test_runbook_records_the_production_fix_for_codex(self) -> None:
        self.assertIn("jarvis/memory_predictions.py", self.text)
        self.assertIn("round(abs(predicted - observed), 9)", self.text)
        self.assertIn("0.15000000000000002", self.text)
        self.assertIn("cannot loosen the gate", self.text)

    def test_runbook_raises_the_tier1_counting_question(self) -> None:
        self.assertIn("Open question for Codex", self.text)
        self.assertIn("three calibrated families", self.text.casefold())

    def test_runbook_labels_the_agreement_figure_an_upper_bound(self) -> None:
        self.assertIn("upper bound", self.text)
        self.assertIn("57 of 60", self.text)

    def test_runbook_documents_the_retry_and_overwrite_guards(self) -> None:
        self.assertIn("--retry-from", self.text)
        self.assertIn("merge-run", self.text)
        self.assertIn("--force", self.text)
    def test_runbook_records_the_thresholds_from_proactive(self) -> None:
        self.assertIn(f"{META_GATE_MAX_BRIER:.2f}", self.text)
        self.assertIn(f"{META_GATE_MAX_CALIBRATION_ERROR:.2f}", self.text)
        self.assertIn(str(META_GATE_MIN_ATTEMPTS), self.text)


if __name__ == "__main__":
    unittest.main()
