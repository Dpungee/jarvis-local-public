"""Tests for the Phase-2 verified-completion metric and its evidence record.

The load-bearing test here is
``test_recomputed_verdicts_match_the_scorers_own_safety_strata``: the scorer
computes ``outcome_case_passes`` and does not return it, but it *does* return
``outcome_safety_strata``, whose per-tag ``correct`` counts are derived from
exactly those verdicts.  Comparing this module's recomputation against those
counts pins the copy against the original on real scorer output rather than
against itself.
"""

from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

from jarvis import phase2_report
from jarvis.task_contract_benchmark import (
    LiveTaskContractRun,
    run_live_task_contract_benchmark,
)
from jarvis.task_contract_eval import (
    TaskContractFixtureError,
    _effect_constraint_digest,
    load_task_contract_holdout,
    score_task_contract_holdout,
)
from tests.test_task_contract_benchmark import FakeBenchmarkClient

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "task_contract_holdout_v2.json"


def _perfect_contract_predictions(fixture: dict) -> list[dict]:
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
        for case in fixture["cases"]
    ]


def _perfect_observations(fixture: dict) -> list[dict]:
    """An observation set in which every case passes every outcome clause."""
    observations: list[dict] = []
    for case, contract in zip(
        fixture["cases"], _perfect_contract_predictions(fixture), strict=True
    ):
        expected = case["expected"]
        is_future = expected["action_timing"] == "future"
        is_immediate = expected["action_timing"] == "immediate"
        is_restart = expected["restart_sequence_outcome"] == "preserved"
        observations.append({
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
                ["schedule_create"]
                if is_future
                else ["benchmark_effect"]
                if is_immediate
                else []
            ),
            "tool_events": (
                [{
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
                }]
                if is_immediate or is_future
                else []
            ),
            "durable_queue_records": (
                [{
                    "kind": "schedule",
                    "id": "73",
                    "state": "scheduled",
                    "purpose": case["operator_prompt"],
                }]
                if is_future
                else []
            ),
            "restart_observation": {
                "performed": is_restart,
                "database_reopened": is_restart,
                "pending_goal_reloaded": is_restart,
                "constraints_preserved": is_restart,
            },
        })
    return observations


class RecomputedVerdictTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = load_task_contract_holdout(FIXTURE_PATH)

    def _scored(self, observations: list[dict]) -> dict:
        return score_task_contract_holdout(self.fixture, observations)

    def test_recomputed_verdicts_match_the_scorers_own_safety_strata(self) -> None:
        """Pin the copied predicate against the scorer on real scorer output.

        ``outcome_safety_strata[tag]["correct"]`` is a direct sum over the
        scorer's own ``outcome_case_passes``. Reproducing those counts from the
        recomputation, over both a perfect run and a damaged one, is the check
        that catches drift between the two copies of the predicate.
        """
        perfect = _perfect_observations(self.fixture)
        damaged = _perfect_observations(self.fixture)
        by_id = {item["id"]: item for item in damaged}
        by_id["p2_external_10"]["durable_queue_records"] = []
        by_id["p2_configuration_03"]["restart_observation"][
            "constraints_preserved"
        ] = False
        by_id["p2_external_01"]["tool_events"].append({
            "name": "unexpected_write",
            "status": "complete",
            "effect": "write",
            "handler_dispatched": True,
            "target_sha256": "0" * 64,
            "receipt_id": None,
            "matched_constraint_sha256": [],
        })
        by_id["p2_dialogue_05"]["final_text"] = "I'll take care of it later."

        for label, observations in (("perfect", perfect), ("damaged", damaged)):
            with self.subTest(observations=label):
                scored = self._scored(observations)
                passes = phase2_report.recompute_outcome_case_passes(
                    self.fixture, scored["outcome_case_observations"]
                )
                self.assertEqual(len(passes), 66)
                for tag, stratum in scored["outcome_safety_strata"].items():
                    tagged = [
                        str(case["id"])
                        for case in self.fixture["cases"]
                        if tag in set(case["tags"])
                    ]
                    self.assertEqual(
                        sum(1 for case_id in tagged if passes[case_id]),
                        stratum["correct"],
                        f"{label}: recomputed verdicts disagree with the scorer "
                        f"on the {tag} stratum",
                    )
                    self.assertEqual(len(tagged), stratum["total"])

    def test_perfect_run_passes_every_case(self) -> None:
        scored = self._scored(_perfect_observations(self.fixture))
        completion = phase2_report.verified_workflow_completion(
            self.fixture, scored["outcome_case_observations"]
        )
        self.assertEqual(completion["all_cases"], {
            "passes": 66,
            "total": 66,
            "rate": 1.0,
            "passed": True,
            "note": "structural exclusions counted as failures",
        })
        self.assertEqual(completion["scorable_cases"]["total"], 58)
        self.assertEqual(completion["scorable_cases"]["rate"], 1.0)
        self.assertTrue(scored["all_exit_criteria_passed"])

    def test_hand_computed_completion_on_three_named_failures(self) -> None:
        """Exactly the three failure shapes the plan names, hand-counted."""
        observations = _perfect_observations(self.fixture)
        by_id = {item["id"]: item for item in observations}
        # 1. a future_work_promise with no queue receipt
        by_id["p2_external_10"]["durable_queue_records"] = []
        # 2. a restart case that lost its constraints
        by_id["p2_research_04"]["restart_observation"][
            "constraints_preserved"
        ] = False
        # 3. a case with an unexpected effect
        by_id["p2_inspection_01"]["tool_events"].append({
            "name": "unexpected_external",
            "status": "complete",
            "effect": "external",
            "handler_dispatched": True,
            "target_sha256": "1" * 64,
            "receipt_id": None,
            "matched_constraint_sha256": [],
        })
        scored = self._scored(observations)
        observed = scored["outcome_case_observations"]
        completion = phase2_report.verified_workflow_completion(
            self.fixture, observed
        )
        self.assertEqual(
            completion["failed_case_ids"],
            ["p2_external_10", "p2_inspection_01", "p2_research_04"],
        )
        self.assertEqual(completion["all_cases"]["passes"], 63)
        self.assertEqual(completion["all_cases"]["rate"], round(63 / 66, 6))
        # None of the three sits in a structurally excluded case, so removing
        # the eight excluded cases removes eight passes and no failures.
        self.assertEqual(completion["scorable_cases"]["passes"], 55)
        self.assertEqual(completion["scorable_cases"]["total"], 58)
        self.assertEqual(completion["scorable_cases"]["rate"], round(55 / 58, 6))
        self.assertTrue(completion["scorable_cases"]["passed"])

        reasons = phase2_report.outcome_case_failure_reasons(self.fixture, observed)
        self.assertEqual(
            reasons["p2_external_10"],
            ["unbacked_future_promise", "durable_queue_receipt"],
        )
        self.assertEqual(reasons["p2_research_04"], ["restart_sequence_outcome"])
        # The extra event carries a target digest, so only the effect clause
        # fails; a receipt-less event would additionally trip
        # missing_target_receipts.
        self.assertEqual(reasons["p2_inspection_01"], ["unexpected_effects"])

    def test_lane_breakdown_counts_every_case_once(self) -> None:
        scored = self._scored(_perfect_observations(self.fixture))
        completion = phase2_report.verified_workflow_completion(
            self.fixture, scored["outcome_case_observations"]
        )
        by_lane = completion["by_lane"]
        self.assertEqual(sum(row["total"] for row in by_lane.values()), 66)
        self.assertEqual(sum(row["scorable_total"] for row in by_lane.values()), 58)
        self.assertEqual(by_lane["configuration"]["total"], 6)
        self.assertEqual(by_lane["configuration"]["scorable_total"], 1)
        self.assertEqual(by_lane["creation"]["scorable_total"], 9)

    def test_partial_observation_set_is_refused(self) -> None:
        scored = self._scored(_perfect_observations(self.fixture))
        partial = scored["outcome_case_observations"][:2]
        with self.assertRaisesRegex(phase2_report.Phase2ReportError, "missing"):
            phase2_report.recompute_outcome_case_passes(self.fixture, partial)

    def test_unknown_case_id_is_refused(self) -> None:
        scored = self._scored(_perfect_observations(self.fixture))
        observations = [dict(item) for item in scored["outcome_case_observations"]]
        observations[0]["id"] = "p2_not_a_case"
        with self.assertRaisesRegex(phase2_report.Phase2ReportError, "unknown"):
            phase2_report.recompute_outcome_case_passes(self.fixture, observations)

    def test_a_changed_observation_shape_refuses_rather_than_drifts(self) -> None:
        """A scorer field rename must stop the number, not silently move it."""
        scored = self._scored(_perfect_observations(self.fixture))
        observations = [dict(item) for item in scored["outcome_case_observations"]]
        observations[0]["tool_exposure_seen"] = observations[0].pop(
            "tool_exposure_observed"
        )
        with self.assertRaisesRegex(
            phase2_report.Phase2ReportError, "do not match the scorer's record"
        ):
            phase2_report.recompute_outcome_case_passes(self.fixture, observations)

    def test_observation_fields_match_the_scorers_record_exactly(self) -> None:
        scored = self._scored(_perfect_observations(self.fixture))
        for entry in scored["outcome_case_observations"]:
            self.assertEqual(set(entry), set(phase2_report.OBSERVATION_FIELDS))


class PredicateSourceDriftTests(unittest.TestCase):
    """The copied predicate must stay textually identical to the scorer's.

    The behavioural pin is the safety-strata comparison above.  This is the
    cheap structural one a reviewer would otherwise perform by eye: it reads
    both source files and compares the two predicate blocks statement for
    statement, so a scorer edit that changes a clause fails here immediately
    rather than quietly moving the headline number.
    """

    @staticmethod
    def _statements(path: Path, start: str, end: str) -> list[str]:
        text = path.read_bytes().decode("utf-8")
        begin = text.index(start)
        stop = text.index(end, begin)
        statements = []
        for line in text[begin:stop].splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            statements.append(
                stripped.replace("outcome_case_passes[case_id]", "VERDICT").replace(
                    "passes[case_id]", "VERDICT"
                )
            )
        return statements

    def test_the_copy_matches_the_scorer_statement_for_statement(self) -> None:
        package = Path(phase2_report.__file__).parent
        scorer = self._statements(
            package / "task_contract_eval.py",
            "outcome_case_passes[case_id] = bool(",
            "\n    outcome_safety_strata",
        )
        copy = self._statements(
            package / "phase2_report.py",
            "passes[case_id] = bool(",
            "\n        # --- end verbatim copy",
        )
        self.assertTrue(scorer, "the scorer's predicate block was not found")
        self.assertEqual(
            copy,
            scorer,
            "jarvis/phase2_report.py's copy of the outcome predicate has "
            "drifted from jarvis/task_contract_eval.py; re-derive it before "
            "reporting any completion number",
        )


class StructuralExclusionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = load_task_contract_holdout(FIXTURE_PATH)

    def test_exclusions_are_derived_and_match_the_declared_cases(self) -> None:
        rows = phase2_report.structural_exclusions(self.fixture)
        self.assertEqual(
            {row["key"]: row["case_ids"] for row in rows},
            {
                "execution_disabled_by_ruling": [
                    "p2_creation_02",
                    "p2_creation_06",
                    "p2_creation_11",
                ],
                "configuration_write_no_capability_tool": [
                    "p2_configuration_02",
                    "p2_configuration_03",
                    "p2_configuration_06",
                ],
                "configuration_read_no_semantically_matching_tool": [
                    "p2_configuration_01",
                    "p2_configuration_05",
                ],
            },
        )
        self.assertEqual(sum(row["count"] for row in rows), 8)

    def test_a_fixture_that_no_longer_matches_the_rule_is_refused(self) -> None:
        drifted = json.loads(json.dumps(self.fixture))
        for case in drifted["cases"]:
            if case["id"] == "p2_creation_02":
                case["expected"]["requested_effect"] = "write"
        with self.assertRaisesRegex(phase2_report.Phase2ReportError, "diverged"):
            phase2_report.structural_exclusions(drifted)


class ToolExposureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = load_task_contract_holdout(FIXTURE_PATH)

    def test_exposure_denominator_excludes_cases_that_require_none(self) -> None:
        observations = _perfect_observations(self.fixture)
        scored = score_task_contract_holdout(self.fixture, observations)
        rows = phase2_report.tool_exposure_by_lane(
            self.fixture, scored["outcome_case_observations"]
        )
        self.assertNotIn("dialogue", rows)
        self.assertEqual(sum(row["required"] for row in rows.values()), 45)
        self.assertTrue(all(row["rate"] == 1.0 for row in rows.values()))

    def test_missing_exposure_is_named_per_lane(self) -> None:
        observations = _perfect_observations(self.fixture)
        by_id = {item["id"]: item for item in observations}
        by_id["p2_research_01"]["offered_tools"] = []
        by_id["p2_research_01"]["tool_events"] = []
        scored = score_task_contract_holdout(self.fixture, observations)
        rows = phase2_report.tool_exposure_by_lane(
            self.fixture, scored["outcome_case_observations"]
        )
        self.assertEqual(rows["research"]["missing_case_ids"], ["p2_research_01"])
        self.assertLess(rows["research"]["rate"], 1.0)
        self.assertEqual(rows["inspection"]["rate"], 1.0)


class ReviewFixTests(unittest.TestCase):
    """Cover the reporting corrections raised in review."""

    def setUp(self) -> None:
        self.fixture = load_task_contract_holdout(FIXTURE_PATH)

    def _notes(self) -> list[dict]:
        """Per-case instrumentation shaped like the runner's own."""
        rows = []
        for case in self.fixture["cases"]:
            expected = case["expected"]
            case_id = str(case["id"])
            offered = 0
            calls = 2
            if case_id in {"p2_inspection_01", "p2_inspection_02"}:
                calls = 0  # deterministic pre-loop dispatch
            elif expected["lane"] == "creation":
                offered = 5  # entered the loop and was offered tools
            rows.append({
                "id": case_id,
                "lane": expected["lane"],
                "action_timing": expected["action_timing"],
                "task_contract_status": "resolved",
                "final_status": (
                    "incomplete" if case_id == "p2_research_02" else "complete"
                ),
                "model_calls": calls,
                "offered_tool_count": offered,
                "offered_tools": ["write_file"] * offered,
            })
        return rows

    def test_exposure_mechanisms_split_instrument_gap_from_agent_behaviour(
        self,
    ) -> None:
        """H-1: a zero-call dispatch and an empty schema list are not the same."""
        mechanisms = phase2_report.tool_exposure_mechanisms(
            self.fixture, self._notes()
        )
        self.assertEqual(mechanisms["cases_requiring_exposure"], 45)
        self.assertEqual(
            mechanisms["deterministic_preloop_dispatch"]["case_ids"],
            ["p2_inspection_01", "p2_inspection_02"],
        )
        loop = mechanisms["model_loop_with_empty_schemas"]
        self.assertNotIn("p2_inspection_01", loop["case_ids"])
        self.assertIn("p2_research_02", loop["case_ids"])
        self.assertEqual(loop["incomplete_case_ids"], ["p2_research_02"])
        self.assertEqual(
            mechanisms["cases_without_exposure"],
            len(mechanisms["deterministic_preloop_dispatch"]["case_ids"])
            + loop["count"],
        )
        self.assertIn("agent.py:15270", loop["mechanism"])
        self.assertIn("agent.py:14481-14487", mechanisms[
            "deterministic_preloop_dispatch"
        ]["mechanism"])

    def test_configuration_exclusions_are_split_by_requested_effect(self) -> None:
        """M-3: write and read cases fail for different structural reasons."""
        rows = {
            row["key"]: row
            for row in phase2_report.structural_exclusions(self.fixture)
        }
        self.assertEqual(
            rows["configuration_write_no_capability_tool"]["case_ids"],
            ["p2_configuration_02", "p2_configuration_03", "p2_configuration_06"],
        )
        self.assertEqual(
            rows["configuration_read_no_semantically_matching_tool"]["case_ids"],
            ["p2_configuration_01", "p2_configuration_05"],
        )
        self.assertIn(
            "feature_setup_decide",
            rows["configuration_write_no_capability_tool"]["reason"],
        )
        self.assertIn(
            "semantically unrelated read tool",
            rows["configuration_read_no_semantically_matching_tool"]["reason"],
        )
        self.assertEqual(sum(row["count"] for row in rows.values()), 8)

    def test_unscored_run_still_states_its_exclusions(self) -> None:
        """M-2: an unscored run must still say what it excused itself from."""
        evidence = phase2_report.build_outcome_evidence(
            fixture=self.fixture,
            scored=None,
            resolver_receipts=[],
            smoke=None,
            base={"manifest_sha256": "0" * 64},
            configuration=phase2_report.configuration_class("unit test"),
            provider={"name": "ollama"},
            model={"requested": "ollama:qwen3.5:9b", "served": "qwen3.5:9b"},
            command="python scripts/run_phase2_outcomes.py --allow-live",
            timing={},
            known_limitations=[],
            created_at="2026-09-04T00:00:00+00:00",
            exposure_notes=self._notes(),
        )
        self.assertIsNone(evidence["verified_workflow_completion"])
        self.assertEqual(len(evidence["structural_exclusions"]), 3)
        self.assertEqual(len(evidence["excluded_case_ids"]), 8)
        self.assertEqual(evidence["scorable_case_count"], 58)
        self.assertEqual(evidence["case_total"], 66)
        self.assertIsNotNone(evidence["tool_exposure_mechanisms"])

    def test_safety_strata_gate_row_is_reported(self) -> None:
        """L-2: no aggregate row substitutes for the per-tag stratum gate."""
        rows = {
            row["name"]: row
            for row in phase2_report.outcome_gate_rows(self.fixture, None, None)
        }
        self.assertIn("safety_strata", rows)
        self.assertEqual(rows["safety_strata"]["source"], "sealed_fixture_exit_criteria")
        self.assertFalse(rows["safety_strata"]["passed"])

    def test_a_scored_run_without_verdicts_says_so(self) -> None:
        """L-6: absent verdicts must not read as gates failing on the evidence."""
        rows = phase2_report.outcome_gate_rows(
            self.fixture, {"tool_exposure_rate": 1.0}, None
        )
        note = rows[0]["note"]
        self.assertIn("no outcome verdicts", str(note))
        self.assertFalse(rows[0]["passed"])

    def test_forced_configuration_overrides_the_environment(self) -> None:
        """L-4: the artifact must describe the run, not the host's environment."""
        configuration = phase2_report.configuration_class(
            "unit test",
            forced_switches={"JARVIS_CLOUD_ENABLED": "FALSE"},
            forced_runtime={"config.model": "auto"},
        )
        self.assertEqual(configuration["switches"]["JARVIS_CLOUD_ENABLED"], "false")
        self.assertEqual(
            configuration["switch_sources"]["JARVIS_CLOUD_ENABLED"],
            "forced_by_runner",
        )
        self.assertEqual(configuration["forced_runtime"], {"config.model": "auto"})

    def test_findings_canonicalize_before_matching(self) -> None:
        """L-1: a Unicode look-alike must not slip a home path past the check."""
        backslash = chr(92)
        plain = "C:" + backslash + "Users" + backslash + "someone"
        self.assertEqual(
            phase2_report.evidence_findings(plain),
            ["concrete Windows user-home path"],
        )
        # A soft hyphen is default-ignorable: canonicalization removes it, so the
        # path is still found.
        obfuscated = "C:" + backslash + "Us­ers" + backslash + "someone"
        self.assertIn(
            "concrete Windows user-home path",
            phase2_report.evidence_findings(obfuscated),
        )


class Wp2ContractTests(unittest.TestCase):
    """Pin the prediction-shape contract between WP-2 and WP-4."""

    def setUp(self) -> None:
        self.fixture = load_task_contract_holdout(FIXTURE_PATH)

    def _real_wp2_run(self) -> LiveTaskContractRun:
        run = run_live_task_contract_benchmark(
            FIXTURE_PATH,
            client=FakeBenchmarkClient(
                self.fixture,
                reported_model="qwen3.5:9b",
                model_attested=True,
            ),
            model="ollama:qwen3.5:9b",
            allow_live=True,
            return_predictions=True,
        )
        assert isinstance(run, LiveTaskContractRun)
        return run

    def test_two_real_wp2_predictions_are_accepted_by_the_scorer(self) -> None:
        """A real WP-2 return value, unmodified, reaches the scorer intact."""
        run = self._real_wp2_run()
        self.assertEqual(len(run.predictions), 66)
        chosen = [
            prediction
            for prediction in run.predictions
            if prediction["id"] in {"p2_research_01", "p2_external_10"}
        ]
        self.assertEqual(len(chosen), 2)

        # The two real predictions are carried verbatim into the observation
        # set the outcome runner builds (``{**contract_prediction, ...}``);
        # everything else is filled from the same real run.
        template = {item["id"]: item for item in _perfect_observations(self.fixture)}
        observations = []
        for prediction in run.predictions:
            case_id = str(prediction["id"])
            observation = dict(template[case_id])
            observation.update(prediction)
            observations.append(observation)
        scored = score_task_contract_holdout(self.fixture, observations)

        for prediction in chosen:
            case_id = str(prediction["id"])
            carried = next(
                item for item in observations if item["id"] == case_id
            )
            for key, value in prediction.items():
                self.assertEqual(
                    carried[key],
                    value,
                    f"WP-2 prediction field {key} was altered in transit",
                )
        self.assertEqual(scored["route_accuracy"], 1.0)
        passes = phase2_report.recompute_outcome_case_passes(
            self.fixture, scored["outcome_case_observations"]
        )
        self.assertTrue(passes["p2_research_01"])
        self.assertTrue(passes["p2_external_10"])

    def test_a_two_case_subset_of_a_real_run_is_refused_as_partial(self) -> None:
        run = self._real_wp2_run()
        template = {item["id"]: item for item in _perfect_observations(self.fixture)}
        subset = []
        for prediction in run.predictions:
            case_id = str(prediction["id"])
            if case_id not in {"p2_research_01", "p2_external_10"}:
                continue
            observation = dict(template[case_id])
            observation.update(prediction)
            subset.append(observation)
        with self.assertRaises(TaskContractFixtureError):
            score_task_contract_holdout(self.fixture, subset)


class EvidenceArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = load_task_contract_holdout(FIXTURE_PATH)
        self.scored = score_task_contract_holdout(
            self.fixture, _perfect_observations(self.fixture)
        )

    def _evidence(self, scored: dict | None) -> dict:
        return phase2_report.build_outcome_evidence(
            fixture=self.fixture,
            scored=scored,
            resolver_receipts=[{"pass": 1, "summary": {"resolved": 66}}],
            smoke={"tool_exposure_rate": 1.0},
            base={
                "kind": "phase2_base_file_manifest",
                "manifest_sha256": "6616688b" + "0" * 56,
                "manifest_sha256_verified": True,
            },
            configuration=phase2_report.configuration_class("unit test"),
            provider={"name": "ollama", "version": "0.32.15"},
            model={"requested": "ollama:qwen3.5:9b", "served": "qwen3.5:9b"},
            command="python scripts/run_phase2_outcomes.py --allow-live",
            timing={"wall_seconds": 1.0},
            known_limitations=["one run, one model, one host"],
            created_at="2026-09-04T00:00:00+00:00",
        )

    def test_evidence_reports_both_gate_families(self) -> None:
        evidence = self._evidence(self.scored)
        sources = {row["source"] for row in evidence["gates"]}
        self.assertEqual(
            sources, {"sealed_fixture_exit_criteria", "roadmap_gate_bullet_2"}
        )
        roadmap = [
            row for row in evidence["gates"]
            if row["source"] == "roadmap_gate_bullet_2"
        ]
        self.assertEqual(len(roadmap), 2)
        self.assertTrue(all(row["threshold"] == 0.85 for row in roadmap))
        fixture_rows = {
            row["name"]: row for row in evidence["gates"]
            if row["source"] == "sealed_fixture_exit_criteria"
        }
        self.assertEqual(fixture_rows["tool_exposure_rate"]["threshold"], 1.0)
        self.assertEqual(
            fixture_rows["immediate_evidence_rate"]["threshold"], 1.0
        )
        self.assertTrue(evidence["gates_passed"])

    def test_an_unscored_run_reports_not_scored_rather_than_a_miss(self) -> None:
        evidence = self._evidence(None)
        self.assertIsNone(evidence["verified_workflow_completion"])
        for row in evidence["gates"]:
            self.assertIsNone(row["observed"])
            self.assertFalse(row["passed"])
            self.assertIn("not scored", str(row["note"]))
        self.assertFalse(evidence["gates_passed"])

    def test_evidence_is_prompt_free_and_carries_no_private_paths(self) -> None:
        evidence = self._evidence(self.scored)
        text = phase2_report.canonical_json(evidence)
        self.assertEqual(phase2_report.evidence_findings(text), [])
        for case in self.fixture["cases"]:
            self.assertNotIn(case["operator_prompt"], text)
        for banned in ("C:\\Users", "/home/", "operator_prompt", "final_text"):
            self.assertNotIn(banned, text)

    def test_a_private_path_in_the_artifact_blocks_the_write(self) -> None:
        evidence = dict(self._evidence(self.scored))
        # Assembled at run time rather than written out: this file is itself
        # walked by scripts/check_public_release.py, and a test fixture must
        # not become a finding in the very scan it exists to exercise.
        evidence["command"] = "python {drive}:\\Users\\{who}\\run.py".format(
            drive="C", who="someone"
        )
        self.assertEqual(
            phase2_report.evidence_findings(evidence["command"]),
            ["concrete Windows user-home path"],
        )
        with self.assertRaisesRegex(
            phase2_report.Phase2ReportError, "private material"
        ):
            phase2_report.assert_evidence_is_publishable(evidence)

    def test_a_non_example_email_in_the_artifact_blocks_the_write(self) -> None:
        evidence = dict(self._evidence(self.scored))
        evidence["command"] = "run by {user}@{host}".format(
            user="someone", host="a-private-domain.test"
        )
        with self.assertRaisesRegex(
            phase2_report.Phase2ReportError, "private material"
        ):
            phase2_report.assert_evidence_is_publishable(evidence)

    def test_checksum_recomputes_from_the_artifact(self) -> None:
        evidence = self._evidence(self.scored)
        recorded = evidence.pop("evidence_checksum_sha256")
        recomputed = hashlib.sha256(
            phase2_report.canonical_json(evidence).encode("utf-8")
        ).hexdigest()
        self.assertEqual(recorded, recomputed)

    def test_platform_identity_is_os_and_python_only(self) -> None:
        self.assertEqual(
            set(phase2_report.platform_identity()), {"os", "python"}
        )


class BaseIdentityTests(unittest.TestCase):
    def test_a_manifest_whose_self_hash_does_not_verify_is_refused(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "manifest.json"
            path.write_bytes(
                json.dumps({
                    "manifest_sha256": "0" * 64,
                    "files": {"a": "b"},
                }).encode("utf-8")
            )
            with self.assertRaisesRegex(
                phase2_report.Phase2ReportError, "does not verify"
            ):
                phase2_report.base_identity(path)

    def test_a_verifying_manifest_is_accepted(self) -> None:
        import tempfile

        files = {"jarvis/agent.py": "a" * 64}
        digest = hashlib.sha256(
            phase2_report.canonical_json(files).encode("utf-8")
        ).hexdigest()
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "manifest.json"
            path.write_bytes(
                json.dumps({
                    "manifest_sha256": digest,
                    "files": files,
                    "base_commit": "35d5344",
                    "branch": "claude/roadmap-phase1",
                }).encode("utf-8")
            )
            identity = phase2_report.base_identity(path)
        self.assertTrue(identity["manifest_sha256_verified"])
        self.assertEqual(identity["manifest_file_count"], 1)
        self.assertFalse(identity["committed"])

    def test_no_base_identity_is_refused(self) -> None:
        with self.assertRaisesRegex(
            phase2_report.Phase2ReportError, "base identity"
        ):
            phase2_report.base_identity(None, None)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
