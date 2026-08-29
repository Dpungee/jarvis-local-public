from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from jarvis import execution
from jarvis.config import Config
from jarvis.execution import (
    DOCKER_IMAGE,
    DockerBackend,
    ExecutionHandle,
    HostBackend,
    build_execution_backend,
    docker_available,
    docker_executable,
)
from jarvis.memory import Memory
from jarvis.tools import ToolBox

TEMP_ROOT = Path(__file__).resolve().parent / ".tmp"
TEMP_ROOT.mkdir(exist_ok=True)


class _NeverRunBackend:
    name = "docker"

    def __init__(self) -> None:
        self.called = False

    def run(self, *args, **kwargs):
        self.called = True
        raise AssertionError("policy validation must happen before backend dispatch")


class ExecutionBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="jarvis-execution-test-"))
        self.workspace = self.root / "workspace"
        self.data_dir = self.root / "data"
        self.workspace.mkdir()
        self.data_dir.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.root)

    def _trusted_docker(self) -> tuple[Path, Path]:
        install_root = self.root / "machine-install"
        executable = install_root / ("docker.exe" if os.name == "nt" else "docker")
        executable.parent.mkdir()
        executable.write_bytes(b"synthetic trusted docker")
        return install_root, executable

    def test_docker_resolution_ignores_poisoned_cwd_path_and_environment(self) -> None:
        poison = self.root / "poison"
        path_bin = poison / "path-bin"
        local_app_data = poison / "LocalAppData"
        program_files = poison / "ProgramFiles"
        path_bin.mkdir(parents=True)
        fake_name = "docker.exe" if os.name == "nt" else "docker"
        fake_paths = (
            poison / fake_name,
            path_bin / fake_name,
            local_app_data / "Docker" / "resources" / "bin" / fake_name,
            program_files / "Docker" / "Docker" / "resources" / "bin" / fake_name,
        )
        for fake in fake_paths:
            fake.parent.mkdir(parents=True, exist_ok=True)
            fake.write_bytes(b"attacker controlled")
        considered: list[Path] = []

        def reject(candidate: Path) -> None:
            considered.append(Path(candidate))

        previous = Path.cwd()
        try:
            os.chdir(poison)
            with (
                patch.dict(
                    os.environ,
                    {
                        "PATH": str(path_bin),
                        "LOCALAPPDATA": str(local_app_data),
                        "ProgramFiles": str(program_files),
                    },
                    clear=False,
                ),
                patch.object(
                    execution,
                    "trusted_install_file",
                    side_effect=reject,
                ),
                patch.object(execution.subprocess, "run") as run,
            ):
                self.assertIsNone(docker_executable())
                self.assertFalse(docker_available())
                run.assert_not_called()
        finally:
            os.chdir(previous)
        considered_paths = {
            str(Path(os.path.abspath(candidate))).casefold()
            for candidate in considered
        }
        for fake in fake_paths:
            self.assertNotIn(str(fake.resolve()).casefold(), considered_paths)

    def test_docker_resolution_requires_an_absolute_trusted_install_file(self) -> None:
        trusted_root, trusted = self._trusted_docker()
        untrusted = self.root / "user-writable" / trusted.name
        untrusted.parent.mkdir()
        untrusted.write_bytes(b"attacker controlled")
        with (
            patch.object(
                execution,
                "_docker_install_candidates",
                return_value=(trusted,),
            ),
            patch(
                "jarvis.trusted_executables._trusted_install_roots",
                return_value=(trusted_root.resolve(),),
            ),
        ):
            self.assertEqual(docker_executable(), str(trusted.resolve()))
            self.assertEqual(
                docker_executable(str(trusted.resolve())),
                str(trusted.resolve()),
            )
            self.assertIsNone(docker_executable(trusted.name))
            self.assertIsNone(docker_executable(untrusted.resolve()))

    def test_one_trusted_docker_path_is_bound_to_verify_run_and_removal(self) -> None:
        trusted_root, trusted = self._trusted_docker()
        trusted = trusted.resolve()
        completed = SimpleNamespace(returncode=0, stdout="27.0.1\n")
        with (
            patch(
                "jarvis.trusted_executables._trusted_install_roots",
                return_value=(trusted_root.resolve(),),
            ),
            patch.object(execution.subprocess, "run", return_value=completed) as run,
        ):
            self.assertTrue(docker_available(executable=trusted))
        availability = run.call_args
        self.assertEqual(availability.args[0][0], str(trusted))
        self.assertEqual(availability.kwargs["cwd"], trusted.parent)
        self.assertEqual(availability.kwargs["env"]["PATH"], str(trusted.parent))

        with (
            patch(
                "jarvis.trusted_executables._trusted_install_roots",
                return_value=(trusted_root.resolve(),),
            ),
            patch.object(execution, "docker_available", return_value=True) as available,
        ):
            backend = DockerBackend(self.workspace, executable=str(trusted))
        available.assert_called_once_with(executable=str(trusted))
        command = backend.command_for(
            "python", ["script.py"], cwd=self.workspace, process_name="unit-test"
        )
        self.assertEqual(command[0], str(trusted))

        handle = ExecutionHandle(
            process=Mock(),
            job=Mock(),
            backend="docker",
            container_name="jarvis-unit-test",
            docker=str(trusted),
        )
        with (
            patch.object(execution, "_terminate_process_tree"),
            patch.object(execution.subprocess, "run") as remove,
        ):
            handle.terminate()
        removal = remove.call_args
        self.assertEqual(
            removal.args[0],
            [str(trusted), "rm", "--force", "jarvis-unit-test"],
        )
        self.assertEqual(removal.kwargs["cwd"], trusted.parent)
        self.assertEqual(removal.kwargs["env"]["PATH"], str(trusted.parent))

    def test_backend_selection_preserves_host_default(self) -> None:
        config = replace(
            Config.load(), workspace=self.workspace, data_dir=self.data_dir,
            execution_backend="host",
        )
        self.assertIsInstance(build_execution_backend(config), HostBackend)

    def test_docker_command_is_ephemeral_networkless_and_resource_bounded(self) -> None:
        trusted_root, trusted = self._trusted_docker()
        with patch(
            "jarvis.trusted_executables._trusted_install_roots",
            return_value=(trusted_root.resolve(),),
        ):
            backend = DockerBackend(
                self.workspace, executable=str(trusted), verify=False
            )
        command = backend.command_for(
            "python", ["script.py"], cwd=self.workspace, process_name="unit-test"
        )
        rendered = " ".join(command)
        self.assertIn(DOCKER_IMAGE, command)
        for required in (
            "--rm", "--network none", "--cap-drop ALL",
            "--security-opt no-new-privileges", "--read-only",
            "--memory 2g", "--cpus 2", "--pids-limit 256",
            "--user 65532:65532",
        ):
            self.assertIn(required, rendered)
        mounts = [command[index + 1] for index, value in enumerate(command) if value == "--mount"]
        self.assertEqual(len(mounts), 1)
        expected_source = str(self.workspace.resolve())
        if os.name == "nt":
            expected_source = expected_source.replace("\\", "/")
        self.assertIn(f"source={expected_source}", mounts[0])
        self.assertNotIn(str(self.data_dir.resolve()), rendered)

    def test_docker_refuses_secret_bearing_or_linked_workspaces(self) -> None:
        trusted_root, trusted = self._trusted_docker()
        with patch(
            "jarvis.trusted_executables._trusted_install_roots",
            return_value=(trusted_root.resolve(),),
        ):
            backend = DockerBackend(
                self.workspace, executable=str(trusted), verify=False
            )
        (self.workspace / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
        with self.assertRaisesRegex(PermissionError, "protected workspace file"):
            backend.command_for("python", ["script.py"], cwd=self.workspace, process_name="x")
        (self.workspace / ".env").unlink()
        target = self.workspace / "ordinary.txt"
        target.write_text("safe", encoding="utf-8")
        link = self.workspace / "linked.txt"
        try:
            link.symlink_to(target)
        except OSError:
            self.skipTest("file links are unavailable in this environment")
        with self.assertRaisesRegex(PermissionError, "workspace links"):
            backend.command_for("python", ["script.py"], cwd=self.workspace, process_name="x")

    def test_policy_rejects_before_backend_dispatch(self) -> None:
        config = replace(
            Config.load(), workspace=self.workspace, data_dir=self.data_dir,
            execution_mode="trusted-host", autonomy="autonomous",
        )
        with Memory(self.data_dir / "jarvis.db") as memory:
            toolbox = ToolBox(config, memory)
            backend = _NeverRunBackend()
            toolbox._execution_backend = backend
            with self.assertRaises(PermissionError):
                toolbox.run_process("curl", ["https://example.com"])
            self.assertFalse(backend.called)


@unittest.skipUnless(docker_available(), "Docker daemon is unavailable")
class LiveDockerBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        # Docker Desktop's Linux bind-mount bridge can create Windows ACLs that
        # are unreadable when the source lives under the per-user AppData temp
        # tree. Exercise the real project mount shape under the ignored test
        # directory instead.
        self.root = Path(tempfile.mkdtemp(
            prefix="jarvis-live-docker-", dir=TEMP_ROOT
        ))
        self.workspace = self.root / "workspace"
        self.data_dir = self.root / "data"
        self.workspace.mkdir()
        self.data_dir.mkdir()
        self.backend = DockerBackend(self.workspace)

    def tearDown(self) -> None:
        shutil.rmtree(self.root)

    def _run(self, source: str, *, timeout: int = 20):
        (self.workspace / "probe.py").write_text(source, encoding="utf-8")
        return self.backend.run(
            "python", ["probe.py"], cwd=self.workspace, timeout=timeout,
            env=dict(os.environ),
        )

    def test_container_executes_writes_workspace_and_cannot_reach_network(self) -> None:
        result = self._run(
            "from pathlib import Path\n"
            "import socket\n"
            "Path('written.txt').write_text('inside', encoding='utf-8')\n"
            "try:\n"
            " socket.create_connection(('1.1.1.1', 53), timeout=1)\n"
            " print('NETWORK_REACHED')\n"
            "except OSError:\n"
            " print('NETWORK_BLOCKED')\n"
            "print(Path('/host-root').exists())\n"
        )
        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertIn("NETWORK_BLOCKED", result.stdout)
        self.assertIn("False", result.stdout)
        self.assertEqual(
            (self.workspace / "written.txt").read_text(encoding="utf-8"), "inside"
        )

    def test_timeout_removes_container_and_output_is_bounded(self) -> None:
        timed = self._run("import time\ntime.sleep(30)\n", timeout=1)
        self.assertTrue(timed.timed_out)
        bounded = self._run("print('x' * 1200000)\n")
        self.assertLessEqual(len(bounded.stdout.encode("utf-8")), 1_000_256)

    def test_toolbox_dispatches_an_allowlisted_command_to_docker(self) -> None:
        (self.workspace / "toolbox_probe.py").write_text(
            "print('TOOLBOX_DOCKER_OK')\n", encoding="utf-8"
        )
        config = replace(
            Config.load(), workspace=self.workspace, data_dir=self.data_dir,
            execution_mode="trusted-host", execution_backend="docker",
            autonomy="autonomous",
        )
        with Memory(self.data_dir / "jarvis.db") as memory:
            result = ToolBox(config, memory).run_process(
                "python", ["toolbox_probe.py"], timeout=30
            )
        self.assertEqual(result["exit_code"], 0, result)
        self.assertEqual(result["execution_backend"], "docker")
        self.assertIn("TOOLBOX_DOCKER_OK", result["stdout"])
