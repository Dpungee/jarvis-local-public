import copy
import json
import tempfile
import unittest
from pathlib import Path

from jarvis.task_contract_eval import (
    FROZEN_TASK_CONTRACT_FIXTURE_V1_SHA256,
    TASK_CONTRACT_LANES,
    TaskContractFixtureError,
    load_task_contract_fixture,
    score_task_contract_predictions,
    task_contract_fixture_sha256,
)


FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "task_contract_generalization_v1.json"
)
EXPECTED_FIXTURE_SHA256 = (
    "3ec7a566646b0f8fdc587fa49043f87c76acae86623185022427397e353a0925"
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


if __name__ == "__main__":
    unittest.main()
