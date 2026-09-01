from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis.vercel_provider import (
    VercelCLIUnavailableError,
    VercelProvider,
    VercelWorkspaceError,
    _deployment_tree_digest,
)
from jarvis.trusted_executables import windows_directory


class FakeRunner:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(self, args, **kwargs):
        command = list(args)
        self.calls.append((command, kwargs))
        if not self.responses:
            raise AssertionError(f"Unexpected CLI call: {command}")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        if callable(response):
            return response(command, kwargs)
        return subprocess.CompletedProcess(
            command,
            response.get("returncode", 0),
            stdout=response.get("stdout", ""),
            stderr=response.get("stderr", ""),
        )


class VercelProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name).resolve()
        self.project = self.workspace / "site"
        self.project.mkdir()
        (self.project / "package.json").write_text("{}", encoding="utf-8")
        self.cli = str(self.workspace / "bin" / "vercel.cmd")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def provider(self, runner: FakeRunner, **kwargs) -> VercelProvider:
        return VercelProvider(
            self.workspace,
            cli_path=self.cli,
            runner=runner,
            **kwargs,
        )

    def assert_safe_call(self, call, *, cwd: Path, timeout_at_most: float) -> None:
        command, kwargs = call
        self.assertIsInstance(command, list)
        self.assertEqual(command[0], self.cli)
        self.assertFalse(kwargs["shell"])
        self.assertNotIn("capture_output", kwargs)
        self.assertTrue(hasattr(kwargs["stdout"], "fileno"))
        self.assertTrue(hasattr(kwargs["stderr"], "fileno"))
        self.assertFalse(kwargs["check"])
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["cwd"], str(cwd))
        self.assertLessEqual(float(kwargs["timeout"]), timeout_at_most)
        self.assertEqual(kwargs["env"]["CI"], "1")
        self.assertEqual(kwargs["env"]["NO_COLOR"], "1")
        self.assertIn("--no-color", command)
        self.assertEqual(command[command.index("--cwd") + 1], str(cwd))

    def test_unavailable_status_never_falls_back_to_npx(self) -> None:
        runner = FakeRunner()
        provider = VercelProvider(
            self.workspace,
            executable_finder=lambda _name: None,
            runner=runner,
        )

        status = provider.status()

        self.assertFalse(status.available)
        self.assertIsNone(status.cli_path)
        self.assertIn("not installed", status.error)
        with self.assertRaises(VercelCLIUnavailableError):
            provider.list_projects()
        self.assertEqual(runner.calls, [])

    def test_custom_executable_requires_custom_runner(self) -> None:
        with self.assertRaisesRegex(ValueError, "custom Vercel executable"):
            VercelProvider(self.workspace, cli_path=self.cli)
        with self.assertRaisesRegex(ValueError, "custom Vercel executable"):
            VercelProvider(
                self.workspace,
                executable_finder=lambda _name: self.cli,
            )

    def test_default_resolution_rejects_workspace_executable(self) -> None:
        poison = self.workspace / "vercel.exe"
        poison.write_bytes(b"MZ")
        with (
            patch(
                "jarvis.trusted_executables.shutil.which",
                return_value=str(poison),
            ),
            patch("jarvis.vercel_provider.subprocess.run") as real_runner,
        ):
            provider = VercelProvider(self.workspace)
            status = provider.status()

        self.assertFalse(status.available)
        self.assertIsNone(status.cli_path)
        real_runner.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows Tasks trust boundary")
    def test_default_resolution_rejects_windows_tasks_executable(self) -> None:
        poison = windows_directory() / "Tasks" / "vercel.exe"
        with (
            patch(
                "jarvis.trusted_executables.shutil.which",
                return_value=str(poison),
            ),
            patch(
                "jarvis.trusted_executables._ordinary_executable",
                return_value=poison,
            ),
            patch("jarvis.vercel_provider.subprocess.run") as real_runner,
        ):
            status = VercelProvider(self.workspace).status()

        self.assertFalse(status.available)
        self.assertIsNone(status.cli_path)
        real_runner.assert_not_called()

    def test_status_auth_and_project_listing_use_read_only_json_commands(self) -> None:
        runner = FakeRunner(
            {"stdout": "Vercel CLI 56.0.0\n"},
            {"stdout": "jarvis-user\n"},
            {"stdout": json.dumps({"projects": [{"id": "prj_1", "name": "alpha"}]})},
        )
        provider = self.provider(runner, command_timeout_seconds=12)

        status = provider.status()
        projects = provider.list_projects()

        self.assertTrue(status.available)
        self.assertEqual(status.version, "Vercel CLI 56.0.0")
        self.assertTrue(status.authenticated)
        self.assertEqual(status.user, "jarvis-user")
        self.assertIsNone(status.error)
        self.assertTrue(projects.ok)
        self.assertEqual(projects.data["projects"][0]["name"], "alpha")
        self.assertEqual(runner.calls[0][0][1:3], ["--version", "--no-color"])
        self.assertEqual(runner.calls[1][0][1:3], ["whoami", "--no-color"])
        self.assertEqual(runner.calls[2][0][1:4], ["project", "ls", "--json"])
        for call in runner.calls:
            self.assert_safe_call(call, cwd=self.workspace, timeout_at_most=12)

    def test_auth_failure_is_structured_and_never_starts_login(self) -> None:
        runner = FakeRunner({"returncode": 1, "stderr": "Error: Not authenticated\n"})
        provider = self.provider(runner)

        auth = provider.auth_status()

        self.assertFalse(auth.ok)
        self.assertEqual(auth.data, {"authenticated": False, "user": None})
        self.assertIn("Not authenticated", auth.error)
        self.assertEqual(runner.calls[0][0][1], "whoami")
        self.assertNotIn("login", runner.calls[0][0])

    def test_deploy_is_explicit_noninteractive_and_workspace_scoped(self) -> None:
        runner = FakeRunner(
            {"stdout": "https://site-preview-abc.vercel.app\n"},
            {"stdout": "https://site-production-abc.vercel.app\n"},
        )
        provider = self.provider(runner, deploy_timeout_seconds=90)

        preview = provider.deploy("site")
        production = provider.deploy(self.project, production=True, wait=True)

        self.assertTrue(preview.ok)
        self.assertEqual(preview.data, {
            "deployment_url": "https://site-preview-abc.vercel.app",
            "target": "preview",
        })
        preview_command = runner.calls[0][0]
        self.assertEqual(preview_command[1:5], [
            "deploy", "--yes", "--target=preview", "--no-wait",
        ])
        self.assertTrue(production.ok)
        production_command = runner.calls[1][0]
        self.assertIn("--prod", production_command)
        self.assertNotIn("--no-wait", production_command)
        self.assertNotIn("--target=preview", production_command)
        for call in runner.calls:
            self.assert_safe_call(call, cwd=self.project, timeout_at_most=90)

    def test_deploy_snapshot_binds_linked_destination_and_tree(self) -> None:
        link = self.project / ".vercel"
        link.mkdir()
        (link / "project.json").write_text(
            json.dumps({"projectId": "prj_alpha", "orgId": "org_owner"}),
            encoding="utf-8",
        )
        provider = self.provider(FakeRunner())

        first = provider.deployment_approval_snapshot("site")
        (self.project / "package.json").write_text('{"changed":true}', encoding="utf-8")
        second = provider.deployment_approval_snapshot("site")

        self.assertEqual(first["project_id"], "prj_alpha")
        self.assertEqual(first["org_id"], "org_owner")
        self.assertNotEqual(first["deploy_tree_sha256"], second["deploy_tree_sha256"])

    def test_deploy_rechecks_approved_snapshot_before_cli_execution(self) -> None:
        link = self.project / ".vercel"
        link.mkdir()
        (link / "project.json").write_text(
            json.dumps({"projectId": "prj_alpha", "orgId": "org_owner"}),
            encoding="utf-8",
        )
        runner = FakeRunner()
        provider = self.provider(runner)
        approved = provider.deployment_approval_snapshot("site")
        (self.project / "package.json").write_text("changed", encoding="utf-8")

        with self.assertRaisesRegex(PermissionError, "changed after approval"):
            provider.deploy("site", expected_approval_snapshot=approved)

        self.assertEqual(runner.calls, [])

    def test_approved_deploy_scrubs_and_pins_destination_environment(self) -> None:
        link = self.project / ".vercel"
        link.mkdir()
        (link / "project.json").write_text(
            json.dumps({"projectId": "prj_alpha", "orgId": "org_owner"}),
            encoding="utf-8",
        )
        runner = FakeRunner({"stdout": "https://site-preview.vercel.app\n"})
        provider = self.provider(runner, scope="approved-team")
        approved = provider.deployment_approval_snapshot("site")
        ambient = {
            "VERCEL_ORG_ID": "org_attacker",
            "VERCEL_PROJECT_ID": "prj_attacker",
            "VERCEL_SCOPE": "attacker-team",
            "VERCEL_TEAM_ID": "team_attacker",
            "OPENAI_API_KEY": "sk-test-unrelated-secret",
            "ANTHROPIC_API_KEY": "sk-ant-test-unrelated-secret",
            "AWS_SECRET_ACCESS_KEY": "test-unrelated-secret",
            "JARVIS_CONNECTOR_SOCIAL_TOKEN": "test-unrelated-secret",
        }

        with patch.dict(os.environ, ambient, clear=False):
            result = provider.deploy("site", expected_approval_snapshot=approved)

        self.assertTrue(result.ok)
        environment = runner.calls[0][1]["env"]
        self.assertEqual(environment["VERCEL_ORG_ID"], "org_owner")
        self.assertEqual(environment["VERCEL_PROJECT_ID"], "prj_alpha")
        self.assertEqual(environment["VERCEL_SCOPE"], "approved-team")
        self.assertNotIn("VERCEL_TEAM_ID", environment)
        for key in (
            "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "AWS_SECRET_ACCESS_KEY",
            "JARVIS_CONNECTOR_SOCIAL_TOKEN",
        ):
            self.assertNotIn(key, environment)

    def test_deploy_snapshot_rejects_oversize_before_opening_file(self) -> None:
        tree = self.workspace / "oversize-tree"
        tree.mkdir()
        oversized = tree / "huge.bin"
        oversized.write_bytes(b"12345")
        real_open = os.open
        opened_oversized = []

        def guarded_open(path, *args, **kwargs):
            if Path(path) == oversized:
                opened_oversized.append(path)
            return real_open(path, *args, **kwargs)

        with (
            patch("jarvis.vercel_provider.MAX_DEPLOY_SNAPSHOT_BYTES", 4),
            patch("jarvis.vercel_provider.MAX_DEPLOY_SNAPSHOT_FILE_BYTES", 4),
            patch("jarvis.vercel_provider.os.open", side_effect=guarded_open),
            self.assertRaisesRegex(VercelWorkspaceError, "size limit|byte limit"),
        ):
            _deployment_tree_digest(tree, source_project=False)

        self.assertEqual(opened_oversized, [])

    def test_deploy_snapshot_bounds_directories_and_concurrent_growth(self) -> None:
        many = self.workspace / "many-directories"
        many.mkdir()
        for index in range(3):
            (many / f"directory-{index}").mkdir()
        with (
            patch("jarvis.vercel_provider.MAX_DEPLOY_SNAPSHOT_DIRECTORIES", 2),
            self.assertRaisesRegex(VercelWorkspaceError, "too many directories"),
        ):
            _deployment_tree_digest(many, source_project=False)

        growing = self.workspace / "growing-tree"
        growing.mkdir()
        target = growing / "app.txt"
        target.write_bytes(b"base")
        real_fstat = os.fstat
        appended = False

        def append_after_open(descriptor):
            nonlocal appended
            details = real_fstat(descriptor)
            if not appended:
                appended = True
                with target.open("ab") as stream:
                    stream.write(b"-concurrent-growth")
            return details

        with (
            patch("jarvis.vercel_provider.os.fstat", side_effect=append_after_open),
            self.assertRaisesRegex(VercelWorkspaceError, "grew beyond"),
        ):
            _deployment_tree_digest(growing, source_project=False)

    def test_deploy_rejects_escape_missing_file_and_option_injection_before_runner(self) -> None:
        runner = FakeRunner()
        provider = self.provider(runner)
        outside = self.workspace.parent / "outside-vercel-provider"

        with self.assertRaises(VercelWorkspaceError):
            provider.deploy(outside)
        with self.assertRaises(VercelWorkspaceError):
            provider.deploy("..")
        with self.assertRaises(VercelWorkspaceError):
            provider.deploy("missing")
        with self.assertRaises(ValueError):
            provider.deploy("site", target="--prod")
        with self.assertRaises(ValueError):
            provider.deployment_status("--yes", project_path="site")
        self.assertEqual(runner.calls, [])

    def test_project_and_deployment_status_are_bounded_read_only_inspections(self) -> None:
        runner = FakeRunner(
            {"stdout": "Project alpha: framework nextjs\n"},
            {"stdout": "status READY\nurl https://site.vercel.app\n"},
            {"stdout": "build line one\nbuild line two\n"},
        )
        provider = self.provider(runner)

        project = provider.project_status("alpha", project_path="site")
        deployment = provider.deployment_status("dpl_abc123", project_path="site")
        build_logs = provider.build_logs(
            "https://site-preview.vercel.app",
            project_path="site",
        )

        self.assertTrue(project.ok)
        self.assertIn("framework nextjs", project.data["details"])
        self.assertTrue(deployment.ok)
        self.assertIn("READY", deployment.data["details"])
        self.assertEqual(build_logs.data["lines"], ["build line one", "build line two"])
        self.assertEqual(runner.calls[0][0][1:4], ["project", "inspect", "alpha"])
        self.assertEqual(runner.calls[1][0][1:3], ["inspect", "dpl_abc123"])
        self.assertEqual(
            runner.calls[2][0][1:4],
            ["inspect", "https://site-preview.vercel.app", "--logs"],
        )
        for call in runner.calls:
            self.assert_safe_call(call, cwd=self.project, timeout_at_most=30)

    def test_runtime_logs_are_non_following_limited_and_parsed_as_json_lines(self) -> None:
        lines = [
            {"level": "info", "message": "ready"},
            {"level": "error", "message": "bounded failure"},
        ]
        runner = FakeRunner({"stdout": "\n".join(json.dumps(line) for line in lines)})
        provider = self.provider(runner)

        result = provider.logs(
            "dpl_abc123",
            project_path="site",
            limit=20,
            since="30m",
            level="error",
            environment="preview",
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.data, {"entries": lines, "complete": True})
        command = runner.calls[0][0]
        for expected in (
            "logs", "--json", "--no-follow", "--limit", "20", "--since", "30m",
            "--deployment", "dpl_abc123", "--level", "error", "--environment", "preview",
        ):
            self.assertIn(expected, command)
        self.assertNotIn("--follow", command)
        self.assert_safe_call(runner.calls[0], cwd=self.project, timeout_at_most=30)

    def test_runtime_logs_and_errors_redact_secrets_without_breaking_json_lines(self) -> None:
        token = "sk-proj-" + "R" * 24
        lines = [
            {"level": "error", "message": f"upstream leaked {token}"},
            {"level": "info", "api_key": "ordinary-secret-value"},
        ]
        runner = FakeRunner({"stdout": "\n".join(json.dumps(line) for line in lines)})
        provider = self.provider(runner)

        result = provider.logs("dpl_abc123", project_path="site")

        self.assertTrue(result.ok)
        self.assertNotIn(token, result.stdout)
        self.assertNotIn("ordinary-secret-value", result.stdout)
        self.assertEqual(result.data["entries"][0]["message"], "upstream leaked [REDACTED]")
        self.assertEqual(result.data["entries"][1]["api_key"], "[REDACTED]")

        failed = self.provider(
            FakeRunner(OSError(f"cannot start {token}"))
        ).auth_status()
        self.assertFalse(failed.ok)
        self.assertNotIn(token, str(failed.error))

    def test_timeout_and_output_are_bounded(self) -> None:
        timeout = subprocess.TimeoutExpired(
            cmd=[self.cli, "inspect"],
            timeout=1,
            output="x" * 5_000,
            stderr="y" * 5_000,
        )
        runner = FakeRunner(timeout)
        provider = self.provider(
            runner,
            command_timeout_seconds=1,
            max_output_chars=1_024,
        )

        result = provider.deployment_status("dpl_timeout", project_path="site")

        self.assertFalse(result.ok)
        self.assertTrue(result.timed_out)
        self.assertTrue(result.truncated)
        self.assertLessEqual(len(result.stdout), 1_024)
        self.assertLessEqual(len(result.stderr), 1_024)
        self.assertIn("timed out after 1 seconds", result.error)
        self.assert_safe_call(runner.calls[0], cwd=self.project, timeout_at_most=1)

    def test_runner_stream_files_are_read_without_capture_output(self) -> None:
        payload = json.dumps({"projects": [{"name": "streamed"}]})

        def write_streams(command, kwargs):
            kwargs["stdout"].write(payload.encode("utf-8"))
            kwargs["stderr"].write(b"diagnostic")
            return subprocess.CompletedProcess(command, 0, stdout=None, stderr=None)

        runner = FakeRunner(write_streams)
        provider = self.provider(runner)

        result = provider.list_projects()

        self.assertTrue(result.ok)
        self.assertEqual(result.data, {"projects": [{"name": "streamed"}]})
        self.assertEqual(result.stderr, "diagnostic")
        self.assert_safe_call(runner.calls[0], cwd=self.workspace, timeout_at_most=30)

    def test_database_discovery_and_installed_resources_use_current_read_only_cli(self) -> None:
        marketplace = {
            "integrations": [
                {"slug": "neon", "name": "Neon Postgres", "category": "Storage"},
                {"slug": "upstash", "name": "Upstash Redis", "category": "Storage"},
                {"slug": "blob-store", "name": "Media blob storage", "category": "Storage"},
                {"slug": "sentry", "name": "Sentry", "category": "Observability"},
            ],
        }
        installed = {
            "resources": [
                {"name": "main-db", "integration": "neon", "product": "Postgres"},
                {"name": "events", "integration": "analytics", "product": "Analytics"},
            ],
        }
        runner = FakeRunner(
            {"stdout": json.dumps(marketplace)},
            {"stdout": json.dumps(installed)},
        )
        provider = self.provider(runner)

        discovered = provider.discover_database_integrations()
        connected = provider.list_database_integrations("alpha", project_path="site")

        self.assertTrue(discovered.ok)
        self.assertEqual(
            {item["slug"] for item in discovered.data},
            {"neon", "upstash"},
        )
        self.assertTrue(connected.ok)
        self.assertEqual([item["name"] for item in connected.data], ["main-db"])
        self.assertEqual(
            runner.calls[0][0][1:4],
            ["integration", "discover", "--format=json"],
        )
        self.assertEqual(
            runner.calls[1][0][1:5],
            ["integration", "list", "alpha", "--format=json"],
        )
        self.assert_safe_call(runner.calls[0], cwd=self.workspace, timeout_at_most=30)
        self.assert_safe_call(runner.calls[1], cwd=self.project, timeout_at_most=30)

    def test_database_provisioning_requires_explicit_choices_and_parses_json(self) -> None:
        provisioned = {
            "resource": {
                "name": "jarvis-db",
                "status": "ready",
                "integration": "neon",
                "product": "postgres",
            },
        }
        runner = FakeRunner({"stdout": json.dumps(provisioned)})
        provider = self.provider(runner, deploy_timeout_seconds=75)

        result = provider.provision_database(
            "neon/postgres",
            resource_name="jarvis-db",
            plan="free",
            metadata={"version": 17, "region": "iad1", "pooled": True},
            environments=("development", "preview"),
            connect=True,
            project_path="site",
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.data, provisioned)
        command = runner.calls[0][0]
        self.assertEqual(command[1:8], [
            "integration", "add", "neon/postgres", "--name", "jarvis-db", "--plan", "free",
        ])
        self.assertEqual(
            [command[index + 1] for index, value in enumerate(command) if value == "--metadata"],
            ["pooled=true", "region=iad1", "version=17"],
        )
        self.assertEqual(
            [command[index + 1] for index, value in enumerate(command) if value == "--environment"],
            ["development", "preview"],
        )
        self.assertIn("--no-env-pull", command)
        self.assertIn("--json", command)
        self.assertIn("--non-interactive", command)
        self.assertNotIn("--no-connect", command)
        self.assertNotIn("login", command)
        self.assert_safe_call(runner.calls[0], cwd=self.project, timeout_at_most=75)

    def test_database_provisioning_can_explicitly_avoid_project_connection(self) -> None:
        runner = FakeRunner({"stdout": json.dumps({"resource": {"name": "isolated-db"}})})
        provider = self.provider(runner)

        result = provider.add_integration(
            "neon",
            resource_name="isolated-db",
            plan="free",
            metadata={},
            connect=False,
            project_path="site",
        )

        self.assertTrue(result.ok)
        command = runner.calls[0][0]
        self.assertIn("--no-connect", command)
        self.assertIn("--no-env-pull", command)
        self.assertNotIn("--environment", command)
        self.assertIn("--non-interactive", command)

    def test_database_provisioning_rejects_implicit_or_unbounded_choices(self) -> None:
        runner = FakeRunner()
        provider = self.provider(runner)
        base = {
            "resource_name": "jarvis-db",
            "plan": "free",
            "project_path": "site",
        }
        invalid_calls = (
            lambda: provider.provision_database("", **base),
            lambda: provider.provision_database("neon/postgres/extra", **base),
            lambda: provider.provision_database("neon", **{**base, "resource_name": "--name"}),
            lambda: provider.provision_database("neon", **{**base, "plan": ""}),
            lambda: provider.provision_database(
                "neon", **base, metadata={f"key{index}": "value" for index in range(17)}
            ),
            lambda: provider.provision_database("neon", **base, metadata={"region": "x\n--prod"}),
            lambda: provider.provision_database("neon", **base, metadata={"region": "   "}),
            lambda: provider.provision_database("neon", **base, metadata={"region": {"nested": True}}),
            lambda: provider.provision_database(
                "neon", **base, environments=("preview", "preview")
            ),
            lambda: provider.provision_database("neon", **base, environments=("staging",)),
            lambda: provider.provision_database("neon", **base, environments=(), connect=True),
            lambda: provider.provision_database("neon", **base, connect=1),
        )

        for invalid_call in invalid_calls:
            with self.subTest(call=invalid_call):
                with self.assertRaises((TypeError, ValueError)):
                    invalid_call()
        self.assertEqual(runner.calls, [])

    def test_database_provisioning_fails_closed_on_malformed_or_oversized_json(self) -> None:
        runner = FakeRunner(
            {"stdout": "not-json"},
            {"stdout": json.dumps({"resource": {"detail": "x" * 2_000}})},
        )
        provider = self.provider(runner, max_output_chars=1_024)

        malformed = provider.provision_database(
            "neon",
            resource_name="first-db",
            plan="free",
            connect=False,
            environments=(),
            project_path="site",
        )
        oversized = provider.provision_database(
            "neon",
            resource_name="second-db",
            plan="free",
            connect=False,
            environments=(),
            project_path="site",
        )

        self.assertFalse(malformed.ok)
        self.assertIn("malformed JSON", malformed.error)
        self.assertFalse(oversized.ok)
        self.assertTrue(oversized.truncated)
        self.assertIn("exceeded", oversized.error)

    def test_symlinked_project_cannot_escape_workspace_when_supported(self) -> None:
        with tempfile.TemporaryDirectory() as outside_temp:
            link = self.workspace / "linked-outside"
            try:
                link.symlink_to(Path(outside_temp), target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("directory symlinks are unavailable")
            runner = FakeRunner()
            provider = self.provider(runner)

            with self.assertRaises(VercelWorkspaceError):
                provider.project_status(project_path=link)
            self.assertEqual(runner.calls, [])


if __name__ == "__main__":
    unittest.main()
