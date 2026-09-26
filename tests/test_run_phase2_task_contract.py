"""Cover the Phase 2 resolver evidence script without spending a provider call.

Exit codes, base-identity verification, gate arithmetic, the per-case
observation block, and a deny-list over the whole evidence document.
"""

import hashlib
import importlib.util
import json
import re
import tempfile
import unittest
from unittest.mock import patch
from argparse import Namespace
from pathlib import Path

from jarvis.task_contract_eval import load_task_contract_holdout


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "run_phase2_task_contract.py"
FIXTURE_PATH = PROJECT_ROOT / "tests" / "fixtures" / "task_contract_holdout_v2.json"


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "run_phase2_task_contract", SCRIPT_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


script = _load_script()


def _write_manifest(root: Path, *, corrupt: bool = False) -> Path:
    files = {"jarvis/example.py": "0" * 64, "tests/example.py": "1" * 64}
    digest = hashlib.sha256(
        json.dumps(
            files, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    manifest = {
        "base_commit": "abc1234",
        "branch": "claude/example",
        "files": files,
        "manifest_sha256": ("f" * 64) if corrupt else digest,
    }
    path = root / "manifest.json"
    path.write_bytes(json.dumps(manifest, indent=2).encode("utf-8"))
    return path


def _receipt(statuses: dict[str, str], *, checks_ok: bool = True) -> dict:
    """Build a receipt shaped exactly like the runner's, for named cases."""
    fixture = load_task_contract_holdout(FIXTURE_PATH)
    cases = []
    for case in fixture["cases"]:
        case_id = str(case["id"])
        status = statuses.get(case_id, "resolved")
        row = {
            "id": case_id,
            "status": status,
            "latency_ms": 1.0,
            "requested_model": "ollama:qwen3.5:9b",
            "model_attestation": "verified" if status == "resolved" else "not_observed",
        }
        if status == "resolved":
            row["checks"] = {
                name: checks_ok
                for name in (
                    "lane",
                    "clarification",
                    "relation",
                    "constraints",
                    "effect",
                    "evidence",
                    "acceptance",
                )
            }
        cases.append(row)
    return {
        "schema_version": 1,
        "benchmark": "task_contract_holdout_v2",
        "fixture_sha256": fixture["fixture_sha256"],
        "created_at": "2026-09-04T12:00:00+00:00",
        "provider": "ollama",
        "provider_model_attestation": "explicit_response_signal_required",
        "requested_model": "ollama:qwen3.5:9b",
        "model_attestation_required": True,
        "exact_model_only": all(row["status"] == "resolved" for row in cases),
        "fallback_count": 0,
        "tools_supplied": 0,
        "training_eligible": False,
        "memory_writes": 0,
        "operator_text_retained": False,
        "summary": {
            "case_count": len(cases),
            "resolved": sum(row["status"] == "resolved" for row in cases),
            "contract_rejected": sum(
                row["status"] == "contract_rejected" for row in cases
            ),
            "provider_error": 0,
            "model_mismatch": 0,
            "model_unattested": 0,
            "p50_latency_ms": 1.0,
            "p95_latency_ms": 1.0,
            "total_latency_ms": float(len(cases)),
            "contract_metrics": None,
        },
        "cases": cases,
        "receipt_checksum_sha256": "0" * 64,
    }


class ExitCodeTests(unittest.TestCase):
    def test_refuses_without_allow_live_with_status_two(self):
        self.assertEqual(script.main(["--model", "ollama:qwen3.5:9b"]), 2)

    def test_refuses_without_a_base_identity_with_status_two(self):
        self.assertEqual(
            script.main(["--model", "ollama:qwen3.5:9b", "--allow-live"]), 2
        )

    def test_refuses_an_unattestable_provider_with_status_two(self):
        with tempfile.TemporaryDirectory() as raw:
            manifest = _write_manifest(Path(raw))
            self.assertEqual(
                script.main([
                    "--model", "claude-cli:claude-sonnet-4-5",
                    "--allow-live",
                    "--base-manifest", str(manifest),
                ]),
                2,
            )

    def test_refuses_an_inexact_model_with_status_two(self):
        with tempfile.TemporaryDirectory() as raw:
            manifest = _write_manifest(Path(raw))
            for model in ("ollama:auto", "gpt-5.6-luna"):
                with self.subTest(model=model):
                    self.assertEqual(
                        script.main([
                            "--model", model,
                            "--allow-live",
                            "--base-manifest", str(manifest),
                        ]),
                        2,
                    )

    def test_refuses_an_unverifiable_manifest_with_status_two(self):
        with tempfile.TemporaryDirectory() as raw:
            manifest = _write_manifest(Path(raw), corrupt=True)
            self.assertEqual(
                script.main([
                    "--model", "ollama:qwen3.5:9b",
                    "--allow-live",
                    "--base-manifest", str(manifest),
                ]),
                2,
            )

    def test_refuses_a_missing_manifest_file_with_status_two(self):
        with tempfile.TemporaryDirectory() as raw:
            self.assertEqual(
                script.main([
                    "--model", "ollama:qwen3.5:9b",
                    "--allow-live",
                    "--base-manifest", str(Path(raw) / "absent.json"),
                ]),
                2,
            )


class OverwriteGuardTests(unittest.TestCase):
    """Recorded evidence is never replaced without an explicit instruction."""

    def _args(self, out: Path, manifest: Path, *, force: bool = False):
        argv = [
            "--model", "ollama:qwen3.5:9b",
            "--allow-live",
            "--base-manifest", str(manifest),
            "--out", str(out),
        ]
        if force:
            argv.append("--force")
        return argv

    def test_existing_artifact_is_not_overwritten_without_force(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest = _write_manifest(root)
            out = root / "existing.json"
            original = b'{"recorded": "evidence"}'
            out.write_bytes(original)
            with patch.object(
                script, "build_exact_model_benchmark_client",
                side_effect=AssertionError("provider must not be constructed"),
            ):
                self.assertEqual(script.main(self._args(out, manifest)), 2)
            # Refused before any provider call, and the file is untouched.
            self.assertEqual(out.read_bytes(), original)

    def test_force_is_accepted_and_gets_past_the_guard(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest = _write_manifest(root)
            out = root / "existing.json"
            out.write_bytes(b"{}")
            # The guard is cleared; the run then fails for an unrelated reason
            # (no provider on this test host), never with the guard status.
            calls: list[str] = []

            def refuse(model, **kwargs):
                calls.append(model)
                raise script.BenchmarkProviderError("no provider in this test")

            original = script.build_exact_model_benchmark_client
            script.build_exact_model_benchmark_client = refuse
            try:
                self.assertEqual(script.main(self._args(out, manifest, force=True)), 2)
            finally:
                script.build_exact_model_benchmark_client = original
            self.assertEqual(calls, ["ollama:qwen3.5:9b"])

    def test_a_fresh_path_needs_no_force(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest = _write_manifest(root)
            out = root / "fresh.json"
            calls: list[str] = []

            def refuse(model, **kwargs):
                calls.append(model)
                raise script.BenchmarkProviderError("no provider in this test")

            original = script.build_exact_model_benchmark_client
            script.build_exact_model_benchmark_client = refuse
            try:
                self.assertEqual(script.main(self._args(out, manifest)), 2)
            finally:
                script.build_exact_model_benchmark_client = original
            # It reached provider construction rather than stopping at the guard.
            self.assertEqual(calls, ["ollama:qwen3.5:9b"])
            self.assertFalse(out.exists())


class BaseIdentityTests(unittest.TestCase):
    def test_verified_manifest_reports_its_recomputed_self_hash(self):
        with tempfile.TemporaryDirectory() as raw:
            manifest = _write_manifest(Path(raw))
            identity = script._base_identity(manifest, None)
        self.assertTrue(identity["manifest_sha256_verified"])
        self.assertEqual(identity["manifest_file_count"], 2)
        self.assertEqual(identity["base_commit"], "abc1234")
        self.assertFalse(identity["committed"])
        self.assertRegex(identity["manifest_sha256"], r"\A[0-9a-f]{64}\Z")

    def test_a_corrupt_self_hash_is_refused(self):
        with tempfile.TemporaryDirectory() as raw:
            manifest = _write_manifest(Path(raw), corrupt=True)
            with self.assertRaises(script.BaseIdentityError):
                script._base_identity(manifest, None)

    def test_a_declared_hash_is_marked_unverified(self):
        identity = script._base_identity(None, "a" * 64)
        self.assertFalse(identity["manifest_sha256_verified"])
        self.assertIsNone(identity["manifest_file_count"])

    def test_no_identity_at_all_is_refused(self):
        with self.assertRaises(script.BaseIdentityError):
            script._base_identity(None, None)


class GateArithmeticTests(unittest.TestCase):
    def _metrics(self, **overrides):
        metrics = {
            "route_accuracy": 0.95,
            "ambiguity_recall": 0.95,
            "specified_false_positive_rate": 0.05,
            "route_by_lane": {
                lane: {"correct": 10, "total": 10, "accuracy": 1.0}
                for lane in ("dialogue", "research")
            },
        }
        metrics.update(overrides)
        return metrics

    def test_all_gates_pass_at_the_thresholds(self):
        rows = script._gate_rows(
            self._metrics(
                route_accuracy=0.90,
                ambiguity_recall=0.90,
                specified_false_positive_rate=0.10,
                route_by_lane={
                    "dialogue": {"correct": 9, "total": 10, "accuracy": 0.90}
                },
            )
        )
        self.assertTrue(all(row["passed"] for row in rows))

    def test_each_gate_fails_just_past_its_threshold(self):
        cases = (
            ("route_accuracy", 0.899),
            ("ambiguity_recall", 0.899),
            ("specified_false_positive_rate", 0.101),
        )
        for field, value in cases:
            with self.subTest(field=field):
                rows = script._gate_rows(self._metrics(**{field: value}))
                failed = [row for row in rows if not row["passed"]]
                self.assertEqual([row["metric_field"] for row in failed], [field])

    def test_one_bad_lane_fails_the_lane_gate_and_reports_the_worst(self):
        rows = script._gate_rows(
            self._metrics(
                route_by_lane={
                    "dialogue": {"correct": 10, "total": 10, "accuracy": 1.0},
                    "inspection": {"correct": 1, "total": 12, "accuracy": 0.083},
                }
            )
        )
        lane_row = next(r for r in rows if r["metric_field"] == "route_by_lane")
        self.assertFalse(lane_row["passed"])
        self.assertEqual(lane_row["observed"], 0.083)
        self.assertEqual(lane_row["observed_is"], "worst lane accuracy")
        self.assertFalse(lane_row["per_lane"]["inspection"]["passed"])
        self.assertTrue(lane_row["per_lane"]["dialogue"]["passed"])

    def test_no_metrics_reports_not_scored_rather_than_a_miss(self):
        rows = script._gate_rows(None)
        self.assertTrue(all(row["observed"] is None for row in rows))
        self.assertTrue(all(row["passed"] is False for row in rows))
        self.assertTrue(all("not scored" in row["note"] for row in rows))

    def test_every_roadmap_gate_bullet_has_a_row(self):
        rows = script._gate_rows(self._metrics())
        self.assertEqual({row["gate"] for row in rows}, {1, 3, 4})
        self.assertEqual(
            {row["metric_field"] for row in rows},
            {
                "route_accuracy",
                "route_by_lane",
                "ambiguity_recall",
                "specified_false_positive_rate",
            },
        )


class CaseObservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = load_task_contract_holdout(FIXTURE_PATH)

    def test_rates_use_resolved_denominators_and_state_them(self):
        # Reject one ambiguous and one specified case.
        ambiguous = [
            str(c["id"]) for c in self.fixture["cases"]
            if c["expected"]["clarification"] is True
        ]
        specified = [
            str(c["id"]) for c in self.fixture["cases"]
            if c["expected"]["clarification"] is False
        ]
        statuses = {
            ambiguous[0]: "contract_rejected",
            specified[0]: "contract_rejected",
        }
        observations = script._case_observations(
            _receipt(statuses), self.fixture, None
        )
        clarification = observations["clarification"]
        self.assertEqual(clarification["ambiguous_total"], len(ambiguous))
        self.assertEqual(clarification["ambiguous_unresolved"], 1)
        self.assertEqual(
            clarification["ambiguous_resolved"], len(ambiguous) - 1
        )
        self.assertEqual(clarification["specified_total"], len(specified))
        self.assertEqual(clarification["specified_unresolved"], 1)
        self.assertEqual(
            clarification["specified_resolved"], len(specified) - 1
        )
        # Resolved counts must reconcile: correct + unnecessary == resolved.
        self.assertEqual(
            clarification["specified_correct"]
            + clarification["specified_unnecessary"],
            clarification["specified_resolved"],
        )
        self.assertEqual(
            clarification["ambiguous_correct"]
            + len(clarification["ambiguous_missed_case_ids"]),
            clarification["ambiguous_resolved"],
        )
        self.assertIn("resolved cases only", clarification["denominator"])

    def test_unnecessary_clarification_rate_is_over_resolved_cases(self):
        # Every specified case wrongly asks for clarification; six rejected.
        specified = [
            str(c["id"]) for c in self.fixture["cases"]
            if c["expected"]["clarification"] is False
        ]
        statuses = {case_id: "contract_rejected" for case_id in specified[:6]}
        observations = script._case_observations(
            _receipt(statuses, checks_ok=False), self.fixture, None
        )
        clarification = observations["clarification"]
        resolved = clarification["specified_resolved"]
        self.assertEqual(resolved, len(specified) - 6)
        self.assertEqual(clarification["specified_unnecessary"], resolved)
        self.assertEqual(
            clarification["unnecessary_clarification_rate_over_resolved"], 1.0
        )

    def test_rates_are_none_when_nothing_resolved_in_a_group(self):
        statuses = {str(c["id"]): "contract_rejected" for c in self.fixture["cases"]}
        observations = script._case_observations(
            _receipt(statuses), self.fixture, None
        )
        clarification = observations["clarification"]
        self.assertIsNone(clarification["ambiguity_recall_over_resolved"])
        self.assertIsNone(
            clarification["unnecessary_clarification_rate_over_resolved"]
        )
        self.assertEqual(clarification["ambiguous_resolved"], 0)

    def test_lane_totals_cover_every_case_and_count_only_resolved_correct(self):
        statuses = {str(self.fixture["cases"][0]["id"]): "contract_rejected"}
        observations = script._case_observations(
            _receipt(statuses), self.fixture, None
        )
        by_lane = observations["by_expected_lane"]
        self.assertEqual(sum(v["total"] for v in by_lane.values()), 66)
        self.assertEqual(sum(v["resolved"] for v in by_lane.values()), 65)
        self.assertEqual(sum(v["lane_correct"] for v in by_lane.values()), 65)
        self.assertEqual(
            observations["unresolved_case_ids_by_status"]["contract_rejected"],
            [str(self.fixture["cases"][0]["id"])],
        )

    def test_the_note_tracks_whether_the_scorer_produced_metrics(self):
        without = script._case_observations(_receipt({}), self.fixture, None)
        self.assertIn("produced no metric", without["note"])
        with_metrics = script._case_observations(
            _receipt({}), self.fixture, {"route_accuracy": 1.0}
        )
        self.assertNotIn("produced no metric", with_metrics["note"])
        self.assertIn("also produced metrics", with_metrics["note"])


class EvidenceDocumentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = load_task_contract_holdout(FIXTURE_PATH)

    def _evidence(self, statuses=None):
        args = Namespace(
            model="ollama:qwen3.5:9b",
            fixture=FIXTURE_PATH,
            out=None,
            base_manifest=Path("manifest.json"),
            base_manifest_sha256=None,
            base_url=None,
            allow_live=True,
        )
        return script.build_evidence(
            receipt=_receipt(statuses or {}),
            fixture=self.fixture,
            args=args,
            provider="ollama",
            provider_version="0.32.15",
            provider_model="qwen3.5:9b",
            base_identity={
                "kind": "phase2_base_file_manifest",
                "manifest_sha256": "6" * 64,
                "manifest_sha256_verified": True,
                "manifest_file_count": 364,
                "base_commit": "abc1234",
                "branch": "claude/example",
                "committed": False,
            },
            wall_seconds=120.5,
        )

    def test_evidence_carries_every_field_the_policy_requires(self):
        evidence = self._evidence()
        self.assertEqual(evidence["platform"]["os"], script.platform.system())
        self.assertEqual(
            set(evidence["platform"]), {"os", "python"}
        )  # never node(), never a path
        self.assertEqual(evidence["provider"], {"name": "ollama", "version": "0.32.15"})
        self.assertIn("--allow-live", evidence["command"])
        self.assertTrue(evidence["known_limitations"])
        self.assertTrue(evidence["base"]["manifest_sha256_verified"])
        self.assertIn("configuration_class", evidence)
        self.assertIn("case_observations", evidence)

    def test_known_limitations_explain_the_attestation_and_rejection_wording(self):
        limitations = " ".join(self._evidence()["known_limitations"])
        self.assertIn("exact_model_only", limitations)
        self.assertIn("model_unattested", limitations)
        self.assertIn("reconcile_task_contract_continuation", limitations)
        self.assertNotIn("parse failure is", limitations)

    def test_deny_list_over_the_whole_evidence_document(self):
        evidence = self._evidence({str(self.fixture["cases"][0]["id"]): "contract_rejected"})
        serialized = json.dumps(evidence, ensure_ascii=False)
        for pattern, label in (
            (r"[A-Za-z]:[\\/]{1,2}Users", "Windows user-home path"),
            (r"/(?:home|Users)/", "POSIX user-home path"),
            (r"(?:\\\\|//)[^\\/\s]+[\\/]+(?:Users|home)", "UNC user-home path"),
            (r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "email address"),
            (r"(?i)\btraceback\b", "provider diagnostic"),
        ):
            with self.subTest(deny=label):
                self.assertIsNone(
                    re.search(pattern, serialized), f"evidence contains {label}"
                )
        # No case text from any field of the sealed fixture.
        for case in self.fixture["cases"]:
            self.assertNotIn(str(case["operator_prompt"]), serialized)
            pending = case.get("pending_contract")
            if isinstance(pending, dict):
                self.assertNotIn(str(pending["goal"]), serialized)
            for turn in case.get("recent_user_turns") or []:
                self.assertNotIn(str(turn), serialized)
            latest = case.get("latest_assistant_context")
            if isinstance(latest, str) and latest.strip():
                self.assertNotIn(latest, serialized)

    def test_configuration_class_records_only_allowlisted_switch_tokens(self):
        configuration = self._evidence()["configuration_class"]
        self.assertEqual(
            set(configuration["switches"]), set(script._CONFIGURATION_SWITCHES)
        )
        for value in configuration["switches"].values():
            self.assertTrue(
                value == "unset"
                or value == "other"
                or script._SWITCH_VALUE_RE.fullmatch(value)
            )

    def test_command_never_echoes_a_supplied_path(self):
        args = Namespace(
            model="ollama:qwen3.5:9b",
            fixture=Path("C:/Users/example-user/custom.json"),
            out=Path("C:/Users/example-user/out.json"),
            base_manifest=Path("C:/Users/example-user/manifest.json"),
            base_manifest_sha256=None,
            base_url="http://192.0.2.10:11434",
            allow_live=True,
        )
        command = script._command_line(args)
        self.assertNotIn("example-user", command)
        self.assertNotIn("192.0.2.10", command)
        self.assertIn("<fixture>", command)
        self.assertIn("<artifact path>", command)
        self.assertIn("<provider base url>", command)


if __name__ == "__main__":
    unittest.main()
