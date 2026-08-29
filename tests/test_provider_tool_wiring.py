from __future__ import annotations

import json
import os
import shutil
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

from jarvis.config import Config
from jarvis.github_provider import GitHubResult
from jarvis.memory import Memory
from jarvis.tools import ToolBox
from jarvis.vercel_provider import VercelResult, VercelStatus

TEMP_ROOT = Path(__file__).resolve().parent / ".tmp"
TEMP_ROOT.mkdir(exist_ok=True)


class ProviderToolWiringTests(unittest.TestCase):
    """Exercise account-provider adapters through the public ToolBox seam."""

    def setUp(self) -> None:
        self.test_dir = TEMP_ROOT / (
            f"provider-tool-wiring-{os.getpid()}-{self._testMethodName}"
        )
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir)
        self.test_dir.mkdir()
        workspace = self.test_dir / "workspace"
        data_dir = self.test_dir / "data"
        workspace.mkdir()
        data_dir.mkdir()
        self.config = replace(
            Config.load(),
            workspace=workspace,
            data_dir=data_dir,
            execution_mode="trusted-host",
            autonomy="autonomous",
            external_access="trusted-external",
        )
        self.memory = Memory(data_dir / "provider-tools.db")
        self.toolbox = ToolBox(self.config, self.memory)

    def tearDown(self) -> None:
        self.memory.close()
        resolved = self.test_dir.resolve()
        self.assertEqual(resolved.parent, TEMP_ROOT.resolve())
        shutil.rmtree(resolved)

    def execute(self, name: str, arguments: dict[str, object]) -> dict[str, object]:
        payload = json.loads(self.toolbox.execute(name, arguments))
        self.assertTrue(payload["ok"], payload)
        self.assertIsInstance(payload["result"], dict)
        return payload["result"]

    @staticmethod
    def github_result(operation: str) -> GitHubResult:
        return GitHubResult(
            operation=operation,
            ok=True,
            data={"sentinel": operation},
        )

    def vercel_result(self, operation: str) -> VercelResult:
        return VercelResult(
            operation=operation,
            ok=True,
            command=("vercel", operation),
            cwd=str(self.config.workspace),
            returncode=0,
            data={"sentinel": operation},
        )

    def test_read_only_provider_tools_are_registered(self) -> None:
        expected = {
            "github_repository_status",
            "github_list_repositories",
            "google_drive_status",
            "google_drive_list_files",
            "google_drive_inventory",
            "vercel_status",
            "vercel_list_projects",
            "vercel_project_status",
            "vercel_deployment_status",
            "vercel_build_logs",
            "vercel_runtime_logs",
            "vercel_discover_databases",
            "vercel_list_databases",
        }

        self.assertTrue(expected.issubset(self.toolbox.tools))

    def test_github_read_tools_reach_the_exact_provider_methods(self) -> None:
        provider = Mock()
        provider.repository_status.return_value = self.github_result(
            "repository_status"
        )
        provider.list_repositories.return_value = self.github_result(
            "list_repositories"
        )
        self.toolbox.github = provider

        status = self.execute("github_repository_status", {"path": "project"})
        repositories = self.execute(
            "github_list_repositories",
            {"owner": "octocat", "limit": 17},
        )

        self.assertEqual(status["data"], {"sentinel": "repository_status"})
        self.assertEqual(repositories["data"], {"sentinel": "list_repositories"})
        provider.repository_status.assert_called_once_with("project")
        provider.list_repositories.assert_called_once_with("octocat", limit=17)

    def test_google_drive_read_tools_reach_the_exact_provider_methods(self) -> None:
        provider = Mock()
        provider.status.return_value = {"state": "ready", "sentinel": "status"}
        provider.list_files.return_value = {"sentinel": "list_files"}
        provider.inventory.return_value = {"sentinel": "inventory"}
        self.toolbox.google_drive = provider

        status = self.execute("google_drive_status", {})
        files = self.execute(
            "google_drive_list_files",
            {
                "folder_id": "folder-123",
                "page_size": 37,
                "page_token": "next-page",
                "include_trashed": True,
            },
        )
        inventory = self.execute(
            "google_drive_inventory",
            {"max_items": 731, "include_trashed": True},
        )

        self.assertEqual(status["sentinel"], "status")
        self.assertEqual(files["sentinel"], "list_files")
        self.assertEqual(inventory["sentinel"], "inventory")
        provider.status.assert_called_once_with()
        provider.list_files.assert_called_once_with(
            "folder-123",
            page_size=37,
            page_token="next-page",
            include_trashed=True,
        )
        provider.inventory.assert_called_once_with(
            max_items=731,
            include_trashed=True,
        )

    def test_vercel_read_tools_reach_the_exact_provider_methods(self) -> None:
        provider = Mock()
        provider.status.return_value = VercelStatus(
            available=True,
            cli_path="C:/tools/vercel.exe",
            version="99.1.0",
            authenticated=True,
            user="operator",
        )
        provider.list_projects.return_value = self.vercel_result("list_projects")
        provider.project_status.return_value = self.vercel_result("project_status")
        provider.deployment_status.return_value = self.vercel_result(
            "deployment_status"
        )
        provider.build_logs.return_value = self.vercel_result("build_logs")
        provider.deployment_logs.return_value = self.vercel_result("deployment_logs")
        provider.discover_database_integrations.return_value = self.vercel_result(
            "discover_database_integrations"
        )
        provider.list_database_integrations.return_value = self.vercel_result(
            "list_database_integrations"
        )
        self.toolbox.vercel = provider

        status = self.execute("vercel_status", {})
        projects = self.execute("vercel_list_projects", {})
        project = self.execute(
            "vercel_project_status",
            {"project_name": "site", "project_path": "apps/site"},
        )
        deployment = self.execute(
            "vercel_deployment_status",
            {"deployment": "site-abc.vercel.app", "project_path": "apps/site"},
        )
        build_logs = self.execute(
            "vercel_build_logs",
            {"deployment": "dpl_123", "project_path": "apps/site"},
        )
        runtime_logs = self.execute(
            "vercel_runtime_logs",
            {
                "deployment": "dpl_456",
                "project_name": "site",
                "project_path": "apps/site",
                "limit": 137,
                "since": "6h",
                "level": "error",
                "environment": "production",
            },
        )
        discovered = self.execute("vercel_discover_databases", {})
        databases = self.execute(
            "vercel_list_databases",
            {"project_name": "site", "project_path": "apps/site"},
        )

        self.assertTrue(status["available"])
        for value, operation in (
            (projects, "list_projects"),
            (project, "project_status"),
            (deployment, "deployment_status"),
            (build_logs, "build_logs"),
            (runtime_logs, "deployment_logs"),
            (discovered, "discover_database_integrations"),
            (databases, "list_database_integrations"),
        ):
            with self.subTest(operation=operation):
                self.assertEqual(value["data"], {"sentinel": operation})

        provider.status.assert_called_once_with()
        provider.list_projects.assert_called_once_with()
        provider.project_status.assert_called_once_with(
            "site", project_path="apps/site"
        )
        provider.deployment_status.assert_called_once_with(
            "site-abc.vercel.app", project_path="apps/site"
        )
        provider.build_logs.assert_called_once_with(
            "dpl_123", project_path="apps/site"
        )
        provider.deployment_logs.assert_called_once_with(
            "dpl_456",
            project_name="site",
            project_path="apps/site",
            limit=137,
            since="6h",
            level="error",
            environment="production",
        )
        provider.discover_database_integrations.assert_called_once_with()
        provider.list_database_integrations.assert_called_once_with(
            "site", project_path="apps/site"
        )


if __name__ == "__main__":
    unittest.main()
