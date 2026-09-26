import json
import re
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from jarvis.agent import Agent
from jarvis.config import Config
from jarvis.model_client import ModelClient
from jarvis.task_contract_benchmark import (
    BenchmarkProviderError,
    ExactModelBenchmarkClient,
    LiveTaskContractRun,
    RequestRecordingBenchmarkClient,
    _observed_tool_effect,
    _observed_tool_names,
    build_exact_model_benchmark_client,
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

class AgentSurfaceModelClient(ModelClient):
    """A real ModelClient subclass exposing the surface Agent reaches for."""

    def __init__(self):
        pass

    def models(self, refresh=True):
        return ["qwen3.5:9b"]

    def preload(self, model, context_length=4096):
        return "preloaded"

    def supports_thinking(self, model):
        return True

    def chat(self, messages, tools, model, **kwargs):
        return {"role": "assistant", "content": "{}"}


class FixedResponseClient:
    """Return one caller-owned response object, unchanged, on every call."""

    def __init__(self, response):
        self.response = response
        self.calls = 0

    def chat(self, messages, tools, model, **kwargs):
        self.calls += 1
        return self.response


class ModelNamingClient:
    """Record the exact model name an adapter put on the wire."""

    def __init__(self, fixture, *, reported_model, model_attested=True):
        self.payloads = [_valid_response_payload(case) for case in fixture["cases"]]
        self.reported_model = reported_model
        self.model_attested = model_attested
        self.models_seen: list[object] = []

    def chat(self, messages, tools, model, **kwargs):
        self.models_seen.append(model)
        payload = self.payloads.pop(0)
        return AttributeModelResponse(
            json.dumps(payload),
            self.reported_model,
            self.model_attested,
        )


class StreamingBenchmarkClient(FakeBenchmarkClient):
    def __init__(self, fixture, **kwargs):
        super().__init__(fixture, **kwargs)
        self.stream_calls = 0

    def chat_stream(self, messages, tools, model, on_delta, **kwargs):
        self.stream_calls += 1
        on_delta("delta")
        return self.chat(messages, tools, model, **kwargs)


class RequestRecordingBenchmarkClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = load_task_contract_holdout(FIXTURE_PATH)

    def test_wrapper_passes_model_and_attestation_through_byte_identically(self):
        for attested in (True, False):
            with self.subTest(model_attested=attested):
                fixed = AttributeModelResponse(
                    '{"version": 1}', "qwen3.5:9b", attested
                )
                inner = FixedResponseClient(fixed)
                wrapper = RequestRecordingBenchmarkClient(inner)
                response = wrapper.chat([], [], "ollama:qwen3.5:9b")
                # The provider's own object reaches the caller untouched, so
                # the attestation bit and the reported model cannot be
                # rewritten, coerced, or invented by the instrumentation.
                self.assertIs(response, fixed)
                self.assertIsInstance(response, AttributeModelResponse)
                self.assertIs(response.model_attested, attested)
                self.assertEqual(response.model, "qwen3.5:9b")
                self.assertEqual(dict(response), dict(fixed))
                self.assertNotIn("model_attested", wrapper.__dict__)
                self.assertNotIn("model", wrapper.__dict__)

    def test_wrapped_client_produces_the_identical_receipt(self):
        """Assert on the receipt the benchmark builds, not on a mock."""
        plain = run_live_task_contract_benchmark(
            FIXTURE_PATH,
            client=AttributeModelBenchmarkClient(
                self.fixture, reported_model="qwen3.5:9b", model_attested=True
            ),
            model="ollama:qwen3.5:9b",
            allow_live=True,
            clock=StepClock(),
            created_at="2026-08-29T12:00:00+00:00",
        )
        recorder = RequestRecordingBenchmarkClient(
            AttributeModelBenchmarkClient(
                self.fixture, reported_model="qwen3.5:9b", model_attested=True
            )
        )
        wrapped = run_live_task_contract_benchmark(
            FIXTURE_PATH,
            client=recorder,
            model="ollama:qwen3.5:9b",
            allow_live=True,
            clock=StepClock(),
            created_at="2026-08-29T12:00:00+00:00",
        )
        self.assertEqual(plain, wrapped)
        self.assertTrue(wrapped["exact_model_only"])
        self.assertEqual(wrapped["summary"]["resolved"], 66)
        self.assertEqual(len(recorder.requests), 66)

    def test_wrapper_records_every_request_with_the_exact_tool_schemas(self):
        inner = FakeBenchmarkClient(self.fixture, model_attested=True)
        wrapper = RequestRecordingBenchmarkClient(inner)
        schemas = [
            [{"type": "function", "function": {"name": "write_file"}}],
            [
                {"type": "function", "function": {"name": "read_file"}},
                {"type": "function", "function": {"name": "schedule_create"}},
            ],
            [],
        ]
        for index, tools in enumerate(schemas):
            wrapper.chat(
                [{"role": "user", "content": f"turn {index}"}],
                tools,
                "ollama:qwen3.5:9b",
                context_length=8_192,
                think=False,
            )
        self.assertEqual(len(wrapper.requests), len(schemas))
        for request, tools in zip(wrapper.requests, schemas):
            self.assertIs(request["tools"], tools)
            self.assertEqual(request["model"], "ollama:qwen3.5:9b")
            self.assertEqual(request["context_length"], 8_192)
            self.assertIs(request["think"], False)
            self.assertFalse(request["streamed"])
        self.assertEqual(len(inner.calls), len(schemas))

        class _Holder:
            pass

        holder = _Holder()
        holder.client = wrapper
        self.assertEqual(
            _observed_tool_names(holder),
            ["read_file", "schedule_create", "write_file"],
        )
        self.assertEqual(
            _observed_tool_names(holder, start_index=1),
            ["read_file", "schedule_create"],
        )

    def test_wrapper_exposes_streaming_only_when_the_wrapped_client_does(self):
        plain = RequestRecordingBenchmarkClient(
            FakeBenchmarkClient(self.fixture, model_attested=True)
        )
        self.assertIsNone(getattr(plain, "chat_stream", None))

        inner = StreamingBenchmarkClient(self.fixture, model_attested=True)
        streaming = RequestRecordingBenchmarkClient(inner)
        stream = getattr(streaming, "chat_stream", None)
        self.assertTrue(callable(stream))
        deltas: list[str] = []
        tools = [{"type": "function", "function": {"name": "write_file"}}]
        stream([], tools, "ollama:qwen3.5:9b", deltas.append, context_length=8_192)
        self.assertEqual(deltas, ["delta"])
        self.assertEqual(inner.stream_calls, 1)
        self.assertEqual(len(streaming.requests), 1)
        self.assertTrue(streaming.requests[0]["streamed"])
        self.assertIs(streaming.requests[0]["tools"], tools)
        # A live callback is not request content and is never retained.
        self.assertNotIn("on_delta", streaming.requests[0])

    def test_wrapped_model_client_still_satisfies_the_agent_resolver_predicate(self):
        """agent.py:8096-8100 disables the resolver for a client that fails this."""
        inner = AgentSurfaceModelClient()
        for client, label in (
            (RequestRecordingBenchmarkClient(inner), "recorder"),
            (
                RequestRecordingBenchmarkClient(
                    ExactModelBenchmarkClient(
                        requested_model="ollama:qwen3.5:9b",
                        provider="ollama",
                        provider_model="qwen3.5:9b",
                        client=inner,
                    )
                ),
                "recorder(adapter)",
            ),
        ):
            with self.subTest(stack=label):
                self.assertTrue(
                    isinstance(client, ModelClient)
                    or bool(getattr(client, "supports_task_contract", False))
                )
                self.assertTrue(client.supports_task_contract)
        # Wrapping must not manufacture a capability the client lacks.
        bare = RequestRecordingBenchmarkClient(
            FakeBenchmarkClient(self.fixture, model_attested=True)
        )
        self.assertFalse(bare.supports_task_contract)
        self.assertFalse(
            isinstance(bare, ModelClient)
            or bool(getattr(bare, "supports_task_contract", False))
        )

    def test_wrapped_stack_answers_every_agent_client_lookup(self):
        """Agent reaches past chat: models/preload/supports_thinking."""
        inner = AgentSurfaceModelClient()
        adapter = ExactModelBenchmarkClient(
            requested_model="ollama:qwen3.5:9b",
            provider="ollama",
            provider_model="qwen3.5:9b",
            client=inner,
        )
        stack = RequestRecordingBenchmarkClient(adapter)
        for client, label in ((adapter, "adapter"), (stack, "recorder(adapter)")):
            with self.subTest(stack=label):
                # agent.py:6024 catches only TypeError, so an AttributeError
                # here would abort Agent construction outright.
                self.assertEqual(client.models(refresh=True), ["qwen3.5:9b"])
                preload = getattr(client, "preload", None)
                self.assertTrue(callable(preload))
                self.assertEqual(preload("qwen3.5:9b", context_length=4096), "preloaded")
                supports_thinking = getattr(client, "supports_thinking", None)
                self.assertTrue(callable(supports_thinking))
                self.assertIs(supports_thinking("qwen3.5:9b"), True)

    def test_wrapper_delegates_unknown_attributes_to_the_wrapped_client(self):
        inner = FakeBenchmarkClient(self.fixture, model_attested=True)
        wrapper = RequestRecordingBenchmarkClient(inner)
        self.assertIs(wrapper.wrapped, inner)
        self.assertIs(wrapper.reported_model, inner.reported_model)
        missing = "no_such_attribute"
        with self.assertRaises(AttributeError):
            getattr(wrapper, missing)

    def test_benchmark_instrumentation_is_absent_from_production_modules(self):
        package = Path(__file__).resolve().parents[1] / "jarvis"
        names = (
            "RequestRecordingBenchmarkClient",
            "ExactModelBenchmarkClient",
            "build_exact_model_benchmark_client",
        )
        offenders = []
        for module in sorted(package.glob("*.py")):
            if module.name == "task_contract_benchmark.py":
                continue
            text = module.read_text(encoding="utf-8")
            if any(name in text for name in names):
                offenders.append(module.name)
        self.assertEqual(offenders, [])


class ExactModelBenchmarkClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = load_task_contract_holdout(FIXTURE_PATH)

    def test_unattestable_provider_is_refused_before_any_provider_call(self):
        built: list[tuple[str, str]] = []

        def factory(provider, provider_model):
            built.append((provider, provider_model))
            raise AssertionError("an unattestable provider must never be constructed")

        for reference in ("claude-cli:claude-sonnet-4-5", "codex-cli:gpt-5.6-luna"):
            with self.subTest(model=reference):
                with self.assertRaisesRegex(BenchmarkProviderError, "attest"):
                    build_exact_model_benchmark_client(
                        reference, client_factory=factory
                    )
        self.assertEqual(built, [])

    def test_inexact_model_references_are_refused(self):
        for reference in ("gpt-5.6-luna", "ollama:auto", ""):
            with self.subTest(model=reference):
                with self.assertRaisesRegex(ValueError, "exact|auto"):
                    build_exact_model_benchmark_client(
                        reference,
                        client_factory=lambda *_args: None,
                    )

    def test_adapter_names_the_bare_provider_model_on_every_call(self):
        inner = ModelNamingClient(self.fixture, reported_model="qwen3.5:9b")
        adapter = build_exact_model_benchmark_client(
            "ollama:qwen3.5:9b",
            client_factory=lambda _provider, _model: inner,
        )
        self.assertIsInstance(adapter, ExactModelBenchmarkClient)
        self.assertEqual(adapter.provider, "ollama")
        self.assertEqual(adapter.provider_model, "qwen3.5:9b")
        self.assertEqual(adapter.requested_model, "ollama:qwen3.5:9b")
        receipt = run_live_task_contract_benchmark(
            FIXTURE_PATH,
            client=adapter,
            model=adapter.requested_model,
            allow_live=True,
            clock=StepClock(),
            created_at="2026-08-29T12:00:00+00:00",
        )
        self.assertEqual(receipt["summary"]["resolved"], 66)
        self.assertTrue(receipt["exact_model_only"])
        self.assertEqual(len(inner.models_seen), 66)
        # Never None, never a different name: the client can never fall back
        # to its own configured default.
        self.assertEqual(set(inner.models_seen), {"qwen3.5:9b"})

    def test_adapter_refuses_a_model_it_was_not_built_for(self):
        inner = ModelNamingClient(self.fixture, reported_model="qwen3.5:9b")
        adapter = build_exact_model_benchmark_client(
            "ollama:qwen3.5:9b",
            client_factory=lambda _provider, _model: inner,
        )
        for other in ("ollama:qwen3:8b", "qwen3:8b", "", None):
            with self.subTest(model=other):
                with self.assertRaisesRegex(BenchmarkProviderError, "not built for"):
                    adapter.chat([], [], other)
        self.assertEqual(inner.models_seen, [])

    def test_cloud_providers_clear_attestation_but_read_no_credential(self):
        for reference in ("openai:gpt-5.6-luna", "anthropic:claude-sonnet-5"):
            with self.subTest(model=reference):
                with self.assertRaisesRegex(
                    BenchmarkProviderError, "client_factory"
                ):
                    build_exact_model_benchmark_client(reference)


class LiveTaskContractPredictionReturnTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = load_task_contract_holdout(FIXTURE_PATH)

    def _run(self, *, return_predictions):
        return run_live_task_contract_benchmark(
            FIXTURE_PATH,
            client=FakeBenchmarkClient(
                self.fixture, reported_model="qwen3.5:9b", model_attested=True
            ),
            model="ollama:qwen3.5:9b",
            allow_live=True,
            clock=StepClock(),
            created_at="2026-08-29T12:00:00+00:00",
            return_predictions=return_predictions,
        )

    def test_predictions_are_returned_without_changing_the_receipt(self):
        receipt_only = self._run(return_predictions=False)
        run = self._run(return_predictions=True)
        self.assertIsInstance(run, LiveTaskContractRun)
        self.assertIsInstance(receipt_only, dict)
        self.assertEqual(run.receipt, receipt_only)
        self.assertEqual(
            run.receipt["receipt_checksum_sha256"],
            receipt_only["receipt_checksum_sha256"],
        )
        self.assertEqual(len(run.predictions), 66)
        self.assertEqual(
            [item["id"] for item in run.predictions],
            [str(case["id"]) for case in self.fixture["cases"]],
        )
        for prediction in run.predictions:
            self.assertEqual(
                set(prediction),
                {
                    "id",
                    "lane",
                    "clarification",
                    "relation",
                    "constraint_quotes",
                    "requested_effect",
                    "evidence_source",
                    "acceptance",
                },
            )

    def test_returned_predictions_never_leak_into_the_receipt(self):
        run = self._run(return_predictions=True)
        serialized = json.dumps(run.receipt, ensure_ascii=False)
        # Short fragments are substrings of ordinary receipt words; assert on
        # the quotes long enough to identify operator text on their own.
        quotes = {
            quote
            for prediction in run.predictions
            for quote in prediction["constraint_quotes"]
            if len(quote) >= 12
        }
        self.assertTrue(quotes)
        for quote in quotes:
            self.assertNotIn(quote, serialized)
        self.assertNotIn("constraint_quotes", serialized)

    def test_receipt_carries_no_path_hostname_or_address_shaped_text(self):
        """Fixed deny-list over the serialized receipt."""
        run = self._run(return_predictions=True)
        serialized = json.dumps(run.receipt, ensure_ascii=False)
        for pattern, label in (
            (r"[A-Za-z]:[\\/]{1,2}Users", "Windows user-home path"),
            (r"/(?:home|Users)/", "POSIX user-home path"),
            (r"(?:\\\\|//)[^\\/\s]+[\\/]+(?:Users|home)", "UNC user-home path"),
            (r"https?://", "URL or host reference"),
            (r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "email address"),
            (r"(?i)\bcontent\b", "model output field"),
            (r"(?i)\bprompt\b", "prompt text"),
            (r"(?i)\btraceback\b", "provider diagnostic"),
        ):
            with self.subTest(deny=label):
                self.assertIsNone(
                    re.search(pattern, serialized),
                    f"receipt contains {label}",
                )

    def test_predictions_are_a_copy_the_caller_cannot_use_to_edit_the_run(self):
        run = self._run(return_predictions=True)
        run.predictions[0]["lane"] = "tampered"
        with self.assertRaises(AttributeError):
            run.predictions = ()
        self.assertEqual(len(run.predictions), 66)


if __name__ == "__main__":
    unittest.main()
