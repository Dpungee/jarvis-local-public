import json
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from jarvis.agent import Agent
from jarvis.config import Config
from jarvis.task_contract_benchmark import (
    _observed_tool_effect,
    run_isolated_task_contract_outcome_benchmark,
    run_live_task_contract_benchmark,
)
from jarvis.task_contract_eval import load_task_contract_holdout
from jarvis.tools import MUTATING_TOOLS
from tests.test_agent import FakeToolBox, ScriptedClient


FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "task_contract_holdout_v2.json"
)


def _valid_response_payload(case: dict) -> dict:
    expected = case["expected"]
    pending = case["pending_contract"]
    relation = expected["relation"]
    lane = expected["lane"]
    clarification = expected["clarification"]
    target = None
    if relation == "continue" and pending is not None:
        target = pending["target"]
    if (
        lane in {"creation", "inspection"}
        and expected["evidence_source"] == "provided"
        and not clarification
        and target is None
    ):
        target = case["operator_prompt"]
    missing_inputs = [{"key": "subject"}] if clarification else []
    return {
        "version": 1,
        "relation": relation,
        "lane": lane,
        "artifact_kind": (
            pending["artifact_kind"]
            if relation == "continue" and pending is not None
            else "other" if lane == "creation" else "none"
        ),
        "evidence_source": expected["evidence_source"],
        "requested_effect": expected["requested_effect"],
        "goal": (
            pending["goal"]
            if relation == "continue" and pending is not None
            else case["operator_prompt"]
        ),
        "target": target,
        "constraint_quotes": list(expected["retained_constraints"]),
        "missing_inputs": missing_inputs,
        "acceptance": (
            [] if clarification else list(expected["acceptance_contains"])
        ),
    }


class FakeBenchmarkClient:
    def __init__(
        self,
        fixture: dict,
        *,
        reported_model: str = "gpt-5.6-luna",
        model_attested: object = False,
    ):
        self.payloads = [_valid_response_payload(case) for case in fixture["cases"]]
        self.reported_model = reported_model
        self.model_attested = model_attested
        self.calls: list[dict] = []

    def chat(self, messages, tools, model, **kwargs):
        self.calls.append({
            "messages": messages,
            "tools": tools,
            "model": model,
            "kwargs": kwargs,
        })
        payload = self.payloads.pop(0)
        return {
            "role": "assistant",
            "content": json.dumps(payload),
            "model": self.reported_model,
            "model_attested": self.model_attested,
        }


class AttributeModelResponse(dict):
    def __init__(self, content: str, model: str, model_attested: object):
        super().__init__(role="assistant", content=content)
        self.model = model
        self.model_attested = model_attested


class AttributeModelBenchmarkClient(FakeBenchmarkClient):
    def chat(self, messages, tools, model, **kwargs):
        request = {
            "messages": messages,
            "tools": tools,
            "model": model,
            "kwargs": kwargs,
        }
        self.calls.append(request)
        payload = self.payloads.pop(0)
        return AttributeModelResponse(
            json.dumps(payload),
            self.reported_model,
            self.model_attested,
        )


class FailingBenchmarkClient:
    def __init__(self):
        self.calls = 0

    def chat(self, messages, tools, model, **kwargs):
        self.calls += 1
        raise RuntimeError(
            "provider failed while handling secret prompt fragment that must not be retained"
        )


class MissingModelBenchmarkClient(FakeBenchmarkClient):
    def chat(self, messages, tools, model, **kwargs):
        self.calls.append({
            "messages": messages,
            "tools": tools,
            "model": model,
            "kwargs": kwargs,
        })
        payload = self.payloads.pop(0)
        return {
            "role": "assistant",
            "content": json.dumps(payload),
            "model_attested": self.model_attested,
        }


class MissingAttestationBenchmarkClient(FakeBenchmarkClient):
    def chat(self, messages, tools, model, **kwargs):
        self.calls.append({
            "messages": messages,
            "tools": tools,
            "model": model,
            "kwargs": kwargs,
        })
        payload = self.payloads.pop(0)
        return {
            "role": "assistant",
            "content": json.dumps(payload),
            "model": self.reported_model,
        }


class StepClock:
    def __init__(self):
        self.value = 10.0

    def __call__(self):
        current = self.value
        self.value += 0.001
        return current


class TaskContractLiveBenchmarkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = load_task_contract_holdout(FIXTURE_PATH)

    def test_live_runner_is_explicit_exact_model_tool_free_and_prompt_free(self):
        client = FakeBenchmarkClient(self.fixture, model_attested=True)
        receipt = run_live_task_contract_benchmark(
            FIXTURE_PATH,
            client=client,
            model="openai:gpt-5.6-luna",
            allow_live=True,
            clock=StepClock(),
            created_at="2026-08-29T12:00:00+00:00",
        )
        self.assertEqual(len(client.calls), 66)
        self.assertTrue(all(call["tools"] == [] for call in client.calls))
        self.assertTrue(
            all(call["model"] == "openai:gpt-5.6-luna" for call in client.calls)
        )
        self.assertTrue(all(call["kwargs"]["think"] is False for call in client.calls))
        self.assertTrue(all(call["kwargs"]["temperature"] == 0.0 for call in client.calls))
        self.assertTrue(all(call["kwargs"]["seed"] == 0 for call in client.calls))
        self.assertTrue(
            all(isinstance(call["kwargs"]["response_format"], dict) for call in client.calls)
        )
        self.assertEqual(receipt["summary"]["resolved"], 66)
        self.assertEqual(receipt["summary"]["provider_error"], 0)
        self.assertEqual(receipt["fallback_count"], 0)
        self.assertEqual(receipt["tools_supplied"], 0)
        self.assertFalse(receipt["training_eligible"])
        self.assertEqual(receipt["memory_writes"], 0)
        self.assertFalse(receipt["operator_text_retained"])
        self.assertTrue(
            receipt["summary"]["contract_metrics"][
                "all_contract_exit_criteria_passed"
            ]
        )
        serialized = json.dumps(receipt, ensure_ascii=False)
        for case in self.fixture["cases"]:
            self.assertNotIn(case["operator_prompt"], serialized)
            pending = case["pending_contract"]
            if pending is not None:
                self.assertNotIn(pending["goal"], serialized)
        self.assertNotIn("content", serialized.casefold())
        self.assertNotIn("prompt", serialized.casefold())
        self.assertTrue(receipt["exact_model_only"])
        self.assertTrue(receipt["model_attestation_required"])
        self.assertEqual(len(receipt["receipt_checksum_sha256"]), 64)
        self.assertNotIn("receipt_sha256", receipt)

    def test_live_runner_fails_closed_without_opt_in_or_exact_model(self):
        client = FakeBenchmarkClient(self.fixture)
        with self.assertRaisesRegex(PermissionError, "allow_live=True"):
            run_live_task_contract_benchmark(
                FIXTURE_PATH,
                client=client,
                model="codex-cli:gpt-5.6-luna",
            )
        self.assertEqual(client.calls, [])
        for invalid in ("gpt-5.6-luna", "codex-cli:auto", ""):
            with self.subTest(model=invalid):
                with self.assertRaisesRegex(ValueError, "exact|auto"):
                    run_live_task_contract_benchmark(
                        FIXTURE_PATH,
                        client=client,
                        model=invalid,
                        allow_live=True,
                    )
        self.assertEqual(client.calls, [])

    def test_model_mismatch_is_not_scored_as_the_requested_model(self):
        client = FakeBenchmarkClient(
            self.fixture,
            reported_model="gpt-5.6-sol",
            model_attested=True,
        )
        receipt = run_live_task_contract_benchmark(
            FIXTURE_PATH,
            client=client,
            model="openai:gpt-5.6-luna",
            allow_live=True,
            clock=StepClock(),
            created_at="2026-08-29T12:00:00+00:00",
        )
        self.assertEqual(receipt["summary"]["resolved"], 0)
        self.assertEqual(receipt["summary"]["model_mismatch"], 66)
        self.assertIsNone(receipt["summary"]["contract_metrics"])

    def test_production_attribute_model_metadata_is_verified(self):
        client = AttributeModelBenchmarkClient(
            self.fixture,
            reported_model="gpt-5.6-sol",
            model_attested=True,
        )
        receipt = run_live_task_contract_benchmark(
            FIXTURE_PATH,
            client=client,
            model="openai:gpt-5.6-luna",
            allow_live=True,
            clock=StepClock(),
            created_at="2026-08-29T12:00:00+00:00",
        )
        self.assertEqual(receipt["summary"]["resolved"], 0)
        self.assertEqual(receipt["summary"]["model_mismatch"], 66)
        self.assertFalse(receipt["exact_model_only"])
        self.assertTrue(all(
            case["model_attestation"] in {"mismatch", "not_observed"}
            for case in receipt["cases"]
        ))
        self.assertNotIn("gpt-5.6-sol", json.dumps(receipt))

    def test_missing_provider_model_attestation_fails_exact_model_claim_closed(self):
        client = MissingModelBenchmarkClient(self.fixture, model_attested=True)
        receipt = run_live_task_contract_benchmark(
            FIXTURE_PATH,
            client=client,
            model="openai:gpt-5.6-luna",
            allow_live=True,
            clock=StepClock(),
            created_at="2026-08-29T12:00:00+00:00",
        )
        self.assertEqual(receipt["summary"]["resolved"], 0)
        self.assertGreater(receipt["summary"]["model_unattested"], 0)
        self.assertFalse(receipt["exact_model_only"])
        self.assertIsNone(receipt["summary"]["contract_metrics"])
        self.assertTrue(all(
            case["model_attestation"] in {"missing", "not_observed"}
            for case in receipt["cases"]
        ))

    def test_missing_explicit_attestation_fails_even_with_matching_model(self):
        client = MissingAttestationBenchmarkClient(self.fixture)
        receipt = run_live_task_contract_benchmark(
            FIXTURE_PATH,
            client=client,
            model="openai:gpt-5.6-luna",
            allow_live=True,
            clock=StepClock(),
            created_at="2026-08-29T12:00:00+00:00",
        )
        self.assertEqual(receipt["summary"]["resolved"], 0)
        self.assertEqual(receipt["summary"]["model_unattested"], 66)
        self.assertFalse(receipt["exact_model_only"])
        self.assertIsNone(receipt["summary"]["contract_metrics"])

    def test_codex_app_server_cannot_satisfy_served_model_proof(self):
        client = FakeBenchmarkClient(self.fixture, model_attested=True)
        receipt = run_live_task_contract_benchmark(
            FIXTURE_PATH,
            client=client,
            model="codex-cli:gpt-5.6-luna",
            allow_live=True,
            clock=StepClock(),
            created_at="2026-08-29T12:00:00+00:00",
        )
        self.assertEqual(receipt["summary"]["resolved"], 0)
        self.assertEqual(receipt["summary"]["model_unattested"], 66)
        self.assertFalse(receipt["exact_model_only"])
        self.assertEqual(
            receipt["provider_model_attestation"],
            "unavailable_for_selected_provider",
        )

    def test_direct_provider_response_attestation_can_satisfy_model_proof(self):
        for requested_model, reported_model in (
            ("anthropic:claude-sonnet-5", "claude-sonnet-5"),
            ("ollama:qwen3.5:9b", "qwen3.5:9b"),
        ):
            with self.subTest(model=requested_model):
                client = FakeBenchmarkClient(
                    self.fixture,
                    reported_model=reported_model,
                    model_attested=True,
                )
                receipt = run_live_task_contract_benchmark(
                    FIXTURE_PATH,
                    client=client,
                    model=requested_model,
                    allow_live=True,
                    clock=StepClock(),
                    created_at="2026-08-29T12:00:00+00:00",
                )
                self.assertEqual(receipt["summary"]["resolved"], 66)
                self.assertEqual(receipt["summary"]["model_unattested"], 0)
                self.assertTrue(receipt["exact_model_only"])
                self.assertEqual(
                    receipt["provider_model_attestation"],
                    "explicit_response_signal_required",
                )

    def test_cli_copied_requested_model_metadata_is_non_exit_evidence(self):
        for requested_model in (
            "codex-cli:gpt-5.6-luna",
            "claude-cli:claude-haiku-4-5",
        ):
            with self.subTest(model=requested_model):
                client = FakeBenchmarkClient(
                    self.fixture,
                    reported_model=requested_model.split(":", 1)[1],
                    model_attested=False,
                )
                receipt = run_live_task_contract_benchmark(
                    FIXTURE_PATH,
                    client=client,
                    model=requested_model,
                    allow_live=True,
                    clock=StepClock(),
                    created_at="2026-08-29T12:00:00+00:00",
                )
                self.assertEqual(receipt["summary"]["resolved"], 0)
                self.assertEqual(receipt["summary"]["model_unattested"], 66)
                self.assertFalse(receipt["exact_model_only"])
                self.assertEqual(
                    receipt["provider_model_attestation"],
                    "unavailable_for_selected_provider",
                )
                self.assertIsNone(receipt["summary"]["contract_metrics"])

    def test_cli_attestation_flag_cannot_override_unattestable_provider(self):
        client = FakeBenchmarkClient(
            self.fixture,
            reported_model="gpt-5.6-sol",
            model_attested=True,
        )
        receipt = run_live_task_contract_benchmark(
            FIXTURE_PATH,
            client=client,
            model="codex-cli:gpt-5.6-luna",
            allow_live=True,
            clock=StepClock(),
            created_at="2026-08-29T12:00:00+00:00",
        )
        self.assertEqual(receipt["summary"]["resolved"], 0)
        self.assertEqual(receipt["summary"]["model_mismatch"], 0)
        self.assertEqual(receipt["summary"]["model_unattested"], 66)
        self.assertFalse(receipt["exact_model_only"])

    def test_provider_errors_are_bounded_and_never_copy_diagnostics(self):
        client = FailingBenchmarkClient()
        receipt = run_live_task_contract_benchmark(
            FIXTURE_PATH,
            client=client,
            model="openai:gpt-5.6-luna",
            allow_live=True,
            clock=StepClock(),
            created_at="2026-08-29T12:00:00+00:00",
        )
        self.assertEqual(client.calls, 66)
        self.assertEqual(receipt["summary"]["provider_error"], 66)
        self.assertEqual(receipt["summary"]["resolved"], 0)
        self.assertIsNone(receipt["summary"]["contract_metrics"])
        serialized = json.dumps(receipt)
        self.assertNotIn("secret prompt fragment", serialized)
        self.assertNotIn("provider failed while handling", serialized)

    def test_every_declared_mutating_tool_is_never_scored_as_a_read(self):
        misclassified = {
            name for name in MUTATING_TOOLS
            if _observed_tool_effect(name) == "read"
        }
        self.assertEqual(misclassified, set())
        self.assertEqual(_observed_tool_effect("process_status"), "read")
        self.assertEqual(_observed_tool_effect("http_health"), "read")

    def test_isolated_outcome_runner_uses_real_agent_temp_db_and_observed_result(self):
        case = {
            "id": "isolated_dialogue",
            "tags": ["dialogue"],
            "operator_prompt": "yo",
            "recent_user_turns": [],
            "latest_assistant_context": None,
            "pending_contract": None,
            "expected": {"action_timing": "none"},
        }
        captured: dict = {}
        factory_calls: list[tuple[Path, bool]] = []

        def factory(_case, memory, workspace, on_event):
            factory_calls.append((workspace, memory.db is not None))
            config = replace(
                Config.load(),
                workspace=workspace,
                data_dir=workspace.parent / "data",
                vault_dir=None,
                model="auto",
                ollama_preload=False,
                execution_mode="trusted-host",
                computer_access="disabled",
            )
            toolbox = FakeToolBox()
            toolbox.task_contract_outcome_isolated = True
            with patch("jarvis.agent.ToolBox", return_value=toolbox):
                return Agent(
                    config,
                    memory,
                    on_event,
                    client=ScriptedClient([]),
                    record_training=False,
                    coding_review=False,
                    coding_planning=False,
                )

        def capture_score(fixture, observations):
            captured["fixture"] = fixture
            captured["observations"] = observations
            return {"all_exit_criteria_passed": False, "observed": True}

        with (
            patch(
                "jarvis.task_contract_benchmark.load_task_contract_holdout",
                return_value={"cases": [case]},
            ),
            patch(
                "jarvis.task_contract_benchmark.score_task_contract_holdout",
                side_effect=capture_score,
            ),
        ):
            result = run_isolated_task_contract_outcome_benchmark(
                FIXTURE_PATH,
                contract_predictions=[{"id": "isolated_dialogue"}],
                agent_factory=factory,
                allow_run=True,
            )

        self.assertTrue(result["observed"])
        self.assertEqual(len(factory_calls), 1)
        observation = captured["observations"][0]
        self.assertEqual(observation["id"], "isolated_dialogue")
        self.assertEqual(observation["final_status"], "complete")
        self.assertTrue(observation["final_text"])
        self.assertEqual(observation["tool_events"], [])
        self.assertEqual(observation["durable_queue_records"], [])
        self.assertFalse(observation["restart_observation"]["performed"])

    def test_isolated_outcome_runner_requires_explicit_opt_in(self):
        with self.assertRaisesRegex(PermissionError, "allow_run=True"):
            run_isolated_task_contract_outcome_benchmark(
                FIXTURE_PATH,
                contract_predictions=[],
                agent_factory=lambda *_args: None,
            )

    def test_isolated_outcome_runner_rejects_a_non_attested_toolbox(self):
        case = {
            "id": "isolated_dialogue",
            "tags": ["dialogue"],
            "operator_prompt": "yo",
            "recent_user_turns": [],
            "latest_assistant_context": None,
            "pending_contract": None,
            "expected": {"action_timing": "none"},
        }

        def factory(_case, memory, workspace, on_event):
            config = replace(
                Config.load(),
                workspace=workspace,
                data_dir=workspace.parent / "data",
                vault_dir=None,
                model="auto",
                ollama_preload=False,
                computer_access="disabled",
            )
            with patch("jarvis.agent.ToolBox", return_value=FakeToolBox()):
                return Agent(
                    config,
                    memory,
                    on_event,
                    client=ScriptedClient([]),
                    record_training=False,
                    coding_review=False,
                    coding_planning=False,
                )

        with patch(
            "jarvis.task_contract_benchmark.load_task_contract_holdout",
            return_value={"cases": [case]},
        ):
            with self.assertRaisesRegex(PermissionError, "isolated"):
                run_isolated_task_contract_outcome_benchmark(
                    FIXTURE_PATH,
                    contract_predictions=[{"id": "isolated_dialogue"}],
                    agent_factory=factory,
                    allow_run=True,
                )

    def test_isolated_outcome_runner_rejects_live_toolbox_even_if_flagged(self):
        case = {
            "id": "isolated_dialogue",
            "tags": ["dialogue"],
            "operator_prompt": "yo",
            "recent_user_turns": [],
            "latest_assistant_context": None,
            "pending_contract": None,
            "expected": {"action_timing": "none"},
        }

        def factory(_case, memory, workspace, on_event):
            config = replace(
                Config.load(),
                workspace=workspace,
                data_dir=workspace.parent / "data",
                vault_dir=None,
                model="auto",
                ollama_preload=False,
                computer_access="disabled",
            )
            agent = Agent(
                config,
                memory,
                on_event,
                client=ScriptedClient([]),
                record_training=False,
                coding_review=False,
                coding_planning=False,
            )
            agent.toolbox.task_contract_outcome_isolated = True
            return agent

        with patch(
            "jarvis.task_contract_benchmark.load_task_contract_holdout",
            return_value={"cases": [case]},
        ):
            with self.assertRaisesRegex(PermissionError, "live-capability"):
                run_isolated_task_contract_outcome_benchmark(
                    FIXTURE_PATH,
                    contract_predictions=[{"id": "isolated_dialogue"}],
                    agent_factory=factory,
                    allow_run=True,
                )

if __name__ == "__main__":
    unittest.main()
