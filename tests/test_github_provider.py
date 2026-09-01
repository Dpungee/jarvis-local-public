import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from jarvis.github_provider import GitHubProvider
from jarvis.trusted_executables import windows_directory


class GitHubProviderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.repository = self.root / "project"
        self.repository.mkdir()
        (self.repository / ".git").mkdir()
        (self.repository / ".git" / "config").write_text(
            "[core]\n"
            "\trepositoryformatversion = 0\n"
            "\tbare = false\n"
            "[remote \"origin\"]\n"
            "\turl = https://github.com/octo/jarvis.git\n"
            "\tfetch = +refs/heads/*:refs/remotes/origin/*\n"
            "[branch \"main\"]\n"
            "\tremote = origin\n"
            "\tmerge = refs/heads/main\n",
            encoding="utf-8",
        )
        self.runner = Mock(return_value=subprocess.CompletedProcess([], 0, b"", b""))
        self.which = Mock(side_effect=lambda name: f"C:/tools/{name}.exe")
        gh_install_directory = Path("C:/tools/gh.exe").parent
        self.expected_gh_cwd = (
            gh_install_directory if gh_install_directory.is_dir() else self.root
        )
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

    def test_custom_executable_finder_requires_custom_runner(self):
        with self.assertRaisesRegex(ValueError, "custom executable finder"):
            GitHubProvider(self.root, which=self.which)

    def test_default_resolution_rejects_workspace_executable(self):
        poison = self.root / "attacker-gh.exe"
        poison.write_bytes(b"MZ")
        with (
            patch(
                "jarvis.trusted_executables.shutil.which",
                return_value=str(poison),
            ),
            patch("jarvis.github_provider.subprocess.run") as real_runner,
        ):
            provider = GitHubProvider(self.root)
            result = provider.auth_status()

        self.assertFalse(result.ok)
        self.assertIn("not available", result.error)
        real_runner.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows Tasks trust boundary")
    def test_default_resolution_rejects_windows_tasks_executables(self):
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
            patch("jarvis.github_provider.subprocess.run") as real_runner,
        ):
            status = GitHubProvider(self.root).cli_status()

        self.assertFalse(status.ok)
        self.assertTrue(all(
            not item["available"] for item in status.data.values()
        ))
        real_runner.assert_not_called()

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
        self.assertEqual(kwargs["cwd"], str(self.expected_gh_cwd))
        self.assertEqual(kwargs["timeout"], 17.0)
        self.assertEqual(kwargs["env"]["GH_PROMPT_DISABLED"], "1")
        self.assertNotIn("GIT_TERMINAL_PROMPT", kwargs["env"])
        self.assertNotIn("GIT_OPTIONAL_LOCKS", kwargs["env"])
        self.assertEqual(kwargs["env"]["GH_PAGER"], "")
        self.assertNotIn(str(self.root), kwargs["env"]["PATH"])

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

    def test_executable_and_network_rewriting_git_config_is_rejected(self):
        dangerous = (
            '[core]\n\tfsmonitor = powershell.exe -File payload.ps1\n',
            '[core]\n\thooksPath = C:/attacker/hooks\n',
            '[core]\n\tworktree = C:/outside\n',
            '[core]\n\tsshCommand = powershell.exe -File payload.ps1\n',
            '[include]\n\tpath = C:/attacker/config\n',
            '[credential]\n\thelper = !powershell.exe -File payload.ps1\n',
            '[diff "unsafe"]\n\ttextconv = powershell.exe -File payload.ps1\n',
            '[filter "unsafe"]\n\tprocess = powershell.exe -File payload.ps1\n',
            '[url "https://attacker.invalid/"]\n\tinsteadOf = https://github.com/\n',
            '[protocol "file"]\n\tallow = always\n',
            '[http]\n\tproxy = http://attacker.invalid:8080\n',
            '[extensions]\n\trefstorage = reftable\n',
        )
        config_path = self.repository / ".git" / "config"
        for fragment in dangerous:
            with self.subTest(fragment=fragment.splitlines()[0]):
                config_path.write_text(
                    "[core]\n\trepositoryformatversion = 0\n\tbare = false\n" + fragment,
                    encoding="utf-8",
                )
                self.runner.reset_mock()
                with self.assertRaises(PermissionError):
                    self.provider.repository_status(self.repository)
                self.runner.assert_not_called()

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
        self.assertNotIn("--source", argv)
        self.assertNotIn("--remote", argv)
        self.assertNotIn("--push", argv)
        self.assertTrue(result.data["remote_configured"])
        self.assertEqual(
            self.runner.call_args.kwargs["cwd"], str(self.expected_gh_cwd)
        )

    def test_create_uses_approved_fully_qualified_owner_and_pins_github_host(self):
        (self.repository / ".git" / "config").write_text(
            "[core]\n\trepositoryformatversion = 0\n\tbare = false\n",
            encoding="utf-8",
        )
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

    def test_create_configures_bounded_remote_then_snapshot_can_resolve_it(self):
        config_path = self.repository / ".git" / "config"
        config_path.write_text(
            "[core]\n\trepositoryformatversion = 0\n\tbare = false\n",
            encoding="utf-8",
        )
        tip = "a" * 40
        self.runner.side_effect = (
            subprocess.CompletedProcess([], 0, b"created\n", b""),
            subprocess.CompletedProcess([], 0, f"{tip}\n".encode(), b""),
        )

        created = self.provider.create_repository(
            self.repository,
            "octo/new-repository",
        )
        snapshot = self.provider.push_approval_snapshot(self.repository, "main")

        self.assertTrue(created.ok, created.error)
        self.assertTrue(created.data["remote_configured"])
        self.assertEqual(
            snapshot["remote_url"],
            "https://github.com/octo/new-repository.git",
        )
        self.assertEqual(snapshot["tip_sha"], tip)
        config = config_path.read_text(encoding="utf-8")
        self.assertIn('[remote "origin"]', config)
        self.assertNotIn("helper", config.casefold())

    def test_create_config_update_failure_preserves_original_bytes(self):
        config_path = self.repository / ".git" / "config"
        original = config_path.read_bytes()
        self.runner.return_value = subprocess.CompletedProcess([], 0, b"created\n", b"")

        with patch("jarvis.github_provider.os.replace", side_effect=OSError("simulated")):
            result = self.provider.create_repository(
                self.repository,
                "octo/atomic-failure",
            )

        self.assertFalse(result.ok)
        self.assertFalse(result.data["remote_configured"])
        self.assertEqual(config_path.read_bytes(), original)
        self.assertFalse((config_path.parent / "config.lock").exists())

    def test_create_does_not_clobber_a_preexisting_config_lock(self):
        config_path = self.repository / ".git" / "config"
        original = config_path.read_bytes()
        lock_path = config_path.parent / "config.lock"
        lock_path.write_text("operator lock", encoding="utf-8")
        self.runner.return_value = subprocess.CompletedProcess([], 0, b"created\n", b"")

        result = self.provider.create_repository(
            self.repository,
            "octo/locked-config",
        )

        self.assertFalse(result.ok)
        self.assertEqual(config_path.read_bytes(), original)
        self.assertEqual(lock_path.read_text(encoding="utf-8"), "operator lock")

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

    def test_push_requires_an_exact_approved_destination_snapshot(self):
        with self.assertRaisesRegex(PermissionError, "approved destination"):
            self.provider.push(self.repository, "feature/provider")
        self.runner.assert_not_called()

    def test_push_snapshot_binds_one_visible_github_destination_and_tip(self):
        tip = "a" * 40
        self.runner.return_value = subprocess.CompletedProcess(
            [], 0, f"{tip}\n".encode(), b""
        )

        snapshot = self.provider.push_approval_snapshot(
            self.repository, "main", remote="origin"
        )

        self.assertEqual(snapshot["remote_url"], "https://github.com/octo/jarvis.git")
        self.assertEqual(snapshot["tip_sha"], tip)
        self.assertEqual(
            self.runner.call_args.args[0][1:],
            ["rev-parse", "--verify", "refs/heads/main^{commit}"],
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
        config_path = self.repository / ".git" / "config"
        for value in unsafe_values:
            with self.subTest(remote=value):
                self.runner.reset_mock()
                config_path.write_text(
                    "[core]\n\trepositoryformatversion = 0\n\tbare = false\n"
                    f"[remote \"origin\"]\n\turl = {value}\n",
                    encoding="utf-8",
                )
                with self.assertRaises((ValueError, PermissionError)):
                    self.provider.push_approval_snapshot(self.repository, "main")
                self.runner.assert_not_called()

    def test_approved_push_rechecks_tip_and_never_runs_hook_on_mismatch(self):
        self.runner.side_effect = (
            subprocess.CompletedProcess([], 0, (("b" * 40) + "\n").encode(), b""),
        )

        with self.assertRaisesRegex(PermissionError, "changed after approval"):
            self.provider.push(
                self.repository,
                "main",
                expected_remote_url="https://github.com/octo/jarvis.git",
                expected_tip_sha="a" * 40,
            )

        self.assertEqual(self.runner.call_count, 1)
        self.assertFalse(any("push" in call.args[0][1:] for call in self.runner.call_args_list))

    def test_approved_push_uses_exact_url_and_sha_then_sets_local_upstream(self):
        tip = "a" * 40
        remote_url = "https://github.com/octo/jarvis.git"
        self.runner.side_effect = (
            subprocess.CompletedProcess([], 0, f"{tip}\n".encode(), b""),
            subprocess.CompletedProcess([], 0, b"pushed\n", b""),
        )

        result = self.provider.push(
            self.repository,
            "main",
            expected_remote_url=remote_url,
            expected_tip_sha=tip,
        )

        self.assertTrue(result.ok)
        push_argv = self.runner.call_args_list[1].args[0]
        self.assertIn("--no-verify", push_argv)
        self.assertIn(remote_url, push_argv)
        self.assertIn(f"{tip}:refs/heads/main", push_argv)
        self.assertNotIn("origin", push_argv)
        self.assertEqual(self.runner.call_count, 2)
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
        tip = "a" * 40
        self.runner.side_effect = (
            subprocess.CompletedProcess([], 0, f"{tip}\n".encode(), b""),
            subprocess.CompletedProcess([], 0, b"pushed\n", b""),
        )
        with patch.dict("os.environ", unsafe, clear=False):
            result = self.provider.push(
                self.repository,
                "main",
                expected_remote_url="https://github.com/octo/jarvis.git",
                expected_tip_sha=tip,
            )

        self.assertTrue(result.ok)
        environment = self.runner.call_args_list[-1].kwargs["env"]
        for key in unsafe:
            self.assertNotEqual(environment.get(key), unsafe[key])
        self.assertEqual(environment["GIT_DIR"], str(self.repository / ".git"))
        self.assertEqual(environment["GIT_WORK_TREE"], str(self.repository))
        self.assertEqual(environment["GIT_CONFIG_GLOBAL"], os.devnull)
        self.assertNotIn("GIT_SSH_COMMAND", environment)
        self.assertIn("--no-verify", self.runner.call_args_list[-1].args[0])

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

    @unittest.skipUnless(shutil.which("git"), "Git executable required")
    def test_real_git_status_works_inside_the_locked_envelope(self):
        live = self.root / "live"
        live.mkdir()
        git = shutil.which("git")
        subprocess.run(
            [git, "init", "--initial-branch=main", str(live)],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        result = GitHubProvider(self.root).repository_status(live)

        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.data["repository_path"], str(live))

    @unittest.skipUnless(os.name == "nt" and shutil.which("git"), "Windows Git required")
    def test_real_fsmonitor_payload_is_rejected_without_running(self):
        live = self.root / "fsmonitor"
        live.mkdir()
        git = shutil.which("git")
        subprocess.run(
            [git, "init", "--initial-branch=main", str(live)],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        marker = self.root / "fsmonitor-ran.txt"
        payload = self.root / "fsmonitor-payload.cmd"
        payload.write_text(f"@echo off\r\necho unsafe>\"{marker}\"\r\n", encoding="utf-8")
        subprocess.run(
            [git, "-C", str(live), "config", "core.fsmonitor", str(payload)],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        with self.assertRaisesRegex(PermissionError, "unsafe setting"):
            GitHubProvider(self.root).repository_status(live)

        self.assertFalse(marker.exists())

    @unittest.skipUnless(shutil.which("git"), "Git executable required")
    def test_real_outside_core_worktree_is_rejected_before_status(self):
        live = self.root / "worktree-escape"
        live.mkdir()
        outside = self.root.parent / f"{self.root.name}-outside-worktree"
        outside.mkdir()
        try:
            secret = outside / "outside-secret-name.txt"
            secret.write_text("private", encoding="utf-8")
            git = shutil.which("git")
            subprocess.run(
                [git, "init", "--initial-branch=main", str(live)],
                check=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            subprocess.run(
                [git, "-C", str(live), "config", "core.worktree", str(outside)],
                check=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            with self.assertRaisesRegex(PermissionError, "unsafe setting"):
                GitHubProvider(self.root).repository_status(live)
        finally:
            shutil.rmtree(outside)

    @unittest.skipUnless(os.name == "nt" and shutil.which("git"), "Windows Git required")
    def test_real_ssh_command_payload_is_rejected_before_approved_push(self):
        live = self.root / "ssh-command"
        live.mkdir()
        git = shutil.which("git")
        subprocess.run(
            [git, "init", "--initial-branch=main", str(live)],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        marker = self.root / "ssh-command-ran.txt"
        payload = self.root / "ssh-command-payload.cmd"
        payload.write_text(f"@echo off\r\necho unsafe>\"{marker}\"\r\n", encoding="utf-8")
        subprocess.run(
            [git, "-C", str(live), "config", "core.sshCommand", str(payload)],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        with self.assertRaisesRegex(PermissionError, "unsafe setting"):
            GitHubProvider(self.root).push(
                live,
                "main",
                expected_remote_url="https://github.com/octo/jarvis.git",
                expected_tip_sha="a" * 40,
            )

        self.assertFalse(marker.exists())

    @unittest.skipUnless(shutil.which("git"), "Git executable required")
    def test_real_common_and_alternate_object_metadata_are_rejected(self):
        git = shutil.which("git")
        outside = self.root.parent / f"{self.root.name}-outside-objects"
        outside.mkdir()
        try:
            for kind in ("commondir", "alternates", "http-alternates"):
                with self.subTest(kind=kind):
                    live = self.root / f"metadata-{kind}"
                    live.mkdir()
                    subprocess.run(
                        [git, "init", "--initial-branch=main", str(live)],
                        check=True,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                    if kind == "commondir":
                        target = live / ".git" / "commondir"
                    else:
                        info = live / ".git" / "objects" / "info"
                        info.mkdir(exist_ok=True)
                        target = info / kind
                    target.write_text(str(outside), encoding="utf-8")

                    with self.assertRaisesRegex(
                        PermissionError, "common, alternate, or graft"
                    ):
                        GitHubProvider(self.root).repository_status(live)
        finally:
            shutil.rmtree(outside)

    @unittest.skipUnless(os.name == "nt" and shutil.which("git"), "Windows Git required")
    def test_real_reparse_reference_tree_cannot_escape_workspace(self):
        live = self.root / "reparse-refs"
        live.mkdir()
        outside = self.root.parent / f"{self.root.name}-outside-refs"
        git = shutil.which("git")
        subprocess.run(
            [git, "init", "--initial-branch=main", str(live)],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        outside.mkdir()
        refs = live / ".git" / "refs" / "heads" / "nested"
        cmd = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32" / "cmd.exe"
        linked = subprocess.run(
            [str(cmd), "/d", "/c", "mklink", "/J", str(refs), str(outside)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if linked.returncode != 0:
            shutil.rmtree(outside)
            self.skipTest("Junction creation is unavailable")
        try:
            with self.assertRaisesRegex(
                PermissionError, "object and reference trees"
            ):
                GitHubProvider(self.root).repository_status(live)
        finally:
            os.rmdir(refs)
            shutil.rmtree(outside)


if __name__ == "__main__":
    unittest.main()
