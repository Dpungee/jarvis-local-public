from __future__ import annotations

import io
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jarvis import cli
from jarvis.agent import Agent, AgentResult
from jarvis.config import Config
from jarvis.memory import Memory
from jarvis.skill_evolution import (
    auto_skill_name,
    distill_verified_skill,
    matching_auto_distilled_skills,
)
from jarvis.skill_library import (
    create_learned_skill,
    list_available_skills,
    read_available_skill,
)
from jarvis.tools import ToolBox


class _NoModelClient:
    def models(self, refresh: bool = False):
        del refresh
        return ["qwen3.5:9b", "gpt-oss:20b", "qwen3-coder:30b"]


class SkillEvolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
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
        self.memory = Memory(self.data / "jarvis.db")
        self.agent = Agent(
            self.config,
            self.memory,
            client=_NoModelClient(),
            coding_review=False,
            coding_planning=False,
        )

    def tearDown(self) -> None:
        self.memory.close()
        self.temporary.cleanup()

    def _seed_calibration(self, family: str, count: int = 19) -> None:
        for _index in range(count):
            prediction = self.memory.record_prediction(
                family=family,
                profile="coding",
                model="test-model",
                predicted_success=0.9,
                predicted_steps=3,
                predicted_verification="process_evidence",
            )
            self.memory.resolve_prediction(
                prediction,
                actual_status="complete",
                actual_steps=2,
                evidence_ok=True,
            )

    def _resolve_agent_outcome(
        self,
        family: str,
        *,
        verified: bool | None,
    ) -> None:
        prediction = self.memory.record_prediction(
            family=family,
            profile="coding",
            model="test-model",
            predicted_success=0.9,
            predicted_steps=3,
            predicted_verification=(
                "not_applicable" if verified is None else "process_evidence"
            ),
        )
        self.agent._active_prediction_id = prediction
        self.agent._active_prediction_family = family
        self.agent._active_prediction_origin = "interactive"
        self.agent._active_prediction_verification = (
            "not_applicable" if verified is None else "process_evidence"
        )
        self.agent._active_prediction_tools = (
            {"read_file", "edit_file", "__verified_after_write__"}
            if verified is True
            else {"read_file", "edit_file"}
        )
        self.agent._resolve_active_prediction(
            AgentResult("verified result", status="complete", tool_calls=3), None
        )

    def test_calibrated_verified_success_creates_then_refines_one_skill(self) -> None:
        self._seed_calibration("code_fix")
        self._resolve_agent_outcome("code_fix", verified=True)

        first = read_available_skill("learned-code-fix", self.workspace)
        self.assertTrue(first["auto_distilled"])
        self.assertEqual(first["family"], "code_fix")
        self.assertEqual(first["verified_outcomes"], 1)
        self.assertIn("Verification oracles observed: process_evidence", first["content"])

        self._resolve_agent_outcome("code_fix", verified=True)
        second = read_available_skill("learned-code-fix", self.workspace)
        self.assertEqual(second["verified_outcomes"], 2)
        self.assertNotEqual(second["sha256"], first["sha256"])
        catalog = [
            item for item in list_available_skills(self.workspace)
            if item.get("auto_distilled") is True and item.get("family") == "code_fix"
        ]
        self.assertEqual([item["name"] for item in catalog], ["learned-code-fix"])

    def test_unverified_or_uncalibrated_outcomes_do_not_distill(self) -> None:
        with self.subTest("unverified"):
            self._seed_calibration("code_fix")
            self._resolve_agent_outcome("code_fix", verified=False)
            self.assertFalse((self.workspace / ".jarvis-skills").exists())

        with self.subTest("evidence-not-applicable"):
            self._resolve_agent_outcome("code_fix", verified=None)
            self.assertFalse((self.workspace / ".jarvis-skills").exists())

        other_workspace = self.root / "other-workspace"
        other_workspace.mkdir()
        other_memory = Memory(self.data / "other.db")
        try:
            other = Agent(
                replace(self.config, workspace=other_workspace),
                other_memory,
                client=_NoModelClient(),
                coding_review=False,
                coding_planning=False,
            )
            prediction = other_memory.record_prediction(
                family="code_build", profile="coding", model="m",
                predicted_success=0.9, predicted_steps=2,
                predicted_verification="process_evidence",
            )
            other._active_prediction_id = prediction
            other._active_prediction_family = "code_build"
            other._active_prediction_verification = "process_evidence"
            other._active_prediction_tools = {
                "write_file", "__verified_after_write__"
            }
            other._resolve_active_prediction(
                AgentResult("done", status="complete", tool_calls=2), None
            )
            self.assertFalse((other_workspace / ".jarvis-skills").exists())
        finally:
            other_memory.close()

    def test_distillation_failure_is_nonfatal(self) -> None:
        self._seed_calibration("code_fix")
        with patch(
            "jarvis.agent.distill_verified_skill",
            side_effect=RuntimeError("synthetic write failure"),
        ):
            self._resolve_agent_outcome("code_fix", verified=True)
        row = self.memory.db.execute(
            "SELECT actual_status, evidence_ok FROM task_predictions ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual((row["actual_status"], row["evidence_ok"]), ("complete", 1))

    def test_streaming_foreground_distillation_does_not_delay_result(self) -> None:
        self._seed_calibration("code_fix")
        entered = threading.Event()
        release = threading.Event()

        def slow_distillation(*_args, **_kwargs):
            entered.set()
            release.wait(timeout=2)

        self.agent._active_defer_skill_distillation = True
        with patch("jarvis.agent.distill_verified_skill", side_effect=slow_distillation):
            started = time.perf_counter()
            self._resolve_agent_outcome("code_fix", verified=True)
            elapsed = time.perf_counter() - started
            self.assertTrue(entered.wait(timeout=1))
            self.assertLess(elapsed, 0.5)
            release.set()

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

    def test_generic_file_tools_cannot_bypass_skill_provenance(self) -> None:
        created = distill_verified_skill(
            self.workspace,
            family="file_ops",
            successful_tools={"read_file", "write_file"},
            verification="tool_success",
        )
        path = f".jarvis-skills/{created['name']}/SKILL.md"
        toolbox = ToolBox(self.config, self.memory)
        operations = (
            lambda: toolbox.read_file(path),
            lambda: toolbox.write_file(path, "unverified replacement"),
            lambda: toolbox.edit_file(
                path,
                "Verified outcomes incorporated: 1",
                "Verified outcomes incorporated: 999",
                created["sha256"],
            ),
        )
        for operation in operations:
            with self.assertRaises(PermissionError):
                operation()
        verified = toolbox.skill_read(created["name"])
        self.assertEqual(verified["sha256"], created["sha256"])

    def test_same_family_skill_enters_context_only_after_calibration(self) -> None:
        distill_verified_skill(
            self.workspace,
            family="code_fix",
            successful_tools={"read_file", "edit_file", "run_process"},
            verification="process_evidence",
        )
        self._seed_calibration("code_fix", count=20)
        active = self.memory.record_prediction(
            family="code_fix", profile="coding", model="m",
            predicted_success=0.9, predicted_steps=2,
            predicted_verification="process_evidence",
        )
        self.agent._active_prediction_id = active
        prompt = self.agent.system_prompt("Fix the parser", task_family="code_fix")
        self.assertIn("<matched_learned_skills>", prompt)
        self.assertIn("learned-code-fix", prompt)

        blocked = self.agent.system_prompt(
            "Build a parser", task_family="code_build"
        )
        self.assertNotIn("learned-code-fix", blocked)
        self.assertEqual(
            [item["name"] for item in matching_auto_distilled_skills(
                self.workspace, "code_fix"
            )],
            ["learned-code-fix"],
        )

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
