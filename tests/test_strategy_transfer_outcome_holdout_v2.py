from __future__ import annotations

import copy
import hashlib
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis.memory import Memory
import jarvis.strategy_transfer_outcome_eval as subject
from jarvis.strategy_transfer_outcome_eval import (
    FROZEN_STRATEGY_TRANSFER_OUTCOME_V2_SHA256,
    STRATEGIES,
    StrategyTransferOutcomeFixtureError,
    evaluate_reordered_fixture_for_test,
    load_strategy_transfer_outcome_fixture,
    run_strategy_transfer_outcome_fixture,
    score_strategy_transfer_outcome_results,
    source_provenance_sha256,
)

FIXTURE_PATH = (
    Path(__file__).parents[1] / "jarvis" / "evaluation_fixtures" /
    "strategy_transfer_outcome_holdout_v2.json"
)
FIXTURE_SHA256 = "68da23c202bfb24ff9f839cd645f33f86de2d6683102a2dc1cf98f100247e569"
EVALUATOR_SHA256 = "6c69b4e3316246e9e51282604ae990d949f869d9752ea3188e5a8e82fe669157"


class StrategyTransferOutcomeHoldoutV2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = load_strategy_transfer_outcome_fixture(FIXTURE_PATH)

    def test_independent_seals_and_immutable_execution(self):
        fixture_digest = hashlib.sha256(FIXTURE_PATH.read_bytes()).hexdigest()
        evaluator_digest = hashlib.sha256(
            Path(subject.__file__).read_bytes()
        ).hexdigest()
        self.assertEqual(fixture_digest, FIXTURE_SHA256)
        self.assertEqual(FROZEN_STRATEGY_TRANSFER_OUTCOME_V2_SHA256, FIXTURE_SHA256)
        self.assertEqual(evaluator_digest, EVALUATOR_SHA256)
        with self.assertRaisesRegex(
            StrategyTransferOutcomeFixtureError, "immutable fixture Path"
        ):
            run_strategy_transfer_outcome_fixture(copy.deepcopy(self.fixture))

    def test_live_families_distinct_sources_and_eligible_receipts(self):
        source_by_id = {item["id"]: item for item in self.fixture["sources"]}
        positives = [
            item for item in self.fixture["cases"]
            if item["category"] == "positive"
        ]
        self.assertEqual(len(positives), 32)
        self.assertEqual(len({item["source_id"] for item in positives}), 32)
        for case in positives:
            source = source_by_id[case["source_id"]]
            self.assertIn(source["source_family"], Memory.PREDICTION_FAMILIES)
            self.assertIn(case["target_family"], Memory.PREDICTION_FAMILIES)
            candidate, receipt = subject._candidate(source)
            self.assertNotEqual(receipt["verification"], "not_applicable")
            self.assertIs(receipt["evidence_ok"], True)
            self.assertTrue(
                subject._source_receipt_eligible(source, candidate, receipt)
            )

    def test_digest_mismatch_and_harmful_negative_controls(self):
        source_by_id = {item["id"]: item for item in self.fixture["sources"]}
        bad = next(
            item for item in self.fixture["sources"]
            if item.get("provenance_sha256")
        )
        candidate, receipt = subject._candidate(bad)
        self.assertNotEqual(
            bad["provenance_sha256"], source_provenance_sha256(bad, receipt)
        )
        self.assertFalse(candidate["provenance_valid"])
        for case in self.fixture["cases"]:
            if case["category"] != "negative_transfer":
                continue
            source = source_by_id[case["source_id"]]
            strategies = subject._candidate(source)[0]["strategies"]
            result = subject._execute(
                case["scenario"], subject._procedure(strategies)
            )
            self.assertFalse(subject._passed(result, case["oracle"]))

    def test_metrics_attestation_and_safety_gate(self):
        report = run_strategy_transfer_outcome_fixture(FIXTURE_PATH)
        self.assertEqual(report["evaluator_sha256"], EVALUATOR_SHA256)
        self.assertEqual(
            (report["baseline_passes"], report["treatment_passes"]),
            (24, 32),
        )
        self.assertEqual(report["completion_lift_points"], 25.0)
        self.assertEqual(report["negative_transfer_rejections"], 12)
        self.assertEqual(
            (report["positive_source_receipts_passed"],
             report["positive_source_receipts_total"]),
            (32, 32),
        )
        self.assertEqual(
            (report["invalid_source_controls_rejected"],
             report["invalid_source_controls_total"]),
            (6, 6),
        )
        self.assertEqual(report["safety_leakage"], 0)
        self.assertTrue(report["passes"]["positive_source_receipts"])
        self.assertTrue(report["passes"]["invalid_source_controls"])
        self.assertTrue(report["all_exit_criteria_passed"])
        rows = copy.deepcopy(report["cases"])
        control = next(
            item for item in rows if item["category"] == "safety_control"
        )
        control.update(advice_count=1, evidence_count=1, safety_leakage=2)
        scored = score_strategy_transfer_outcome_results(self.fixture, rows)
        self.assertFalse(scored["all_exit_criteria_passed"])

    def test_order_oracles_and_mutation_controls(self):
        normal = run_strategy_transfer_outcome_fixture(FIXTURE_PATH)
        reordered = evaluate_reordered_fixture_for_test(self.fixture)
        for key in (
            "pairs",
            "outcomes",
            "lift",
            "negative_rejection",
            "zero_regressions",
            "safety_leakage",
        ):
            self.assertEqual(normal["passes"][key], reordered["passes"][key])
        for case in self.fixture["cases"]:
            oracle = json.dumps(case["oracle"])
            self.assertTrue(all(name not in oracle for name in STRATEGIES))
        raw = FIXTURE_PATH.read_bytes() + b" "
        with patch.object(Path, "read_bytes", return_value=raw):
            with self.assertRaisesRegex(
                StrategyTransferOutcomeFixtureError, "digest does not match"
            ):
                load_strategy_transfer_outcome_fixture(FIXTURE_PATH)


if __name__ == "__main__":
    unittest.main()
