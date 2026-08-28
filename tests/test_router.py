import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from jarvis.config import Config
from jarvis.router import ModelRouter, Route


class RouterTests(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.gettempdir()) / "jarvis-router-test"
        self.config = Config(
            root=root,
            workspace=root / "workspace",
            data_dir=root / "data",
            soul_path=root / "SOUL.md",
            model="auto",
            fast_model="qwen3.5:9b",
            reasoning_model="gpt-oss:20b",
            coding_model="qwen3-coder:30b",
            ollama_url="http://127.0.0.1:11434",
            ollama_api_key=None,
            max_steps=20,
            context_length=16384,
            command_timeout=120,
            autonomy="autonomous",
        )
        models = ["qwen3.5:9b", "gpt-oss:20b", "qwen3-coder:30b"]
        self.router = ModelRouter(self.config, models)

    def test_routes_simple_task_to_fast_model(self):
        self.assertEqual(self.router.select("Hello!").profile, "fast")

    def test_negative_file_constraint_does_not_force_coding_model(self):
        prompt = "Inspect current PC health. Do not create or modify files."
        self.assertEqual(self.router.select(prompt).profile, "fast")

    def test_routes_research_planning_to_reasoning_model(self):
        route = self.router.select("Research and compare the latest battery technology with sources")
        self.assertEqual(route.model, "gpt-oss:20b")

    def test_routes_coding_to_coder(self):
        route = self.router.select("Build a Python API and add integration tests")
        self.assertEqual(route.model, "qwen3-coder:30b")

    def test_normal_explanations_and_current_queries_stay_fast(self):
        prompts = (
            "What is Python?",
            "Plan a simple dinner",
            "What is the current time?",
            "Compare 2 and 3",
        )
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                self.assertEqual(self.router.select(prompt).profile, "fast")

    def test_human_conversation_never_pays_reasoning_or_coding_latency(self):
        prompts = (
            "Good morning, ready to work?",
            "what do you think of community gardens",
            "I may practice guitar this afternoon; what do you think?",
            "I spent the morning sketching and gardening",
            "can you fix my grammar in this sentence",
        )
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                route = self.router.select(prompt)
                self.assertEqual(route.profile, "fast")

    def test_explicit_deep_analysis_uses_reasoning(self):
        route = self.router.select("Do a rigorous deep analysis of these trade-offs")
        self.assertEqual(route.model, "gpt-oss:20b")

    def test_cybersecurity_and_network_engineering_use_deep_specialist(self):
        prompts = (
            "Triage this SIEM alert and build an incident response plan",
            "Analyze these firewall rules for lateral-movement risk",
            "Diagnose asymmetric routing between these two VLANs",
            "Design IPv6 subnetting and OSPF for a redundant campus network",
            "Interpret this PCAP and explain the TCP retransmissions",
            "Provide expert defensive cybersecurity and network analysis",
            "Improve defensive cybersecurity coverage for our systems",
            "Is there anything suspicious on my network?",
        )
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                route = self.router.select(prompt)
                self.assertEqual(route.profile, "deep")
                self.assertIn("specialist task", route.reason)

    def test_unrelated_uses_of_security_and_network_do_not_trigger_specialist(self):
        prompts = (
            "How much is the security deposit for an apartment?",
            "Write a social network marketing slogan",
            "Explain a neural network simply",
            "I am networking with people at a conference",
        )
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                self.assertEqual(self.router.select(prompt).profile, "fast")

    def test_multi_skill_reasoning_rubric_uses_reasoning(self):
        route = self.router.select(
            "Under a strict deterministic rubric, calculate a weighted percentage, "
            "find the LCM, and identify edge cases."
        )
        self.assertEqual(route.model, "gpt-oss:20b")

    def test_no_tools_evaluation_overrides_incidental_coding_language(self):
        route = self.router.select(
            "Under a strict deterministic rubric, do not use tools. Return only valid JSON. "
            "One item asks what to do when fixing an unfamiliar repository with tests."
        )
        self.assertEqual(route.profile, "reasoning")
        self.assertEqual(route.model, "gpt-oss:20b")

    def test_manual_override(self):
        self.assertEqual(self.router.select("Hello", "coding").profile, "coding")

    def test_deep_profile_is_manual_except_for_security_and_network_specialists(self):
        automatic = self.router.select("Do a rigorous deep analysis of this architecture")
        manual = self.router.select("Analyze this architecture", "deep")

        self.assertEqual(automatic.profile, "reasoning")
        self.assertEqual(manual.profile, "deep")
        self.assertEqual(manual.model, "qwen3-coder:30b")

    def test_openai_tiers_route_by_task_role(self):
        config = replace(
            self.config,
            fast_model="openai:gpt-5.6-luna",
            reasoning_model="openai:gpt-5.6-terra",
            coding_model="openai:gpt-5.6-sol",
            deep_model="openai:gpt-5.6-sol",
        )
        router = ModelRouter(
            config,
            [
                "openai:gpt-5.6-luna",
                "openai:gpt-5.6-terra",
                "openai:gpt-5.6-sol",
            ],
        )

        self.assertEqual(router.select("Hello").model, "openai:gpt-5.6-luna")
        self.assertEqual(
            router.select("Research and compare current battery systems with sources").model,
            "openai:gpt-5.6-terra",
        )
        self.assertEqual(
            router.select("Build a Python API and test it").model,
            "openai:gpt-5.6-sol",
        )
        self.assertEqual(
            router.select("Triage this SIEM alert and design containment").model,
            "openai:gpt-5.6-sol",
        )

    def test_manual_cloud_model_is_available_when_provider_is_configured(self):
        router = ModelRouter(
            self.config,
            ["qwen3.5:9b", "openai:gpt-5.6", "anthropic:claude-sonnet-5"],
        )
        openai = router.select("Analyze this", "openai:gpt-5.6-terra")
        anthropic = router.select("Review this", "anthropic:claude-opus-5")
        self.assertEqual(openai.model, "openai:gpt-5.6-terra")
        self.assertEqual(anthropic.model, "anthropic:claude-opus-5")

    def test_manual_cloud_model_requires_its_provider_key(self):
        router = ModelRouter(self.config, ["qwen3.5:9b", "openai:gpt-5.6"])
        with self.assertRaisesRegex(ValueError, "not installed"):
            router.select("Review this", "anthropic:claude-sonnet-5")

    def test_codex_subscription_model_routes_when_cli_is_configured(self):
        config = replace(
            self.config,
            fast_model="codex-cli:auto",
            reasoning_model="codex-cli:auto",
            coding_model="codex-cli:auto",
            deep_model="codex-cli:auto",
        )
        router = ModelRouter(config, ["codex-cli:auto"])
        self.assertEqual(router.select("Hello").model, "codex-cli:auto")
        self.assertEqual(
            router.select("Review this", "codex-cli:gpt-5.5").model,
            "codex-cli:gpt-5.5",
        )

    def test_image_uses_configured_codex_cli_vision_transport(self):
        config = replace(
            self.config,
            fast_model="codex-cli:auto",
            reasoning_model="openai:gpt-5.6-terra",
            coding_model="qwen3-coder:30b",
            deep_model="anthropic:claude-sonnet-5",
        )
        router = ModelRouter(
            config,
            ["codex-cli:auto", "qwen3-coder:30b", "openai:gpt-5.6-terra"],
        )

        route = router.select("What is wrong here?", requires_vision=True)

        self.assertEqual(route.model, "codex-cli:auto")
        self.assertIn("vision-capable", route.reason)

    def test_image_fails_clearly_without_vision_provider(self):
        router = ModelRouter(self.config, ["qwen3.5:9b"])

        with self.assertRaisesRegex(ValueError, "image input requires a configured vision model"):
            router.select("Look at this", requires_vision=True)

    def test_missing_specialist_falls_back(self):
        router = ModelRouter(self.config, ["qwen3.5:9b"])
        route = router.select("Research current developments in distributed systems")
        self.assertEqual(route.model, "qwen3.5:9b")
        self.assertTrue(route.fallback)

    def test_failover_candidates_include_every_distinct_ready_model(self):
        config = replace(
            self.config,
            fast_model="openai:gpt-5.6-luna",
            reasoning_model="openai:gpt-5.6-terra",
            coding_model="openai:gpt-5.6-sol",
            deep_model="openai:gpt-5.6-sol",
        )
        router = ModelRouter(
            config,
            [
                "openai:gpt-5.6",
                "openai:gpt-5.6-luna",
                "openai:gpt-5.6-terra",
                "openai:gpt-5.6-sol",
            ],
        )

        candidates = router.failover_candidates(
            Route("custom", "openai:gpt-5.6", "manual"),
            "request failed",
        )

        self.assertEqual(
            [item.model for item in candidates],
            [
                "openai:gpt-5.6-terra",
                "openai:gpt-5.6-sol",
                "openai:gpt-5.6-luna",
            ],
        )

    def test_escalates_fast_coding_failure(self):
        current = self.router.select("Hello")
        route = self.router.escalate(current, "Fix this Python application")
        self.assertEqual(route.profile, "coding")


if __name__ == "__main__":
    unittest.main()
