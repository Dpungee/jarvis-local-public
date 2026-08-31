from __future__ import annotations

import copy
import hashlib
import json
import unittest
from pathlib import Path
from jarvis.strategy_transfer_trial import arm_for_slot, sha256_json
from jarvis.strategy_transfer_trial_eval import (
    StrategyTransferTrialError,
    evaluate_strategy_transfer_trial,
    evaluate_strategy_transfer_trial_artifact,
)

FIXTURE = (
    Path(__file__).parents[1]
    / "jarvis"
    / "evaluation_fixtures"
    / "strategy_transfer_trial_holdout_v1.json"
)
ARTIFACT_SHA256 = "8e53934a9fbae5797a7d6d1483eb6092ef5044a22e9e69a5d108934acad0a6df"
MANIFEST_SHA256 = "1132af2a40a037ea8f8fd6611055728a107db77321398edd0bbe50a4667b9659"
EVALUATOR_SHA256 = "1998d4f177bb2a8f10b0fcdee162aec4a3e681c82d2b97b31a8df55b38c1dc8b"


def _reseal_row(row):
    assignment = row["assignment"]
    assignment["assignment_sha256"] = sha256_json({
        key: value for key, value in assignment.items()
        if key != "assignment_sha256"
    })
    prompt = row["prompt_receipt"]
    prompt["assignment_sha256"] = assignment["assignment_sha256"]
    prompt["prompt_receipt_sha256"] = sha256_json({
        key: value for key, value in prompt.items()
        if key != "prompt_receipt_sha256"
    })
    dispatch = row["provider_dispatch"]
    dispatch["assignment_sha256"] = assignment["assignment_sha256"]
    dispatch["prompt_receipt_sha256"] = prompt["prompt_receipt_sha256"]
    dispatch["provider_dispatch_sha256"] = sha256_json({
        key: value for key, value in dispatch.items()
        if key != "provider_dispatch_sha256"
    })
    outcome = row["outcome"]
    outcome["assignment_sha256"] = assignment["assignment_sha256"]
    outcome["prompt_receipt_sha256"] = prompt["prompt_receipt_sha256"]
    outcome["outcome_sha256"] = sha256_json({
        key: value for key, value in outcome.items()
        if key != "outcome_sha256"
    })


def _reseal_application(application):
    application["application_sha256"] = sha256_json({
        key: value for key, value in application.items()
        if key != "application_sha256"
    })


class StrategyTransferTrialEvalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.artifact = json.loads(FIXTURE.read_text())

    def _mutated(self, artifact):
        return evaluate_strategy_transfer_trial_artifact(
            artifact,
            expected_manifest_sha256=MANIFEST_SHA256,
        )

    def test_v39_fixture_and_pure_export_pass_without_activation(self):
        fixture_bytes = FIXTURE.read_bytes()
        self.assertNotIn(
            b"\r\n",
            fixture_bytes,
            "sealed fixture must retain repository-declared LF line endings",
        )
        self.assertEqual(hashlib.sha256(fixture_bytes).hexdigest(), ARTIFACT_SHA256)
        evaluator = Path(__file__).parents[1] / "jarvis" / "strategy_transfer_trial_eval.py"
        self.assertEqual(hashlib.sha256(evaluator.read_bytes()).hexdigest(), EVALUATOR_SHA256)
        report = evaluate_strategy_transfer_trial(
            FIXTURE,
            expected_artifact_sha256=ARTIFACT_SHA256,
            expected_manifest_sha256=MANIFEST_SHA256,
        )
        pure = self._mutated(copy.deepcopy(self.artifact))
        self.assertEqual(report["outcomes_per_arm"], {"control": 30, "treatment": 30})
        self.assertEqual(report["completed_blocks"], 15)
        self.assertEqual(report["source_target_pairs"], 3)
        self.assertEqual(report["lift_points"], 50.0)
        self.assertGreater(report["difference_ci_95"][0], 0)
        self.assertTrue(report["all_exit_criteria_passed"])
        self.assertTrue(pure["all_exit_criteria_passed"])
        self.assertFalse(report["activation_authorized"])

    def test_assignments_recompute_from_seed_family_local_block_and_slot(self):
        seed = self.artifact["manifest"]["seed"]
        for row in self.artifact["rows"]:
            assignment = row["assignment"]
            expected = arm_for_slot(
                seed=seed,
                target_family=assignment["target_family"],
                block_index=assignment["block_index"],
                block_slot=assignment["block_slot"],
            )
            self.assertEqual(assignment["arm"], expected)
            self.assertEqual(
                row["block_id"],
                f"{assignment['target_family']}:{assignment['block_index']}",
            )

    def test_multiple_source_lessons_may_support_one_declared_strategy(self):
        artifact = copy.deepcopy(self.artifact)
        row = artifact["rows"][0]
        additional = copy.deepcopy(row["applications"][0])
        additional["rank"] = 1
        additional["memory_id"] += 100000
        _reseal_application(additional)
        row["applications"].append(additional)
        report = self._mutated(artifact)
        self.assertTrue(report["all_exit_criteria_passed"])

    def test_post_hoc_replay_incomplete_imbalance_and_duplicate_slot_fail(self):
        post_hoc = copy.deepcopy(self.artifact)
        post_hoc["rows"][0]["assignment"]["created_at"] = (
            "2026-08-02T12:00:00.000000+00:00"
        )
        _reseal_row(post_hoc["rows"][0])
        with self.assertRaisesRegex(StrategyTransferTrialError, "pre-outcome"):
            self._mutated(post_hoc)
        replay = copy.deepcopy(self.artifact)
        replay["rows"][1] = copy.deepcopy(replay["rows"][0])
        with self.assertRaisesRegex(StrategyTransferTrialError, "replayed|imbalanced"):
            self._mutated(replay)
        incomplete = copy.deepcopy(self.artifact)
        incomplete["rows"].pop()
        with self.assertRaisesRegex(StrategyTransferTrialError, "imbalanced"):
            self._mutated(incomplete)
        wrong_arm = copy.deepcopy(self.artifact)
        wrong_arm["rows"][0]["assignment"]["arm"] = (
            "control" if wrong_arm["rows"][0]["assignment"]["arm"] == "treatment"
            else "treatment"
        )
        _reseal_row(wrong_arm["rows"][0])
        with self.assertRaisesRegex(StrategyTransferTrialError, "randomization"):
            self._mutated(wrong_arm)
        duplicate = copy.deepcopy(self.artifact)
        duplicate["rows"][1]["assignment"]["block_slot"] = 0
        _reseal_row(duplicate["rows"][1])
        with self.assertRaisesRegex(StrategyTransferTrialError, "coordinate|imbalanced"):
            self._mutated(duplicate)

    def test_tamper_unknown_time_and_arbitrary_pair_fail(self):
        tampered = copy.deepcopy(self.artifact)
        tampered["rows"][0]["assignment"]["project_id"] = 2
        with self.assertRaisesRegex(StrategyTransferTrialError, "scope|digest"):
            self._mutated(tampered)
        unknown = copy.deepcopy(self.artifact)
        unknown["rows"][0]["assignment"]["private_note"] = "forbidden"
        with self.assertRaisesRegex(StrategyTransferTrialError, "fields"):
            self._mutated(unknown)
        malformed = copy.deepcopy(self.artifact)
        malformed["rows"][0]["assignment"]["created_at"] = "not-a-time"
        _reseal_row(malformed["rows"][0])
        with self.assertRaisesRegex(StrategyTransferTrialError, "canonical UTC"):
            self._mutated(malformed)
        same_family = copy.deepcopy(self.artifact)
        application = same_family["rows"][0]["applications"][0]
        application["source_family"] = application["target_family"]
        _reseal_application(application)
        with self.assertRaisesRegex(StrategyTransferTrialError, "application contract"):
            self._mutated(same_family)
        mismatched_strategy = copy.deepcopy(self.artifact)
        row = mismatched_strategy["rows"][0]
        row["applications"][0]["strategy"] = "checkpoint_and_resume"
        _reseal_application(row["applications"][0])
        with self.assertRaisesRegex(StrategyTransferTrialError, "application contract"):
            self._mutated(mismatched_strategy)

    def test_contamination_and_family_negative_effect_fail_closed(self):
        contaminated = copy.deepcopy(self.artifact)
        contaminated["rows"][0]["outcome"]["status"] = "contaminated"
        contaminated["rows"][0]["outcome"]["status_reason"] = "runtime_drift"
        _reseal_row(contaminated["rows"][0])
        with self.assertRaisesRegex(StrategyTransferTrialError, "contaminated"):
            self._mutated(contaminated)
        negative = copy.deepcopy(self.artifact)
        selected = [
            row for row in negative["rows"]
            if row["assignment"]["arm"] == "treatment"
            and row["assignment"]["target_family"] == "code_fix"
        ]
        for index, row in enumerate(selected):
            success = int(index < 4)
            row["outcome"]["successful"] = success
            row["applications"][0]["successful"] = success
            _reseal_application(row["applications"][0])
            _reseal_row(row)
        report = self._mutated(negative)
        self.assertLess(report["target_family_effects"]["code_fix"], 0)
        self.assertFalse(report["passes"]["no_negative_family_effect"])
        self.assertFalse(report["all_exit_criteria_passed"])


if __name__ == "__main__":
    unittest.main()
