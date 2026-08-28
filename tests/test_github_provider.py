import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from jarvis.github_provider import GitHubProvider


class GitHubProviderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.repository = self.root / "project"
        self.repository.mkdir()
        (self.repository / ".git").mkdir()
        self.runner = Mock(return_value=subprocess.CompletedProcess([], 0, b"", b""))
        self.which = Mock(side_effect=lambda name: f"C:/tools/{name}.exe")
        self.provider = GitHubProvider(
            self.root,
            runner=self.runner,
            which=self.which,
            timeout_seconds=17,
            max_output_bytes=1024,
        )

    def tearDown(self):
        self.temporary.cleanup()

    def test_cli_status_does_not_execute_commands(self):
        result = self.provider.cli_status()

        self.assertTrue(result.ok)
        self.assertTrue(result.data["gh"]["available"])
        self.assertTrue(result.data["git"]["available"])
        self.runner.assert_not_called()

    def test_workspace_root_cannot_be_a_filesystem_root(self):
        with self.assertRaisesRegex(ValueError, "filesystem root"):
            GitHubProvider(
                Path(self.root.anchor),
                runner=self.runner,
                which=self.which,
            )

    def test_auth_status_is_noninteractive_and_never_requests_token_output(self):
        self.runner.return_value = subprocess.CompletedProcess([], 0, b"authenticated", b"")

        result = self.provider.auth_status()

        self.assertTrue(result.ok)
        self.assertTrue(result.data["authenticated"])
        args, kwargs = self.runner.call_args
        self.assertEqual(
            args[0],
            [
                "C:/tools/gh.exe",
                "auth",
                "status",
                "--active",
                "--hostname",
                "github.com",
            ],
        )
        self.assertNotIn("--show-token", args[0])
        self.assertFalse(kwargs["shell"])
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["cwd"], str(self.root))
        self.assertEqual(kwargs["timeout"], 17.0)
        self.assertEqual(kwargs["env"]["GH_PROMPT_DISABLED"], "1")
        self.assertEqual(kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(kwargs["env"]["GIT_OPTIONAL_LOCKS"], "0")

    def test_repository_status_uses_explicit_contained_cwd_and_parses_changes(self):
        self.runner.return_value = subprocess.CompletedProcess(
            [], 0, b"## main...origin/main\n M app.py\n?? new.py\n", b""
        )

        result = self.provider.repository_status("project")

        self.assertTrue(result.ok)
        self.assertEqual(result.data["branch"], "main...origin/main")
        self.assertFalse(result.data["clean"])
        self.assertEqual(result.data["changes"], [" M app.py", "?? new.py"])
        args, kwargs = self.runner.call_args
        self.assertEqual(args[0][0:2], ["C:/tools/git.exe", "status"])
        self.assertEqual(kwargs["cwd"], str(self.repository))
        self.assertNotIn("-C", args[0])

    def test_repository_path_escape_and_non_repository_are_rejected_before_execution(self):
        outside = self.root.parent / f"{self.root.name}-outside"
        outside.mkdir(exist_ok=True)
        try:
            (outside / ".git").mkdir(exist_ok=True)
            with self.assertRaises(PermissionError):
                self.provider.repository_status(outside)
            empty = self.root / "empty"
            empty.mkdir()
            with self.assertRaisesRegex(ValueError, "root of a Git repository"):
                self.provider.repository_status(empty)
            self.runner.assert_not_called()
        finally:
            (outside / ".git").rmdir()
            outside.rmdir()

    def test_gitfile_cannot_redirect_metadata_outside_workspace(self):
        worktree = self.root / "worktree"
        worktree.mkdir()
        outside = self.root.parent / f"{self.root.name}-metadata"
        outside.mkdir(exist_ok=True)
        try:
            (worktree / ".git").write_text(f"gitdir: {outside}\n", encoding="utf-8")
            with self.assertRaisesRegex(PermissionError, "metadata"):
                self.provider.repository_status(worktree)
            self.runner.assert_not_called()
        finally:
            outside.rmdir()

    def test_list_repositories_uses_bounded_json_contract(self):
        repositories = [{
            "name": "jarvis",
            "nameWithOwner": "octo/jarvis",
            "url": "https://github.com/octo/jarvis",
            "visibility": "PRIVATE",
        }]
        self.runner.return_value = subprocess.CompletedProcess(
            [], 0, json.dumps(repositories).encode(), b""
        )

        result = self.provider.list_repositories("octo", limit=12)

        self.assertTrue(result.ok)
        self.assertEqual(result.data, repositories)
        argv = self.runner.call_args.args[0]
        self.assertEqual(argv[:4], ["C:/tools/gh.exe", "repo", "list", "octo"])
        self.assertEqual(argv[argv.index("--limit") + 1], "12")
        self.assertIn("--json", argv)

    def test_list_repositories_rejects_unbounded_or_malformed_inputs(self):
        for limit in (0, 101, True, "10"):
            with self.subTest(limit=limit):
                with self.assertRaises((TypeError, ValueError)):
                    self.provider.list_repositories(limit=limit)
        for owner in ("-option", "owner/repo", "owner--name"):
            with self.subTest(owner=owner), self.assertRaises(ValueError):
                self.provider.list_repositories(owner)
        self.runner.assert_not_called()

    def test_invalid_repository_json_is_a_structured_failure(self):
        self.runner.return_value = subprocess.CompletedProcess([], 0, b"not-json", b"")

        result = self.provider.list_repositories()

        self.assertFalse(result.ok)
        self.assertEqual(result.data, [])
        self.assertIn("invalid repository JSON", result.error)

    def test_create_is_private_by_default_and_never_pushes(self):
        self.runner.return_value = subprocess.CompletedProcess(
            [], 0, b"https://github.com/octo/jarvis\n", b""
        )

        result = self.provider.create_repository(
            self.repository,
            "octo/jarvis",
            description="Local assistant",
        )

        self.assertTrue(result.ok)
        self.assertFalse(result.data["pushed"])
        argv = self.runner.call_args.args[0]
        self.assertEqual(argv[:4], ["C:/tools/gh.exe", "repo", "create", "octo/jarvis"])
        self.assertIn("--private", argv)
        self.assertEqual(argv[argv.index("--source") + 1], str(self.repository))
        self.assertEqual(argv[argv.index("--remote") + 1], "origin")
        self.assertNotIn("--push", argv)

    def test_create_uses_approved_fully_qualified_owner_and_pins_github_host(self):
        approved = {
            "resolved_path": str(self.repository),
            "authenticated_login": "approved-owner",
            "repository_slug": "approved-owner/demo",
        }
        self.runner.side_effect = (
            subprocess.CompletedProcess([], 0, b"approved-owner\n", b""),
            subprocess.CompletedProcess(
                [], 0, b"https://github.com/approved-owner/demo\n", b""
            ),
        )

        with patch.dict("os.environ", {"GH_HOST": "enterprise.example"}, clear=False):
            result = self.provider.create_repository(
                self.repository,
                "demo",
                expected_approval_snapshot=approved,
            )

        self.assertTrue(result.ok)
        account_argv = self.runner.call_args_list[0].args[0]
        self.assertEqual(
            account_argv[1:7],
            ["api", "--hostname", "github.com", "user", "--jq", ".login"],
        )
        create_argv = self.runner.call_args_list[1].args[0]
        self.assertEqual(create_argv[1:4], ["repo", "create", "approved-owner/demo"])
        for call in self.runner.call_args_list:
            self.assertEqual(call.kwargs["env"]["GH_HOST"], "github.com")

    def test_create_rejects_option_injection_and_invalid_visibility_before_execution(self):
        cases = (
            {"name": "--help"},
            {"name": "owner/repo/extra"},
            {"name": 123},
            {"name": "repo", "visibility": "secret"},
            {"name": "repo", "remote": "--upload-pack=bad"},
            {"name": "repo", "description": "line one\nline two"},
        )
        for values in cases:
            name = values["name"]
            options = {key: value for key, value in values.items() if key != "name"}
            with self.subTest(values=values), self.assertRaises((TypeError, ValueError)):
                self.provider.create_repository(self.repository, name, **options)
        self.runner.assert_not_called()

    def test_push_accepts_only_one_non_force_branch(self):
        result = self.provider.push(self.repository, "feature/provider")

        self.assertTrue(result.ok)
        argv = self.runner.call_args.args[0]
        self.assertEqual(
            argv,
            [
                "C:/tools/git.exe",
                "push",
                "--no-verify",
                "--set-upstream",
                "origin",
                "feature/provider",
            ],
        )
        self.assertNotIn("--force", argv)
        self.assertIn("--no-verify", argv)
        self.assertEqual(self.runner.call_args.kwargs["cwd"], str(self.repository))

    def test_push_snapshot_binds_one_visible_github_destination_and_tip(self):
        tip = "a" * 40
        self.runner.side_effect = (
            subprocess.CompletedProcess([], 0, b"git@github.com:octo/jarvis.git\n", b""),
            subprocess.CompletedProcess([], 0, f"{tip}\n".encode(), b""),
        )

        snapshot = self.provider.push_approval_snapshot(
            self.repository, "main", remote="origin"
        )

        self.assertEqual(snapshot["remote_url"], "git@github.com:octo/jarvis.git")
        self.assertEqual(snapshot["tip_sha"], tip)
        self.assertEqual(
            self.runner.call_args_list[0].args[0][1:],
            ["remote", "get-url", "--push", "--all", "origin"],
        )

    def test_push_snapshot_rejects_hidden_extra_and_unsafe_remote_helpers(self):
        multiple = "\n".join(
            ["https://github.com/octo/jarvis.git", *[
                f"https://github.com/octo/hidden-{index}.git" for index in range(1, 6)
            ]]
        )
        unsafe_values = (
            multiple,
            "ext::powershell -File steal.ps1",
            "file:///tmp/repository.git",
            "https://github.com/octo/jarvis.git?access_token=secret-value",
            "https://github.com/octo/jarvis.git#credential",
        )
        for value in unsafe_values:
            with self.subTest(remote=value):
                self.runner.reset_mock()
                self.runner.side_effect = None
                self.runner.return_value = subprocess.CompletedProcess(
                    [], 0, f"{value}\n".encode(), b""
                )
                with self.assertRaisesRegex(ValueError, "exactly one credential-free"):
                    self.provider.push_approval_snapshot(self.repository, "main")
                self.assertEqual(self.runner.call_count, 1)

    def test_approved_push_rechecks_tip_and_never_runs_hook_on_mismatch(self):
        self.runner.side_effect = (
            subprocess.CompletedProcess(
                [], 0, b"https://github.com/octo/jarvis.git\n", b""
            ),
            subprocess.CompletedProcess([], 0, (("b" * 40) + "\n").encode(), b""),
        )

        with self.assertRaisesRegex(PermissionError, "changed after approval"):
            self.provider.push(
                self.repository,
                "main",
                expected_remote_url="https://github.com/octo/jarvis.git",
                expected_tip_sha="a" * 40,
            )

        self.assertEqual(self.runner.call_count, 2)
        self.assertFalse(any("push" in call.args[0][1:] for call in self.runner.call_args_list))

    def test_approved_push_uses_exact_url_and_sha_then_sets_local_upstream(self):
        tip = "a" * 40
        remote_url = "https://github.com/octo/jarvis.git"
        self.runner.side_effect = (
            subprocess.CompletedProcess([], 0, f"{remote_url}\n".encode(), b""),
            subprocess.CompletedProcess([], 0, f"{tip}\n".encode(), b""),
            subprocess.CompletedProcess([], 0, b"pushed\n", b""),
            subprocess.CompletedProcess([], 0, b"", b""),
            subprocess.CompletedProcess([], 0, b"", b""),
        )

        result = self.provider.push(
            self.repository,
            "main",
            expected_remote_url=remote_url,
            expected_tip_sha=tip,
        )

        self.assertTrue(result.ok)
        push_argv = self.runner.call_args_list[2].args[0]
        self.assertIn("--no-verify", push_argv)
        self.assertIn(remote_url, push_argv)
        self.assertIn(f"{tip}:refs/heads/main", push_argv)
        self.assertNotIn("origin", push_argv)
        self.assertEqual(
            self.runner.call_args_list[3].args[0][1:5],
            ["config", "--local", "--replace-all", "branch.main.remote"],
        )
        self.assertTrue(result.data["upstream_configured"])

    def test_git_override_environment_is_scrubbed_for_push(self):
        unsafe = {
            "GIT_SSH_COMMAND": "powershell -File steal.ps1",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.hooksPath",
            "GIT_CONFIG_VALUE_0": "C:/malicious-hooks",
            "GIT_DIR": "C:/elsewhere/.git",
            "OPENAI_API_KEY": "sk-test-unrelated-secret",
            "ANTHROPIC_API_KEY": "sk-ant-test-unrelated-secret",
            "AWS_SECRET_ACCESS_KEY": "test-unrelated-secret",
            "JARVIS_CONNECTOR_SOCIAL_TOKEN": "test-unrelated-secret",
        }
        with patch.dict("os.environ", unsafe, clear=False):
            result = self.provider.push(self.repository, "main")

        self.assertTrue(result.ok)
        environment = self.runner.call_args.kwargs["env"]
        for key in unsafe:
            self.assertNotIn(key, environment)
        self.assertIn("--no-verify", self.runner.call_args.args[0])

    def test_push_rejects_refspecs_options_and_invalid_remote_before_execution(self):
        for branch in (
            "--all", "+main:main", "main:other", "main..other", "refs/heads/x^y", 123
        ):
            with self.subTest(branch=branch), self.assertRaises((TypeError, ValueError)):
                self.provider.push(self.repository, branch)
        with self.assertRaises(ValueError):
            self.provider.push(self.repository, "main", remote="--force")
        with self.assertRaises(TypeError):
            self.provider.push(self.repository, "main", set_upstream="yes")
        self.runner.assert_not_called()

    def test_missing_cli_timeout_output_bound_and_secret_redaction_are_structured(self):
        unavailable = GitHubProvider(
            self.root,
            runner=self.runner,
            which=lambda _name: None,
            max_output_bytes=1024,
        )
        missing = unavailable.auth_status()
        self.assertFalse(missing.ok)
        self.assertIn("not available", missing.error)
        self.runner.assert_not_called()

        self.runner.side_effect = subprocess.TimeoutExpired(
            ["gh"],
            17,
            output=(b"x" * 2000) + b" ghp_abcdefghijklmnopqrstuvwxyz",
            stderr=b"timed out",
        )
        timed_out = self.provider.auth_status()
        self.assertFalse(timed_out.ok)
        self.assertTrue(timed_out.timed_out)
        self.assertTrue(timed_out.truncated)
        self.assertLessEqual(len(timed_out.stdout.encode("utf-8")), 1024)

        self.runner.side_effect = None
        self.runner.return_value = subprocess.CompletedProcess(
            [], 1, b"", b"token github_pat_abcdefghijklmnopqrstuvwxyz failed"
        )
        failed = self.provider.auth_status()
        self.assertFalse(failed.ok)
        self.assertNotIn("github_pat_abcdefghijklmnopqrstuvwxyz", failed.error)
        self.assertIn("[redacted]", failed.error)


if __name__ == "__main__":
    unittest.main()
