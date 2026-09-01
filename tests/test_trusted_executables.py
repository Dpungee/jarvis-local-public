from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import jarvis.self_diagnosis as diagnosis
from jarvis import execution
from jarvis.trusted_executables import (
    trusted_path_executable,
    windows_directory,
    windows_system_executable,
)


class TrustedExecutableTests(unittest.TestCase):
    def test_path_executable_rejects_other_user_writable_path_entries(self):
        with tempfile.TemporaryDirectory(prefix="jarvis-path-poison-") as temporary:
            root = Path(temporary)
            trusted_root = root / "system"
            untrusted_root = root / "user" / "AppData" / "Local" / "Temp"
            trusted_root.mkdir()
            untrusted_root.mkdir(parents=True)
            executable = untrusted_root / ("git.exe" if os.name == "nt" else "git")
            executable.write_bytes(b"synthetic executable")
            with (
                patch(
                    "jarvis.trusted_executables.shutil.which",
                    return_value=str(executable),
                ),
                patch(
                    "jarvis.trusted_executables._trusted_install_roots",
                    return_value=(trusted_root.resolve(),),
                ),
            ):
                self.assertIsNone(trusted_path_executable("git"))

    def test_path_executable_rejects_workspace_controlled_files(self):
        with tempfile.TemporaryDirectory(prefix="jarvis-trusted-program-") as temporary:
            root = Path(temporary)
            executable = root / ("git.exe" if os.name == "nt" else "git")
            executable.write_bytes(b"synthetic executable")
            with patch(
                "jarvis.trusted_executables.shutil.which", return_value=str(executable)
            ):
                self.assertIsNone(
                    trusted_path_executable("git", prohibited_roots=(root,))
                )

    @unittest.skipUnless(os.name == "nt", "Windows Tasks trust boundary")
    def test_path_executable_rejects_windows_tasks_descendants(self):
        poison = windows_directory() / "Tasks" / "gh.exe"
        with (
            patch(
                "jarvis.trusted_executables.shutil.which",
                return_value=str(poison),
            ),
            patch(
                "jarvis.trusted_executables._ordinary_executable",
                return_value=poison,
            ),
        ):
            self.assertIsNone(trusted_path_executable("gh"))

    @unittest.skipUnless(os.name == "nt", "Windows canonical-directory check")
    def test_windows_utility_ignores_poisoned_systemroot(self):
        with tempfile.TemporaryDirectory(prefix="jarvis-fake-windows-") as temporary:
            fake_root = Path(temporary)
            fake_taskkill = fake_root / "System32" / "taskkill.exe"
            fake_taskkill.parent.mkdir()
            fake_taskkill.write_bytes(b"attacker controlled")
            with patch.dict(os.environ, {"SystemRoot": str(fake_root)}, clear=False):
                canonical = windows_directory()
                taskkill = windows_system_executable("System32", "taskkill.exe")
        self.assertNotEqual(canonical, fake_root.resolve())
        self.assertNotEqual(taskkill, fake_taskkill.resolve())
        self.assertEqual(taskkill.name.casefold(), "taskkill.exe")

    def test_process_tree_termination_uses_resolved_taskkill_path(self):
        trusted = Path("C:/Windows/System32/taskkill.exe")
        process = Mock()
        process.pid = 4321
        process.poll.side_effect = [None, None]
        job = SimpleNamespace(close=Mock())
        with (
            patch.object(execution.os, "name", "nt"),
            patch.object(execution, "windows_system_executable", return_value=trusted),
            patch.object(execution.subprocess, "run") as run,
        ):
            execution._terminate_process_tree(process, job)
        job.close.assert_called_once_with()
        self.assertEqual(run.call_args.args[0][0], str(trusted))
        process.kill.assert_called_once_with()

    def test_self_diagnosis_git_metadata_uses_one_resolved_absolute_program(self):
        with tempfile.TemporaryDirectory(prefix="jarvis-git-metadata-") as temporary:
            root = Path(temporary)
            (root / ".git").mkdir()
            candidate = root / "module.py"
            candidate.write_text("VALUE = 1\n", encoding="utf-8")
            git = Path("C:/Program Files/Git/cmd/git.exe")
            completed = SimpleNamespace(returncode=0, stdout="abc123\n")
            with (
                patch.object(diagnosis, "trusted_path_executable", return_value=git),
                patch.object(diagnosis.subprocess, "run", return_value=completed) as run,
            ):
                value = diagnosis._last_commit(root, candidate)
        self.assertEqual(value, "abc123")
        self.assertEqual(run.call_args.args[0][0], str(git))
        self.assertNotEqual(run.call_args.args[0][0], "git")


if __name__ == "__main__":
    unittest.main()
