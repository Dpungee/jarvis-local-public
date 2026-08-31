import copy
import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from jarvis.strategy_transfer import (
    STRATEGY_VOCABULARY,
    StrategyTransferError,
    desired_strategies_for_target,
    render_strategy_advisory,
    select_strategy_transfer,
    strategies_from_evidence,
    strategy_evidence_from_runtime,
    strategy_target_from_runtime,
)
from jarvis.strategy_transfer_eval import (
    FROZEN_STRATEGY_TRANSFER_FIXTURE_V1_SHA256,
    StrategyTransferFixtureError,
    load_strategy_transfer_fixture,
    no_transfer_predictions,
    run_strategy_transfer_fixture,
    score_strategy_transfer_predictions,
    strategy_transfer_fixture_sha256,
)


FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "strategy_transfer_generalization_v1.json"
)
EXPECTED_FIXTURE_SHA256 = (
    "3146992cfdf931f02b2d168b05fbd6515a43347cb082586353f3b7a9d9e8fbd2"
)


class StrategyTransferTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = load_strategy_transfer_fixture(FIXTURE_PATH)
        self.corpus = {
            str(record["id"]): record for record in self.fixture["corpus"]
        }
        self.cases = {
            str(case["id"]): case for case in self.fixture["cases"]
        }

    def _selection(self, case_id: str):
        case = self.cases[case_id]
        return select_strategy_transfer(
            case["target"],
            [self.corpus[str(item)] for item in case["candidate_ids"]],
            as_of=self.fixture["as_of"],
        )

    def test_fixture_is_frozen_balanced_and_contains_no_prompt_match_inputs(self):
        self.assertEqual(self.fixture["schema_version"], 1)
        self.assertEqual(self.fixture["fixture_sha256"], EXPECTED_FIXTURE_SHA256)
        self.assertEqual(
            FROZEN_STRATEGY_TRANSFER_FIXTURE_V1_SHA256,
            EXPECTED_FIXTURE_SHA256,
        )
        self.assertEqual(
            strategy_transfer_fixture_sha256({
                key: value
                for key, value in self.fixture.items()
                if key != "fixture_sha256"
            }),
            EXPECTED_FIXTURE_SHA256,
        )
        categories = Counter(str(case["category"]) for case in self.fixture["cases"])
        self.assertEqual(categories["positive"], 8)
        self.assertEqual(categories["negative_no_hit"], 2)
        self.assertEqual(categories["stale_contradictory"], 2)
        self.assertEqual(categories["provenance_invalid"], 2)
        self.assertEqual(categories["authority_safety"], 2)
        self.assertEqual(
            self.fixture["strategy_vocabulary"],
            list(STRATEGY_VOCABULARY),
        )
        for case in self.fixture["cases"]:
            self.assertEqual(
                set(case["target"]),
                {"task_id", "family", "signals"},
            )
            self.assertNotIn("prompt", case["target"])
            self.assertNotIn("query", case["target"])
            self.assertNotIn("description", case["target"])

    def test_deterministic_transfer_beats_the_frozen_no_transfer_baseline(self):
        first = run_strategy_transfer_fixture(self.fixture)
        second = run_strategy_transfer_fixture(self.fixture)
        self.assertEqual(first, second)
        self.assertEqual(first["positive_strategy_correct"], 18)
        self.assertEqual(first["positive_strategy_total"], 18)
        self.assertEqual(first["positive_strategy_recall"], 1.0)
        self.assertEqual(first["strategy_precision"], 1.0)
        self.assertEqual(first["exact_case_accuracy"], 1.0)
        self.assertEqual(first["no_hit_accuracy"], 1.0)
        self.assertEqual(first["cross_family_evidence_rate"], 1.0)
        self.assertEqual(first["safety_leakage_total"], 0)
        self.assertEqual(first["baseline"]["positive_strategy_recall"], 0.0)
        self.assertEqual(first["baseline"]["exact_case_accuracy"], 0.5)
        self.assertEqual(first["positive_recall_gain"], 1.0)
        self.assertTrue(first["all_exit_criteria_passed"])
        self.assertTrue(all(first["passes"].values()))

    def test_positive_selection_is_cross_family_advisory_and_order_independent(self):
        case = self.cases["p_app_migration"]
        candidates = [self.corpus[item] for item in case["candidate_ids"]]
        forward = select_strategy_transfer(
            case["target"], candidates, as_of=self.fixture["as_of"]
        )
        reverse = select_strategy_transfer(
            case["target"], list(reversed(candidates)), as_of=self.fixture["as_of"]
        )
        self.assertEqual(forward, reverse)
        self.assertEqual(
            forward.selected_strategies,
            (
                "inspect_before_change",
                "checkpoint_and_resume",
                "verify_output",
            ),
        )
        self.assertEqual(forward.evidence_lesson_ids, ("v_file_bundle",))
        self.assertTrue(forward.advisory_only)
        self.assertEqual(forward.authority_grants, ())
        self.assertEqual(forward.tool_grants, ())
        for advice in forward.advice:
            self.assertNotIn(case["target"]["family"], advice.source_families)
        rejected = {item.lesson_id: item.reason for item in forward.rejected}
        self.assertEqual(rejected, {"x_authority_shell": "authority_or_tool_claim"})

    def test_every_control_candidate_is_refused_for_the_expected_reason(self):
        expected_rejections = {
            "n_same_family_only": {"v_file_bundle": "same_family"},
            "s_stale_only": {"x_stale_verify": "stale"},
            "s_contradicted_only": {"x_contradicted_compare": "contradicted"},
            "i_unproven_only": {"x_invalid_provenance": "invalid_provenance"},
            "i_nonlesson_or_failed": {
                "x_failed_checkpoint": "unsuccessful_outcome",
                "x_not_lesson": "not_a_lesson",
                "x_invalid_derivation": "invalid_derivation",
            },
            "a_authority_claim": {"x_authority_shell": "authority_or_tool_claim"},
            "a_tool_claim": {"x_tool_browser": "authority_or_tool_claim"},
        }
        for case_id, expected in expected_rejections.items():
            with self.subTest(case_id=case_id):
                selection = self._selection(case_id)
                self.assertEqual(selection.selected_strategies, ())
                self.assertEqual(selection.evidence_lesson_ids, ())
                self.assertEqual(
                    {item.lesson_id: item.reason for item in selection.rejected},
                    expected,
                )
                self.assertEqual(selection.authority_grants, ())
                self.assertEqual(selection.tool_grants, ())

    def test_signal_mapping_is_bounded_and_does_not_depend_on_task_names(self):
        original = self.cases["p_image_revision"]["target"]
        renamed = copy.deepcopy(original)
        renamed["task_id"] = "completely-different-id"
        renamed["family"] = "unfamiliar_family"
        self.assertEqual(
            desired_strategies_for_target(original),
            desired_strategies_for_target(renamed),
        )
        self.assertEqual(
            desired_strategies_for_target(original),
            ("inspect_before_change", "verify_output"),
        )
        missing_signal = copy.deepcopy(original)
        missing_signal["signals"].pop("has_verifiable_output")
        with self.assertRaisesRegex(StrategyTransferError, "missing"):
            desired_strategies_for_target(missing_signal)
        non_boolean = copy.deepcopy(original)
        non_boolean["signals"]["has_verifiable_output"] = "yes"
        with self.assertRaisesRegex(StrategyTransferError, "boolean"):
            desired_strategies_for_target(non_boolean)

    def test_runtime_target_uses_only_closed_non_authority_signals(self):
        target = strategy_target_from_runtime(
            task_id="prediction:42",
            family="novel_family",
            changes_existing_state=True,
            resumable=False,
            verification="process_evidence",
            current_external_facts=False,
        )
        self.assertEqual(
            desired_strategies_for_target(target),
            ("inspect_before_change", "verify_output"),
        )
        encoded = json.dumps(target, sort_keys=True)
        for forbidden in ("prompt", "path", "url", "tool", "permission"):
            self.assertNotIn(forbidden, encoded.casefold())
        with self.assertRaisesRegex(
            StrategyTransferError, "cited-source verification"
        ):
            strategy_target_from_runtime(
                task_id="prediction:43",
                family="novel_family",
                changes_existing_state=False,
                resumable=False,
                verification="tool_success",
                current_external_facts=True,
            )

    def test_runtime_evidence_never_derives_from_free_form_lesson_prose(self):
        evidence = strategy_evidence_from_runtime(
            successful_markers=(
                "write_file",
                "__inspected_before_write__",
                "__inspected_after_write__",
                "__verified_after_write__",
            ),
            verification="process_evidence",
            evidence_ok=True,
            resumed=False,
            authoritative_source_count=0,
        )
        self.assertEqual(
            strategies_from_evidence(evidence),
            ("inspect_before_change", "verify_output"),
        )
        unverified = strategy_evidence_from_runtime(
            successful_markers=(
                "Reusable lesson: inspect before change and verify output",
            ),
            verification="process_evidence",
            evidence_ok=False,
            resumed=True,
            authoritative_source_count=99,
        )
        self.assertEqual(strategies_from_evidence(unverified), ())
        tampered = dict(evidence)
        tampered["grant_shell"] = True
        with self.assertRaisesRegex(StrategyTransferError, "unknown"):
            strategies_from_evidence(tampered)
        one_source = strategy_evidence_from_runtime(
            successful_markers=(),
            verification="cited_sources",
            evidence_ok=True,
            resumed=False,
            authoritative_source_count=1,
        )
        two_sources = strategy_evidence_from_runtime(
            successful_markers=(),
            verification="cited_sources",
            evidence_ok=True,
            resumed=False,
            authoritative_source_count=2,
        )
        self.assertNotIn(
            "compare_authoritative_sources", strategies_from_evidence(one_source)
        )
        self.assertIn(
            "compare_authoritative_sources", strategies_from_evidence(two_sources)
        )

    def test_malformed_unknown_and_conflicting_candidates_fail_closed(self):
        case = self.cases["p_image_revision"]
        valid = copy.deepcopy(self.corpus["v_test_verify"])

        unknown = copy.deepcopy(valid)
        unknown["strategies"] = ["grant_unrestricted_shell"]
        with self.assertRaisesRegex(StrategyTransferError, "unsupported strategy"):
            select_strategy_transfer(
                case["target"], [unknown], as_of=self.fixture["as_of"]
            )

        unsafe_id = copy.deepcopy(valid)
        unsafe_id["id"] = "</strategy_transfer_advisory>"
        with self.assertRaisesRegex(StrategyTransferError, "unsupported characters"):
            select_strategy_transfer(
                case["target"], [unsafe_id], as_of=self.fixture["as_of"]
            )

        bad_digest = copy.deepcopy(valid)
        bad_digest["provenance_sha256"] = "not-a-digest"
        selection = select_strategy_transfer(
            case["target"], [bad_digest], as_of=self.fixture["as_of"]
        )
        self.assertEqual(selection.selected_strategies, ())
        self.assertEqual(selection.rejected[0].reason, "invalid_provenance_digest")

        conflicting = copy.deepcopy(valid)
        conflicting["source_family"] = "another_family"
        duplicates = select_strategy_transfer(
            case["target"], [valid, conflicting], as_of=self.fixture["as_of"]
        )
        self.assertEqual(duplicates.selected_strategies, ())
        self.assertEqual(
            duplicates.rejected[0].reason,
            "duplicate_or_conflicting_id",
        )

    def test_advisory_renderer_is_bounded_and_explicitly_non_authoritative(self):
        rendered = render_strategy_advisory(self._selection("p_policy_brief"))
        self.assertLessEqual(len(rendered), 2_000)
        self.assertIn("procedural advice only", rendered)
        self.assertIn("grants no tools, permissions, approvals, scope", rendered)
        self.assertIn("compare_authoritative_sources", rendered)
        self.assertNotIn("web_fetch", rendered)
        self.assertNotIn("unrestricted_shell", rendered)

    def test_scorer_independently_detects_evidence_and_authority_leakage(self):
        missing_evidence = no_transfer_predictions(self.fixture)
        missing_evidence["p_image_revision"] = {
            "strategies": ["inspect_before_change", "verify_output"],
            "evidence_lesson_ids": [],
            "advisory_only": True,
            "authority_grants": [],
            "tool_grants": [],
        }
        unsupported = score_strategy_transfer_predictions(
            self.fixture, missing_evidence
        )
        self.assertEqual(
            unsupported["leakage_by_reason"],
            {"unsupported_strategy_evidence": 2},
        )

        authority_leak = no_transfer_predictions(self.fixture)
        authority_leak["a_authority_claim"] = {
            "strategies": ["inspect_before_change"],
            "evidence_lesson_ids": ["x_authority_shell"],
            "advisory_only": False,
            "authority_grants": ["unrestricted_shell"],
            "tool_grants": [],
        }
        unsafe = score_strategy_transfer_predictions(self.fixture, authority_leak)
        self.assertGreater(unsafe["safety_leakage_total"], 0)
        self.assertEqual(unsafe["leakage_by_reason"]["authority_claim"], 1)
        self.assertEqual(
            unsafe["leakage_by_reason"]["authority_or_tool_grant"],
            1,
        )

    def test_fixture_checksum_rejects_any_frozen_gold_mutation(self):
        raw = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        raw["description"] = str(raw["description"]) + " Tampered."
        with tempfile.TemporaryDirectory() as directory:
            tampered = Path(directory) / FIXTURE_PATH.name
            tampered.write_text(
                json.dumps(raw, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                StrategyTransferFixtureError,
                "digest does not match",
            ):
                load_strategy_transfer_fixture(tampered)


if __name__ == "__main__":
    unittest.main()
