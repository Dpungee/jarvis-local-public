import os
import shutil
import unittest
from pathlib import Path

from jarvis.policy import resolve_workspace_path, validate_command, validate_process


TEMP_ROOT = Path(__file__).resolve().parent / ".tmp"
TEMP_ROOT.mkdir(exist_ok=True)


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.workspace = TEMP_ROOT / f"policy-{os.getpid()}-{self._testMethodName}"
        if self.workspace.exists():
            shutil.rmtree(self.workspace)
        self.workspace.mkdir()

    def tearDown(self):
        resolved = self.workspace.resolve()
        self.assertEqual(resolved.parent, TEMP_ROOT.resolve())
        shutil.rmtree(resolved)

    def test_raw_shell_commands_are_always_blocked(self):
        commands = [
            "python -m unittest",
            "Get-ChildItem -Recurse",
            "Remove-Item C:\\ -Recurse -Force",
            "shutdown /s",
            "powershell -EncodedCommand ZgBvAG8=",
            "Get-Content $env:USERPROFILE\\.ssh\\id_rsa",
            "cmd /c rd /s /q C:\\Users\\victim\\Documents",
            "Clear-Disk -Number 0 -RemoveData -Confirm:$false",
        ]
        for command in commands:
            with self.subTest(command=command):
                self.assertFalse(validate_command(command)[0])

    def test_structured_process_allowlist(self):
        workspace = self.workspace
        self.assertTrue(validate_process(workspace, "python", ["-m", "unittest"])[0])
        blocked = [
            ("powershell", ["Get-ChildItem"]),
            ("cmd", ["/c", "dir"]),
            ("curl", ["https://example.com"]),
            ("reg.exe", ["delete", "HKCU\\Software\\Foo"]),
            ("python", ["-c", "print(1)"]),
            ("node", ["--eval", "console.log(1)"]),
            ("git", ["-c", "core.fsmonitor=evil", "status"]),
            ("python", ["..\\escape.py"]),
            ("python", ["$env:USERPROFILE\\secret.py"]),
        ]
        for program, arguments in blocked:
            with self.subTest(program=program, arguments=arguments):
                self.assertFalse(validate_process(workspace, program, arguments)[0])

    def test_intrinsically_non_executing_verifiers_and_runtime_hooks_are_blocked(self):
        blocked = [
            ("pytest", ["--collect-only"]),
            ("pytest", ["--setup-only"]),
            ("python", ["-m", "pytest", "--collect-only"]),
            ("py", ["-m", "pytest", "--setup-only"]),
            ("cargo", ["test", "--no-run"]),
            ("go", ["test", "-count=0", "./..."]),
            ("go", ["test", "-count", "0", "./..."]),
            ("go", ["test", "-list", ".*", "./..."]),
            ("ctest", ["-N"]),
            ("ctest", ["--show-only=json-v1"]),
            ("dotnet", ["test", "--list-tests"]),
            ("java", ["-javaagent:untrusted.jar", "Main"]),
        ]
        for program, arguments in blocked:
            with self.subTest(program=program, arguments=arguments):
                allowed, _reason = validate_process(self.workspace, program, arguments)
                self.assertFalse(allowed)

        allowed_selectors = [
            ("go", ["test", "-run", "TestReal", "./..."]),
            ("go", ["test", "-skip", "Slow", "./..."]),
            ("go", ["test", "-count=1", "./..."]),
            ("ctest", ["-R", "RealSuite"]),
            ("ctest", ["--label-regex=unit"]),
            ("dotnet", ["test", "--filter", "Category=Unit"]),
        ]
        for program, arguments in allowed_selectors:
            with self.subTest(allowed_program=program, arguments=arguments):
                self.assertTrue(validate_process(self.workspace, program, arguments)[0])

    def test_workspace_boundary_and_windows_devices(self):
        workspace = self.workspace
        self.assertEqual(resolve_workspace_path(workspace, "src/app.py"), workspace / "src" / "app.py")
        for path in ("../escape.txt", "NUL", "file.txt:secret", "\\\\server\\share\\file"):
            with self.subTest(path=path), self.assertRaises(PermissionError):
                resolve_workspace_path(workspace, path)


if __name__ == "__main__":
    unittest.main()
