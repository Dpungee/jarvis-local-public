from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from jarvis.skill_library import (
    create_learned_skill,
    list_available_skills,
    list_builtin_skills,
    read_available_skill,
    read_builtin_skill,
    update_learned_skill,
)


class SkillLibraryTests(unittest.TestCase):
    def test_expert_catalog_is_progressive_and_bounded(self):
        catalog = list_builtin_skills()
        names = {item["name"] for item in catalog}
        self.assertEqual(names, {
            "capability-engineering",
            "browser-web-operations",
            "computer-use-operator",
            "cyber-defense-analyst",
            "document-generation",
            "evidence-research",
            "local-ai-engineering",
            "long-running-operations",
            "network-engineering",
            "safety-reliability",
            "software-engineering",
            "task-orchestration",
            "tool-integration",
        })
        self.assertTrue(all("content" not in item for item in catalog))
        for item in catalog:
            self.assertLessEqual(len(item["description"]), 300)

        cyber = read_builtin_skill("cyber-defense-analyst")
        self.assertIn("competing hypotheses", cyber["content"])
        self.assertIn("Iterative defensive lab", cyber["content"])
        self.assertIn("every observed bypass as a reproducible regression test", cyber["content"])
        self.assertIn("Verification", cyber["content"])
        self.assertEqual(cyber["trust"], "operator-bundled reference guidance")
        self.assertRegex(cyber["sha256"], r"^[0-9a-f]{64}$")

        capability = read_builtin_skill("capability-engineering")
        self.assertIn("connector_validate", capability["content"])
        self.assertIn("Verification", capability["content"])

        documents = read_builtin_skill("document-generation")
        self.assertIn("PowerPoint (.pptx)", documents["content"])
        self.assertIn("Word (.docx)", documents["content"])
        self.assertIn("Excel (.xlsx)", documents["content"])
        self.assertIn("PDF (.pdf)", documents["content"])
        self.assertIn("Never claim a document was created", documents["content"])

    def test_skill_names_cannot_escape_the_catalog(self):
        for name in ("../SOUL.md", "CYBER", "", "network_engineering"):
            with self.subTest(name=name), self.assertRaises((ValueError, KeyError)):
                read_builtin_skill(name)

    def test_workspace_learned_skill_is_discoverable_but_untrusted(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            learned = workspace / ".jarvis-skills" / "agent-benchmarking"
            learned.mkdir(parents=True)
            learned.joinpath("SKILL.md").write_text(
                "---\nname: agent-benchmarking\n"
                "description: Compare agent harnesses and turn verified gaps into improvements.\n"
                "version: 1.0.0\n---\n# Method\nVerify sources and test each adopted pattern.\n",
                encoding="utf-8",
            )

            catalog = list_available_skills(workspace)
            self.assertIn("agent-benchmarking", {item["name"] for item in catalog})
            skill = read_available_skill("agent-benchmarking", workspace)
            self.assertEqual(skill["origin"], "workspace-learned")
            self.assertIn("untrusted reference data", skill["trust"])
            self.assertIn("test each adopted pattern", skill["content"])

    def test_learned_skill_create_update_requires_exact_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            created = create_learned_skill(
                workspace,
                "release-review",
                "Review a release artifact and verify its evidence.",
                "# Workflow\n\n1. Inspect the artifact.\n2. Verify the result.\n",
            )
            self.assertTrue(created["created"])
            self.assertRegex(created["sha256"], r"^[0-9a-f]{64}$")
            self.assertIn("release-review", {
                item["name"] for item in list_available_skills(workspace)
            })

            updated = update_learned_skill(
                workspace,
                "release-review",
                created["sha256"],
                "Review releases using exact build and test evidence.",
                "# Workflow\n\n1. Inspect the artifact.\n2. Verify build and test evidence.\n",
            )
            self.assertTrue(updated["updated"])
            self.assertNotEqual(updated["sha256"], created["sha256"])
            self.assertIn("build and test evidence", updated["content"])
            with self.assertRaises(RuntimeError):
                update_learned_skill(
                    workspace,
                    "release-review",
                    created["sha256"],
                    updated["description"],
                    updated["content"],
                )

    def test_learned_skill_creation_refuses_escape_secrets_and_bundled_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            cases = (
                ("../escape", "Safe description", "# Workflow\nInspect."),
                ("secret-skill", "Safe description", "# Workflow\napi_key=hunter2"),
                ("software-engineering", "Replacement", "# Workflow\nReplace it."),
            )
            for name, description, content in cases:
                with self.subTest(name=name), self.assertRaises((ValueError, PermissionError)):
                    create_learned_skill(workspace, name, description, content)


if __name__ == "__main__":
    unittest.main()
