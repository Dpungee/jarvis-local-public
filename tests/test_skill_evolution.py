"""The skill-distillation module, after M4 removed its runtime call path.

``distill_verified_skill`` is no longer reachable from any runtime module: it
survives as the owner of ``_skill_content``, the template
``learning_ladder.build_staged_document`` composes with, and as a seeding
helper for these tests.  The Agent-driven tests that used to live here moved
to ``tests/test_agent_learning_ladder.py`` (surface) when the agent-side
distiller call was removed, and the file-tool probe moved to
``tests/test_tools_hardening.py`` beside the live root's identical probe;
design 8.1 records the split.
"""
from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jarvis import cli
from jarvis.config import Config
from jarvis.skill_evolution import (
    auto_skill_name,
    distill_verified_skill,
    matching_auto_distilled_skills,
)
from jarvis.skill_library import create_learned_skill, read_available_skill


class SkillEvolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.data = self.root / "data"
        self.workspace.mkdir()
        self.data.mkdir()
        self.config = replace(
            Config.load(),
            workspace=self.workspace,
            data_dir=self.data,
            vault_dir=None,
            model="auto",
            fast_model="qwen3.5:9b",
            reasoning_model="gpt-oss:20b",
            coding_model="qwen3-coder:30b",
            ollama_preload=False,
            memory_embeddings="disabled",
        )

    def test_distilled_content_is_redacted_bounded_and_path_safe(self) -> None:
        secret = "sk-proj-" + "A" * 32
        created = distill_verified_skill(
            self.workspace,
            family="file_ops",
            successful_tools={"read_file", f"api_key={secret}"},
            verification=f"token={secret}",
        )
        self.assertNotIn(secret, created["content"])
        self.assertIn("[REDACTED]", created["content"])
        self.assertLessEqual(
            (self.workspace / ".jarvis-skills" / created["name"] / "SKILL.md").stat().st_size,
            32 * 1024,
        )
        with self.assertRaises(ValueError):
            auto_skill_name("../escape")
        with self.assertRaises(ValueError):
            create_learned_skill(
                self.workspace,
                "oversized",
                "Oversized guidance.",
                "x" * (33 * 1024),
            )
        self.assertFalse((self.root / "escape").exists())

    def test_the_template_is_unchanged_when_no_staging_context_is_given(self) -> None:
        """M4 added an optional ``staging`` argument; the old shape must not move."""
        created = distill_verified_skill(
            self.workspace,
            family="code_fix",
            successful_tools={"read_file", "run_tests"},
            verification="tool_success",
        )
        self.assertIn("Tools observed: read_file, run_tests", created["content"])
        self.assertIn("Verified outcomes incorporated: 1", created["content"])
        self.assertNotIn("Tools sampled from", created["content"])
        self.assertNotIn("Ledger at staging", created["content"])
        self.assertNotIn("Calibration at staging", created["content"])

    def test_refinement_still_merges_observed_tools_in_place(self) -> None:
        distill_verified_skill(
            self.workspace, family="code_fix",
            successful_tools={"read_file"}, verification="tool_success",
        )
        refined = distill_verified_skill(
            self.workspace, family="code_fix",
            successful_tools={"run_tests"}, verification="process_evidence",
        )
        self.assertEqual(refined["verified_outcomes"], 2)
        self.assertIn("Tools observed: read_file, run_tests", refined["content"])
        self.assertIn(
            "Verification oracles observed: process_evidence, tool_success",
            refined["content"],
        )

    def test_matching_auto_distilled_skills_is_bounded_and_family_scoped(self) -> None:
        distill_verified_skill(
            self.workspace, family="code_fix",
            successful_tools={"read_file"}, verification="tool_success",
        )
        distill_verified_skill(
            self.workspace, family="file_ops",
            successful_tools={"read_file"}, verification="tool_success",
        )
        create_learned_skill(
            self.workspace, "hand-written", "Operator guidance.", "Body text.",
        )
        matches = matching_auto_distilled_skills(self.workspace, "code_fix")
        self.assertEqual([item["name"] for item in matches], ["learned-code-fix"])
        self.assertEqual(
            matching_auto_distilled_skills(self.workspace, "code_fix", limit=0), []
        )
        self.assertEqual(
            matching_auto_distilled_skills(self.workspace, "security_analysis"), []
        )
        with self.assertRaises(ValueError):
            matching_auto_distilled_skills(self.workspace, "Not A Family")
        self.assertTrue(
            read_available_skill("hand-written", self.workspace)["auto_distilled"]
            is False
        )

    def test_no_runtime_module_calls_the_distiller(self) -> None:
        """Design 7.12: after M4 the only promotion path is the ladder's.

        Skips, loudly, while surface has yet to remove the agent-side call
        (design 8.1 day 2); it can never pass with an unexpected caller.
        """
        package = Path(__file__).resolve().parent.parent / "jarvis"
        allowed = {"skill_evolution.py", "learning_ladder.py"}
        callers = sorted(
            path.name
            for path in package.glob("*.py")
            if path.name not in allowed
            and "distill_verified_skill" in path.read_text(encoding="utf-8")
        )
        if callers == ["agent.py"]:
            self.skipTest(
                "surface has not yet removed the agent-side distiller call "
                "(design 8.1 day 2, H-2)"
            )
        self.assertEqual(callers, [])

    def test_cli_lists_shows_and_forgets_only_learned_skill(self) -> None:
        distill_verified_skill(
            self.workspace,
            family="file_ops",
            successful_tools={"read_file", "write_file"},
            verification="tool_success",
        )
        output = io.StringIO()
        with patch.object(cli.Config, "load", return_value=self.config), redirect_stdout(output):
            self.assertEqual(
                cli._run_skill(SimpleNamespace(skill_command="list")), 0
            )
            self.assertEqual(
                cli._run_skill(
                    SimpleNamespace(skill_command="show", name="learned-file-ops")
                ),
                0,
            )
            self.assertEqual(
                cli._run_skill(
                    SimpleNamespace(skill_command="forget", name="learned-file-ops")
                ),
                0,
            )
        rendered = output.getvalue()
        self.assertIn("auto-distilled family=file_ops", rendered)
        self.assertIn("verified outcomes: 1", rendered)
        self.assertIn("Forgot learned skill: learned-file-ops", rendered)
        self.assertFalse(
            self.workspace.joinpath(".jarvis-skills", "learned-file-ops").exists()
        )


if __name__ == "__main__":
    unittest.main()
