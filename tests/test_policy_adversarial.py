from __future__ import annotations

import unittest
from pathlib import Path

from jarvis.policy import validate_process


class AdversarialProcessPolicyTests(unittest.TestCase):
    def test_mutating_hooks_and_argument_smuggling_are_blocked(self):
        workspace = Path("workspace").resolve()
        blocked = (
            ("git", ["config", "alias.pwn", "!python malicious.py"]),
            ("git", ["pwn"]),
            ("git", ["push", "--force"]),
            ("git", ["clean", "-fdx"]),
            ("git", ["reset", "--hard"]),
            ("git", ["branch", "--unset-upstream"]),
            ("git", ["branch", "--edit-description"]),
            ("git", ["diff", "--output=artifact.patch"]),
            ("git", ["diff", "--ext-diff"]),
            ("npm", ["publish"]),
            ("npm", ["test", "--script-shell=powershell"]),
            ("npm", ["test", "--registry=https://registry.example"]),
            ("cargo", ["publish"]),
            ("cargo", ["test", "--config=target.x86_64-pc-windows-msvc.runner=powershell"]),
            ("go", ["env", "-w", "GOPROXY=https://attacker.example"]),
            ("go", ["test", "-exec=powershell"]),
            ("dotnet", ["tool", "install", "evil"]),
            ("cmake", ["-E", "remove_directory", "."]),
            ("python", ["NUL"]),
            ("python", ["script.py:payload"]),
            ("python", ['"../outside.py"']),
        )
        for program, arguments in blocked:
            with self.subTest(program=program, arguments=arguments):
                allowed, reason = validate_process(workspace, program, arguments)
                self.assertFalse(allowed, reason)

    def test_python_version_can_run_but_is_not_a_build_or_test_claim(self):
        allowed, reason = validate_process(Path("workspace").resolve(), "python", ["--version"])
        self.assertTrue(allowed, reason)


if __name__ == "__main__":
    unittest.main()
