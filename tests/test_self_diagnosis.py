from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import jarvis.self_diagnosis as diagnosis
from jarvis.memory import Memory


class SelfDiagnosisTests(unittest.TestCase):
    def test_selftest_environment_uses_isolated_home_and_drops_ambient_secrets(self):
        secrets = {
            "OPENAI_API_KEY": "sk-test-secret",
            "GITHUB_TOKEN": "ghp_test_secret_value",
            "VERCEL_TOKEN": "test-secret",
            "AWS_SECRET_ACCESS_KEY": "test-secret",
            "JARVIS_CONNECTOR_SOCIAL_TOKEN": "test-secret",
        }
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, secrets, clear=False
        ):
            environment = diagnosis._selftest_environment(Path(temporary))

        for key in secrets:
            self.assertNotIn(key, environment)
        self.assertEqual(environment["PYTHONNOUSERSITE"], "1")
        self.assertIn(str(Path(temporary).resolve()), environment["USERPROFILE"])

    def test_repair_draft_rejects_hardlinked_live_source_before_reading(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "source"
            data = Path(temporary) / "data"
            outside = Path(temporary) / "outside.py"
            (root / "jarvis").mkdir(parents=True)
            (root / "tests").mkdir()
            data.mkdir()
            outside.write_text("VALUE = 1\n", encoding="utf-8")
            os.link(outside, root / "jarvis" / "linked.py")
            config = SimpleNamespace(
                self_inspect="read-only", self_repair="propose", data_dir=data
            )
            with (
                Memory(data / "jarvis.db") as memory,
                patch.object(diagnosis, "SOURCE_ROOT", root),
            ):
                result = diagnosis.create_repair_draft(
                    config,
                    memory,
                    trigger="hardlink containment",
                    edits=[{
                        "path": "jarvis/linked.py",
                        "old_text": "VALUE = 1",
                        "new_text": "VALUE = 2",
                    }],
                )

        self.assertEqual(result["status"], "voided")
        self.assertIn("ordinary single-link", result["void_reason"])

    def test_repair_draft_voids_protected_files_and_proposes_only_verified_copy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "source"
            data = Path(temporary) / "data"
            (root / "jarvis").mkdir(parents=True)
            (root / "tests").mkdir()
            data.mkdir()
            (root / "jarvis" / "__init__.py").write_text("", encoding="utf-8")
            (root / "jarvis" / "helper.py").write_text("VALUE = 1\n", encoding="utf-8")
            (root / "tests" / "test_helper.py").write_text(
                "import unittest\nfrom jarvis.helper import VALUE\n"
                "class T(unittest.TestCase):\n    def test_value(self): self.assertEqual(VALUE, 2)\n",
                encoding="utf-8",
            )
            config = SimpleNamespace(
                self_inspect="read-only", self_repair="propose", data_dir=data
            )
            with (
                Memory(data / "jarvis.db") as memory,
                patch.object(diagnosis, "SOURCE_ROOT", root),
                patch.object(
                    diagnosis,
                    "_candidate_anchor_test",
                    return_value={"passed": True, "cases": ["anchor"]},
                ),
                patch.object(diagnosis, "_candidate_execution_available", return_value=True),
            ):
                voided = diagnosis.create_repair_draft(
                    config, memory, trigger="attempt gate change",
                    edits=[{"path": "jarvis/policy.py", "old_text": "x", "new_text": "y"}],
                )
                traversal = diagnosis.create_repair_draft(
                    config, memory, trigger="attempt path traversal",
                    edits=[{
                        "path": "jarvis/../../evil.py",
                        "old_text": "VALUE = 1",
                        "new_text": "VALUE = 2",
                    }],
                )
                proposed = diagnosis.create_repair_draft(
                    config, memory, trigger="failing helper test",
                    edits=[{
                        "path": "jarvis/helper.py",
                        "old_text": "VALUE = 1",
                        "new_text": "VALUE = 2",
                    }],
                    failing_tests=["tests.test_helper.T.test_value"],
                    timeout=60,
                )
                rows = memory.list_repair_proposals()

        self.assertEqual(voided["status"], "voided")
        self.assertIn("permanently immutable", voided["void_reason"])
        self.assertEqual(traversal["status"], "voided")
        self.assertIn("parent-directory", traversal["void_reason"])
        self.assertEqual(proposed["status"], "proposed")
        self.assertTrue(proposed["verification"]["passed"])
        self.assertTrue(proposed["verification"]["anchor_eval"]["passed"])
        self.assertFalse(proposed["apply_supported"])
        self.assertEqual({row["status"] for row in rows}, {"voided", "proposed"})
        proposed_row = next(row for row in rows if row["status"] == "proposed")
        self.assertEqual(proposed["diff_sha256"], proposed_row["diff_sha256"])

    def test_repair_rejects_parent_directory_traversal(self):
        # A path that escapes SOURCE_ROOT must be voided by the containment gate
        # with a PATH reason, not by a downstream read/test failure.
        for bad in (
            "jarvis/../../evil.py",
            "jarvis/../evil.py",
            "jarvis/subdir/../../../etc/passwd.py",
        ):
            reason = diagnosis._repair_path_reason(bad)
            self.assertIsNotNone(reason, f"{bad} should be rejected")
            self.assertIn("parent-directory", reason)

        # A normal module is still an allowed target.
        self.assertIsNone(diagnosis._repair_path_reason("jarvis/router.py"))

    def test_public_presence_security_boundary_is_immutable_to_self_repair(self):
        protected = (
            "jarvis/public_bridge.py",
            "jarvis/public_presence_service.py",
            "jarvis/public_presence_store.py",
            "jarvis/public_tools.py",
            "jarvis/moltbook_adapter.py",
        )
        for path in protected:
            with self.subTest(path=path):
                reason = diagnosis._repair_path_reason(path)
                self.assertIsNotNone(reason)
                self.assertIn("permanently immutable", reason)

    def test_repair_never_executes_model_authored_candidate_without_os_sandbox(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "source"
            data = Path(temporary) / "data"
            (root / "jarvis").mkdir(parents=True)
            (root / "tests").mkdir()
            data.mkdir()
            (root / "jarvis" / "helper.py").write_text("VALUE = 1\n", encoding="utf-8")
            config = SimpleNamespace(
                self_inspect="read-only", self_repair="propose", data_dir=data
            )
            with (
                Memory(data / "jarvis.db") as memory,
                patch.object(diagnosis, "SOURCE_ROOT", root),
                patch.object(diagnosis, "_candidate_test") as candidate_test,
                patch.object(diagnosis, "_candidate_anchor_test") as anchor_test,
            ):
                result = diagnosis.create_repair_draft(
                    config,
                    memory,
                    trigger="untrusted candidate",
                    edits=[{
                        "path": "jarvis/helper.py",
                        "old_text": "VALUE = 1",
                        "new_text": "VALUE = 2",
                    }],
                )

        self.assertEqual(result["status"], "voided")
        self.assertIn("OS sandbox", result["void_reason"])
        self.assertTrue(result["verification"]["execution_blocked"])
        candidate_test.assert_not_called()
        anchor_test.assert_not_called()

    def test_selftest_is_disabled_by_default_and_never_copies_live_state(self):
        with self.assertRaisesRegex(PermissionError, "disabled"):
            diagnosis.run_isolated_selftest(
                SimpleNamespace(self_inspect="disabled"), full=True
            )

        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source"
            (source / "jarvis").mkdir(parents=True)
            (source / "tests").mkdir()
            (source / "jarvis" / "__init__.py").write_text("", encoding="utf-8")
            (source / "jarvis" / "example.py").write_text(
                "VALUE = 7\n", encoding="utf-8"
            )
            (source / "tests" / "__init__.py").write_text("", encoding="utf-8")
            (source / "tests" / "test_example.py").write_text(
                "import unittest\n"
                "from jarvis.example import VALUE\n"
                "class ExampleTests(unittest.TestCase):\n"
                "    def test_value(self): self.assertEqual(VALUE, 7)\n",
                encoding="utf-8",
            )
            (source / ".env").write_text(
                "OPENAI_API_KEY=must-not-copy\n", encoding="utf-8"
            )
            with patch.object(diagnosis, "SOURCE_ROOT", source):
                result = diagnosis.run_isolated_selftest(
                    SimpleNamespace(self_inspect="read-only"), full=True
                )

        self.assertTrue(result["passed"])
        self.assertFalse(result["isolation"]["dotenv_copied"])
        self.assertFalse(result["isolation"]["provider_keys_inherited"])

    def test_fault_localization_ranks_imported_runtime_module(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            (source / "jarvis").mkdir()
            (source / "tests").mkdir()
            (source / "jarvis" / "foo.py").write_text("VALUE = 1\n", encoding="utf-8")
            (source / "tests" / "test_foo.py").write_text(
                "from jarvis.foo import VALUE\n", encoding="utf-8"
            )

            result = diagnosis.localize_failures(
                ["tests.test_foo.FooTests.test_broken"], source_root=source
            )

        self.assertEqual(result["mapped_failing_tests"], 1)
        self.assertEqual(result["suspect_modules"][0]["module"], "jarvis/foo.py")
        self.assertEqual(result["confidence"], 1.0)

    def test_phase_five_anchor_eval_runs_in_an_isolated_copy(self):
        result = diagnosis.run_isolated_selftest(
            SimpleNamespace(self_inspect="read-only"),
            anchors=True,
            timeout=120,
        )

        self.assertTrue(result["passed"], result["stderr"] or result["stdout"])
        self.assertTrue(result["anchors"])
        self.assertFalse(result["full"])
        self.assertFalse(result["isolation"]["provider_keys_inherited"])

    def test_isolated_copy_ignores_transient_and_private_runtime_directories(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            names = [
                ".tmp", "backups", "data", "workspace", "workspace-projects",
                "jarvis", "tests",
            ]
            for name in names:
                (root / name).mkdir()

            ignored = diagnosis._copy_ignore(str(root), names)

        self.assertTrue(
            {".tmp", "backups", "data", "workspace", "workspace-projects"}
            <= ignored
        )
        self.assertFalse({"jarvis", "tests"} & ignored)

    def test_repair_draft_is_voided_when_phase_five_anchor_regresses(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "source"
            data = Path(temporary) / "data"
            (root / "jarvis").mkdir(parents=True)
            (root / "tests").mkdir()
            data.mkdir()
            (root / "jarvis" / "helper.py").write_text(
                "VALUE = 1\n", encoding="utf-8"
            )
            config = SimpleNamespace(
                self_inspect="read-only", self_repair="propose", data_dir=data
            )
            with (
                Memory(data / "jarvis.db") as memory,
                patch.object(diagnosis, "SOURCE_ROOT", root),
                patch.object(
                    diagnosis,
                    "_candidate_test",
                    return_value={"passed": True, "failing_test_ids": []},
                ),
                patch.object(
                    diagnosis,
                    "_candidate_anchor_test",
                    return_value={
                        "passed": False,
                        "cases": ["immutable-anchor"],
                        "failing_test_ids": ["immutable-anchor"],
                    },
                ),
                patch.object(diagnosis, "_candidate_execution_available", return_value=True),
            ):
                result = diagnosis.create_repair_draft(
                    config,
                    memory,
                    trigger="anchor regression",
                    edits=[{
                        "path": "jarvis/helper.py",
                        "old_text": "VALUE = 1",
                        "new_text": "VALUE = 2",
                    }],
                )

        self.assertEqual(result["status"], "voided")
        self.assertIn("anchor evaluation", result["void_reason"])
        self.assertFalse(result["verification"]["passed"])

    def test_capability_canaries_never_invoke_sensitive_tools(self):
        class FakeToolBox:
            def __init__(self):
                self.tools = {"list_files": object(), "github_push": object()}
                self.calls = []

            def execute(self, name, arguments):
                self.calls.append((name, arguments))
                return json.dumps({"ok": True, "result": []})

        with tempfile.TemporaryDirectory() as temporary:
            toolbox = FakeToolBox()
            config = SimpleNamespace(
                workspace=Path(temporary), execution_mode="disabled", autonomy="readonly"
            )
            with patch.object(diagnosis, "ToolBox", return_value=toolbox):
                results = diagnosis.run_capability_canaries(config, SimpleNamespace())

        self.assertEqual([name for name, _arguments in toolbox.calls], ["list_files"])
        by_tool = {item["tool"]: item for item in results}
        self.assertEqual(by_tool["list_files"]["status"], "pass")
        self.assertEqual(by_tool["github_push"]["status"], "skip")

    def test_recovery_test_attests_restart_lease_approval_and_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            workspace = root / "workspace"
            data.mkdir()
            workspace.mkdir()
            config = SimpleNamespace(data_dir=data, workspace=workspace)
            with Memory(data / "jarvis.db") as memory:
                result = diagnosis.run_recovery_test(config, memory)
                latest = memory.latest_recovery_attestation()

        self.assertTrue(result["passed"])
        self.assertTrue(all(result["checks"].values()))
        self.assertEqual(latest["id"], result["attestation_id"])
        self.assertEqual(latest["passed"], 1)


if __name__ == "__main__":
    unittest.main()
