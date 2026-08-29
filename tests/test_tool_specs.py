import hashlib
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from jarvis.feature_onboarding import FEATURE_SPECS
from jarvis.network_inventory import MAX_SCAN_HOSTS
from jarvis.offline_documents import SUPPORTED_DOCUMENT_TYPES
from jarvis.tool_specs import build_tool_specs
from jarvis.tools import (
    MAX_BATCH_READ_FILES,
    MAX_RESEARCH_QUESTION_RESULTS,
    MAX_TOOL_DEFINITION_BYTES,
    MAX_TOOL_OUTPUT,
    ToolBox,
)


EXPECTED_TOOL_NAMES = (
    "tool_catalog", "tool_create", "web_search", "web_fetch",
    "research_question", "delegate_specialist", "specialist_reports",
    "github_cli_status", "github_auth_status", "github_repository_status",
    "github_list_repositories", "github_create_repository", "github_push",
    "google_drive_status", "google_workspace_status", "prepare_email_draft",
    "prepare_calendar_event", "google_drive_authenticate",
    "google_drive_list_files", "google_drive_inventory",
    "google_drive_create_folder", "google_drive_upload_file",
    "google_drive_download_file", "google_drive_organize_files",
    "vercel_status", "vercel_list_projects", "vercel_project_status",
    "vercel_deploy", "vercel_deployment_status", "vercel_build_logs",
    "vercel_runtime_logs", "vercel_discover_databases",
    "vercel_list_databases", "list_files", "read_file", "read_files",
    "write_file", "build_document", "build_document_preview",
    "image_visual_qa", "image_generation_status", "generate_image",
    "edit_attached_image", "edit_file", "make_directory", "copy_path",
    "move_path", "trash_path", "search_files", "detect_project",
    "install_project_dependencies", "run_process", "start_process",
    "process_status", "process_logs", "stop_process", "http_health",
    "remember", "recall", "session_search", "screen_companion_status",
    "screen_companion_control", "schedule_create", "schedule_list",
    "schedule_set_enabled", "schedule_delete", "connector_list",
    "connector_describe", "connector_validate", "connector_install",
    "connector_call", "skill_list", "feature_setup_status",
    "feature_setup_plan", "feature_setup_decide", "skill_read",
    "skill_create", "skill_github_sync", "skill_update", "self_source_list",
    "self_source_read", "self_repair_draft", "computer_list_files",
    "computer_read_file", "computer_write_file", "computer_search_files",
    "computer_storage_report", "system_snapshot", "network_inventory",
    "bluetooth_inventory", "home_device_status", "home_device_control",
    "windows_list_apps", "windows_open_apps", "windows_launch_app",
    "windows_app_diagnose", "windows_app_repair", "windows_open_url",
    "desktop_active_window", "desktop_interact",
    "photoshop_remove_background", "launch_artifact",
)
EXPECTED_SCHEMA_SHA256 = (
    "d28091c2abf10c273df8573ebfcb657340a2c64927bfa38d7dfe517006fc23ed"
)


def _specs():
    return build_tool_specs(
        feature_specs=FEATURE_SPECS,
        max_batch_read_files=MAX_BATCH_READ_FILES,
        max_research_question_results=MAX_RESEARCH_QUESTION_RESULTS,
        max_scan_hosts=MAX_SCAN_HOSTS,
        max_tool_definition_bytes=MAX_TOOL_DEFINITION_BYTES,
        max_tool_output=MAX_TOOL_OUTPUT,
        supported_document_types=SUPPORTED_DOCUMENT_TYPES,
    )


def _all_capabilities_config():
    return SimpleNamespace(
        computer_access="trusted-desktop",
        network_access="private-lan",
        bluetooth_access="paired-readonly",
        home_assistant_access="paired",
        autonomy="autonomous",
        execution_mode="trusted-host",
        external_access="trusted-external",
        self_inspect="read-only",
        self_repair="propose",
    )


def _schemas(specs):
    return [
        {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.parameters,
            },
        }
        for spec in specs
    ]


class ToolSpecTests(unittest.TestCase):
    def test_registry_order_names_and_schema_digest_are_unchanged(self):
        specs = _specs()
        names = tuple(spec.name for spec in specs)

        self.assertEqual(names, EXPECTED_TOOL_NAMES)
        self.assertEqual(len(names), len(set(names)))
        canonical = json.dumps(
            _schemas(specs), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
        self.assertEqual(
            hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            EXPECTED_SCHEMA_SHA256,
        )

    def test_registry_factory_returns_equal_but_independent_schemas(self):
        first = _specs()
        second = _specs()

        self.assertEqual(_schemas(first), _schemas(second))
        self.assertTrue(
            all(left.parameters is not right.parameters for left, right in zip(first, second))
        )

    def test_every_declared_handler_still_resolves(self):
        for spec in _specs():
            with self.subTest(tool=spec.name, handler=spec.handler_name):
                self.assertTrue(callable(getattr(ToolBox, spec.handler_name, None)))

    def test_toolbox_binding_preserves_schema_identity_and_exact_order(self):
        specs = _specs()
        toolbox = object.__new__(ToolBox)
        toolbox.config = _all_capabilities_config()

        with patch("jarvis.tools.build_tool_specs", return_value=specs):
            tools = toolbox._build_tools()

        self.assertEqual([tool.schema() for tool in tools], _schemas(specs))
        self.assertEqual(tuple(tool.name for tool in tools), EXPECTED_TOOL_NAMES)
        for tool, spec in zip(tools, specs):
            with self.subTest(tool=tool.name):
                self.assertIs(tool.parameters, spec.parameters)
                self.assertIs(tool.function.__self__, toolbox)
                self.assertIs(tool.function.__func__, getattr(ToolBox, spec.handler_name))


if __name__ == "__main__":
    unittest.main()
