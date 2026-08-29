from __future__ import annotations

import io
import tempfile
import unittest
from contextvars import ContextVar
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from jarvis.github_provider import GitHubResult
from jarvis.tools import ToolBox
from jarvis.vercel_provider import VercelResult


class ToolBoxHandlerSeamTests(unittest.TestCase):
    """Exercise thin ToolBox handlers without replacing the handlers themselves."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="jarvis-toolbox-seams-")
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.computer_root = self.root / "computer"
        self.workspace.mkdir()
        self.computer_root.mkdir()
        self.toolbox = object.__new__(ToolBox)
        self.toolbox.config = SimpleNamespace(
            workspace=self.workspace,
            computer_root=self.computer_root,
            computer_access="trusted-desktop",
            autonomy="autonomous",
            execution_mode="trusted-host",
            cloud_enabled=True,
            openai_images_enabled=True,
        )
        self.toolbox.memory = Mock()
        self.toolbox.github = Mock()
        self.toolbox.google_drive = Mock()
        self.toolbox.vercel = Mock()
        self.toolbox.connectors = Mock()
        self.toolbox.openai_images = Mock()
        self.toolbox.home_assistant = Mock()
        self.toolbox.windows_apps = Mock()
        self.toolbox.windows_app_repair = Mock()
        self.toolbox._approved_sensitive_arguments = ContextVar(
            f"approved_test_{id(self)}", default=None
        )
        self.toolbox._agent_execution_context = ContextVar(
            f"agent_test_{id(self)}", default=None
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def call_approved(self, name: str, approved: dict, method, *args, **kwargs):
        token = self.toolbox._approved_sensitive_arguments.set((name, approved))
        try:
            return method(*args, **kwargs)
        finally:
            self.toolbox._approved_sensitive_arguments.reset(token)

    def test_github_status_handlers_return_bounded_provider_results(self) -> None:
        cli = GitHubResult("cli_status", True, data={"version": "2.1"})
        auth = GitHubResult("auth_status", False, error="not authenticated")
        self.toolbox.github.cli_status.return_value = cli
        self.toolbox.github.auth_status.return_value = auth

        self.assertEqual(self.toolbox.github_cli_status(), cli.as_dict())
        self.assertEqual(self.toolbox.github_auth_status(), auth.as_dict())
        self.toolbox.github.cli_status.assert_called_once_with()
        self.toolbox.github.auth_status.assert_called_once_with()

    def test_github_mutations_forward_arguments_and_approved_snapshots(self) -> None:
        created = GitHubResult("create_repository", True, data={"name": "demo"})
        pushed = GitHubResult("push", True, data={"branch": "main"})
        self.toolbox.github.create_repository.return_value = created
        self.toolbox.github.push.return_value = pushed
        create_approval = {
            "resolved_path": str(self.workspace),
            "authenticated_login": "operator",
            "repository_slug": "operator/demo",
        }
        result = self.call_approved(
            "github_create_repository",
            create_approval,
            self.toolbox.github_create_repository,
            ".",
            "demo",
            visibility="private",
            description="bounded",
            remote="public",
        )
        self.assertEqual(result, created.as_dict())
        self.toolbox.github.create_repository.assert_called_once_with(
            ".",
            "demo",
            visibility="private",
            description="bounded",
            remote="public",
            expected_approval_snapshot=create_approval,
        )

        push_approval = {"remote_url": "https://github.com/example/demo.git", "tip_sha": "a" * 40}
        result = self.call_approved(
            "github_push",
            push_approval,
            self.toolbox.github_push,
            ".",
            "main",
            remote="public",
            set_upstream=False,
        )
        self.assertEqual(result, pushed.as_dict())
        self.toolbox.github.push.assert_called_once_with(
            ".",
            "main",
            remote="public",
            set_upstream=False,
            expected_remote_url=push_approval["remote_url"],
            expected_tip_sha=push_approval["tip_sha"],
        )

    def test_google_drive_handlers_forward_exact_approval_state(self) -> None:
        self.toolbox.google_drive.authenticate.return_value = {"state": "ready"}
        self.assertEqual(
            self.toolbox.google_drive_authenticate(open_browser=False),
            {"state": "ready"},
        )
        self.toolbox.google_drive.authenticate.assert_called_once_with(open_browser=False)

        download_approval = {
            "drive_account_permission_id": "permission-1",
            "download_item": {"id": "file-1", "name": "report"},
            "resolved_export_mime_type": "application/pdf",
        }
        self.toolbox.google_drive.download_file.return_value = {"downloaded": True}
        downloaded = self.call_approved(
            "google_drive_download_file",
            download_approval,
            self.toolbox.google_drive_download_file,
            "file-1",
            "report.pdf",
            overwrite=True,
            export_mime_type="application/pdf",
        )
        self.assertEqual(downloaded, {"downloaded": True})
        self.toolbox.google_drive.download_file.assert_called_once_with(
            "file-1",
            "report.pdf",
            overwrite=True,
            export_mime_type="application/pdf",
            expected_approval_snapshot=download_approval,
        )

        organize_approval = {
            "drive_account_permission_id": "permission-1",
            "organize_items": [{"file_id": "file-1", "parent_id": "folder-1"}],
        }
        operations = [{"action": "move", "file_id": "file-1", "folder_id": "folder-1"}]
        self.toolbox.google_drive.organize_files.return_value = {"completed": 1}
        organized = self.call_approved(
            "google_drive_organize_files",
            organize_approval,
            self.toolbox.google_drive_organize_files,
            operations,
        )
        self.assertEqual(organized, {"completed": 1})
        self.toolbox.google_drive.organize_files.assert_called_once_with(
            operations, expected_approval_snapshot=organize_approval
        )

    def test_google_drive_handlers_fail_closed_when_provider_is_disabled(self) -> None:
        self.toolbox.google_drive = None
        with self.assertRaises(PermissionError):
            self.toolbox.google_drive_authenticate()
        with self.assertRaises(PermissionError):
            self.toolbox.google_drive_download_file("id", "file")
        with self.assertRaises(PermissionError):
            self.toolbox.google_drive_organize_files([])

    def test_connector_handlers_forward_and_install_binds_complete_snapshot(self) -> None:
        self.toolbox.connectors.describe.return_value = {"id": "weather", "actions": []}
        self.toolbox.connectors.validate_workspace_manifest.return_value = {"valid": True}
        self.assertEqual(
            self.toolbox.connector_describe("weather"),
            {"id": "weather", "actions": []},
        )
        self.assertEqual(
            self.toolbox.connector_validate("connectors/weather.json"), {"valid": True}
        )
        self.toolbox.connectors.describe.assert_called_once_with("weather")
        self.toolbox.connectors.validate_workspace_manifest.assert_called_once_with(
            "connectors/weather.json"
        )

        approval = {
            "path": "connectors/weather.json",
            "id": "weather",
            "name": "Weather",
            "version": "1.0.0",
            "description": "Forecasts",
            "actions": ["current"],
            "credential_reference": None,
            "manifest_sha256": "b" * 64,
            "valid": True,
        }
        self.toolbox.connectors.install.return_value = {"installed": "weather"}
        installed = self.call_approved(
            "connector_install",
            approval,
            self.toolbox.connector_install,
            "connectors/weather.json",
        )
        self.assertEqual(installed, {"installed": "weather"})
        self.toolbox.connectors.install.assert_called_once_with(
            "connectors/weather.json", expected_snapshot=approval
        )

    def test_connector_provider_failure_propagates(self) -> None:
        self.toolbox.connectors.describe.side_effect = RuntimeError("gateway unavailable")
        with self.assertRaisesRegex(RuntimeError, "gateway unavailable"):
            self.toolbox.connector_describe("weather")

    def test_document_preview_and_image_visual_qa_use_real_bounded_helpers(self) -> None:
        (self.workspace / "brief.md").write_text("# Brief\n\nVerified text.\n", encoding="utf-8")
        preview = self.toolbox.build_document_preview("brief.md", "brief.html")
        self.assertEqual(preview["relative_path"], "brief.html")
        self.assertTrue(preview["qa_passed"])
        self.assertTrue((self.workspace / "brief.html").is_file())

        from PIL import Image

        image = io.BytesIO()
        Image.new("RGB", (1, 1), "navy").save(image, format="PNG")
        (self.workspace / "pixel.png").write_bytes(image.getvalue())
        inspected = self.toolbox.image_visual_qa("pixel.png")
        self.assertEqual(inspected["path"], "pixel.png")
        self.assertEqual(inspected["mime"], "image/png")
        self.assertEqual(inspected["width"], 1)
        self.assertEqual(inspected["height"], 1)

    def test_document_preview_respects_readonly_and_image_status_is_fail_closed(self) -> None:
        self.toolbox.config.autonomy = "readonly"
        with self.assertRaises(PermissionError):
            self.toolbox.build_document_preview("brief.md", "brief.html")

        self.toolbox.config.cloud_enabled = False
        self.toolbox.openai_images.status.return_value = {
            "configured": True,
            "provider": "openai",
        }
        status = self.toolbox.image_generation_status()
        self.assertFalse(status["enabled"])
        self.assertFalse(status["configured"])
        self.assertIn("JARVIS_CLOUD_ENABLED", status["next_action"])

    def test_computer_search_files_is_bounded_and_validates_pattern(self) -> None:
        (self.computer_root / "notes.txt").write_text(
            "alpha\nNeedle value\nomega\n", encoding="utf-8"
        )
        matches = self.toolbox.computer_search_files("needle")
        self.assertEqual(len(matches), 1)
        self.assertIn("notes.txt:2: Needle value", matches[0])
        with self.assertRaises(ValueError):
            self.toolbox.computer_search_files("")

    def test_home_and_desktop_status_are_provider_or_approval_visible(self) -> None:
        self.toolbox.home_assistant.status.return_value = {
            "state": "paired",
            "entities": 3,
        }
        self.assertEqual(
            self.toolbox.home_device_status(), {"state": "paired", "entities": 3}
        )
        self.toolbox.home_assistant = None
        with self.assertRaises(PermissionError):
            self.toolbox.home_device_status()

        with self.assertRaises(PermissionError):
            self.toolbox.desktop_active_window()
        foreground = {
            "application": "Editor",
            "title": "Draft",
            "context_sha256": "c" * 64,
        }
        visible = self.call_approved(
            "desktop_active_window",
            {"foreground": foreground, "private_detail": "not returned"},
            self.toolbox.desktop_active_window,
        )
        self.assertEqual(visible, foreground)
        self.assertIsNot(visible, foreground)

    def test_windows_app_handlers_forward_filter_and_fail_safely(self) -> None:
        self.toolbox.windows_apps.list_apps.return_value = {
            "apps": [{"name": "Paint"}],
            "count": 1,
        }
        self.assertEqual(
            self.toolbox.windows_list_apps("paint", limit=7)["count"], 1
        )
        self.toolbox.windows_apps.list_apps.assert_called_once_with("paint", 7)

        self.toolbox.windows_app_repair.diagnose.return_value = {
            "application": "Epic Games",
            "status": "diagnosed",
            "_private_plan": {"path": "secret"},
        }
        diagnosis = self.toolbox.windows_app_diagnose("Epic Games", "blank_or_unrendered")
        self.assertEqual(diagnosis, {"application": "Epic Games", "status": "diagnosed"})
        self.toolbox.windows_app_repair.diagnose.assert_called_once_with(
            "Epic Games", "blank_or_unrendered"
        )
        self.toolbox.windows_app_repair.diagnose.side_effect = OSError("changed")
        with self.assertRaisesRegex(RuntimeError, "changed or became unavailable"):
            self.toolbox.windows_app_diagnose("Epic Games")

    def test_windows_open_url_requires_approval_and_forwards_safe_url(self) -> None:
        with self.assertRaises(PermissionError):
            self.toolbox.windows_open_url("https://example.com/docs")
        approval = {"url": "https://example.com/docs", "resolved_host": "example.com"}
        self.toolbox.windows_apps.open_url.return_value = {"opened": True}
        result = self.call_approved(
            "windows_open_url",
            approval,
            self.toolbox.windows_open_url,
            "https://example.com/docs",
        )
        self.assertEqual(result, {"opened": True})
        self.toolbox.windows_apps.open_url.assert_called_once_with(
            "https://example.com/docs", approved=approval
        )

    def test_recall_and_schedule_mutations_forward_project_scope(self) -> None:
        self.toolbox.memory.search.return_value = [{"kind": "fact", "text": "remembered"}]
        self.assertEqual(
            self.toolbox.recall("project preference"),
            [{"kind": "fact", "text": "remembered"}],
        )
        self.toolbox.memory.search.assert_called_once_with("project preference")

        token = self.toolbox._agent_execution_context.set((41, 5, None, None))
        try:
            self.toolbox.memory.set_scheduled_job_enabled.return_value = True
            self.assertEqual(
                self.toolbox.schedule_set_enabled(8, False),
                {"job_id": 8, "enabled": False},
            )
            self.toolbox.memory.set_scheduled_job_enabled.assert_called_once_with(
                8, False, project_id=41
            )
            self.toolbox.memory.delete_scheduled_job.return_value = True
            self.assertEqual(
                self.toolbox.schedule_delete(8), {"job_id": 8, "deleted": True}
            )
            self.toolbox.memory.delete_scheduled_job.assert_called_once_with(
                8, project_id=41
            )
            self.toolbox.memory.delete_scheduled_job.return_value = False
            with self.assertRaises(KeyError):
                self.toolbox.schedule_delete(99)
        finally:
            self.toolbox._agent_execution_context.reset(token)

    def test_vercel_deploy_forwards_complete_approval_snapshot(self) -> None:
        approval = {
            "resolved_project_path": str(self.workspace),
            "project_id": "project-1",
            "org_id": "org-1",
            "account_scope": "operator",
            "project_link_sha256": "d" * 64,
            "prebuilt": False,
            "deploy_tree_sha256": "e" * 64,
            "deploy_file_count": 4,
            "deploy_total_bytes": 1024,
        }
        provider_result = VercelResult(
            operation="deploy",
            ok=True,
            command=("vercel", "deploy"),
            cwd=str(self.workspace),
            returncode=0,
            data={"url": "https://example.vercel.app"},
        )
        self.toolbox.vercel.deploy.return_value = provider_result
        result = self.call_approved(
            "vercel_deploy",
            approval,
            self.toolbox.vercel_deploy,
            ".",
            production=True,
            target="production",
            prebuilt=False,
            wait=True,
        )
        self.assertEqual(result["operation"], "deploy")
        self.assertTrue(result["ok"])
        self.toolbox.vercel.deploy.assert_called_once_with(
            ".",
            production=True,
            target="production",
            prebuilt=False,
            wait=True,
            expected_approval_snapshot=approval,
        )


if __name__ == "__main__":
    unittest.main()
