from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from jarvis.agent import (
    Agent,
    _is_clear_tool_free_dialogue,
    _may_request_feature_configuration,
    _weather_clarification_location,
)
from jarvis.config import Config
from jarvis.feature_onboarding import FEATURE_SPECS
from jarvis.memory import Memory
from jarvis.ollama_client import OllamaError
from jarvis.router import Route
from jarvis.task_contract import TaskContract, parse_task_contract
from jarvis.tools import FEATURE_SETUP_READ_TOOLS, FEATURE_SETUP_TOOLS


class FakeResponse(dict):
    def __init__(self, content: str = "", tool_calls=None):
        super().__init__(role="assistant", content=content)
        if tool_calls is not None:
            self["tool_calls"] = tool_calls
        self.done = True
        self.done_reason = None


class ContractCapableClient:
    supports_task_contract = True

    def __init__(self, responses=()):
        self.responses = list(responses)
        self.requests: list[dict] = []

    def models(self, refresh=True):
        return ["qwen3.5:9b", "gpt-oss:20b", "qwen3-coder:30b"]

    def chat(self, messages, tools, model, **kwargs):
        self.requests.append({
            "messages": messages,
            "tools": tools,
            "model": model,
            **kwargs,
        })
        if not self.responses:
            raise AssertionError("Unexpected model call")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def contract_payload(
    prompt: str,
    *,
    lane: str,
    target: str | None,
    relation: str = "new",
    missing_inputs=(),
) -> dict:
    by_lane = {
        "dialogue": ("none", "none", "none", ["answer"]),
        "research": ("none", "public_web", "read", ["sources"]),
        "creation": ("document", "provided", "write", ["artifact"]),
        "inspection": ("none", "provided", "read", ["answer"]),
        "configuration": ("none", "none", "read", ["answer"]),
        "external_action": (
            "none",
            "provided",
            "external",
            ["external_receipt"],
        ),
    }
    artifact_kind, evidence_source, requested_effect, acceptance = by_lane[lane]
    return {
        "version": 1,
        "relation": relation,
        "lane": lane,
        "artifact_kind": artifact_kind,
        "evidence_source": evidence_source,
        "requested_effect": requested_effect,
        "goal": prompt,
        "target": target,
        "constraint_quotes": [],
        "missing_inputs": list(missing_inputs),
        "acceptance": acceptance,
    }


class TaskContractAgentIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="jarvis-contract-agent-"))
        self.workspace = self.temp_dir / "workspace"
        self.data_dir = self.temp_dir / "data"
        self.workspace.mkdir()
        self.data_dir.mkdir()
        base = Config.load()
        self.config = replace(
            base,
            workspace=self.workspace,
            data_dir=self.data_dir,
            model="auto",
            fast_model="qwen3.5:9b",
            reasoning_model="gpt-oss:20b",
            coding_model="qwen3-coder:30b",
            deep_model="qwen3-coder:30b",
            fast_context_length=16_384,
            reasoning_context_length=16_384,
            coding_context_length=16_384,
            deep_context_length=16_384,
            ollama_preload=False,
            vault_dir=None,
        )
        self.memory = Memory(self.data_dir / "agent.db")

    def tearDown(self):
        self.memory.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def make_agent(self, responses=(), *, events=None):
        client = ContractCapableClient(responses)
        agent = Agent(
            self.config,
            self.memory,
            (events if events is not None else []).append,
            client=client,
            coding_review=False,
            coding_planning=False,
        )
        return agent, client

    def test_resolver_gate_includes_general_pending_and_catalog_configuration(self):
        general = Route("fast", "qwen3.5:9b", "quick/general task")
        deterministic_fast = Route("fast", "qwen3.5:9b", "simple task")
        routed_research = Route("reasoning", "gpt-oss:20b", "research task (score 1)")

        self.assertTrue(Agent._should_resolve_task_contract(
            route=general,
            has_pending_contract=False,
            deterministic_route_claimed=False,
        ))
        self.assertTrue(Agent._should_resolve_task_contract(
            route=routed_research,
            has_pending_contract=True,
            deterministic_route_claimed=False,
        ))
        self.assertTrue(Agent._should_resolve_task_contract(
            route=routed_research,
            has_pending_contract=False,
            deterministic_route_claimed=False,
            semantic_configuration_candidate=True,
        ))
        for route, pending, claimed, task_id in (
            (deterministic_fast, False, False, None),
            (routed_research, False, False, None),
            (general, False, True, None),
            (general, True, False, 17),
        ):
            with self.subTest(
                route=route.reason,
                pending=pending,
                claimed=claimed,
                task_id=task_id,
            ):
                self.assertFalse(Agent._should_resolve_task_contract(
                    route=route,
                    has_pending_contract=pending,
                    deterministic_route_claimed=claimed,
                    task_id=task_id,
                ))

    def test_resolver_call_is_single_bounded_structured_and_tool_free(self):
        prompt = "Map the current provenance of ceramic aerogel standards."
        raw = contract_payload(
            prompt,
            lane="research",
            target="ceramic aerogel standards",
        )
        agent, client = self.make_agent([FakeResponse(json.dumps(raw))])

        result = agent._resolve_task_contract(
            prompt,
            conversation_id=1,
            route=Route("fast", "qwen3.5:9b", "quick/general task"),
            recent_user_turns=(),
            latest_assistant_context="Earlier assistant-only referent.",
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.lane, "research")
        self.assertEqual(len(client.requests), 1)
        request = client.requests[0]
        self.assertEqual(request["tools"], [])
        self.assertEqual(request["temperature"], 0.0)
        self.assertFalse(request["think"])
        self.assertEqual(request["seed"], 0)
        self.assertFalse(request["response_format"]["additionalProperties"])
        self.assertLessEqual(
            len(json.dumps(request["messages"], ensure_ascii=False)),
            7_500,
        )
        resolver_context = request["messages"][1]["content"]
        self.assertIn("Earlier assistant-only referent.", resolver_context)

    def test_malformed_or_failed_resolver_falls_back_without_retrying(self):
        prompt = "Interpret the unfamiliar cobalt lattice note."
        route = Route("fast", "qwen3.5:9b", "quick/general task")
        for response in (
            FakeResponse("{not-json"),
            FakeResponse(json.dumps({
                **contract_payload(prompt, lane="inspection", target="cobalt lattice note"),
                "permissions": ["everything"],
                "tool_names": ["github_push"],
            })),
            OllamaError("private provider failure", retryable=True),
        ):
            with self.subTest(response=type(response).__name__):
                events: list[str] = []
                agent, client = self.make_agent([response], events=events)
                result = agent._resolve_task_contract(
                    prompt,
                    conversation_id=1,
                    route=route,
                    recent_user_turns=(),
                )
                self.assertIsNone(result)
                self.assertEqual(len(client.requests), 1)
                self.assertIn(
                    "task contract unavailable - deterministic routing retained",
                    events,
                )

    def test_unavailable_contract_keeps_the_existing_agent_path(self):
        prompt = "Interpret the unfamiliar cobalt lattice note."
        agent, client = self.make_agent([
            FakeResponse("The note describes a bounded lattice interpretation."),
        ])

        with patch.object(agent, "_resolve_task_contract", return_value=None) as resolver:
            result = agent.run(prompt)

        self.assertEqual(result.status, "complete")
        self.assertIn("bounded lattice interpretation", str(result))
        resolver.assert_called_once()
        self.assertEqual(len(client.requests), 1)

    def test_dialogue_contract_uses_normal_constitution_bearing_response_path(self):
        prompt = "Reflect briefly on whether velvet silence feels hopeful."
        contract = parse_task_contract(
            contract_payload(
                prompt,
                lane="dialogue",
                target="velvet silence",
            ),
            grounding_texts=[prompt],
        )
        answer = (
            "It feels hopeful to me—quiet, but with the sense that something gentle "
            "is still possible."
        )
        agent, client = self.make_agent([FakeResponse(answer)])

        with patch.object(agent, "_resolve_task_contract", return_value=contract) as resolver:
            result = agent.run(prompt)

        self.assertEqual(result.status, "complete")
        self.assertEqual(str(result), answer)
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(client.requests[0]["tools"], [])
        self.assertIn(
            "<trusted_constitution",
            str(client.requests[0]["messages"][0]["content"]),
        )
        resolver.assert_called_once()

    def test_grounded_clarification_is_persisted_and_next_answer_resumes_it(self):
        prompt = "Assess the supplied telemetry for the anomaly."
        clarification = parse_task_contract(
            contract_payload(
                prompt,
                lane="inspection",
                target="telemetry",
                missing_inputs=[{"key": "capture"}],
            ),
            grounding_texts=[prompt],
        )
        continued = TaskContract(
            version=1,
            relation="continue",
            lane="inspection",
            artifact_kind="none",
            evidence_source="provided",
            requested_effect="read",
            goal=prompt,
            target="midnight capture",
            constraint_quotes=("Use the midnight capture.",),
            missing_inputs=(),
            acceptance=("answer",),
        )
        agent, client = self.make_agent([
            FakeResponse("The midnight capture shows no unexplained drift."),
        ])
        conversation_id = self.memory.new_conversation("telemetry")

        with patch.object(
            agent,
            "_resolve_task_contract",
            side_effect=[clarification, continued],
        ) as resolver:
            first = agent.run(prompt, conversation_id=conversation_id)
            pending = self.memory.pending_conversation_goal(conversation_id)
            second = agent.run(
                "Use the midnight capture.",
                conversation_id=conversation_id,
            )

        self.assertEqual(str(first), "What should I use for the missing capture?")
        self.assertIsNotNone(pending)
        self.assertEqual(pending["contract"]["missing_inputs"][0]["key"], "capture")
        self.assertEqual(second.status, "complete")
        self.assertIn("no unexplained drift", str(second))
        self.assertIsNone(self.memory.pending_conversation_goal(conversation_id))
        self.assertEqual(resolver.call_count, 2)
        self.assertIsNone(resolver.call_args_list[0].kwargs["pending_goal"])
        self.assertIsNotNone(resolver.call_args_list[1].kwargs["pending_goal"])
        goals = self.memory.list_conversation_goals(conversation_id)
        self.assertEqual(goals[0]["resume_count"], 1)

    def test_creation_without_identified_source_clarifies_instead_of_inventing(self):
        prompt = "Shape these notes into a field guide."
        raw = contract_payload(
            prompt,
            lane="creation",
            target=None,
        )
        agent, client = self.make_agent([FakeResponse(json.dumps(raw))])
        conversation_id = self.memory.new_conversation("missing source")

        result = agent.run(prompt, conversation_id=conversation_id)

        self.assertEqual(
            str(result),
            "What should I use for the missing source material?",
        )
        self.assertEqual(len(client.requests), 1)
        pending = self.memory.pending_conversation_goal(conversation_id)
        self.assertIsNotNone(pending)
        self.assertEqual(
            pending["contract"]["missing_inputs"],
            [{"key": "source_material"}],
        )

    def test_planned_resolver_and_answer_calls_are_not_counted_as_retries(self):
        prompt = (
            "Shape the supplied axiom lattice into a field guide: verify inputs and "
            "preserve last-known-good data."
        )
        raw = contract_payload(
            prompt,
            lane="creation",
            target="verify inputs and preserve last-known-good data",
        )
        agent, client = self.make_agent([
            FakeResponse(json.dumps(raw)),
            FakeResponse("Field guide created from the supplied axioms."),
        ])

        result = agent.run(prompt)

        self.assertEqual(result.status, "complete")
        self.assertEqual(len(client.requests), 2)
        self.assertEqual(result.metrics["model_attempts"], 2)
        self.assertEqual(result.metrics["retries"], 0)

    def test_framed_source_answer_resumes_even_if_resolver_repeats_missing(self):
        original_prompt = "Shape these notes into a field guide."
        pending_contract = parse_task_contract(
            contract_payload(
                original_prompt,
                lane="creation",
                target=None,
            ),
            grounding_texts=[original_prompt],
        )
        conversation_id = self.memory.new_conversation("framed continuation")
        self.memory.begin_conversation_goal(
            conversation_id,
            original_prompt,
            "conversation",
            contract=pending_contract,
        )
        update = (
            "Use these notes: verify inputs; preserve last-known-good data."
        )
        repeated_missing = contract_payload(
            original_prompt,
            lane="creation",
            target=None,
            relation="continue",
        )
        repeated_missing["evidence_source"] = "none"
        agent, client = self.make_agent([
            FakeResponse(json.dumps(repeated_missing)),
            FakeResponse("Field guide created from the supplied notes."),
        ])

        result = agent.run(update, conversation_id=conversation_id)

        self.assertEqual(result.status, "complete")
        self.assertIn("supplied notes", str(result))
        self.assertEqual(len(client.requests), 2)
        self.assertEqual(result.metrics["model_attempts"], 2)
        self.assertEqual(result.metrics["retries"], 0)
        self.assertIsNone(self.memory.pending_conversation_goal(conversation_id))

    def test_unfamiliar_semantics_map_to_broad_lanes_without_phrase_rules(self):
        cases = (
            (
                "Map the current provenance of ceramic aerogel standards.",
                "research",
                "ceramic aerogel standards",
            ),
            (
                "Shape these notes into a field guide.",
                "creation",
                "field guide",
            ),
            (
                "Assess this telemetry for hidden drift.",
                "inspection",
                "telemetry",
            ),
            (
                "Send this signed brief to the review board.",
                "external_action",
                "review board",
            ),
        )
        responses = [
            FakeResponse(json.dumps(contract_payload(
                prompt, lane=lane, target=target,
            )))
            for prompt, lane, target in cases
        ]
        agent, client = self.make_agent(responses)
        route = Route("fast", "qwen3.5:9b", "quick/general task")

        for prompt, expected_lane, _target in cases:
            with self.subTest(prompt=prompt):
                result = agent._resolve_task_contract(
                    prompt,
                    conversation_id=1,
                    route=route,
                    recent_user_turns=(),
                )
                self.assertIsNotNone(result)
                self.assertEqual(result.lane, expected_lane)
        self.assertEqual(len(client.requests), len(cases))
        self.assertTrue(all(request["tools"] == [] for request in client.requests))

    def test_catalog_configuration_candidate_is_structural_and_catalog_derived(self):
        self.assertTrue(_may_request_feature_configuration(
            "What optional capabilities are configured?"
        ))
        self.assertTrue(_may_request_feature_configuration(
            "What optional features do you have?"
        ))
        self.assertTrue(_may_request_feature_configuration(
            "Are automatic home-network checks enabled?"
        ))
        self.assertTrue(_may_request_feature_configuration(
            "Disable automatic paired-Bluetooth checks."
        ))
        self.assertTrue(_may_request_feature_configuration("Turn Bluetooth on."))
        self.assertTrue(_may_request_feature_configuration(
            "Turn network monitoring off."
        ))
        self.assertTrue(_may_request_feature_configuration(
            "Set up the security popups."
        ))
        self.assertFalse(_may_request_feature_configuration(
            "What do you think about taking a walk?"
        ))
        self.assertFalse(_may_request_feature_configuration(
            "Do you think automatic home-network checks are useful?"
        ))
        for spec in FEATURE_SPECS:
            with self.subTest(capability_id=spec.capability_id):
                self.assertTrue(_may_request_feature_configuration(
                    f"Is {spec.title} configured?"
                ))

    def test_configuration_contract_exposes_only_bounded_catalog_tools(self):
        prompt = "What optional capabilities are configured?"
        contract = parse_task_contract(
            contract_payload(
                prompt,
                lane="configuration",
                target="optional capabilities",
            ),
            grounding_texts=[prompt],
        )
        config = replace(
            self.config,
            root=self.temp_dir,
            autonomy="full",
            network_access="disabled",
            bluetooth_access="disabled",
        )
        client = ContractCapableClient([
            FakeResponse(tool_calls=[{
                "function": {
                    "name": "feature_setup_status",
                    "arguments": {},
                }
            }]),
            FakeResponse("I checked the bounded optional-capability catalog."),
        ])
        agent = Agent(
            config,
            self.memory,
            lambda _event: None,
            client=client,
            coding_review=False,
            coding_planning=False,
        )

        with patch.object(agent, "_resolve_task_contract", return_value=contract) as resolver:
            result = agent.run(prompt)

        self.assertEqual(result.status, "complete")
        self.assertIn("bounded optional-capability", str(result))
        resolver.assert_called_once()
        offered = {
            item["function"]["name"] for item in client.requests[0]["tools"]
        }
        self.assertEqual(offered, FEATURE_SETUP_READ_TOOLS)
        self.assertNotIn("feature_setup_decide", offered)
        self.assertFalse(config.network_access == "private-lan")
        self.assertEqual(config.bluetooth_access, "disabled")

        ordinary = {
            item["function"]["name"]
            for item in agent._schemas_for_state(
                research_mode=False,
                web_tainted=False,
                local_tainted=False,
                allow_write=False,
                allow_execution=False,
                allow_memory_write=False,
            )
        }
        self.assertTrue(FEATURE_SETUP_TOOLS.isdisjoint(ordinary))

        writable = {
            item["function"]["name"]
            for item in agent._schemas_for_state(
                research_mode=False,
                web_tainted=False,
                local_tainted=False,
                allow_write=False,
                allow_execution=False,
                allow_memory_write=False,
                allow_feature_setup=True,
                allow_feature_setup_write=True,
            )
            if item["function"]["name"] in FEATURE_SETUP_TOOLS
        }
        self.assertEqual(writable, FEATURE_SETUP_TOOLS)

    def test_skip_and_disable_reduce_feature_authority_without_approval(self):
        config = replace(
            self.config,
            root=self.temp_dir,
            autonomy="full",
            network_access="disabled",
            bluetooth_access="disabled",
        )
        agent = Agent(
            config,
            self.memory,
            lambda _event: None,
            client=ContractCapableClient(()),
            coding_review=False,
            coding_planning=False,
        )

        for decision in ("skip", "disable"):
            with self.subTest(decision=decision):
                outcome = json.loads(agent.toolbox.execute(
                    "feature_setup_decide",
                    {
                        "capability_id": "bluetooth-monitoring",
                        "decision": decision,
                    },
                ))
                self.assertTrue(outcome["ok"])
                self.assertFalse(outcome.get("approval_required", False))

    def test_deterministic_clarifiers_run_before_semantic_resolver(self):
        cases = (
            ("what's the weather?", "city or ZIP code"),
            ("research", "What topic or question"),
            ("do it", "What should I use or continue"),
            ("Can you set it up?", "What should I use or continue"),
        )
        for prompt, expected in cases:
            with self.subTest(prompt=prompt):
                agent, client = self.make_agent()
                with patch.object(
                    agent,
                    "_resolve_task_contract",
                    side_effect=AssertionError("resolver must not run"),
                ) as resolver:
                    result = agent.run(prompt)
                self.assertIn(expected, str(result))
                resolver.assert_not_called()
                self.assertEqual(client.requests, [])

    def test_clear_dialogue_skips_resolver_and_uses_one_answer_call(self):
        cases = (
            "How are we doing so far today?",
            "Tell me what you think makes an old workshop feel alive.",
            "Hey Jarvis, how are you doing today?",
            "I've been sketching and gardening today.",
            "For this fictional story, Mara is captain and Jules is engineer. Acknowledge briefly.",
            "If she asks him to inspect its engine, who is asking whom?",
            "Without looking anything up, list the captain and engineer by name.",
            "In exactly two sentences, explain zero trust to a home user.",
            "Sounds good.",
            "That makes sense.",
            "Keep it concise and scientific.",
            "Make that friendlier.",
            "A dog is a lot of work.",
            "Zero trust is confusing.",
            "How much storage does this game need?",
            "How much RAM should a gaming PC have?",
            "How much RAM should my computer have?",
            "How does Python memory management work?",
            "How many hard drives should a NAS have?",
        )
        for prompt in cases:
            with self.subTest(prompt=prompt):
                agent, client = self.make_agent([
                    FakeResponse("We're doing well so far.")
                ])
                with patch.object(
                    agent,
                    "_resolve_task_contract",
                    side_effect=AssertionError(
                        "clear dialogue must not spend a resolver call"
                    ),
                ) as resolver, patch.object(
                    agent,
                    "_queue_automatic_specialist_consultation",
                    side_effect=AssertionError(
                        "clear dialogue must not queue a specialist"
                    ),
                ) as specialist:
                    result = agent.run(prompt)

                self.assertEqual(str(result), "We're doing well so far.")
                resolver.assert_not_called()
                specialist.assert_not_called()
                self.assertEqual(len(client.requests), 1)
                self.assertEqual(client.requests[0]["model"], self.config.fast_model)
                self.assertEqual(client.requests[0]["tools"], [])
                self.assertFalse(client.requests[0]["think"])
                self.assertEqual(result.metrics["model_attempts"], 1)
                self.assertEqual(result.metrics["retries"], 0)

    def test_operational_and_ambiguous_requests_do_not_claim_fast_dialogue(self):
        for prompt in (
            "List my installed applications.",
            "Can you set it up?",
            "Open Photoshop.",
            "Research current Python releases.",
            "Create a PDF report.",
            "Show my scheduled tasks.",
            "How much disk space do I have?",
            "How many hard drives are installed?",
            "What apps are running right now?",
            "What is my CPU temperature?",
            "How much RAM is free?",
            "Is companion mode on?",
            "Which programs are currently open?",
            "Which apps do I have installed?",
            "Do I have enough disk space for this game?",
        ):
            with self.subTest(prompt=prompt):
                self.assertFalse(_is_clear_tool_free_dialogue(prompt))

    def test_weather_clarification_uses_only_immediately_preceding_assistant_turn(self):
        clarification = {
            "role": "assistant",
            "content": "What city or ZIP code should I use for the weather?",
        }
        self.assertEqual(
            _weather_clarification_location("10001", [clarification]),
            "ZIP 10001",
        )
        self.assertIsNone(_weather_clarification_location(
            "10001",
            [
                clarification,
                {"role": "user", "content": "Tell me about dogs instead."},
                {"role": "assistant", "content": "Dogs can be great companions."},
            ],
        ))
        self.assertIsNone(_weather_clarification_location(
            "10001",
            [clarification, {"role": "user", "content": "One more thing."}],
        ))

    def test_resolver_failure_preserves_pending_goal_without_second_model_call(self):
        original_prompt = "Assess the supplied telemetry for the anomaly."
        pending_contract = parse_task_contract(
            contract_payload(
                original_prompt,
                lane="inspection",
                target="telemetry",
                missing_inputs=[{"key": "capture"}],
            ),
            grounding_texts=[original_prompt],
        )
        conversation_id = self.memory.new_conversation("pending")
        goal_id = self.memory.begin_conversation_goal(
            conversation_id,
            original_prompt,
            "file_ops",
            contract=pending_contract,
        )
        agent, client = self.make_agent()

        with patch.object(agent, "_resolve_task_contract", return_value=None) as resolver:
            result = agent.run("the midnight capture", conversation_id=conversation_id)

        pending = self.memory.pending_conversation_goal(conversation_id)
        self.assertIsNotNone(pending)
        self.assertEqual(pending["id"], goal_id)
        self.assertIn("continues it, replaces it, or cancels it", str(result))
        resolver.assert_called_once()
        self.assertEqual(client.requests, [])

    def test_malformed_stored_pending_contract_fails_before_provider_call(self):
        agent, client = self.make_agent()
        result = agent._resolve_task_contract(
            "continue with it",
            conversation_id=1,
            route=Route("fast", "qwen3.5:9b", "quick/general task"),
            recent_user_turns=(),
            pending_goal={
                "id": 7,
                "goal_text": "original goal",
                "context": [],
                "contract": {"version": 999},
            },
        )
        self.assertIsNone(result)
        self.assertEqual(client.requests, [])

    def test_cancel_only_reports_success_for_the_exact_pending_version(self):
        original_prompt = "Assess the supplied telemetry for the anomaly."
        pending_contract = parse_task_contract(
            contract_payload(
                original_prompt,
                lane="inspection",
                target="telemetry",
                missing_inputs=[{"key": "capture"}],
            ),
            grounding_texts=[original_prompt],
        )
        cancel_contract = parse_task_contract(
            contract_payload(
                "cancel it",
                lane="dialogue",
                target="it",
                relation="cancel",
            ),
            grounding_texts=["cancel it", original_prompt],
            has_pending_goal=True,
        )
        conversation_id = self.memory.new_conversation("cancel")
        self.memory.begin_conversation_goal(
            conversation_id,
            original_prompt,
            "file_ops",
            contract=pending_contract,
        )
        original = self.memory.pending_conversation_goal(conversation_id)
        self.assertIsNotNone(original)
        agent, _client = self.make_agent()

        with patch.object(agent, "_resolve_task_contract", return_value=cancel_contract):
            with patch.object(
                self.memory,
                "cancel_conversation_goal_if_current",
                return_value=False,
            ) as cancel_if_current:
                result = agent.run("cancel it", conversation_id=conversation_id)

        self.assertNotIn("Okay - I cancelled", str(result))
        self.assertIn("pending state changed", str(result))
        self.assertIsNotNone(self.memory.pending_conversation_goal(conversation_id))
        cancel_if_current.assert_called_once_with(
            int(original["id"]),
            conversation_id,
            str(original["updated_at"]),
        )

    def test_atomic_cancel_closes_the_exact_pending_goal(self):
        original_prompt = "Assess the supplied telemetry for the anomaly."
        pending_contract = parse_task_contract(
            contract_payload(
                original_prompt,
                lane="inspection",
                target="telemetry",
                missing_inputs=[{"key": "capture"}],
            ),
            grounding_texts=[original_prompt],
        )
        cancel_contract = parse_task_contract(
            contract_payload(
                "cancel it",
                lane="dialogue",
                target="it",
                relation="cancel",
            ),
            grounding_texts=["cancel it", original_prompt],
            has_pending_goal=True,
        )
        conversation_id = self.memory.new_conversation("cancel exact")
        self.memory.begin_conversation_goal(
            conversation_id,
            original_prompt,
            "file_ops",
            contract=pending_contract,
        )
        agent, _client = self.make_agent()

        with patch.object(agent, "_resolve_task_contract", return_value=cancel_contract):
            result = agent.run("cancel it", conversation_id=conversation_id)

        self.assertEqual(str(result), "Okay - I cancelled that pending task.")
        self.assertIsNone(self.memory.pending_conversation_goal(conversation_id))

    def test_contract_cannot_expose_external_tools_or_bypass_approval_gate(self):
        prompt = "Send this signed brief to the review board."
        client = ContractCapableClient()
        agent = Agent(
            replace(
                self.config,
                execution_mode="trusted-host",
                external_access="trusted-external",
            ),
            self.memory,
            lambda _event: None,
            client=client,
            coding_review=False,
            coding_planning=False,
        )
        external = parse_task_contract(
            contract_payload(
                prompt,
                lane="external_action",
                target="review board",
            ),
            grounding_texts=[prompt],
        )

        # A semantic contract is not an argument to the hard schema gate. Even
        # an external-action classification cannot expose mutation tools unless
        # the independently computed runtime authority allows them.
        self.assertEqual(external.requested_effect, "external")
        schemas = agent._schemas_for_state(
            research_mode=False,
            web_tainted=False,
            local_tainted=False,
            allow_write=False,
            allow_execution=False,
            allow_memory_write=False,
            allow_external_mutation=False,
        )
        names = {
            str(item.get("function", {}).get("name") or "")
            for item in schemas
        }
        self.assertNotIn("github_push", names)

        # Even if the runtime separately exposes the family, the sensitive
        # action still stops at the centralized exact-resource approval gate.
        with patch.object(
            agent.toolbox,
            "_effective_approval_arguments",
            side_effect=lambda _name, arguments: dict(arguments),
        ):
            with agent.toolbox.approval_context("conversation:1"):
                result = json.loads(agent.toolbox.execute(
                    "github_push",
                    {"path": ".", "branch": "main"},
                ))
        self.assertFalse(result["ok"])
        self.assertTrue(result["approval_required"])
        self.assertIsInstance(result["approval_id"], int)

    def test_contract_lane_alone_cannot_expose_read_write_execute_or_external_tools(self):
        prompt = "Interpret the unfamiliar cobalt lattice note."
        contracts = (
            parse_task_contract(
                contract_payload(prompt, lane="research", target="cobalt lattice note"),
                grounding_texts=[prompt],
            ),
            parse_task_contract(
                {
                    **contract_payload(
                        prompt,
                        lane="inspection",
                        target="cobalt lattice note",
                    ),
                    "evidence_source": "workspace",
                },
                grounding_texts=[prompt],
            ),
            parse_task_contract(
                {
                    **contract_payload(
                        prompt,
                        lane="inspection",
                        target="cobalt lattice note",
                    ),
                    "evidence_source": "computer",
                },
                grounding_texts=[prompt],
            ),
            parse_task_contract(
                {
                    **contract_payload(prompt, lane="creation", target="cobalt lattice note"),
                    "evidence_source": "none",
                },
                grounding_texts=[prompt],
            ),
            parse_task_contract(
                {
                    **contract_payload(prompt, lane="creation", target="cobalt lattice note"),
                    "evidence_source": "none",
                    "requested_effect": "execute",
                },
                grounding_texts=[prompt],
            ),
            parse_task_contract(
                contract_payload(
                    prompt,
                    lane="external_action",
                    target="cobalt lattice note",
                ),
                grounding_texts=[prompt],
            ),
        )
        forbidden = {
            "web_search", "web_fetch", "list_files", "read_file", "write_file",
            "edit_file", "run_process", "github_push", "google_drive_upload",
            "computer_list_files", "computer_read_file", "computer_storage_report",
        }
        for contract in contracts:
            with self.subTest(lane=contract.lane):
                agent, client = self.make_agent([FakeResponse("Bounded response.")])
                with patch.object(agent, "_resolve_task_contract", return_value=contract):
                    result = agent.run(prompt)
                self.assertEqual(result.status, "complete")
                exposed = {
                    str(item.get("function", {}).get("name") or "")
                    for item in client.requests[0]["tools"]
                }
                self.assertFalse(exposed.intersection(forbidden), exposed)

    def test_model_selected_cancel_without_explicit_language_fails_closed(self):
        original_prompt = "Assess the supplied telemetry for the anomaly."
        pending_contract = parse_task_contract(
            contract_payload(
                original_prompt,
                lane="inspection",
                target="telemetry",
                missing_inputs=[{"key": "capture"}],
            ),
            grounding_texts=[original_prompt],
        )
        conversation_id = self.memory.new_conversation("false cancellation")
        self.memory.begin_conversation_goal(
            conversation_id,
            original_prompt,
            "file_ops",
            contract=pending_contract,
        )
        raw_cancel = contract_payload(
            "thanks",
            lane="dialogue",
            target=None,
            relation="cancel",
        )
        agent, client = self.make_agent([FakeResponse(json.dumps(raw_cancel))])

        result = agent._resolve_task_contract(
            "thanks",
            conversation_id=conversation_id,
            route=Route("fast", "qwen3.5:9b", "quick/general task"),
            recent_user_turns=[original_prompt],
            pending_goal=self.memory.pending_conversation_goal(conversation_id),
        )

        self.assertIsNone(result)
        self.assertEqual(len(client.requests), 1)
        self.assertIsNotNone(self.memory.pending_conversation_goal(conversation_id))


if __name__ == "__main__":
    unittest.main()
