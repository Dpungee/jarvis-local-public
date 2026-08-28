from __future__ import annotations

import hashlib
import os
import shutil
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import jarvis.config as config_module
from jarvis.agent import Agent
from jarvis.config import (
    MAX_CONSTITUTION_BYTES,
    MAX_SOUL_BYTES,
    Config,
    load_constitution,
    load_soul,
)
from jarvis.desktop import resolve_computer_path
from jarvis.memory import Memory
from jarvis.tools import ToolBox
from tests.test_agent import FakeResponse, ScriptedClient


TEMP_ROOT = Path(__file__).resolve().parent / ".tmp"
TEMP_ROOT.mkdir(exist_ok=True)


class ConstitutionRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.test_dir = TEMP_ROOT / (
            f"constitution-{os.getpid()}-{self._testMethodName}"
        )
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir)
        self.workspace = self.test_dir / "workspace"
        self.data_dir = self.test_dir / "data"
        self.profile = self.test_dir / "profile"
        for directory in (self.workspace, self.data_dir, self.profile):
            directory.mkdir(parents=True, exist_ok=True)
        self.constitution = self.test_dir / "CONSTITUTION.md"
        self.constitution.write_text(
            "# Test Constitution\n\nCONSTITUTION_ORDER_SENTINEL\n",
            encoding="utf-8",
        )
        self.soul = self.test_dir / "SOUL.md"
        self.soul.write_text("SOUL_ORDER_SENTINEL\n", encoding="utf-8")
        self.config = replace(
            Config.load(),
            model="auto",
            workspace=self.workspace,
            data_dir=self.data_dir,
            soul_path=self.soul,
            constitution_path=self.constitution,
            execution_mode="trusted-host",
            computer_access="trusted-desktop",
            computer_root=self.profile,
        )
        self.memory = Memory(self.data_dir / "constitution.db")

    def tearDown(self):
        self.memory.close()
        resolved = self.test_dir.resolve()
        self.assertEqual(resolved.parent, TEMP_ROOT.resolve())
        shutil.rmtree(resolved)

    def test_prompt_order_is_contract_then_constitution_then_soul(self):
        client = ScriptedClient([FakeResponse(content="ok")])
        agent = Agent(self.config, self.memory, client=client)
        agent.run("Explain how you operate")
        system = client.requests[0]["messages"][0]["content"]
        self.assertLess(system.index("## Enforced runtime contract"), system.index(
            "CONSTITUTION_ORDER_SENTINEL"
        ))
        self.assertLess(system.index("CONSTITUTION_ORDER_SENTINEL"), system.index(
            "SOUL_ORDER_SENTINEL"
        ))
        self.assertIn(self.config.constitution_sha256, system)

    def test_hash_matches_exact_file_bytes(self):
        content, digest = load_constitution(self.constitution)
        raw = self.constitution.read_bytes()
        self.assertEqual(content.encode("utf-8"), raw)
        self.assertEqual(digest, hashlib.sha256(raw).hexdigest())
        self.assertEqual(self.config.constitution_sha256, digest)

    def test_loader_rejects_missing_oversize_and_link_files(self):
        with self.assertRaisesRegex(ValueError, "missing"):
            load_constitution(self.test_dir / "missing.md")
        oversized = self.test_dir / "oversized.md"
        oversized.write_bytes(b"x" * (MAX_CONSTITUTION_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "exceeds"):
            load_constitution(oversized)
        link = self.test_dir / "constitution-link.md"
        try:
            link.symlink_to(self.constitution)
        except OSError:
            self.skipTest("Creating a symlink is unavailable for this Windows user")
        with self.assertRaisesRegex(ValueError, "non-link"):
            load_constitution(link)

    def test_soul_loader_rejects_oversize_and_link_files(self):
        oversized = self.test_dir / "oversized-soul.md"
        oversized.write_bytes(b"x" * (MAX_SOUL_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "exceeds"):
            load_soul(oversized)
        link = self.test_dir / "soul-link.md"
        try:
            link.symlink_to(self.soul)
        except OSError:
            self.skipTest("Creating a symlink is unavailable for this Windows user")
        with self.assertRaisesRegex(ValueError, "non-link"):
            load_soul(link)

    def test_config_override_and_location_rules_fail_closed(self):
        custom = self.test_dir / "custom-constitution.md"
        custom.write_text("custom constitution\n", encoding="utf-8")
        with (
            patch.object(config_module, "ROOT", self.test_dir),
            patch.dict(
                os.environ,
                {
                    "JARVIS_WORKSPACE": str(self.workspace),
                    "JARVIS_DATA": str(self.data_dir),
                    "JARVIS_SOUL": str(self.soul),
                    "JARVIS_CONSTITUTION": str(custom),
                },
                clear=True,
            ),
        ):
            loaded = Config.load()
        self.assertEqual(loaded.constitution_path, custom.resolve())

        inside_workspace = self.workspace / "constitution-custom.md"
        inside_workspace.write_text("inside workspace\n", encoding="utf-8")
        with (
            patch.object(config_module, "ROOT", self.test_dir),
            patch.dict(
                os.environ,
                {
                    "JARVIS_WORKSPACE": str(self.workspace),
                    "JARVIS_DATA": str(self.data_dir),
                    "JARVIS_SOUL": str(self.soul),
                    "JARVIS_CONSTITUTION": str(inside_workspace),
                },
                clear=True,
            ),
            self.assertRaisesRegex(ValueError, "must stay outside"),
        ):
            Config.load()

    def test_workspace_and_desktop_boundaries_protect_control_files(self):
        toolbox = ToolBox(self.config, self.memory)
        protected = (
            "CONSTITUTION.md",
            "SOUL.md",
            "policy.py",
            "evaluation_cases.jsonl",
            "promotion-gate.json",
        )
        for path in protected:
            with self.subTest(boundary="workspace", path=path):
                with self.assertRaises(PermissionError):
                    toolbox.write_file(path, "blocked")
            with self.subTest(boundary="desktop", path=path):
                with self.assertRaises(PermissionError):
                    resolve_computer_path(self.profile, path)


if __name__ == "__main__":
    unittest.main()
