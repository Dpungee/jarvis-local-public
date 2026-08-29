from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from jarvis.config import Config
from jarvis.memory import Memory
from jarvis.tools import ToolBox, _minimal_environment, _shared_dependency_install_lock


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
            external_access="trusted-external",
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
                "--only-binary=:all:", "--require-hashes", "-r", "requirements.lock",
            ],
        )
        environment = Path(result["python_environment"])
        self.assertTrue((environment / ".jarvis-ready").is_file())
        self.assertEqual(
            self.toolbox._project_python_command("python", ["app.py"], self.workspace),
            [str(self.toolbox._venv_python(environment).resolve()), "app.py"],
        )

    def test_pyproject_only_project_is_refused_without_executing_build_backend(self) -> None:
        (self.workspace / "pyproject.toml").write_text(
            "[build-system]\nrequires = []\nbuild-backend = 'build_backend'\n",
            encoding="utf-8",
        )
        (self.workspace / "build_backend.py").write_text(
            "raise RuntimeError('this local code must never execute')\n", encoding="utf-8"
        )
        with patch.object(self.toolbox, "_run_dependency_command") as runner:
            with self.assertRaisesRegex(FileNotFoundError, "No safe dependency manifest"):
                self.toolbox.install_project_dependencies()
        runner.assert_not_called()
        rejected = json.loads(self.toolbox.execute(
            "install_project_dependencies", {"packages": ["arbitrary-package"]}
        ))
        self.assertFalse(rejected["ok"])
        self.assertIn("Unknown argument", rejected["error"])

    def test_long_windows_data_path_uses_compact_contained_environment(self) -> None:
        (self.workspace / "requirements.txt").write_text(
            "example==1.0\n", encoding="utf-8"
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
        self.assertEqual(
            captured[-1][-4:],
            ["ci", "--ignore-scripts", "--no-audit", "--no-fund"],
        )
        self.assertEqual(locked["lockfiles"], ["package-lock.json"])

        (self.workspace / "package-lock.json").unlink()
        captured.clear()
        with patch("jarvis.tools._program_command", side_effect=fake_program), patch.object(
            self.toolbox, "_run_dependency_command", side_effect=fake_run
        ):
            unlocked = self.toolbox.install_project_dependencies()
        self.assertTrue(unlocked["success"])
        self.assertEqual(
            captured[-1][-4:],
            ["install", "--ignore-scripts", "--no-audit", "--no-fund"],
        )

    def test_node_manager_reads_only_staged_manifests_and_publishes_modules(self) -> None:
        package = self.workspace / "package.json"
        package.write_text(
            '{"dependencies":{"example":"1.0.0"}}\n', encoding="utf-8"
        )
        observed_cwds: list[Path] = []

        def fake_run(command: list[str], cwd: Path, _timeout: int):
            observed_cwds.append(cwd)
            self.assertNotEqual(cwd, self.workspace.resolve())
            self.assertEqual((cwd / "package.json").read_bytes(), package.read_bytes())
            self.assertEqual((cwd / ".npmrc").read_bytes(), b"")
            installed = cwd / "node_modules" / "example"
            installed.mkdir(parents=True)
            (installed / "index.js").write_text("module.exports = 1;\n", encoding="utf-8")
            return self._success(command)

        with patch(
            "jarvis.tools._program_command", return_value=["node", "npm-cli"]
        ), patch.object(
            self.toolbox, "_run_dependency_command", side_effect=fake_run
        ):
            result = self.toolbox.install_project_dependencies()

        self.assertTrue(result["success"])
        self.assertEqual(len(observed_cwds), 1)
        self.assertTrue((self.workspace / "node_modules" / "example" / "index.js").is_file())
        self.assertFalse(observed_cwds[0].exists())

    def test_post_check_workspace_npmrc_injection_cannot_reach_npm(self) -> None:
        (self.workspace / "package.json").write_text(
            '{"dependencies":{"example":"1.0.0"}}\n', encoding="utf-8"
        )
        manager_observations: list[tuple[Path, bytes]] = []

        def inject_after_final_check(command: list[str], cwd: Path, _timeout: int):
            # This is the former exploitable instant: the approved source check
            # has completed but npm has not opened its project configuration.
            (self.workspace / ".npmrc").write_text(
                "registry=https://attacker.invalid/\n", encoding="utf-8"
            )
            manager_observations.append((cwd, (cwd / ".npmrc").read_bytes()))
            return self._success(command)

        with patch(
            "jarvis.tools._program_command", return_value=["node", "npm-cli"]
        ), patch.object(
            self.toolbox,
            "_run_dependency_command",
            side_effect=inject_after_final_check,
        ):
            with self.assertRaisesRegex(PermissionError, "npmrc"):
                self.toolbox.install_project_dependencies()

        self.assertEqual(len(manager_observations), 1)
        manager_cwd, manager_config = manager_observations[0]
        self.assertNotEqual(manager_cwd, self.workspace.resolve())
        self.assertEqual(manager_config, b"")
        self.assertFalse((self.workspace / "node_modules").exists())

    def test_lockfile_remote_sources_are_registry_and_integrity_bound(self) -> None:
        (self.workspace / "package.json").write_text(
            '{"dependencies":{"example":"1.0.0"}}\n', encoding="utf-8"
        )
        lockfile = self.workspace / "package-lock.json"
        valid_integrity = "sha512-" + "A" * 86 + "=="

        def locked(resolved: str, integrity: str | None = valid_integrity) -> str:
            entry: dict[str, str] = {"version": "1.0.0", "resolved": resolved}
            if integrity is not None:
                entry["integrity"] = integrity
            return json.dumps({"lockfileVersion": 3, "packages": {"node_modules/example": entry}})

        unsafe = (
            "http://registry.npmjs.org/example/-/example-1.0.0.tgz",
            "https://127.0.0.1/example.tgz",
            "https://169.254.169.254/latest/meta-data/example.tgz",
            "https://192.168.1.2/example.tgz",
            "https://attacker.example/example.tgz",
            "https://fixture-user:fixture-value;@registry.npmjs.org/example/-/example-1.0.0.tgz",
            "https://registry.npmjs.org/example/-/../payload.tgz",
            "vendor/payload.tgz",
            "payload.tgz",
            "registry.npmjs.org/foo.tgz",
            "node_modules/foo",
        )
        for resolved in unsafe:
            with self.subTest(resolved=resolved):
                lockfile.write_text(locked(resolved), encoding="utf-8")
                with patch.object(self.toolbox, "_run_dependency_command") as runner:
                    with self.assertRaises(PermissionError):
                        self.toolbox.install_project_dependencies()
                runner.assert_not_called()

        lockfile.write_text(json.dumps({
            "lockfileVersion": 3,
            "packages": {"node_modules/../../outside": {"version": "1.0.0"}},
        }), encoding="utf-8")
        with patch.object(self.toolbox, "_run_dependency_command") as runner:
            with self.assertRaisesRegex(PermissionError, "outside-workspace"):
                self.toolbox.install_project_dependencies()
        runner.assert_not_called()

        official = "https://registry.npmjs.org/example/-/example-1.0.0.tgz"
        lockfile.write_text(locked(official, None), encoding="utf-8")
        with patch.object(self.toolbox, "_run_dependency_command") as runner:
            with self.assertRaisesRegex(PermissionError, "integrity"):
                self.toolbox.install_project_dependencies()
        runner.assert_not_called()

        lockfile.write_text(locked(official), encoding="utf-8")
        with patch(
            "jarvis.tools._program_command", return_value=["node", "npm-cli"]
        ), patch.object(
            self.toolbox,
            "_run_dependency_command",
            return_value=self._success(["node", "npm-cli"]),
        ) as runner:
            result = self.toolbox.install_project_dependencies()
        self.assertTrue(result["success"])
        runner.assert_called_once()

    def test_dependency_environment_pins_empty_config_and_official_registry(self) -> None:
        environment = _minimal_environment(self.data_dir)
        self.assertEqual(environment["NPM_CONFIG_REGISTRY"], "https://registry.npmjs.org/")
        self.assertEqual(environment["NPM_CONFIG_STRICT_SSL"], "true")
        self.assertEqual(environment["NPM_CONFIG_IGNORE_SCRIPTS"], "true")
        self.assertEqual(environment["NPM_CONFIG_PROXY"], "")
        self.assertEqual(environment["NPM_CONFIG_HTTPS_PROXY"], "")
        for key in ("NPM_CONFIG_USERCONFIG", "NPM_CONFIG_GLOBALCONFIG"):
            config = Path(environment[key])
            self.assertTrue(config.is_file())
            self.assertEqual(config.read_bytes(), b"")

    def test_external_access_is_required_before_dependency_execution(self) -> None:
        (self.workspace / "requirements.txt").write_text(
            "example==1.0\n", encoding="utf-8"
        )
        disabled = ToolBox(
            replace(self.config, external_access="disabled"), self.memory
        )
        self.assertNotIn("install_project_dependencies", disabled.tools)
        with patch.object(disabled, "_run_dependency_command") as runner:
            with self.assertRaisesRegex(PermissionError, "network access is disabled"):
                disabled.install_project_dependencies()
        runner.assert_not_called()

    def test_dependency_install_lock_is_shared_across_toolboxes(self) -> None:
        (self.workspace / "requirements.txt").write_text(
            "example==1.0\n", encoding="utf-8"
        )
        other = ToolBox(self.config, self.memory)
        self.assertIs(
            self.toolbox._dependency_install_lock,
            other._dependency_install_lock,
        )
        self.assertTrue(self.toolbox._dependency_install_lock.acquire(blocking=False))
        try:
            with patch.object(other, "_run_dependency_command") as runner:
                with self.assertRaisesRegex(RuntimeError, "already running"):
                    other.install_project_dependencies()
            runner.assert_not_called()
        finally:
            self.toolbox._dependency_install_lock.release()

    def test_dependency_install_lock_blocks_a_separate_process(self) -> None:
        script = """
import sys
from dataclasses import replace
from pathlib import Path
from jarvis.config import Config
from jarvis.tools import _shared_dependency_install_lock

config = replace(
    Config.load(),
    workspace=Path(sys.argv[1]),
    data_dir=Path(sys.argv[2]),
)
lock = _shared_dependency_install_lock(config)
acquired = lock.acquire(timeout=0.25)
print("acquired" if acquired else "blocked")
if acquired:
    lock.release()
"""
        lock = _shared_dependency_install_lock(self.config)
        self.assertTrue(lock.acquire(timeout=0.25))
        try:
            blocked = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    script,
                    str(self.workspace.resolve()),
                    str(self.data_dir.resolve()),
                ],
                cwd=Path(__file__).resolve().parents[1],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
                check=False,
            )
        finally:
            lock.release()
        self.assertEqual(blocked.returncode, 0, blocked.stderr)
        self.assertEqual(blocked.stdout.strip(), "blocked")

        acquired = subprocess.run(
            [
                sys.executable,
                "-c",
                script,
                str(self.workspace.resolve()),
                str(self.data_dir.resolve()),
            ],
            cwd=Path(__file__).resolve().parents[1],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(acquired.returncode, 0, acquired.stderr)
        self.assertEqual(acquired.stdout.strip(), "acquired")

    def test_execute_approval_binds_exact_manifest_and_rejects_changed_bytes(self) -> None:
        manifest = self.workspace / "package.json"
        manifest.write_text('{"dependencies":{"example":"1.0.0"}}\n', encoding="utf-8")
        node = self.test_dir / "trusted-node.exe"
        npm_cli = self.test_dir / "trusted-npm-cli.js"
        node.write_bytes(b"trusted node bytes")
        npm_cli.write_bytes(b"trusted npm cli bytes")
        command = [str(node), str(npm_cli)]
        scope = "conversation:777"
        with patch("jarvis.tools._program_command", return_value=command):
            with self.toolbox.approval_context(scope):
                blocked = json.loads(self.toolbox.execute(
                    "install_project_dependencies", {"cwd": "."}
                ))
        self.assertTrue(blocked["approval_required"])
        row = next(
            item for item in self.memory.list_approvals()
            if item["id"] == blocked["approval_id"]
        )
        resource = json.loads(row["resource"])
        arguments = resource["arguments"]
        self.assertEqual(arguments["manifest_names"], ["package.json"])
        self.assertRegex(arguments["manifest_tree_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(arguments["direct_dependency_count"], 1)
        self.assertEqual(
            arguments["direct_dependencies"],
            ["node/dependencies: example@1.0.0"],
        )
        self.assertEqual(arguments["omitted_dependency_count"], 0)
        node_executor, npm_executor = arguments["executors"]
        self.assertEqual(node_executor["identity"], "node")
        node_path = node_executor["path"]
        if isinstance(node_path, dict):
            self.assertTrue(str(node.resolve()).startswith(node_path["prefix"]))
            self.assertEqual(node_path["characters"], len(str(node.resolve())))
        else:
            self.assertEqual(node_path, str(node.resolve()))
        self.assertEqual(
            node_executor["sha256"],
            hashlib.sha256(node.read_bytes()).hexdigest(),
        )
        self.assertEqual(npm_executor["identity"], "npm-cli")
        npm_path = npm_executor["path"]
        if isinstance(npm_path, dict):
            self.assertTrue(str(npm_cli.resolve()).startswith(npm_path["prefix"]))
            self.assertEqual(npm_path["characters"], len(str(npm_cli.resolve())))
        else:
            self.assertEqual(npm_path, str(npm_cli.resolve()))
        self.assertEqual(
            npm_executor["sha256"],
            hashlib.sha256(npm_cli.read_bytes()).hexdigest(),
        )
        self.assertTrue(self.memory.decide_approval(blocked["approval_id"], True))

        manifest.write_text('{"dependencies":{"changed":"2.0.0"}}\n', encoding="utf-8")
        with patch("jarvis.tools._program_command", return_value=command), patch.object(
            self.toolbox, "_run_dependency_command"
        ) as runner:
            with self.toolbox.approval_context(scope):
                changed = json.loads(self.toolbox.execute(
                    "install_project_dependencies", {"cwd": "."}
                ))
        self.assertTrue(changed["approval_required"])
        self.assertNotEqual(changed["approval_id"], blocked["approval_id"])
        runner.assert_not_called()

    def test_path_poisoned_node_executor_is_rejected_before_approval(self) -> None:
        (self.workspace / "package.json").write_text(
            '{"dependencies":{"example":"1.0.0"}}\n', encoding="utf-8"
        )
        poison = self.test_dir / "user-writable-bin"
        npm_tree = poison / "node_modules" / "npm" / "bin"
        npm_tree.mkdir(parents=True)
        node = poison / ("node.exe" if os.name == "nt" else "node")
        npm = poison / ("npm.cmd" if os.name == "nt" else "npm")
        npm_cli = npm_tree / "npm-cli.js"
        node.write_bytes(b"poison node")
        npm.write_bytes(b"poison npm")
        npm_cli.write_bytes(b"poison cli")

        def poisoned_which(name: str) -> str | None:
            return str(node if name == "node" else npm) if name in {"node", "npm"} else None

        with patch("jarvis.tools.shutil.which", side_effect=poisoned_which):
            with self.toolbox.approval_context("conversation:779"):
                result = json.loads(self.toolbox.execute(
                    "install_project_dependencies", {"cwd": "."}
                ))

        self.assertFalse(result["ok"])
        self.assertIn("OS-administered", result["error"])
        self.assertEqual(self.memory.list_approvals(), [])

    def test_node_executor_change_after_final_approval_check_never_runs(self) -> None:
        (self.workspace / "package.json").write_text(
            '{"dependencies":{"example":"1.0.0"}}\n', encoding="utf-8"
        )
        trusted_node = self.test_dir / "trusted-node.exe"
        trusted_cli = self.test_dir / "trusted-npm-cli.js"
        changed_node = self.test_dir / "changed-node.exe"
        changed_cli = self.test_dir / "changed-npm-cli.js"
        trusted_node.write_bytes(b"trusted node")
        trusted_cli.write_bytes(b"trusted cli")
        changed_node.write_bytes(b"changed node")
        changed_cli.write_bytes(b"changed cli")
        trusted = [str(trusted_node), str(trusted_cli)]
        changed = [str(changed_node), str(changed_cli)]
        scope = "conversation:780"

        with patch("jarvis.tools._program_command", return_value=trusted):
            with self.toolbox.approval_context(scope):
                pending = json.loads(self.toolbox.execute(
                    "install_project_dependencies", {"cwd": "."}
                ))
        self.assertTrue(self.memory.decide_approval(pending["approval_id"], True))

        command_calls = 0

        def changed_after_confirmation(*_args):
            nonlocal command_calls
            command_calls += 1
            return trusted if command_calls <= 2 else changed

        with patch(
            "jarvis.tools._program_command", side_effect=changed_after_confirmation
        ), patch.object(self.toolbox, "_run_dependency_command") as runner:
            with self.toolbox.approval_context(scope):
                result = json.loads(self.toolbox.execute(
                    "install_project_dependencies", {"cwd": "."}
                ))

        self.assertFalse(result["ok"])
        self.assertIn("changed after approval", result["error"])
        runner.assert_not_called()

    def test_manifest_change_after_final_approval_check_never_runs(self) -> None:
        manifest = self.workspace / "requirements.txt"
        manifest.write_text("safe-package==1.0\n", encoding="utf-8")
        scope = "conversation:781"
        with self.toolbox.approval_context(scope):
            pending = json.loads(self.toolbox.execute(
                "install_project_dependencies", {"cwd": "."}
            ))
        self.assertTrue(self.memory.decide_approval(pending["approval_id"], True))

        original_effective = self.toolbox._effective_approval_arguments
        effective_calls = 0

        def mutate_after_confirmation(name, arguments):
            nonlocal effective_calls
            result = original_effective(name, arguments)
            if name == "install_project_dependencies":
                effective_calls += 1
                if effective_calls == 2:
                    manifest.write_text("changed-package==9.9\n", encoding="utf-8")
            return result

        with patch.object(
            self.toolbox,
            "_effective_approval_arguments",
            side_effect=mutate_after_confirmation,
        ), patch.object(self.toolbox, "_run_dependency_command") as runner:
            with self.toolbox.approval_context(scope):
                result = json.loads(self.toolbox.execute(
                    "install_project_dependencies", {"cwd": "."}
                ))

        self.assertFalse(result["ok"])
        self.assertIn("changed after approval", result["error"])
        runner.assert_not_called()

    def test_staged_manifest_is_bound_directly_to_approved_bytes(self) -> None:
        manifest = self.workspace / "requirements.txt"
        approved_bytes = "safe-package==1.0\n"
        alternate_bytes = "alternate-package==9.9\n"
        manifest.write_text(approved_bytes, encoding="utf-8")
        scope = "conversation:782"
        with self.toolbox.approval_context(scope):
            pending = json.loads(self.toolbox.execute(
                "install_project_dependencies", {"cwd": "."}
            ))
        self.assertTrue(self.memory.decide_approval(pending["approval_id"], True))

        original_assert = self.toolbox._assert_approved_dependency_snapshot

        def toggle_around_approved_check(working_directory: Path) -> None:
            manifest.write_text(approved_bytes, encoding="utf-8")
            original_assert(working_directory)
            # A sequential source-vs-source check can be fooled if the mutable
            # workspace changes back after the approved check. The staged tree
            # must independently remain bound to the approved digest.
            manifest.write_text(alternate_bytes, encoding="utf-8")

        with patch.object(
            self.toolbox,
            "_assert_approved_dependency_snapshot",
            side_effect=toggle_around_approved_check,
        ), patch.object(self.toolbox, "_run_dependency_command") as runner:
            with self.toolbox.approval_context(scope):
                result = json.loads(self.toolbox.execute(
                    "install_project_dependencies", {"cwd": "."}
                ))

        self.assertFalse(result["ok"])
        self.assertIn("Staged dependency inputs do not match approval", result["error"])
        runner.assert_not_called()

    def test_requirements_include_is_rejected_before_approval_or_execution(self) -> None:
        manifest = self.workspace / "requirements.txt"
        included = self.workspace / "extra.txt"
        manifest.write_text("-r extra.txt\n", encoding="utf-8")
        included.write_text("example==1.0\n", encoding="utf-8")
        scope = "conversation:778"

        with patch.object(self.toolbox, "_run_dependency_command") as runner:
            with self.toolbox.approval_context(scope):
                first = json.loads(self.toolbox.execute(
                    "install_project_dependencies", {"cwd": "."}
                ))
            included.write_text("changed==2.0\n", encoding="utf-8")
            with self.toolbox.approval_context(scope):
                changed = json.loads(self.toolbox.execute(
                    "install_project_dependencies", {"cwd": "."}
                ))

        self.assertFalse(first["ok"])
        self.assertFalse(changed["ok"])
        self.assertIn("directives", first["error"])
        self.assertEqual(self.memory.list_approvals(), [])
        runner.assert_not_called()

        for unsafe in (
            "example@file:///outside/example.whl\n",
            "example@https://packages.example/example.whl\n",
            "vendor/example.whl\n",
            "payload.whl\n",
        ):
            with self.subTest(requirement=unsafe.strip()):
                manifest.write_text(unsafe, encoding="utf-8")
                with patch.object(self.toolbox, "_run_dependency_command") as runner:
                    with self.assertRaisesRegex(PermissionError, "direct URLs|local paths"):
                        self.toolbox.install_project_dependencies()
                runner.assert_not_called()

    def test_project_npmrc_and_local_node_sources_are_rejected(self) -> None:
        package = self.workspace / "package.json"
        package.write_text(
            json.dumps({"dependencies": {"example": "1.0.0"}}), encoding="utf-8"
        )
        (self.workspace / ".npmrc").write_text("global=true\n", encoding="utf-8")
        with patch.object(self.toolbox, "_run_dependency_command") as runner:
            with self.assertRaisesRegex(PermissionError, "npmrc"):
                self.toolbox.install_project_dependencies()
        runner.assert_not_called()

        (self.workspace / ".npmrc").unlink()
        package.write_text(
            json.dumps({"dependencies": {"example": "file:../outside"}}),
            encoding="utf-8",
        )
        with patch.object(self.toolbox, "_run_dependency_command") as runner:
            with self.assertRaisesRegex(PermissionError, "unsupported local"):
                self.toolbox.install_project_dependencies()
        runner.assert_not_called()

        package.write_text(
            json.dumps({
                "workspaces": ["packages/*"],
                "dependencies": {"example": "1.0.0"},
            }),
            encoding="utf-8",
        )
        with patch.object(self.toolbox, "_run_dependency_command") as runner:
            with self.assertRaisesRegex(PermissionError, "workspaces"):
                self.toolbox.install_project_dependencies()
        runner.assert_not_called()

        for source in ("user/repo", "user/repo#main"):
            with self.subTest(source=source):
                package.write_text(
                    json.dumps({"dependencies": {"example": source}}),
                    encoding="utf-8",
                )
                with patch.object(self.toolbox, "_run_dependency_command") as runner:
                    with self.assertRaisesRegex(PermissionError, "unsupported local"):
                        self.toolbox.install_project_dependencies()
                runner.assert_not_called()

        package.write_text(
            json.dumps({
                "dependencies": {"example": "npm:@safe-scope/registry-package@1.2.3"}
            }),
            encoding="utf-8",
        )
        with patch("jarvis.tools._program_command", return_value=["node", "npm-cli"]), patch.object(
            self.toolbox, "_run_dependency_command", return_value=self._success(["node", "npm-cli"])
        ) as runner:
            result = self.toolbox.install_project_dependencies()
        self.assertTrue(result["success"])
        runner.assert_called_once()

    def test_manifest_credentials_are_rejected_and_command_output_is_redacted(self) -> None:
        token = "sk-proj-" + "D" * 24
        manifest = self.workspace / "requirements.txt"
        manifest.write_text(
            f"example @ https://user:{token}@packages.example/example.whl\n",
            encoding="utf-8",
        )
        with patch.object(self.toolbox, "_run_dependency_command") as runner:
            with self.assertRaisesRegex(PermissionError, "may not embed credentials"):
                self.toolbox.install_project_dependencies()
        runner.assert_not_called()

        manifest.write_text("example==1.0\n", encoding="utf-8")
        command = [
            str(Path(os.sys.executable)),
            "-c",
            f"print('provider leaked {token}')",
        ]
        result = self.toolbox._run_dependency_command(command, self.workspace, 5)
        self.assertNotIn(token, result["stdout"])
        self.assertIn("[REDACTED]", result["stdout"])

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
            with self.assertRaisesRegex(FileNotFoundError, "No safe dependency manifest"):
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
