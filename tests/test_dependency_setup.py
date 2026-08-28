from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from jarvis.config import Config
from jarvis.memory import Memory
from jarvis.tools import ToolBox


TEMP_ROOT = Path(tempfile.gettempdir()) / "jarvis-dependency-tests"
TEMP_ROOT.mkdir(exist_ok=True)


class DependencySetupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.test_dir = TEMP_ROOT / f"dependencies-{os.getpid()}-{self._testMethodName}"
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir)
        self.workspace = self.test_dir / "workspace"
        self.data_dir = self.test_dir / "data"
        self.workspace.mkdir(parents=True)
        self.data_dir.mkdir()
        self.config = replace(
            Config.load(),
            workspace=self.workspace,
            data_dir=self.data_dir,
            execution_mode="trusted-host",
            autonomy="autonomous",
            computer_access="disabled",
            command_timeout=120,
        )
        self.memory = Memory(self.data_dir / "test.db")
        self.toolbox = ToolBox(self.config, self.memory)

    def tearDown(self) -> None:
        self.memory.close()
        resolved = self.test_dir.resolve()
        self.assertEqual(resolved.parent, TEMP_ROOT.resolve())
        shutil.rmtree(resolved)

    @staticmethod
    def _success(command: list[str], phase: str = "") -> dict[str, object]:
        return {
            "command": [Path(command[0]).name, *command[1:]],
            "exit_code": 0,
            "timed_out": False,
            "duration_seconds": 0.01,
            "stdout": phase,
            "stderr": "",
        }

    def test_requirements_lock_creates_isolated_environment_and_uses_hashes(self) -> None:
        (self.workspace / "requirements.lock").write_text(
            "example==1.0 --hash=sha256:" + "a" * 64 + "\n", encoding="utf-8"
        )
        commands: list[list[str]] = []

        def fake_run(command: list[str], _cwd: Path, _timeout: int):
            commands.append(command)
            if command[1:3] == ["-m", "venv"]:
                environment = Path(command[-1])
                interpreter = self.toolbox._venv_python(environment)
                interpreter.parent.mkdir(parents=True)
                interpreter.write_bytes(b"python")
            return self._success(command)

        with patch.object(self.toolbox, "_run_dependency_command", side_effect=fake_run):
            result = self.toolbox.install_project_dependencies(timeout=60)

        self.assertTrue(result["success"])
        self.assertEqual([step["phase"] for step in result["steps"]], [
            "python-venv", "python-dependencies",
        ])
        self.assertEqual(commands[0][1:3], ["-m", "venv"])
        self.assertEqual(
            commands[1][1:],
            [
                "-m", "pip", "install", "--disable-pip-version-check", "--no-input",
                "--require-hashes", "-r", "requirements.lock",
            ],
        )
        environment = Path(result["python_environment"])
        self.assertTrue((environment / ".jarvis-ready").is_file())
        self.assertEqual(
            self.toolbox._project_python_command("python", ["app.py"], self.workspace),
            [str(self.toolbox._venv_python(environment).resolve()), "app.py"],
        )

    def test_pyproject_uses_local_project_target_without_model_supplied_packages(self) -> None:
        (self.workspace / "pyproject.toml").write_text(
            "[build-system]\nrequires = ['setuptools']\n", encoding="utf-8"
        )
        commands: list[list[str]] = []

        def fake_run(command: list[str], _cwd: Path, _timeout: int):
            commands.append(command)
            if command[1:3] == ["-m", "venv"]:
                interpreter = self.toolbox._venv_python(Path(command[-1]))
                interpreter.parent.mkdir(parents=True)
                interpreter.write_bytes(b"python")
            return self._success(command)

        with patch.object(self.toolbox, "_run_dependency_command", side_effect=fake_run):
            result = self.toolbox.install_project_dependencies()

        self.assertTrue(result["success"])
        self.assertEqual(commands[-1][-1], ".")
        rejected = json.loads(self.toolbox.execute(
            "install_project_dependencies", {"packages": ["arbitrary-package"]}
        ))
        self.assertFalse(rejected["ok"])
        self.assertIn("Unknown argument", rejected["error"])

    def test_long_windows_data_path_uses_compact_contained_environment(self) -> None:
        (self.workspace / "pyproject.toml").write_text(
            "[build-system]\nrequires = ['setuptools']\n", encoding="utf-8"
        )
        toolbox = self.toolbox
        key = hashlib.sha256(
            os.path.normcase(str(self.workspace.resolve())).encode("utf-8")
        ).hexdigest()[:20]
        baseline = self.data_dir.resolve()
        legacy_interpreter = toolbox._venv_python(
            baseline / "project-environments" / key
        )
        padding = max(1, 246 - len(str(legacy_interpreter)) - 1)
        long_data = baseline / ("d" * padding)
        self.assertGreater(
            len(str(toolbox._venv_python(long_data / "project-environments" / key))),
            245,
        )
        self.assertLessEqual(
            len(str(toolbox._venv_python(long_data / "v" / key))),
            245,
        )
        toolbox.config = replace(self.config, data_dir=long_data)
        commands: list[list[str]] = []

        def fake_run(command: list[str], _cwd: Path, _timeout: int):
            commands.append(command)
            if command[1:3] == ["-m", "venv"]:
                interpreter = toolbox._venv_python(Path(command[-1]))
                interpreter.parent.mkdir(parents=True)
                interpreter.write_bytes(b"python")
            return self._success(command)

        with patch.object(toolbox, "_run_dependency_command", side_effect=fake_run):
            result = toolbox.install_project_dependencies()
        environment = Path(result["python_environment"])
        self.assertTrue(result["success"])
        self.assertEqual(environment.parent, long_data.resolve() / "v")
        self.assertTrue(environment.is_relative_to(long_data.resolve()))
        self.assertLessEqual(len(str(toolbox._venv_python(environment))), 245)
        self.assertEqual(commands[0][1:3], ["-m", "venv"])

    def test_existing_long_legacy_environment_is_preserved(self) -> None:
        long_data = self.data_dir
        self.toolbox.config = replace(self.config, data_dir=long_data)
        working_directory = self.workspace.resolve()
        key = hashlib.sha256(
            os.path.normcase(str(working_directory)).encode("utf-8")
        ).hexdigest()[:20]
        legacy = long_data.resolve() / "project-environments" / key
        legacy.mkdir(parents=True)
        self.assertEqual(
            self.toolbox._project_environment(working_directory),
            legacy,
        )

    def test_excessive_windows_data_path_fails_before_environment_creation(self) -> None:
        if os.name != "nt":
            self.skipTest("Windows path budget")
        excessive = self.test_dir / ("x" * 300)
        self.toolbox.config = replace(self.config, data_dir=excessive)

        with self.assertRaisesRegex(OSError, "shorten JARVIS_DATA"):
            self.toolbox._project_environment(self.workspace, create=True)
        self.assertFalse(excessive.exists())

    def test_node_lockfile_uses_npm_ci_and_no_lock_uses_install(self) -> None:
        (self.workspace / "package.json").write_text(
            json.dumps({"dependencies": {"example": "1.0.0"}}), encoding="utf-8"
        )
        (self.workspace / "package-lock.json").write_text("{}\n", encoding="utf-8")
        captured: list[list[str]] = []

        def fake_program(_program: str, arguments: list[str], _workspace: Path):
            return ["trusted-node", "trusted-npm-cli", *arguments]

        def fake_run(command: list[str], _cwd: Path, _timeout: int):
            captured.append(command)
            return self._success(command)

        with patch("jarvis.tools._program_command", side_effect=fake_program), patch.object(
            self.toolbox, "_run_dependency_command", side_effect=fake_run
        ):
            locked = self.toolbox.install_project_dependencies()
        self.assertTrue(locked["success"])
        self.assertEqual(captured[-1][-3:], ["ci", "--no-audit", "--no-fund"])
        self.assertEqual(locked["lockfiles"], ["package-lock.json"])

        (self.workspace / "package-lock.json").unlink()
        captured.clear()
        with patch("jarvis.tools._program_command", side_effect=fake_program), patch.object(
            self.toolbox, "_run_dependency_command", side_effect=fake_run
        ):
            unlocked = self.toolbox.install_project_dependencies()
        self.assertTrue(unlocked["success"])
        self.assertEqual(captured[-1][-3:], ["install", "--no-audit", "--no-fund"])

    def test_failure_stops_later_managers_and_preserves_bounded_result_shape(self) -> None:
        (self.workspace / "requirements.txt").write_text("example==1.0\n", encoding="utf-8")
        (self.workspace / "package.json").write_text("{}\n", encoding="utf-8")
        phases: list[str] = []

        def fake_run(command: list[str], _cwd: Path, _timeout: int):
            is_venv = command[1:3] == ["-m", "venv"]
            phases.append("venv" if is_venv else "pip")
            if is_venv:
                interpreter = self.toolbox._venv_python(Path(command[-1]))
                interpreter.parent.mkdir(parents=True)
                interpreter.write_bytes(b"python")
                return self._success(command)
            return {
                **self._success(command),
                "exit_code": 1,
                "stderr": "dependency resolution failed",
            }

        with patch.object(self.toolbox, "_run_dependency_command", side_effect=fake_run), patch(
            "jarvis.tools._program_command"
        ) as npm_program:
            result = self.toolbox.install_project_dependencies()

        self.assertFalse(result["success"])
        self.assertEqual(phases, ["venv", "pip"])
        npm_program.assert_not_called()
        self.assertEqual(result["steps"][-1]["stderr"], "dependency resolution failed")
        self.assertFalse((Path(result["python_environment"]) / ".jarvis-ready").exists())

    def test_manifest_and_capability_gates_fail_before_process_execution(self) -> None:
        with patch.object(self.toolbox, "_run_dependency_command") as runner:
            with self.assertRaisesRegex(FileNotFoundError, "No supported dependency manifest"):
                self.toolbox.install_project_dependencies()
        runner.assert_not_called()

        readonly = ToolBox(replace(self.config, autonomy="readonly"), self.memory)
        disabled = ToolBox(replace(self.config, execution_mode="disabled"), self.memory)
        self.assertNotIn("install_project_dependencies", readonly.tools)
        self.assertNotIn("install_project_dependencies", disabled.tools)
        with self.assertRaisesRegex(PermissionError, "readonly"):
            readonly.install_project_dependencies()
        with self.assertRaisesRegex(PermissionError, "disabled"):
            disabled.install_project_dependencies()


if __name__ == "__main__":
    unittest.main()
