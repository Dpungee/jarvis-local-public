from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from jarvis.long_horizon_eval import (
    CRASH_POINTS,
    EVALUATION_CONFIG,
    LongHorizonEvaluationError,
    _run_worker,
    evaluate_long_horizon_holdout,
    sha256_json,
    validate_long_horizon_fixture_artifact,
)


ROOT = Path(__file__).parents[1]
FIXTURE = (
    ROOT
    / "jarvis"
    / "evaluation_fixtures"
    / "long_horizon_restart_holdout_v1.json"
)
EVALUATOR = ROOT / "jarvis" / "long_horizon_eval.py"
WORKER = ROOT / "jarvis" / "long_horizon_eval_worker.py"
FIXTURE_SHA256 = "d58d8c701b62cd5b1f55f97598d26b47e1a7c5f6be650313e7c8e2b3cd8e5ffb"
EVALUATOR_SHA256 = "cc668abc247c837da8f05e17e991a2c9069d0ca5a63b15ba60fd657118781287"
WORKER_SHA256 = "e81378ed4e27c9ffcdbd25017369376a6d5d67ab488ed341d2677d339f456bef"


def _reseal(artifact: dict) -> dict:
    artifact["fixture_manifest_sha256"] = sha256_json(
        {
            key: value
            for key, value in artifact.items()
            if key != "fixture_manifest_sha256"
        }
    )
    return artifact


class LongHorizonEvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.artifact = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_frozen_fixture_shape_is_valid_but_not_activation_evidence(self) -> None:
        raw = FIXTURE.read_bytes()
        self.assertNotIn(b"\r\n", raw)
        self.assertEqual(hashlib.sha256(raw).hexdigest(), FIXTURE_SHA256)
        self.assertEqual(
            hashlib.sha256(EVALUATOR.read_bytes()).hexdigest(),
            EVALUATOR_SHA256,
        )
        self.assertEqual(hashlib.sha256(WORKER.read_bytes()).hexdigest(), WORKER_SHA256)
        validated = validate_long_horizon_fixture_artifact(self.artifact)
        self.assertEqual(validated["config_sha256"], sha256_json(EVALUATION_CONFIG))
        self.assertEqual(len(validated["workflows"]), 24)
        self.assertGreaterEqual(
            len({item["family"] for item in validated["workflows"]}),
            5,
        )
        self.assertEqual(
            {item["crash_point"] for item in validated["workflows"]},
            CRASH_POINTS,
        )
        self.assertEqual(
            {item["project_id"] for item in validated["workflows"]},
            {1, 2},
        )

    def test_benchmark_executes_real_runtime_without_authorizing_activation(self) -> None:
        report = evaluate_long_horizon_holdout(
            FIXTURE,
            expected_fixture_sha256=FIXTURE_SHA256,
            expected_evaluator_sha256=EVALUATOR_SHA256,
        )
        self.assertEqual(report["workflow_count"], 24)
        self.assertEqual(report["workflows_passed"], 24)
        self.assertEqual(report["negative_controls_rejected"], 10)
        self.assertEqual(report["duplicate_effects"], 0)
        self.assertEqual(report["budget_enforcement_rate"], 1.0)
        self.assertEqual(report["independent_verification_rate"], 1.0)
        self.assertEqual(report["restart_process_rate"], 1.0)
        self.assertEqual(report["executor_authority_secret_leaks"], 0)
        self.assertTrue(report["all_exit_criteria_passed"])
        self.assertFalse(report["activation_authorized"])
        self.assertEqual(
            report["claim_scope"],
            "deterministic_benchmark_only_not_live_activation",
        )

    def test_unknown_fields_and_manifest_tampering_fail_closed(self) -> None:
        unknown = copy.deepcopy(self.artifact)
        unknown["operator_note"] = "not sealed"
        with self.assertRaisesRegex(LongHorizonEvaluationError, "fields are invalid"):
            validate_long_horizon_fixture_artifact(unknown)

        tampered = copy.deepcopy(self.artifact)
        tampered["workflows"][0]["project_id"] += 1
        with self.assertRaisesRegex(LongHorizonEvaluationError, "manifest digest mismatch"):
            validate_long_horizon_fixture_artifact(tampered)

    def test_duplicate_and_cross_project_controls_are_structural(self) -> None:
        duplicate = copy.deepcopy(self.artifact)
        duplicate["workflows"][1]["workflow_id"] = duplicate["workflows"][0][
            "workflow_id"
        ]
        _reseal(duplicate)
        with self.assertRaisesRegex(LongHorizonEvaluationError, "IDs must be unique"):
            validate_long_horizon_fixture_artifact(duplicate)

        missing_control = copy.deepcopy(self.artifact)
        missing_control["negative_controls"] = missing_control["negative_controls"][:-1]
        _reseal(missing_control)
        with self.assertRaisesRegex(
            LongHorizonEvaluationError,
            "all negative control kinds are required",
        ):
            validate_long_horizon_fixture_artifact(missing_control)

    def test_candidate_order_is_metamorphic_but_dependency_order_is_not(self) -> None:
        reordered = copy.deepcopy(self.artifact)
        reordered["workflows"].reverse()
        reordered["negative_controls"].reverse()
        _reseal(reordered)
        validated = validate_long_horizon_fixture_artifact(reordered)
        self.assertEqual(len(validated["workflows"]), 24)

        unsafe = copy.deepcopy(self.artifact)
        unsafe["templates"][0]["stages"][2]["depends_on"] = ["inspect"]
        _reseal(unsafe)
        with self.assertRaisesRegex(
            LongHorizonEvaluationError,
            "dependencies are not canonical",
        ):
            validate_long_horizon_fixture_artifact(unsafe)

    def test_each_workflow_binds_semantics_project_and_split_budgets(self) -> None:
        for workflow in self.artifact["workflows"]:
            with self.subTest(workflow=workflow["workflow_id"]):
                self.assertEqual(len(workflow["contract_sha256"]), 64)
                self.assertGreater(workflow["project_id"], 0)
                self.assertEqual(
                    set(workflow["budgets"]),
                    {
                        "time_ms",
                        "tool_calls",
                        "model_calls",
                        "prompt_tokens",
                        "completion_tokens",
                        "retries",
                    },
                )

    def test_separate_verifier_rejects_missing_and_modified_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = {
                "artifact_path": str(root / "artifact.json"),
                "artifact_sha256": hashlib.sha256(b"expected").hexdigest(),
                "ledger": str(root / "receipts.db"),
            }
            request_path = root / "request.json"
            request_path.write_text(json.dumps(request), encoding="utf-8")
            missing = _run_worker("verify", request_path, secret="00" * 32)
            self.assertNotEqual(missing.returncode, 0)
            self.assertIn("artifact is unavailable", missing.stderr)

            Path(request["artifact_path"]).write_bytes(b"modified")
            modified = _run_worker("verify", request_path, secret="00" * 32)
            self.assertNotEqual(modified.returncode, 0)
            self.assertIn("artifact digest does not match", modified.stderr)


if __name__ == "__main__":
    unittest.main()
