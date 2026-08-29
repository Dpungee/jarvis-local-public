import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from jarvis.task_contract_eval import (
    FROZEN_TASK_CONTRACT_HOLDOUT_V2_SHA256,
    FROZEN_TASK_CONTRACT_FIXTURE_V1_SHA256,
    TASK_CONTRACT_HOLDOUT_LANES,
    TASK_CONTRACT_LANES,
    TaskContractFixtureError,
    _effect_constraint_digest,
    load_task_contract_holdout,
    load_task_contract_fixture,
    score_task_contract_holdout,
    score_task_contract_holdout_contracts,
    score_task_contract_predictions,
    task_contract_fixture_sha256,
)


FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "task_contract_generalization_v1.json"
)
EXPECTED_FIXTURE_SHA256 = (
    "3ec7a566646b0f8fdc587fa49043f87c76acae86623185022427397e353a0925"
)
HOLDOUT_PATH = Path(__file__).parent / "fixtures" / "task_contract_holdout_v2.json"
EXPECTED_HOLDOUT_SHA256 = (
    "b81229d42790ec0be8a8f87d22cf6447feb21351859db6732a8e9d11395d9a8e"
)


class TaskContractEvaluationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = load_task_contract_fixture(FIXTURE_PATH)

    def _perfect_predictions(self):
        routes = [
            {"id": item["id"], "lane": item["expected_lane"]}
            for item in self.fixture["route_cases"]
        ]
        clarifications = [
            {
                "id": item["id"],
                "clarify": item["expected_clarification"],
            }
            for item in self.fixture["clarification_cases"]
        ]
        return routes, clarifications

    def _worst_predictions(self):
        ordered_lanes = sorted(TASK_CONTRACT_LANES)
        routes = []
        for item in self.fixture["route_cases"]:
            expected = item["expected_lane"]
            wrong = next(lane for lane in ordered_lanes if lane != expected)
            routes.append({"id": item["id"], "lane": wrong})
        clarifications = [
            {
                "id": item["id"],
                "clarify": not item["expected_clarification"],
            }
            for item in self.fixture["clarification_cases"]
        ]
        return routes, clarifications

    @staticmethod
    def _write_candidate(directory: Path, payload: dict, name: str = "candidate.json") -> Path:
        path = directory / name
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path

    def test_fixture_is_exact_balanced_and_independently_pinned(self):
        self.assertEqual(self.fixture["schema_version"], 1)
        self.assertEqual(len(self.fixture["route_cases"]), 30)
        self.assertEqual(len(self.fixture["clarification_cases"]), 20)
        self.assertEqual(
            {
                lane: sum(
                    item["expected_lane"] == lane
                    for item in self.fixture["route_cases"]
                )
                for lane in TASK_CONTRACT_LANES
            },
            {lane: 6 for lane in TASK_CONTRACT_LANES},
        )
        self.assertEqual(
            sum(
                item["expected_clarification"] is True
                for item in self.fixture["clarification_cases"]
            ),
            10,
        )
        self.assertEqual(
            sum(
                item["expected_clarification"] is False
                for item in self.fixture["clarification_cases"]
            ),
            10,
        )
        self.assertEqual(self.fixture["fixture_sha256"], EXPECTED_FIXTURE_SHA256)
        self.assertEqual(
            FROZEN_TASK_CONTRACT_FIXTURE_V1_SHA256,
            EXPECTED_FIXTURE_SHA256,
        )
        self.assertEqual(
            task_contract_fixture_sha256({
                key: value
                for key, value in self.fixture.items()
                if key != "fixture_sha256"
            }),
            EXPECTED_FIXTURE_SHA256,
        )

    def test_perfect_and_worst_predictions_have_exact_scores(self):
        perfect_routes, perfect_clarifications = self._perfect_predictions()
        perfect = score_task_contract_predictions(
            self.fixture,
            perfect_routes,
            perfect_clarifications,
        )
        self.assertEqual(perfect["route_correct"], 30)
        self.assertEqual(perfect["route_accuracy"], 1.0)
        self.assertEqual(perfect["ambiguity_true_positives"], 10)
        self.assertEqual(perfect["ambiguity_recall"], 1.0)
        self.assertEqual(perfect["specified_false_positives"], 0)
        self.assertEqual(perfect["specified_false_positive_rate"], 0.0)
        self.assertEqual(perfect["clarification_correct"], 20)
        self.assertTrue(perfect["all_exit_criteria_passed"])

        worst_routes, worst_clarifications = self._worst_predictions()
        worst = score_task_contract_predictions(
            self.fixture,
            worst_routes,
            worst_clarifications,
        )
        self.assertEqual(worst["route_correct"], 0)
        self.assertEqual(worst["route_accuracy"], 0.0)
        self.assertEqual(worst["ambiguity_true_positives"], 0)
        self.assertEqual(worst["ambiguity_recall"], 0.0)
        self.assertEqual(worst["specified_false_positives"], 10)
        self.assertEqual(worst["specified_false_positive_rate"], 1.0)
        self.assertEqual(worst["clarification_correct"], 0)
        self.assertFalse(worst["all_exit_criteria_passed"])

    def test_raw_legacy_predictions_preserve_the_recovered_counts(self):
        baseline = self.fixture["raw_legacy_baseline"]
        metrics = score_task_contract_predictions(
            self.fixture,
            baseline["route_predictions"],
            baseline["clarification_predictions"],
        )
        self.assertEqual(metrics["route_correct"], 14)
        self.assertEqual(metrics["route_total"], 30)
        self.assertEqual(metrics["route_accuracy"], 0.466667)
        self.assertEqual(metrics["ambiguity_true_positives"], 3)
        self.assertEqual(metrics["ambiguity_total"], 10)
        self.assertEqual(metrics["ambiguity_recall"], 0.3)
        self.assertEqual(metrics["specified_false_positives"], 0)
        self.assertEqual(metrics["specified_total"], 10)
        self.assertEqual(metrics["specified_false_positive_rate"], 0.0)
        self.assertEqual(metrics["clarification_correct"], 13)
        self.assertFalse(metrics["all_exit_criteria_passed"])
        self.assertEqual(
            {
                item["id"]
                for item in metrics["route_cases"]
                if item["correct"]
            },
            {
                "route_01", "route_02", "route_03", "route_04", "route_05",
                "route_06", "route_08", "route_11", "route_17", "route_22",
                "route_23", "route_24", "route_29", "route_30",
            },
        )

    def test_prediction_sets_reject_missing_unknown_duplicate_and_invalid_values(self):
        routes, clarifications = self._perfect_predictions()
        cases = (
            (routes[:-1], clarifications, "missing route_30"),
            (
                [*routes, {"id": "route_unknown", "lane": "dialogue"}],
                clarifications,
                "unknown route_unknown",
            ),
            ([*routes, dict(routes[0])], clarifications, "Duplicate route"),
            (
                [{**routes[0], "lane": "unsupported"}, *routes[1:]],
                clarifications,
                "unsupported lane",
            ),
            (routes, clarifications[:-1], "missing clarify_20"),
            (
                routes,
                [*clarifications, {"id": "clarify_unknown", "clarify": False}],
                "unknown clarify_unknown",
            ),
            (routes, [*clarifications, dict(clarifications[0])], "Duplicate clarification"),
            (
                routes,
                [{**clarifications[0], "clarify": 1}, *clarifications[1:]],
                "must be a boolean",
            ),
        )
        for route_values, clarification_values, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(TaskContractFixtureError, message):
                    score_task_contract_predictions(
                        self.fixture,
                        route_values,
                        clarification_values,
                    )

    def test_fixture_rejects_missing_unknown_duplicate_and_digest_drift(self):
        original = {
            key: value
            for key, value in self.fixture.items()
            if key != "fixture_sha256"
        }
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)

            missing = copy.deepcopy(original)
            missing.pop("description")
            with self.assertRaisesRegex(TaskContractFixtureError, "missing description"):
                load_task_contract_fixture(self._write_candidate(directory, missing))

            unknown = copy.deepcopy(original)
            unknown["unexpected"] = True
            with self.assertRaisesRegex(TaskContractFixtureError, "unknown unexpected"):
                load_task_contract_fixture(self._write_candidate(directory, unknown))

            duplicate_id = copy.deepcopy(original)
            duplicate_id["route_cases"][1]["id"] = duplicate_id["route_cases"][0]["id"]
            with self.assertRaisesRegex(TaskContractFixtureError, "unique"):
                load_task_contract_fixture(
                    self._write_candidate(directory, duplicate_id)
                )

            duplicate_json = directory / "duplicate.json"
            duplicate_json.write_text(
                '{"schema_version":1,"schema_version":1}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(TaskContractFixtureError, "Duplicate JSON field"):
                load_task_contract_fixture(duplicate_json)

            drifted = copy.deepcopy(original)
            drifted["description"] += " changed"
            with self.assertRaisesRegex(TaskContractFixtureError, "code-pinned"):
                load_task_contract_fixture(
                    self._write_candidate(
                        directory,
                        drifted,
                        "task_contract_generalization_v1.json",
                    )
                )


class TaskContractHoldoutV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = load_task_contract_holdout(HOLDOUT_PATH)

    @staticmethod
    def _write_candidate(directory: Path, payload: dict, name: str) -> Path:
        path = directory / name
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path

    def _perfect_contract_predictions(self) -> list[dict]:
        return [
            {
                "id": case["id"],
                "lane": case["expected"]["lane"],
                "clarification": case["expected"]["clarification"],
                "relation": case["expected"]["relation"],
                "constraint_quotes": list(case["expected"]["retained_constraints"]),
                "requested_effect": case["expected"]["requested_effect"],
                "evidence_source": case["expected"]["evidence_source"],
                "acceptance": list(case["expected"]["acceptance_contains"]),
            }
            for case in self.fixture["cases"]
        ]

    def _perfect_full_predictions(self) -> list[dict]:
        result = []
        for case, contract in zip(
            self.fixture["cases"],
            self._perfect_contract_predictions(),
            strict=True,
        ):
            expected = case["expected"]
            is_future = expected["action_timing"] == "future"
            is_immediate = expected["action_timing"] == "immediate"
            is_restart = expected["restart_sequence_outcome"] == "preserved"
            result.append({
                **contract,
                "final_status": "complete",
                "final_text": (
                    "I'll report back after it runs. Scheduled task #73 is active."
                    if is_future
                    else "The requested action completed in this run."
                    if is_immediate
                    else "Here is the requested answer."
                ),
                "offered_tools": (
                    ["schedule_create"] if is_future else
                    ["benchmark_effect"] if is_immediate else []
                ),
                "tool_events": [
                    {
                        "name": "schedule_create" if is_future else "benchmark_effect",
                        "status": "complete",
                        "effect": "queue" if is_future else expected["requested_effect"],
                        "handler_dispatched": True,
                        "target_sha256": hashlib.sha256(
                            str(case["id"]).encode("utf-8")
                        ).hexdigest(),
                        "receipt_id": "73" if is_future else None,
                        "matched_constraint_sha256": [
                            _effect_constraint_digest(value)
                            for value in expected["retained_constraints"]
                        ],
                    }
                ] if is_immediate or is_future else [],
                "durable_queue_records": [{
                    "kind": "schedule",
                    "id": "73",
                    "state": "scheduled",
                    "purpose": case["operator_prompt"],
                }] if is_future else [],
                "restart_observation": {
                    "performed": is_restart,
                    "database_reopened": is_restart,
                    "pending_goal_reloaded": is_restart,
                    "constraints_preserved": is_restart,
                },
            })
        return result

    def test_holdout_is_new_pinned_training_excluded_and_broad(self):
        self.assertEqual(self.fixture["schema_version"], 2)
        self.assertEqual(len(self.fixture["cases"]), 66)
        self.assertEqual(self.fixture["fixture_sha256"], EXPECTED_HOLDOUT_SHA256)
        self.assertEqual(
            FROZEN_TASK_CONTRACT_HOLDOUT_V2_SHA256,
            EXPECTED_HOLDOUT_SHA256,
        )
        self.assertEqual(
            self.fixture["exclusion_policy"],
            {
                "training": False,
                "memory": False,
                "lesson_distillation": False,
                "prompt_receipts": False,
            },
        )
        lanes = {case["expected"]["lane"] for case in self.fixture["cases"]}
        self.assertEqual(lanes, TASK_CONTRACT_HOLDOUT_LANES)
        tags = {tag for case in self.fixture["cases"] for tag in case["tags"]}
        self.assertTrue({
            "ambiguous",
            "current_information",
            "future_queue",
            "misspelling",
            "pending_contract",
            "restart",
            "short_followup",
            "slang",
        }.issubset(tags))
        legacy = load_task_contract_fixture(FIXTURE_PATH)
        legacy_prompts = {
            item["prompt"].casefold()
            for key in ("route_cases", "clarification_cases")
            for item in legacy[key]
        }
        self.assertFalse(
            legacy_prompts.intersection(
                case["operator_prompt"].casefold()
                for case in self.fixture["cases"]
            )
        )

    def test_contract_and_outcome_scoring_cover_every_phase_two_gate(self):
        contract = score_task_contract_holdout_contracts(
            self.fixture,
            self._perfect_contract_predictions(),
        )
        self.assertEqual(contract["route_accuracy"], 1.0)
        self.assertEqual(contract["ambiguity_recall"], 1.0)
        self.assertEqual(contract["specified_false_positive_rate"], 0.0)
        self.assertEqual(contract["continuation_recall"], 1.0)
        self.assertEqual(contract["constraint_retention"], 1.0)
        self.assertEqual(contract["constraint_precision"], 1.0)
        self.assertEqual(contract["ungrounded_constraints"], 0)
        self.assertEqual(contract["effect_accuracy"], 1.0)
        self.assertEqual(contract["evidence_accuracy"], 1.0)
        self.assertEqual(contract["acceptance_retention"], 1.0)
        self.assertTrue(contract["all_contract_exit_criteria_passed"])

        full = score_task_contract_holdout(
            self.fixture,
            self._perfect_full_predictions(),
        )
        self.assertEqual(full["false_unavailable"], 0)
        self.assertEqual(full["immediate_evidence_rate"], 1.0)
        self.assertEqual(full["promise_only_immediate"], 0)
        self.assertEqual(full["unbacked_future_promises"], 0)
        self.assertEqual(full["future_queue_receipt_rate"], 1.0)
        self.assertEqual(full["restart_preservation_rate"], 1.0)
        self.assertEqual(full["unexpected_effects"], 0)
        self.assertEqual(full["target_binding_failures"], 0)
        self.assertEqual(full["unbound_material_effects"], 0)
        self.assertEqual(full["duplicate_external_effects"], 0)
        self.assertTrue(full["outcome_passes"]["safety_strata"])
        self.assertTrue(full["all_exit_criteria_passed"])

    def test_scoring_detects_false_unavailable_promises_queue_and_restart_loss(self):
        predictions = self._perfect_full_predictions()
        by_id = {item["id"]: item for item in predictions}
        by_id["p2_research_01"]["final_text"] = (
            "I can't access the web because internet tools are unavailable."
        )
        by_id["p2_research_02"]["tool_events"] = []
        by_id["p2_research_02"]["final_text"] = (
            "I'll research it and report back."
        )
        by_id["p2_external_10"]["durable_queue_records"] = []
        by_id["p2_external_11"]["final_text"] = (
            "I'll report back after it runs; scheduled task #999 is active."
        )
        by_id["p2_configuration_03"]["restart_observation"][
            "constraints_preserved"
        ] = False
        metrics = score_task_contract_holdout(self.fixture, predictions)
        self.assertEqual(metrics["false_unavailable"], 1)
        self.assertEqual(metrics["promise_only_immediate"], 1)
        self.assertEqual(metrics["unbacked_future_promises"], 3)
        self.assertLess(metrics["future_queue_receipt_rate"], 1.0)
        self.assertLess(metrics["restart_preservation_rate"], 1.0)
        self.assertFalse(metrics["all_exit_criteria_passed"])

    def test_unrelated_successful_tool_cannot_prove_the_requested_effect(self):
        observations = self._perfect_full_predictions()
        target = next(
            item for item in observations if item["id"] == "p2_creation_01"
        )
        target["offered_tools"].append("recall")
        target["tool_events"] = [{
            "name": "recall",
            "status": "complete",
            "effect": "read",
            "handler_dispatched": True,
            "target_sha256": "0" * 64,
            "receipt_id": None,
            "matched_constraint_sha256": [],
        }]
        metrics = score_task_contract_holdout(self.fixture, observations)
        self.assertLess(metrics["immediate_evidence_rate"], 1.0)
        self.assertFalse(metrics["outcome_passes"]["immediate_evidence_rate"])
        self.assertFalse(metrics["all_exit_criteria_passed"])

    def test_created_but_inactive_queue_row_is_not_a_durable_future_receipt(self):
        observations = self._perfect_full_predictions()
        target = next(
            item for item in observations if item["id"] == "p2_external_10"
        )
        target["durable_queue_records"][0]["state"] = "created"
        metrics = score_task_contract_holdout(self.fixture, observations)
        self.assertLess(metrics["future_queue_receipt_rate"], 1.0)
        self.assertFalse(metrics["outcome_passes"]["future_queue_receipt_rate"])

    def test_queue_receipt_must_be_bound_to_the_exact_schedule_tool_result(self):
        observations = self._perfect_full_predictions()
        target = next(
            item for item in observations if item["id"] == "p2_external_10"
        )
        target["tool_events"][0]["receipt_id"] = "999"

        metrics = score_task_contract_holdout(self.fixture, observations)

        self.assertLess(metrics["future_queue_receipt_rate"], 1.0)
        self.assertFalse(metrics["outcome_passes"]["future_queue_receipt_rate"])

    def test_completed_effect_without_exact_target_receipt_fails_closed(self):
        observations = self._perfect_full_predictions()
        target = next(
            item for item in observations if item["id"] == "p2_creation_01"
        )
        target["tool_events"][0]["target_sha256"] = None

        metrics = score_task_contract_holdout(self.fixture, observations)

        self.assertEqual(metrics["missing_target_receipts"], 1)
        self.assertFalse(metrics["outcome_passes"]["target_receipts"])
        self.assertFalse(metrics["all_exit_criteria_passed"])

    def test_mutation_receipt_must_bind_every_grounded_target_constraint(self):
        observations = self._perfect_full_predictions()
        target = next(
            item for item in observations if item["id"] == "p2_external_01"
        )
        # A syntactically valid call digest says which call ran, not whether it
        # addressed the requested recipient, subject, and exact body.
        target["tool_events"][0]["matched_constraint_sha256"] = [
            _effect_constraint_digest("alex@example.com")
        ]

        metrics = score_task_contract_holdout(self.fixture, observations)

        self.assertEqual(metrics["target_binding_failures"], 1)
        self.assertFalse(metrics["outcome_passes"]["target_binding"])
        self.assertFalse(metrics["all_exit_criteria_passed"])

    def test_duplicate_external_mutation_cannot_pass_same_effect_class(self):
        observations = self._perfect_full_predictions()
        target = next(
            item for item in observations if item["id"] == "p2_external_01"
        )
        uncertain_first_effect = copy.deepcopy(target["tool_events"][0])
        uncertain_first_effect["status"] = "failed"
        target["tool_events"].append(uncertain_first_effect)

        metrics = score_task_contract_holdout(self.fixture, observations)

        self.assertEqual(metrics["duplicate_external_effects"], 1)
        self.assertFalse(metrics["outcome_passes"]["external_cardinality"])
        self.assertFalse(metrics["all_exit_criteria_passed"])

    def test_extra_wrong_target_write_cannot_hide_in_allowed_effect_class(self):
        observations = self._perfect_full_predictions()
        target = next(
            item for item in observations if item["id"] == "p2_creation_01"
        )
        target["tool_events"].append({
            "name": "write_file",
            "status": "complete",
            "effect": "write",
            "handler_dispatched": True,
            "target_sha256": "3" * 64,
            "receipt_id": None,
            "matched_constraint_sha256": [],
        })

        metrics = score_task_contract_holdout(self.fixture, observations)

        self.assertEqual(metrics["unbound_material_effects"], 1)
        self.assertFalse(
            metrics["outcome_passes"]["material_target_binding"]
        )
        self.assertFalse(metrics["all_exit_criteria_passed"])

    def test_future_queue_receipt_binds_schedule_arguments_to_constraints(self):
        observations = self._perfect_full_predictions()
        target = next(
            item for item in observations if item["id"] == "p2_external_10"
        )
        target["tool_events"][0]["matched_constraint_sha256"] = []

        metrics = score_task_contract_holdout(self.fixture, observations)

        self.assertLess(metrics["future_queue_receipt_rate"], 1.0)
        self.assertEqual(metrics["target_binding_failures"], 1)
        self.assertFalse(metrics["outcome_passes"]["target_binding"])

    def test_extra_or_cancelled_effect_cannot_hide_behind_aggregate_success(self):
        observations = self._perfect_full_predictions()
        target = next(
            item for item in observations if item["id"] == "p2_research_01"
        )
        target["offered_tools"].append("publish_external")
        target["tool_events"].append({
            "name": "publish_external",
            "status": "complete",
            "effect": "external",
            "handler_dispatched": True,
            "target_sha256": "1" * 64,
            "receipt_id": None,
            "matched_constraint_sha256": [],
        })
        cancelled = next(
            item for item in observations if item["id"] == "p2_dialogue_05"
        )
        cancelled["offered_tools"] = ["publish_external"]
        cancelled["tool_events"] = [{
            "name": "publish_external",
            "status": "complete",
            "effect": "external",
            "handler_dispatched": True,
            "target_sha256": "2" * 64,
            "receipt_id": None,
            "matched_constraint_sha256": [],
        }]
        metrics = score_task_contract_holdout(self.fixture, observations)
        self.assertEqual(metrics["unexpected_effects"], 2)
        self.assertFalse(metrics["outcome_passes"]["unexpected_effects"])
        self.assertFalse(
            metrics["outcome_safety_strata"]["cancellation"]["passed"]
        )
        self.assertFalse(metrics["all_exit_criteria_passed"])

    def test_self_reported_outcome_booleans_are_not_accepted_as_evidence(self):
        observations = self._perfect_full_predictions()
        observations[0].pop("tool_events")
        observations[0]["immediate_action_evidence"] = True
        with self.assertRaisesRegex(
            TaskContractFixtureError,
            "missing tool_events|unknown immediate_action_evidence",
        ):
            score_task_contract_holdout(self.fixture, observations)

    def test_per_lane_and_exact_safety_strata_block_aggregate_masking(self):
        predictions = self._perfect_contract_predictions()
        for item in predictions:
            if item["id"].startswith("p2_configuration_"):
                item["lane"] = "dialogue"
        metrics = score_task_contract_holdout_contracts(self.fixture, predictions)
        self.assertGreaterEqual(metrics["route_accuracy"], 0.9)
        self.assertFalse(metrics["route_by_lane_passes"]["configuration"])
        self.assertFalse(metrics["safety_strata"]["configuration"]["passed"])
        self.assertFalse(metrics["all_contract_exit_criteria_passed"])

        predictions = self._perfect_contract_predictions()
        cancelled = next(
            item for item in predictions if item["id"] == "p2_dialogue_05"
        )
        cancelled["relation"] = "continue"
        metrics = score_task_contract_holdout_contracts(self.fixture, predictions)
        self.assertGreaterEqual(metrics["relation_accuracy"], 0.9)
        self.assertFalse(metrics["safety_strata"]["cancellation"]["passed"])
        self.assertFalse(metrics["all_contract_exit_criteria_passed"])

    def test_constraint_extras_lose_precision_and_ungrounded_text_fails(self):
        predictions = self._perfect_contract_predictions()
        predictions[0]["constraint_quotes"].append("PUBLISH PRIVATE DATA")
        metrics = score_task_contract_holdout_contracts(self.fixture, predictions)
        self.assertGreater(metrics["unexpected_constraints"], 0)
        self.assertGreater(metrics["ungrounded_constraints"], 0)
        self.assertLess(metrics["constraint_precision"], 1.0)
        self.assertFalse(metrics["passes"]["constraint_precision"])
        self.assertFalse(metrics["passes"]["constraint_grounding"])
        self.assertFalse(metrics["all_contract_exit_criteria_passed"])

    def test_scoring_reauthenticates_loaded_fixture_before_every_score(self):
        mutated = copy.deepcopy(self.fixture)
        mutated["exit_criteria"]["route_accuracy_min"] = 0.0
        with self.assertRaisesRegex(TaskContractFixtureError, "code-pinned"):
            score_task_contract_holdout_contracts(
                mutated, self._perfect_contract_predictions()
            )

        stale = copy.deepcopy(self.fixture)
        stale["fixture_sha256"] = "0" * 64
        with self.assertRaisesRegex(TaskContractFixtureError, "stale|forged"):
            score_task_contract_holdout_contracts(
                stale, self._perfect_contract_predictions()
            )

    def test_holdout_rejects_training_eligibility_underfilled_and_digest_drift(self):
        original = {
            key: copy.deepcopy(value)
            for key, value in self.fixture.items()
            if key != "fixture_sha256"
        }
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            trainable = copy.deepcopy(original)
            trainable["exclusion_policy"]["training"] = True
            with self.assertRaisesRegex(TaskContractFixtureError, "excluded from training"):
                load_task_contract_holdout(
                    self._write_candidate(directory, trainable, "trainable.json")
                )

            underfilled = copy.deepcopy(original)
            underfilled["cases"] = underfilled["cases"][:59]
            with self.assertRaisesRegex(TaskContractFixtureError, "at least 60"):
                load_task_contract_holdout(
                    self._write_candidate(directory, underfilled, "underfilled.json")
                )

            drifted = copy.deepcopy(original)
            drifted["description"] += " drift"
            with self.assertRaisesRegex(TaskContractFixtureError, "code-pinned"):
                load_task_contract_holdout(self._write_candidate(
                    directory, drifted, "task_contract_holdout_v2.json"
                ))
            with self.assertRaisesRegex(TaskContractFixtureError, "canonical filename"):
                load_task_contract_holdout(self._write_candidate(
                    directory, original, "renamed.json"
                ))


if __name__ == "__main__":
    unittest.main()
