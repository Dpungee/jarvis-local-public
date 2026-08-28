from __future__ import annotations

import os
import json
import shutil
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from jarvis.agent import (
    Agent,
    _contextual_missing_tool_target,
    _explicit_skill_references,
    _is_capability_acquisition,
    _is_iterative_defensive_lab_task,
    _is_skill_library_mutation,
    _matching_offered_capabilities,
    _requires_coding,
    _requires_web,
    _task_family,
)
from jarvis.config import Config
from jarvis.memory import Memory
from jarvis.security_expertise import classify_security_expertise
from jarvis.task_contract import TaskContract
from tests.test_agent import FakeResponse, FakeToolBox, ScriptedClient, tool_call


TEMP_ROOT = Path(__file__).resolve().parent / ".tmp"
TEMP_ROOT.mkdir(exist_ok=True)


class CapabilityToolBox(FakeToolBox):
    NAMES = (
        *FakeToolBox.NAMES,
        "tool_catalog",
        "tool_create",
        "connector_list",
        "connector_describe",
        "connector_validate",
        "connector_install",
        "connector_call",
        "skill_list",
        "skill_read",
        "skill_create",
        "skill_update",
        "computer_write_file",
        "schedule_create",
        "detect_project",
        "install_project_dependencies",
        "start_process",
        "process_status",
        "process_logs",
        "stop_process",
        "http_health",
        "make_directory",
        "copy_path",
        "move_path",
        "trash_path",
    )


class GitHubSkillSyncToolBox(FakeToolBox):
    NAMES = (*FakeToolBox.NAMES, "skill_list", "skill_github_sync")

    def execute(self, name, arguments):
        if name == "skill_github_sync":
            self.calls.append((name, arguments))
            return json.dumps({
                "ok": True,
                "result": {
                    "repository": arguments["repository"],
                    "commit": "a" * 40,
                    "imported": [{"name": "handoff", "sha256": "b" * 64}],
                    "existing": [],
                    "skipped": [],
                    "next_offset": None,
                    "complete": True,
                },
            })
        return super().execute(name, arguments)


class AgentCapabilityRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.test_dir = TEMP_ROOT / f"agent-capabilities-{os.getpid()}-{self._testMethodName}"
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir)
        self.workspace = self.test_dir / "workspace"
        self.data_dir = self.test_dir / "data"
        self.workspace.mkdir(parents=True)
        self.data_dir.mkdir()
        self.config = replace(
            Config.load(),
            model="auto",
            workspace=self.workspace,
            data_dir=self.data_dir,
            vault_dir=None,
            max_steps=8,
            context_length=4096,
            execution_mode="trusted-host",
            autonomy="autonomous",
            computer_access="disabled",
        )
        self.memory = Memory(self.data_dir / "agent.db")

    def tearDown(self) -> None:
        self.memory.close()
        resolved = self.test_dir.resolve()
        self.assertEqual(resolved.parent, TEMP_ROOT.resolve())
        shutil.rmtree(resolved)

    def _run_one_tool(self, prompt: str, name: str, arguments: dict[str, object]):
        toolbox = CapabilityToolBox()
        client = ScriptedClient([
            FakeResponse(tool_calls=[tool_call(name, arguments)]),
            FakeResponse(content=f"Completed {name}."),
        ])
        agent = Agent(
            self.config,
            self.memory,
            client=client,
            coding_review=True,
            coding_planning=True,
        )
        agent.toolbox = toolbox
        result = agent.run(prompt)
        return result, toolbox, client

    @staticmethod
    def _family(prompt: str) -> str:
        requires_coding = _requires_coding(prompt)
        requires_web = _requires_web(prompt)
        return _task_family(
            prompt,
            casual_greeting=False,
            learning_task=Agent._is_learning_task(prompt),
            deep_research_task=Agent._is_deep_research_task(prompt),
            requires_coding=requires_coding,
            requires_web=requires_web,
            allow_external_mutation=False,
            allow_computer_files=False,
            security_task=classify_security_expertise(prompt).active,
        )

    def test_calibration_routine_examples_reach_the_claimed_families(self) -> None:
        self.assertEqual(
            self._family("Summarize what you did today."),
            "conversation",
        )
        self.assertFalse(_requires_web("Summarize what you did today."))
        self.assertEqual(
            self._family("Create a notes file summarizing local LLM tuning."),
            "file_ops",
        )
        self.assertFalse(_requires_coding(
            "Create a notes file summarizing local LLM tuning."
        ))
        self.assertEqual(
            self._family("Write tests for the util module in your workspace."),
            "code_test",
        )
        self.assertEqual(
            self._family(
                "Research current local-LLM quantization tradeoffs and cite sources."
            ),
            "deep_research",
        )

    def test_casual_personal_topics_stay_an_ordinary_conversation(self) -> None:
        prompts = (
            "I may practice guitar this afternoon; what do you think?",
            "Would it be a good idea to stretch today?",
            "Let's discuss a study routine.",
            "What do you think of houseplants?",
            "What's your opinion on sketching?",
            "Do you enjoy board games?",
            "What do you think about self-paced learning?",
            "I was considering journaling today; what do you think?",
        )
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                self.assertFalse(_requires_web(prompt))
                self.assertEqual(self._family(prompt), "conversation")

        self.assertTrue(_requires_web("Retrieve the current forecast for ZIP code 10001."))
        self.assertTrue(_requires_web("What is the current Bitcoin crypto price?"))
        self.assertTrue(_requires_web("What do you think about today's news?"))
        self.assertTrue(_requires_web(
            "Research current dog nutrition guidance and cite sources."
        ))
        self.assertTrue(_requires_web(
            "Check Example Artist's official live-event listings and tell me whether any "
            "future performance is announced."
        ))

    def test_non_code_notes_request_exposes_write_without_coding_gates(self) -> None:
        prompt = "Create a notes file summarizing local LLM tuning."
        arguments = {
            "path": "notes/local-llm-tuning.md",
            "content": "# Local LLM tuning\n",
        }
        result, toolbox, client = self._run_one_tool(prompt, "write_file", arguments)

        first_tools = {
            schema["function"]["name"] for schema in client.requests[0]["tools"]
        }
        self.assertIn("write_file", first_tools)
        self.assertEqual(toolbox.calls, [("write_file", arguments)])
        self.assertEqual(result.status, "complete")
        self.assertNotIn("read-only coding reconnaissance", str(client.requests))

    def test_direct_managed_process_requests_expose_the_full_lifecycle_without_coding_gates(self) -> None:
        cases = (
            (
                "Run the existing Python process app.py.",
                "run_process",
                {"program": "python", "arguments": ["app.py"]},
            ),
            (
                "Start the existing server process.",
                "start_process",
                {"program": "python", "arguments": ["server.py"], "name": "server"},
            ),
            (
                "Check the status of the server process.",
                "process_status",
                {"process_id": "123456789abc"},
            ),
            (
                "Show logs for the server process.",
                "process_logs",
                {"process_id": "123456789abc", "stream": "both"},
            ),
            (
                "Stop the server process.",
                "stop_process",
                {"process_id": "123456789abc"},
            ),
        )
        lifecycle = {
            "run_process",
            "install_project_dependencies",
            "start_process",
            "process_status",
            "process_logs",
            "stop_process",
            "http_health",
        }
        for prompt, tool_name, arguments in cases:
            with self.subTest(tool=tool_name):
                self.assertFalse(_requires_coding(prompt))
                result, toolbox, client = self._run_one_tool(prompt, tool_name, arguments)
                first_tools = {
                    schema["function"]["name"] for schema in client.requests[0]["tools"]
                }
                self.assertTrue(lifecycle.issubset(first_tools))
                self.assertEqual(toolbox.calls, [(tool_name, arguments)])
                self.assertEqual(result.status, "complete")
                self.assertEqual(result.tool_calls, 1)
                self.assertEqual(len(client.requests), 2)
                serialized = str(client.requests)
                self.assertNotIn("read-only coding reconnaissance", serialized)
                self.assertNotIn("Runtime acceptance check failed", serialized)

    def test_structural_file_requests_expose_mutations_without_coding_completion_gates(self) -> None:
        cases = (
            (
                "Make a new directory called archives.",
                "make_directory",
                {"path": "archives"},
            ),
            (
                "Copy the file notes.txt to archives/notes.txt.",
                "copy_path",
                {"source": "notes.txt", "destination": "archives/notes.txt"},
            ),
            (
                "Move the file notes.txt to archives/notes.txt.",
                "move_path",
                {"source": "notes.txt", "destination": "archives/notes.txt"},
            ),
            (
                "Trash the file notes.txt.",
                "trash_path",
                {"path": "notes.txt"},
            ),
        )
        structural = {"make_directory", "copy_path", "move_path", "trash_path"}
        for prompt, tool_name, arguments in cases:
            with self.subTest(tool=tool_name):
                self.assertFalse(_requires_coding(prompt))
                result, toolbox, client = self._run_one_tool(prompt, tool_name, arguments)
                first_tools = {
                    schema["function"]["name"] for schema in client.requests[0]["tools"]
                }
                self.assertTrue(structural.issubset(first_tools))
                self.assertEqual(toolbox.calls, [(tool_name, arguments)])
                self.assertEqual(result.status, "complete")
                self.assertEqual(result.tool_calls, 1)
                self.assertEqual(len(client.requests), 2)
                serialized = str(client.requests)
                self.assertNotIn("read-only coding reconnaissance", serialized)
                self.assertNotIn("Runtime acceptance check failed", serialized)

    def test_learn_competitor_skills_is_an_executable_capability_build(self) -> None:
        prompt = (
            "Research established software-agent frameworks, identify capabilities "
            "missing from this runtime, and build verified reusable skills to close the gaps."
        )
        self.assertTrue(_is_capability_acquisition(prompt))
        self.assertTrue(_requires_coding(prompt))
        self.assertTrue(Agent._is_deep_research_task(prompt))

    def test_explicit_tool_creation_is_capability_work_not_dialogue(self) -> None:
        positives = (
            "Create a tool that formats workspace release notes.",
            "Build that missing tool for me.",
            "Develop a reusable integration for this workflow.",
        )
        for prompt in positives:
            with self.subTest(prompt=prompt):
                self.assertTrue(_is_capability_acquisition(prompt))
                self.assertTrue(_requires_coding(prompt))
        self.assertFalse(_is_capability_acquisition(
            "Explain what a software tool is in one sentence."
        ))

    def test_tool_creation_followup_is_grounded_in_prior_operator_request(self) -> None:
        original = "Upload the finished report to Example Service."
        messages = [
            {"role": "user", "content": original},
            {
                "role": "assistant",
                "content": (
                    "I can't complete that because the required connector tool is unavailable."
                ),
            },
        ]

        recovered = _contextual_missing_tool_target(
            "Can you create that tool?", messages
        )

        self.assertIsNotNone(recovered)
        self.assertIn(original, recovered)
        self.assertIn("Search the configured tool catalog first", recovered)
        self.assertIsNone(_contextual_missing_tool_target(
            "Can you create that tool?",
            [
                {"role": "user", "content": original},
                {"role": "assistant", "content": "The upload completed successfully."},
            ],
        ))

    def test_capability_task_exposes_full_authorized_creation_surface(self) -> None:
        toolbox = CapabilityToolBox()
        client = ScriptedClient([
            FakeResponse(content="No verified artifact was created yet.")
            for _ in range(12)
        ])
        agent = Agent(
            self.config,
            self.memory,
            client=client,
            coding_review=False,
            coding_planning=False,
        )
        agent.toolbox = toolbox

        result = agent.run(
            "Create a reusable tool that formats workspace release notes."
        )

        self.assertEqual(result.status, "incomplete")
        work_request = next(
            request for request in client.requests
            if any(
                item["function"]["name"] == "tool_catalog"
                for item in request["tools"]
            )
        )
        offered = {
            item["function"]["name"] for item in work_request["tools"]
        }
        expected = {
            "tool_catalog", "tool_create", "connector_list", "connector_describe",
            "connector_validate", "connector_install", "skill_create",
            "write_file", "run_process",
        }
        self.assertTrue(expected.issubset(offered), expected - offered)
        self.assertNotIn("connector_call", offered)
        self.assertNotIn("computer_write_file", offered)
        self.assertNotIn("schedule_create", offered)
        serialized = json.dumps(work_request["messages"])
        self.assertIn("Call tool_catalog first", serialized)
        self.assertIn("call tool_create", serialized)

    def test_unverified_missing_tool_claim_retries_the_already_offered_tool(self) -> None:
        toolbox = CapabilityToolBox()
        arguments = {
            "path": "notes/local-llm-tuning.md",
            "content": "# Local LLM tuning\n",
        }
        client = ScriptedClient([
            FakeResponse(content=(
                "I can't complete this because the required file-writing tool is unavailable."
            )),
            FakeResponse(tool_calls=[tool_call("write_file", arguments)]),
            FakeResponse(content="Created the requested notes file."),
        ])
        events: list[str] = []
        agent = Agent(
            self.config,
            self.memory,
            events.append,
            client=client,
            coding_review=False,
            coding_planning=False,
        )
        agent.toolbox = toolbox

        result = agent.run("Create a notes file summarizing local LLM tuning.")

        self.assertEqual(result.status, "complete")
        self.assertIn(
            "capability claim contradicted - retrying an already offered tool",
            events,
        )
        recovered = {
            item["function"]["name"] for item in client.requests[1]["tools"]
        }
        self.assertIn("write_file", recovered)
        self.assertNotIn("tool_catalog", recovered)
        self.assertNotIn("tool_create", recovered)
        self.assertEqual(toolbox.calls, [("write_file", arguments)])
        self.assertEqual(events.count(
            "capability claim contradicted - retrying an already offered tool"
        ), 1)

    def test_genuine_missing_capability_keeps_bounded_catalog_recovery(self) -> None:
        toolbox = CapabilityToolBox()
        client = ScriptedClient([
            FakeResponse(content=(
                "I can't complete this because the desktop-interaction tool is unavailable."
            )),
            *[
                FakeResponse(content="No verified desktop action was completed.")
                for _ in range(12)
            ],
        ])
        events: list[str] = []
        agent = Agent(
            self.config,
            self.memory,
            events.append,
            client=client,
            coding_review=False,
            coding_planning=False,
        )
        agent.toolbox = toolbox

        result = agent.run(
            "Use my keyboard and mouse to interact with the current desktop app."
        )

        self.assertEqual(result.status, "incomplete")
        self.assertIn(
            "capability claim unverified - searching configured tools before stopping",
            events,
        )
        recovered = {
            item["function"]["name"] for item in client.requests[1]["tools"]
        }
        self.assertIn("tool_catalog", recovered)
        self.assertIn("tool_create", recovered)

    def test_offered_capability_matching_uses_live_schema_not_exact_prompt_phrases(self) -> None:
        schemas = [{
            "type": "function",
            "function": {
                "name": "windows_open_apps",
                "description": (
                    "List bounded executable names that currently own visible top-level "
                    "Windows application windows."
                ),
                "parameters": {"type": "object", "properties": {}},
            },
        }, {
            "type": "function",
            "function": {
                "name": "tool_catalog",
                "description": "Search for capabilities.",
                "parameters": {"type": "object", "properties": {}},
            },
        }]

        for prompt in (
            "Which programs are currently open?",
            "Tell me what visible desktop applications are running at the moment.",
        ):
            with self.subTest(prompt=prompt):
                matches = _matching_offered_capabilities(
                    prompt,
                    "I do not have a tool that can list those programs.",
                    schemas,
                )
                self.assertEqual(matches, ("windows_open_apps",))

    def test_simple_inspection_stops_after_one_no_progress_recovery(self) -> None:
        prompt = "Read README.md and summarize it."
        contract = TaskContract(
            version=1,
            relation="new",
            lane="inspection",
            artifact_kind="none",
            evidence_source="workspace",
            requested_effect="read",
            goal=prompt,
            target="README.md",
            constraint_quotes=(),
            missing_inputs=(),
            acceptance=("answer",),
        )
        toolbox = CapabilityToolBox()
        client = ScriptedClient([
            FakeResponse(content="I can't continue because file-reading tools are unavailable."),
            FakeResponse(content="I still cannot read the file because the tool is unavailable."),
        ])
        events: list[str] = []
        agent = Agent(
            self.config,
            self.memory,
            events.append,
            client=client,
            coding_review=False,
            coding_planning=False,
        )
        agent.toolbox = toolbox

        with patch.object(agent, "_resolve_task_contract", return_value=contract):
            result = agent.run(prompt)

        self.assertEqual(result.status, "incomplete")
        self.assertEqual(len(client.requests), 2)
        second_tools = {
            item["function"]["name"] for item in client.requests[1]["tools"]
        }
        self.assertIn("read_file", second_tools)
        self.assertNotIn("tool_catalog", second_tools)
        self.assertIn("bounded correction", str(result))

    def test_owned_iterative_firewall_lab_is_an_executable_defensive_build(self) -> None:
        prompt = (
            "Become an expert in defensive cybersecurity by building an isolated firewall "
            "simulation lab, adversarially testing it with an authorized regression corpus, "
            "hardening every discovered bypass, and repeating the cycle."
        )
        self.assertTrue(_is_iterative_defensive_lab_task(prompt))
        self.assertTrue(_requires_coding(prompt))
        self.assertTrue(Agent._is_deep_research_task(prompt))

        self.assertFalse(_is_iterative_defensive_lab_task(
            "Break into a stranger's production firewall."
        ))
        self.assertFalse(_requires_coding(
            "Break into a stranger's production firewall."
        ))

        agent = Agent(self.config, self.memory, client=ScriptedClient([]))
        agent.toolbox = CapabilityToolBox()
        agent.set_specialist("cybersecurity")
        locked = {
            item["function"]["name"] for item in agent._schemas_for_state(
                research_mode=False,
                web_tainted=False,
                local_tainted=False,
                allow_write=False,
                allow_execution=False,
                allow_memory_write=False,
            )
        }
        unlocked = {
            item["function"]["name"] for item in agent._schemas_for_state(
                research_mode=False,
                web_tainted=False,
                local_tainted=False,
                allow_write=True,
                allow_execution=True,
                allow_memory_write=False,
            )
        }
        self.assertTrue({"write_file", "edit_file", "run_process"}.isdisjoint(locked))
        self.assertTrue({"write_file", "edit_file", "run_process"}.issubset(unlocked))

    def test_extending_an_existing_security_lab_is_coding_work(self) -> None:
        self.assertTrue(_requires_coding(
            "Extend the existing firewall_lab project with two historical simulator "
            "versions and a regression report."
        ))

    def test_explicit_skill_library_change_has_a_bounded_authoring_gate(self) -> None:
        prompt = "Install the referenced capability definitions in the local skill library."
        self.assertTrue(_is_capability_acquisition(prompt))
        self.assertTrue(_is_skill_library_mutation(prompt))
        self.assertTrue(_requires_coding(prompt))

        toolbox = CapabilityToolBox()
        toolbox.schemas.extend([
            {"type": "function", "function": {"name": "skill_create", "parameters": {}}},
            {"type": "function", "function": {"name": "skill_update", "parameters": {}}},
            {"type": "function", "function": {"name": "skill_github_sync", "parameters": {}}},
        ])
        agent = Agent(self.config, self.memory, client=ScriptedClient([]))
        agent.toolbox = toolbox
        denied = {
            item["function"]["name"] for item in agent._schemas_for_state(
                research_mode=False,
                web_tainted=False,
                local_tainted=False,
                allow_write=True,
                allow_execution=True,
                allow_memory_write=False,
                allow_skill_write=False,
            )
        }
        allowed = {
            item["function"]["name"] for item in agent._schemas_for_state(
                research_mode=False,
                web_tainted=False,
                local_tainted=False,
                allow_write=True,
                allow_execution=True,
                allow_memory_write=False,
                allow_skill_write=True,
            )
        }
        skill_writes = {"skill_create", "skill_update", "skill_github_sync"}
        self.assertTrue(skill_writes.isdisjoint(denied))
        self.assertTrue(skill_writes.issubset(allowed))

    def test_agent_creates_and_rereads_skill_before_reporting_completion(self) -> None:
        client = ScriptedClient([
            FakeResponse(tool_calls=[tool_call("skill_list", {})]),
            FakeResponse(tool_calls=[tool_call("skill_create", {
                "name": "release-auditing",
                "description": "Audit release evidence before reporting completion.",
                "instructions": "# Workflow\n\n1. Inspect evidence.\n2. Verify the release.\n",
            })]),
            FakeResponse(tool_calls=[tool_call(
                "skill_read", {"name": "release-auditing"}
            )]),
        ])
        agent = Agent(
            self.config,
            self.memory,
            client=client,
            coding_review=True,
            coding_planning=True,
        )

        result = agent.run("Add release auditing to your skill library.")

        self.assertEqual(result.status, "complete")
        self.assertEqual(result.tool_calls, 3)
        learned = self.workspace / ".jarvis-skills" / "release-auditing" / "SKILL.md"
        self.assertTrue(learned.is_file())
        self.assertIn("Verify the release", learned.read_text(encoding="utf-8"))
        offered = {
            schema["function"]["name"] for schema in client.requests[0]["tools"]
        }
        self.assertTrue({"skill_create", "skill_update"}.issubset(offered))

    def test_agent_can_sync_official_github_skills_without_research_loop(self) -> None:
        client = ScriptedClient([
            FakeResponse(tool_calls=[tool_call("skill_github_sync", {
                "repository": "openclaw/openclaw",
            })]),
        ])
        toolbox = GitHubSkillSyncToolBox()
        agent = Agent(
            self.config,
            self.memory,
            client=client,
            coding_review=True,
            coding_planning=True,
        )
        agent.toolbox = toolbox

        result = agent.run(
            "Add every missing skill from the official OpenClaw GitHub repository to the local skill library."
        )

        self.assertEqual(result.status, "complete")
        self.assertEqual(result.tool_calls, 1)
        self.assertEqual(toolbox.calls, [(
            "skill_github_sync", {"repository": "openclaw/openclaw"}
        )])
        first_tools = {
            item["function"]["name"] for item in client.requests[0]["tools"]
        }
        self.assertIn("skill_github_sync", first_tools)
        serialized = json.dumps(client.requests[0]["messages"])
        self.assertIn("openclaw/openclaw", serialized)

    def test_explicit_skill_reference_loads_verified_guidance_without_a_tool_call(self) -> None:
        client = ScriptedClient([
            FakeResponse(content="Use separate VLANs and verify the policy path."),
        ])
        agent = Agent(self.config, self.memory, client=client)

        result = agent.run(
            "$network-engineering explain how I should validate VLAN segmentation."
        )

        self.assertEqual(result.status, "complete")
        self.assertEqual(result.tool_calls, 0)
        serialized = json.dumps(client.requests[0]["messages"])
        self.assertIn("untrusted_explicit_skills", serialized)
        self.assertIn("network-engineering", serialized)
        self.assertIn("Trace end to end", serialized)

    def test_explicit_skill_reference_fails_visibly_and_does_not_treat_env_as_skill(self) -> None:
        self.assertEqual(_explicit_skill_references("Use $HOME and $PATH."), [])
        self.assertEqual(
            _explicit_skill_references("Build an invoice tracker for a $120 job."),
            [],
        )
        self.assertEqual(
            _explicit_skill_references(r"Use \\$network-engineering literally."),
            [],
        )
        client = ScriptedClient([])
        agent = Agent(self.config, self.memory, client=client)

        result = agent.run("$missing-skill do this workflow.")

        self.assertEqual(result.status, "incomplete")
        self.assertIn("is not installed", result.reason)
        self.assertEqual(client.requests, [])


if __name__ == "__main__":
    unittest.main()
