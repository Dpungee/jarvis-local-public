"""Offline outcome-runner contracts: identity, provenance, smoke gates, rebuilds."""
from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scripts import run_phase2_outcomes as runner


class OutcomeRunnerContractTests(unittest.TestCase):
    def test_exact_model_pin_preserves_responses_and_counts_only_true_attestation(self):
        responses = [SimpleNamespace(model_attested=True), {"model_attested": False}, {"model_attested": "true"}]
        client = SimpleNamespace(chat=Mock(side_effect=responses), detail="synthetic")
        pin = runner.PinnedBenchmarkModelClient(client, "ollama:synthetic", supports_task_contract=True)
        self.assertEqual(pin.models(refresh=True), ["ollama:synthetic"])
        self.assertIs(pin.wrapped, client)
        self.assertEqual(pin.detail, "synthetic")
        self.assertTrue(pin.supports_task_contract)
        for response in responses:
            self.assertIs(pin.chat([], [], "ollama:synthetic"), response)
        self.assertEqual((pin.calls, pin.attested_calls), (3, 1))
        for wrong in ("ollama:other", "", None):
            with self.assertRaises(runner.BenchmarkProviderError):
                pin.chat([], [], wrong)
        self.assertEqual(client.chat.call_count, 3)
        self.assertEqual(pin.refusals, ["ollama:other", "<empty>", "<empty>"])
        self.assertFalse(hasattr(pin, "chat_stream"))

    def test_stream_pin_forwards_callback_and_never_invents_attestation(self):
        response = SimpleNamespace(content="synthetic", model_attested=None)
        client = SimpleNamespace(chat_stream=Mock(return_value=response))
        pin = runner.PinnedBenchmarkModelClient(client, "ollama:synthetic", supports_task_contract=False)
        callback = Mock()
        self.assertIs(pin.chat_stream([], [], "ollama:synthetic", callback, temperature=0), response)
        client.chat_stream.assert_called_once_with([], [], "ollama:synthetic", callback, temperature=0)
        self.assertEqual((pin.calls, pin.attested_calls), (1, 0))
        with self.assertRaises(runner.BenchmarkProviderError):
            pin.chat_stream([], [], "other", callback)
        self.assertEqual(client.chat_stream.call_count, 1)

    def test_offered_schema_names_ignore_malformed_rows_and_deduplicate(self):
        client = SimpleNamespace(requests=[None, {"tools": "not schemas"}, {"tools": [
            None, {}, {"function": "bad"}, {"function": {"name": 1}},
            {"function": {"name": " "}}, {"function": {"name": " read_file "}},
            {"function": {"name": "read_file"}}, {"function": {"name": "list_files"}},
        ]}])
        self.assertEqual(runner.offered_tool_names(client), ["list_files", "read_file"])
        self.assertEqual(runner.offered_tool_names(object()), [])

    def test_case_report_contains_counts_not_prompt_or_event_bodies(self):
        factory = runner.OutcomeAgentFactory(None, None, provider_model="ollama:synthetic", supports_task_contract=False)
        client = SimpleNamespace(requests=[{"model": "ollama:synthetic", "messages": "SYNTHETIC_PROMPT",
                                            "tools": [{"function": {"name": "read_file"}}]}])
        factory.records.append({"id": "case-1", "lane": "inspection", "action_timing": "immediate",
                                "requested_effect": "read", "task_contract_status": "accepted",
                                "final_status": "complete", "client": client,
                                "pin": SimpleNamespace(calls=1, attested_calls=0, refusals=[]),
                                "events": ["tool - SYNTHETIC_EVENT_BODY"]})
        row = factory.case_rows()[0]
        self.assertEqual(row["model_calls"], 1)
        self.assertEqual(row["attested_responses"], 0)
        self.assertEqual(row["offered_tools"], ["read_file"])
        self.assertEqual(row["event_kinds"], ["tool"])
        self.assertNotIn("SYNTHETIC_PROMPT", json.dumps(row))
        self.assertNotIn("SYNTHETIC_EVENT_BODY", json.dumps(row))

    def test_resolver_recovery_preserves_first_observation_and_removes_aggregate_metrics(self):
        def result(predictions, unresolved):
            return runner.LiveTaskContractRun(receipt={
                "summary": {"resolved": len(predictions), "case_count": 2, "contract_metrics": {"not_evidence": True}},
                "fixture_sha256": "a" * 64, "exact_model_only": True, "receipt_checksum_sha256": "b" * 64,
                "cases": [{"id": name, "status": "rejected"} for name in unresolved],
            }, predictions=tuple(predictions))
        passes = [result([{"id": "one", "value": "first"}], ["two"]),
                  result([{"id": "one", "value": "later"}, {"id": "two", "value": "recovered"}], [])]
        with patch.object(runner, "run_live_task_contract_benchmark", side_effect=passes) as synthetic:
            predictions, receipts = runner.run_resolver_passes(Path("fixture.json"), client=object(),
                model="ollama:synthetic", max_passes=5, case_ids=["one", "two"], log=lambda text: None)
        self.assertEqual(synthetic.call_count, 2)
        self.assertEqual(predictions["one"]["value"], "first")
        self.assertEqual(receipts[1]["recovered_case_ids"], ["two"])
        self.assertEqual(receipts[0]["unresolved_case_ids"], ["two"])
        self.assertTrue(all("contract_metrics" not in receipt["summary"] for receipt in receipts))

    def test_smoke_distinguishes_tool_exposure_from_unsupported_contract_wiring(self):
        rows = [{"id": name, "lane": "inspection", "action_timing": "immediate", "model_calls": 1,
                 "offered_tools": ["read_file"] if index == 0 else [], "tool_exposure_observed": index == 0,
                 "task_contract_status": "accepted" if index == 0 else "not_supported"}
                for index, name in enumerate(runner.SMOKE_CASE_IDS[:2])]
        factory = SimpleNamespace(case_rows=lambda: rows)
        with patch.object(runner, "run_isolated_task_contract_outcome_benchmark",
                          side_effect=runner.TaskContractFixtureError("partial set")):
            smoke = runner.run_smoke(Path("fixture.json"), predictions={}, factory=factory, log=lambda text: None)
        self.assertEqual(smoke["tool_exposure_rate"], 0.5)
        self.assertFalse(smoke["plan_gate_met"])
        self.assertFalse(smoke["task_contract_wiring_ok"])
        self.assertTrue(smoke["partial_set_refused"])
        self.assertEqual(smoke["missing_predictions"], list(runner.SMOKE_CASE_IDS))
        self.assertTrue(runner.plan_gate_fields(1.0)["plan_gate_met"])
        self.assertFalse(runner.plan_gate_fields(None)["plan_gate_met"])

    def test_live_entry_refuses_before_any_provider_without_authority_or_identity(self):
        with patch.object(runner, "build_clients", side_effect=AssertionError("provider forbidden")) as provider, redirect_stdout(io.StringIO()):
            self.assertEqual(runner.main([]), 2)
            self.assertEqual(runner.main(["--allow-live"]), 2)
            provider.assert_not_called()
        with patch.object(runner, "build_clients", side_effect=runner.BenchmarkProviderError("unattestable")), redirect_stdout(io.StringIO()):
            self.assertEqual(runner.main(["--allow-live", "--base-manifest-sha256", "a" * 64]), 2)

    def test_command_receipt_uses_placeholder_instead_of_manifest_path(self):
        args = runner.parse_args(["--base-manifest", "sensitive-location.json", "--base-manifest-sha256", "a" * 64,
                                  "--skip-smoke", "--smoke-only", "--resolver-passes", "2"])
        command = runner.command_line(args)
        self.assertIn("<phase 2 base manifest>", command)
        self.assertNotIn("sensitive-location.json", command)
        self.assertIn("--resolver-passes 2", command)
        self.assertIn("--skip-smoke", command)
        self.assertIn("--smoke-only", command)

    def test_exit_codes_do_not_turn_missing_measurement_into_success(self):
        row = {"name": "gate", "comparator": ">=", "threshold": 0.85, "observed": 0.8,
               "passed": False, "source": "roadmap_gate_bullet_2"}
        for passed, score, rows, expected in ((True, {}, [], 0), (False, None, [], 5),
                                             (False, {}, [row], 4), (False, {}, [{**row, "source": "sealed"}], 1)):
            with self.subTest(expected=expected):
                self.assertEqual(runner.report_exit_code({"gates": rows, "gates_passed": passed,
                    "verified_workflow_completion": score}, lambda text: None), expected)

    def test_saved_evidence_rebuild_is_offline_and_preserves_observations(self):
        source_path = runner.EVIDENCE_DIR / "phase2_task_contract_outcomes_ollama_6616688b.json"
        before = source_path.read_bytes()
        source = json.loads(before)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "rebuilt.json"
            with patch.object(runner, "build_clients", side_effect=AssertionError("provider forbidden")), redirect_stdout(io.StringIO()):
                result = runner.main(["--rebuild-from", str(source_path), "--out", str(target)])
            rebuilt = json.loads(target.read_bytes())
            self.assertEqual(result, runner.report_exit_code(rebuilt, lambda text: None))
            self.assertEqual(rebuilt["resolver_passes"], source["resolver_passes"])
            self.assertEqual(rebuilt["tool_exposure_notes"], source["tool_exposure_notes"])
            self.assertEqual(rebuilt["timing"], source["timing"])
        self.assertEqual(source_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
