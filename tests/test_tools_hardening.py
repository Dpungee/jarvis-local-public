from __future__ import annotations

import json
import hashlib
import os
import shutil
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from jarvis.approvals import SENSITIVE_ACTIONS, approval_resource
from jarvis.config import Config
from jarvis.memory import Memory
from jarvis.tools import (
    EXTERNAL_TOOLS, LOCAL_RESEARCH_TOOLS, MUTATING_TOOLS,
    SELF_INSPECTION_TOOLS, SELF_REPAIR_TOOLS, UNTRUSTED_WEB_TOOLS, Tool, ToolBox,
)


TEMP_ROOT = Path(__file__).resolve().parent / ".tmp"
TEMP_ROOT.mkdir(exist_ok=True)
EXPECTED_SENSITIVE_TOOLS = frozenset({
    "computer_list_files",
    "computer_read_file",
    "computer_search_files",
    "computer_storage_report",
    "computer_write_file",
    "windows_launch_app",
    "windows_open_url",
    "desktop_active_window",
    "desktop_interact",
    "photoshop_remove_background",
    "home_device_control",
    "github_create_repository",
    "github_push",
    "google_drive_authenticate",
    "google_drive_create_folder",
    "google_drive_upload_file",
    "google_drive_download_file",
    "google_drive_organize_files",
    "vercel_deploy",
    "connector_install",
    "connector_call",
    "feature_setup_decide",
})


class ToolCapabilityHardeningTests(unittest.TestCase):
    def setUp(self):
        self.test_dir = TEMP_ROOT / f"tool-hardening-{os.getpid()}-{self._testMethodName}"
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
            external_access="disabled",
            self_inspect="disabled",
        )
        self.memory = Memory(data_dir / "tools.db")

    def tearDown(self):
        self.memory.close()
        resolved = self.test_dir.resolve()
        self.assertEqual(resolved.parent, TEMP_ROOT.resolve())
        shutil.rmtree(resolved)

    def test_credential_and_repository_control_paths_are_protected_everywhere(self):
        toolbox = ToolBox(self.config, self.memory)
        paths = (
            ".env",
            ".env.production",
            ".git/config",
            ".SSH/id_rsa",
            ".aws/credentials",
            ".jarvis-runtime/home/file.txt",
            ".jarvis-skills/learned-code-fix/SKILL.md",
            "data/codex-cli-home/state.json",
            ".kube/config",
            ".npmrc",
        )
        for path in paths:
            with self.subTest(path=path):
                with self.assertRaises(PermissionError):
                    toolbox.list_files(path)
                with self.assertRaises(PermissionError):
                    toolbox.read_file(path)
                with self.assertRaises(PermissionError):
                    toolbox.write_file(path, "blocked")
                with self.assertRaises(PermissionError):
                    toolbox.search_files("needle", path)
                with self.assertRaises(PermissionError):
                    toolbox.run_process("python", ["--version"], cwd=path)
                result = json.loads(toolbox.execute("read_file", {"path": path}))
                self.assertFalse(result["ok"])

    def test_tool_availability_matrix_matches_capability_modes(self):
        all_tools = {
            "tool_catalog", "tool_create", "web_search", "web_fetch", "research_question", "list_files", "read_file", "read_files",
            "write_file", "edit_file", "make_directory", "copy_path", "move_path",
            "trash_path", "build_document", "search_files", "detect_project", "run_process",
            "build_document_preview", "image_visual_qa", "image_generation_status",
            "generate_image", "edit_attached_image",
            "install_project_dependencies",
            "start_process", "process_status", "process_logs", "stop_process",
            "http_health", "remember", "recall", "session_search", "skill_list", "skill_read",
            "screen_companion_status", "screen_companion_control",
            "schedule_create", "schedule_list", "schedule_set_enabled", "schedule_delete",
            "skill_create", "skill_update", "skill_github_sync",
            "delegate_specialist", "specialist_reports",
            "connector_list", "connector_describe", "connector_validate", "connector_install",
            "google_workspace_status", "prepare_email_draft", "prepare_calendar_event",
            "feature_setup_status", "feature_setup_plan", "feature_setup_decide",
        }
        readonly_tools = {
            "tool_catalog", "web_search", "web_fetch", "research_question", "list_files", "read_file", "read_files",
            "search_files", "detect_project", "process_status", "process_logs",
            "http_health", "recall", "session_search", "skill_list", "skill_read",
            "screen_companion_status", "screen_companion_control",
            "schedule_list",
            "specialist_reports",
            "connector_list", "connector_describe", "connector_validate",
            "google_workspace_status", "prepare_email_draft", "prepare_calendar_event",
            "image_visual_qa", "image_generation_status",
            "feature_setup_status", "feature_setup_plan",
        }
        execution_tools = {
            "run_process", "install_project_dependencies", "start_process", "process_status", "process_logs",
            "stop_process", "http_health",
        }
        cases = (
            ("autonomous", "trusted-host", all_tools),
            ("autonomous", "disabled", all_tools - execution_tools),
            ("readonly", "trusted-host", readonly_tools),
            ("readonly", "disabled", readonly_tools - {"process_status", "process_logs", "http_health"}),
        )
        for autonomy, execution_mode, expected in cases:
            with self.subTest(autonomy=autonomy, execution_mode=execution_mode):
                config = replace(
                    self.config,
                    autonomy=autonomy,
                    execution_mode=execution_mode,
                    computer_access="disabled",
                    network_access="disabled",
                    bluetooth_access="disabled",
                )
                toolbox = ToolBox(config, self.memory)
                self.assertEqual(set(toolbox.tools), expected)
                schema_names = {item["function"]["name"] for item in toolbox.schemas}
                self.assertEqual(schema_names, expected)

    def test_worker_tool_activity_is_bound_to_its_task(self):
        toolbox = ToolBox(self.config, self.memory)
        with toolbox.approval_context("task:4321", task_id=4321):
            result = json.loads(toolbox.execute("list_files", {"path": "."}))
        self.assertTrue(result["ok"])
        activity = self.memory.list_activity(limit=1)[0]
        self.assertEqual(activity["category"], "tool")
        self.assertEqual(activity["task_id"], 4321)

    def test_full_capability_toolbox_registers_only_resolvable_callables(self):
        toolbox = ToolBox(replace(
            self.config,
            computer_access="trusted-desktop",
            external_access="trusted-external",
        ), self.memory)
        toolbox.desktop = Mock()
        toolbox.desktop.snapshot.return_value = {
            "application": "test.exe", "title": "Test window",
            "left": 0, "top": 0, "right": 100, "bottom": 100,
            "width": 100, "height": 100,
            "context_sha256": "a" * 64, "excluded": False,
            "exclusion_reason": None,
        }

        self.assertTrue(EXTERNAL_TOOLS.issubset(toolbox.tools))
        self.assertEqual(set(toolbox.tools), {
            schema["function"]["name"] for schema in toolbox.schemas
        })
        for name, tool in toolbox.tools.items():
            with self.subTest(name=name):
                self.assertTrue(callable(tool.function))
                self.assertIs(getattr(tool.function, "__self__", None), toolbox)

    def test_workspace_drafts_are_reviewable_without_auth_or_external_execution(self):
        toolbox = ToolBox(self.config, self.memory)
        status = json.loads(toolbox.execute("google_workspace_status", {}))
        self.assertTrue(status["ok"])
        self.assertFalse(status["result"]["all_connected"])

        email = json.loads(toolbox.execute("prepare_email_draft", {
            "to": ["Owner@Example.com", "owner@example.com"],
            "subject": "Status",
            "body": "Ready for review.",
        }))
        self.assertTrue(email["ok"])
        self.assertEqual(email["result"]["to"], ["owner@example.com"])
        self.assertFalse(email["result"]["external_mutation"])
        self.assertTrue(email["result"]["execution_requires_approval"])

        event = json.loads(toolbox.execute("prepare_calendar_event", {
            "title": "Review",
            "start": "2026-08-27T10:00:00-04:00",
            "end": "2026-08-27T10:30:00-04:00",
        }))
        self.assertTrue(event["ok"])
        self.assertEqual(event["result"]["kind"], "calendar_event_draft")

    def test_only_jarvis_context_can_delegate_or_read_specialist_reports(self):
        toolbox = ToolBox(self.config, self.memory)
        conversation = self.memory.new_conversation("delegate")
        budget_scope = "request:" + "e" * 32
        with toolbox.agent_context(
            1, conversation_id=conversation, model_budget_scope=budget_scope
        ):
            queued = json.loads(toolbox.execute(
                "delegate_specialist",
                {"task": "Fix the Python parser and run its tests."},
            ))
            task_id = queued["result"]["task_id"]
            reports = json.loads(toolbox.execute(
                "specialist_reports", {"task_id": task_id, "wait_seconds": 0}
            ))
        self.assertTrue(queued["ok"])
        self.assertEqual(queued["result"]["specialist"], "Forge")
        self.assertEqual(
            self.memory.task_model_budget_scope(task_id), budget_scope
        )
        self.assertEqual(reports["result"][0]["status"], "queued")

        with toolbox.agent_context(
            1, conversation_id=conversation, specialist_key="coding"
        ):
            blocked_delegate = json.loads(toolbox.execute(
                "delegate_specialist",
                {"task": "Research current Python releases."},
            ))
            blocked_reports = json.loads(toolbox.execute(
                "specialist_reports", {},
            ))
        self.assertFalse(blocked_delegate["ok"])
        self.assertFalse(blocked_reports["ok"])
        self.assertIn("cannot delegate", blocked_delegate["error"])
        self.assertIn("cannot discover", blocked_reports["error"])

    def test_session_search_is_scoped_to_active_project(self):
        toolbox = ToolBox(self.config, self.memory)
        other_project = self.memory.add_project("Other", "@projects/other")
        current = self.memory.new_conversation("Current", project_id=1)
        other = self.memory.new_conversation("Other", project_id=other_project)
        self.memory.add_message(current, "user", "Private project marker alpha-current")
        self.memory.add_message(other, "user", "Private project marker alpha-other")

        with toolbox.agent_context(1, conversation_id=current):
            payload = json.loads(toolbox.execute(
                "session_search", {"query": "private project marker alpha", "limit": 8}
            ))

        self.assertTrue(payload["ok"])
        self.assertEqual(
            {item["conversation_id"] for item in payload["result"]}, {current}
        )

    def test_schedules_are_durable_and_project_scoped(self):
        toolbox = ToolBox(self.config, self.memory)
        conversation = self.memory.new_conversation("schedule", project_id=1)
        with toolbox.agent_context(1, conversation_id=conversation):
            created = json.loads(toolbox.execute("schedule_create", {
                "name": "Health heartbeat",
                "task": "Check Jarvis health and write a short local status brief.",
                "interval_minutes": 360,
            }))
            listed = json.loads(toolbox.execute("schedule_list", {}))
        self.assertTrue(created["ok"])
        self.assertEqual(created["result"]["interval_minutes"], 360)
        self.assertTrue(created["result"]["next_run_at"])
        self.assertEqual([row["id"] for row in listed["result"]], [created["result"]["id"]])

    def test_self_repair_tool_is_proposal_only_and_voids_gate_changes(self):
        enabled = replace(
            self.config,
            self_inspect="read-only",
            self_repair="propose",
        )
        toolbox = ToolBox(enabled, self.memory)
        self.assertEqual(SELF_REPAIR_TOOLS, {"self_repair_draft"})
        payload = json.loads(toolbox.execute("self_repair_draft", {
            "trigger": "attempt protected change",
            "edits": [{
                "path": "jarvis/redaction.py",
                "old_text": "old",
                "new_text": "new",
            }],
        }))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["result"]["status"], "voided")
        self.assertIn("permanently immutable", payload["result"]["void_reason"])
        self.assertNotIn("self_source_write", toolbox.tools)

    def test_every_sensitive_tool_is_gated_at_the_toolbox_chokepoint(self):
        computer_root = self.test_dir / "computer"
        computer_root.mkdir()
        with patch("jarvis.tools.HomeAssistantProvider") as home_assistant_type:
            home_assistant_type.return_value.approval_snapshot.return_value = {
                "resolved_entity": "remote.test_tv",
                "resolved_friendly_name": "Test TV",
                "resolved_action": "power",
                "resolved_app": None,
                "provider_origin": "http://192.168.50.2:8123",
            }
            toolbox = ToolBox(replace(
                self.config,
                computer_access="trusted-desktop",
                computer_root=computer_root,
                external_access="trusted-external",
                home_assistant_access="paired",
                home_assistant_url="http://192.168.50.2:8123",
                home_assistant_token="test-token-" + "x" * 24,
                home_assistant_entities=("remote.test_tv",),
            ), self.memory)
        vercel_link = self.config.workspace / ".vercel"
        vercel_link.mkdir()
        (vercel_link / "project.json").write_text(
            json.dumps({"projectId": "prj_test", "orgId": "org_test"}),
            encoding="utf-8",
        )
        self.assertEqual(set(SENSITIVE_ACTIONS), EXPECTED_SENSITIVE_TOOLS)
        self.assertTrue(EXPECTED_SENSITIVE_TOOLS.issubset(toolbox.tools))
        schema = {
            "type": "object",
            "properties": {"target": {"type": "string"}},
            "required": ["target"],
            "additionalProperties": False,
        }

        for index, (name, (expected_action, _reason)) in enumerate(
            SENSITIVE_ACTIONS.items(), start=1
        ):
            with self.subTest(tool=name):
                self.assertIn(name, toolbox.tools)
                calls = []

                def handler(target, *, _name=name, _calls=calls):
                    _calls.append((_name, target))
                    return {"tool": _name, "target": target}

                toolbox.tools[name] = Tool(name, "approval test", schema, handler)
                arguments = {"target": f"resource-{index}"}
                scope = f"conversation:{index}"

                with toolbox.approval_context(scope):
                    blocked = json.loads(toolbox.execute(name, arguments))
                self.assertFalse(blocked["ok"])
                self.assertTrue(blocked["approval_required"])
                self.assertEqual(calls, [])

                request = next(
                    item for item in self.memory.list_approvals()
                    if item["id"] == blocked["approval_id"]
                )
                self.assertEqual(request["action"], expected_action)
                self.assertEqual(request["scope"], scope)
                self.assertIn(name, request["resource"])
                self.assertTrue(
                    self.memory.decide_approval(blocked["approval_id"], True, ttl_hours=2)
                )

                with toolbox.approval_context(scope):
                    allowed = json.loads(toolbox.execute(name, arguments))
                self.assertTrue(allowed["ok"])
                self.assertEqual(calls, [(name, f"resource-{index}")])

                with toolbox.approval_context(scope):
                    blocked_again = json.loads(toolbox.execute(name, arguments))
                self.assertFalse(blocked_again["ok"])
                self.assertNotEqual(blocked_again["approval_id"], blocked["approval_id"])
                self.assertEqual(calls, [(name, f"resource-{index}")])

    def test_sensitive_tool_without_execution_scope_fails_closed(self):
        toolbox = ToolBox(self.config, self.memory)
        calls = []
        toolbox.tools["computer_read_file"] = Tool(
            "computer_read_file",
            "test read",
            {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
            lambda path: calls.append(path) or {"path": path},
        )

        blocked = json.loads(toolbox.execute(
            "computer_read_file", {"path": "Documents/note.txt"}
        ))
        self.assertFalse(blocked["ok"])
        self.assertTrue(blocked["approval_required"])
        self.assertIsNone(blocked["approval_id"])
        self.assertIn("ApprovalScopeRequired", blocked["error"])
        self.assertEqual(calls, [])
        self.assertEqual(self.memory.list_approvals(), [])

    def test_exact_read_only_grant_crosses_conversations_until_revoked(self):
        computer_root = self.test_dir / "computer"
        documents = computer_root / "Documents"
        documents.mkdir(parents=True)
        (documents / "note.txt").write_text("safe", encoding="utf-8")
        toolbox = ToolBox(replace(
            self.config,
            computer_access="trusted-desktop",
            computer_root=computer_root,
        ), self.memory)
        arguments = {"path": "Documents", "recursive": False}

        with toolbox.approval_context("conversation:501"):
            blocked = json.loads(toolbox.execute("computer_list_files", arguments))
        self.assertTrue(blocked["approval_required"])
        grant_id = self.memory.decide_approval_always(blocked["approval_id"])
        self.assertIsInstance(grant_id, int)

        with toolbox.approval_context("conversation:502"):
            allowed = json.loads(toolbox.execute("computer_list_files", arguments))
        self.assertTrue(allowed["ok"])
        self.assertTrue(any(
            str(path).replace("\\", "/").endswith("/note.txt")
            for path in allowed["result"]
        ))

        with toolbox.approval_context("conversation:502"):
            changed = json.loads(toolbox.execute(
                "computer_list_files", {**arguments, "recursive": True}
            ))
        self.assertTrue(changed["approval_required"])

        self.assertTrue(self.memory.revoke_persistent_approval(grant_id))
        with toolbox.approval_context("conversation:503"):
            blocked_again = json.loads(
                toolbox.execute("computer_list_files", arguments)
            )
        self.assertTrue(blocked_again["approval_required"])

    def test_write_approval_resource_binds_redacted_content_by_digest(self):
        first = approval_resource(
            "computer_write_file", {"path": "Documents/note.txt", "content": "alpha"}
        )
        second = approval_resource(
            "computer_write_file", {"path": "Documents/note.txt", "content": "beta"}
        )
        self.assertNotEqual(first, second)
        self.assertNotIn("alpha", first)
        self.assertNotIn("beta", second)
        self.assertIn("sha256", first)

        token = "sk-proj-" + "A" * 24
        secret_resource = approval_resource(
            "google_drive_authenticate",
            {"api_key": token, "arguments": ["--token", token]},
        )
        self.assertNotIn(token, secret_resource)
        self.assertGreaterEqual(secret_resource.count('"redacted":true'), 2)

        # Build documentation-only examples at runtime so static secret scanners
        # do not report the test source as a credential leak.
        first_aws_key = "AKIA" + "IOSFODNN7EXAMPLE"
        second_aws_key = "AKIA" + "IOSFODNN7EXAMPLF"
        first_secret_resource = approval_resource(
            "github_create_repository",
            {"name": "demo", "description": f"credential {first_aws_key}"},
        )
        second_secret_resource = approval_resource(
            "github_create_repository",
            {"name": "demo", "description": f"credential {second_aws_key}"},
        )
        self.assertNotEqual(first_secret_resource, second_secret_resource)
        self.assertNotIn(first_aws_key, first_secret_resource)
        self.assertNotIn(second_aws_key, second_secret_resource)

        long_base = "x" * 500
        long_private = approval_resource(
            "github_create_repository",
            {
                "path": long_base,
                "name": long_base,
                "description": long_base,
                "remote": long_base,
                "visibility": "private",
            },
        )
        long_public = approval_resource(
            "github_create_repository",
            {
                "path": long_base,
                "name": long_base,
                "description": long_base,
                "remote": long_base,
                "visibility": "public",
            },
        )
        self.assertLessEqual(len(long_private), 2_000)
        self.assertNotEqual(long_private, long_public)
        _, private_id = self.memory.authorize_or_request(
            "publish_external",
            long_private,
            "Create repository",
            approval_scope="conversation:80",
        )
        self.assertTrue(self.memory.decide_approval(private_id, True, ttl_hours=2))
        public_allowed, public_id = self.memory.authorize_or_request(
            "publish_external",
            long_public,
            "Create repository",
            approval_scope="conversation:80",
        )
        self.assertFalse(public_allowed)
        self.assertNotEqual(public_id, private_id)

    def test_approval_resource_includes_effective_defaults_and_resolved_targets(self):
        computer_root = self.test_dir / "computer-defaults"
        computer_root.mkdir()
        (self.config.workspace / "artifact.txt").write_text("approved bytes", encoding="utf-8")
        toolbox = ToolBox(replace(
            self.config,
            computer_access="trusted-desktop",
            computer_root=computer_root,
            external_access="trusted-external",
        ), self.memory)
        vercel_link = self.config.workspace / ".vercel"
        vercel_link.mkdir()
        (vercel_link / "project.json").write_text(
            json.dumps({"projectId": "prj_test", "orgId": "org_test"}),
            encoding="utf-8",
        )
        resolved_workspace = str(self.config.workspace.resolve())
        toolbox.github.create_repository_approval_snapshot = Mock(return_value={
            "resolved_path": resolved_workspace,
            "authenticated_login": "approved-owner",
            "repository_slug": "approved-owner/demo",
        })
        toolbox.github.push_approval_snapshot = Mock(return_value={
            "resolved_path": resolved_workspace,
            "branch": "main",
            "remote": "origin",
            "remote_url": "https://github.com/approved-owner/demo.git",
            "tip_sha": "a" * 40,
        })
        drive_destination = {
            "drive_account_permission_id": "account123",
            "resolved_folder_id": "folder123",
        }
        toolbox.google_drive.approval_destination_snapshot = Mock(
            return_value=drive_destination
        )
        toolbox.google_drive.upload_approval_snapshot = Mock(return_value={
            "resolved_local_path": str(
                (self.config.workspace / "artifact.txt").resolve()
            ),
            "folder_id": "root",
            "drive_name": "artifact.txt",
            "mime_type": "text/plain",
            "local_size_bytes": len("approved bytes"),
            "local_sha256": hashlib.sha256(b"approved bytes").hexdigest(),
            **drive_destination,
        })
        vercel_snapshot = toolbox.vercel.deployment_approval_snapshot(".")
        cases = (
            (
                "computer_list_files",
                {},
                {
                    "path": str(computer_root.resolve()),
                    "recursive": False,
                    "resolved_path": str(computer_root.resolve()),
                },
            ),
            (
                "computer_search_files",
                {"pattern": "needle"},
                {
                    "path": str(computer_root.resolve()),
                    "resolved_path": str(computer_root.resolve()),
                },
            ),
            (
                "computer_storage_report",
                {"path": ".", "limit": 30},
                {
                    "path": str(computer_root.resolve()),
                    "limit": 100,
                    "resolved_path": str(computer_root.resolve()),
                },
            ),
            (
                "github_create_repository",
                {"path": ".", "name": "demo"},
                {
                    "visibility": "private", "remote": "origin", "description": "",
                    "authenticated_login": "approved-owner",
                    "repository_slug": "approved-owner/demo",
                },
            ),
            (
                "github_push",
                {"path": ".", "branch": "main"},
                {
                    "remote": "origin", "set_upstream": True,
                    "remote_url": "https://github.com/approved-owner/demo.git",
                    "tip_sha": "a" * 40,
                },
            ),
            (
                "google_drive_create_folder",
                {"name": "demo"},
                {"parent_id": "root", **drive_destination},
            ),
            (
                "google_drive_upload_file",
                {"local_path": "artifact.txt"},
                {
                    "folder_id": "root",
                    "drive_name": "artifact.txt",
                    "mime_type": "text/plain",
                    "local_size_bytes": len("approved bytes"),
                    "local_sha256": hashlib.sha256(b"approved bytes").hexdigest(),
                    **drive_destination,
                },
            ),
            (
                "vercel_deploy",
                {},
                {
                    "project_path": ".",
                    "target": "preview",
                    "production": False,
                    **vercel_snapshot,
                },
            ),
        )

        for index, (name, arguments, expected) in enumerate(cases, start=1):
            with self.subTest(tool=name):
                with toolbox.approval_context(f"conversation:{100 + index}"):
                    blocked = json.loads(toolbox.execute(name, arguments))
                self.assertTrue(blocked["approval_required"])
                row = next(
                    item for item in self.memory.list_approvals()
                    if item["id"] == blocked["approval_id"]
                )
                resource = json.loads(row["resource"])
                for key, value in expected.items():
                    actual = resource["arguments"][key]
                    if isinstance(value, str) and len(value) > 160:
                        self.assertEqual(actual["prefix"], value[:160])
                        self.assertEqual(actual["characters"], len(value))
                        self.assertEqual(
                            actual["sha256"],
                            hashlib.sha256(value.encode("utf-8")).hexdigest(),
                        )
                    else:
                        self.assertEqual(actual, value)

        report_from_alias = toolbox._effective_approval_arguments(
            "computer_storage_report", {"path": ".", "limit": 30}
        )
        report_from_absolute = toolbox._effective_approval_arguments(
            "computer_storage_report",
            {"path": str(computer_root.resolve()), "limit": 50},
        )
        self.assertEqual(report_from_alias, report_from_absolute)
        self.assertEqual(
            approval_resource("computer_storage_report", report_from_alias),
            approval_resource("computer_storage_report", report_from_absolute),
        )

    def test_storage_report_stops_at_the_time_bound(self):
        computer_root = self.test_dir / "computer-storage-deadline"
        computer_root.mkdir()
        (computer_root / "large.bin").write_bytes(b"x" * 1024)
        toolbox = ToolBox(replace(
            self.config,
            computer_access="trusted-desktop",
            computer_root=computer_root,
        ), self.memory)

        with patch(
            "jarvis.tools.time.monotonic",
            side_effect=[0.0, 13.0, 13.0],
        ):
            report = toolbox.computer_storage_report(".")

        self.assertTrue(report["truncated"])
        self.assertEqual(report["truncation_reason"], "time_limit")
        self.assertEqual(report["scan_time_ms"], 13_000.0)
        self.assertEqual(report["scanned_files"], 0)
        self.assertFalse(report["content_read"])
        self.assertEqual(report["files_deleted"], 0)

    def test_changed_upload_bytes_do_not_consume_prior_approval(self):
        source = self.config.workspace / "upload.txt"
        source.write_text("first bytes", encoding="utf-8")
        toolbox = ToolBox(replace(
            self.config,
            external_access="trusted-external",
        ), self.memory)
        toolbox.google_drive.approval_destination_snapshot = Mock(return_value={
            "drive_account_permission_id": "account123",
            "resolved_folder_id": "folder123",
        })
        arguments = {"local_path": "upload.txt"}
        scope = "conversation:150"

        with toolbox.approval_context(scope):
            first = json.loads(toolbox.execute("google_drive_upload_file", arguments))
        self.assertTrue(self.memory.decide_approval(first["approval_id"], True, ttl_hours=2))
        source.write_text("second bytes", encoding="utf-8")
        with toolbox.approval_context(scope):
            second = json.loads(toolbox.execute("google_drive_upload_file", arguments))

        self.assertFalse(second["ok"])
        self.assertTrue(second["approval_required"])
        self.assertNotEqual(second["approval_id"], first["approval_id"])
        statuses = {item["id"]: item["status"] for item in self.memory.list_approvals()}
        self.assertEqual(statuses[first["approval_id"]], "approved")

    def test_final_effective_target_recheck_blocks_mid_execution_change(self):
        source = self.config.workspace / "race.txt"
        source.write_text("approved", encoding="utf-8")
        toolbox = ToolBox(replace(
            self.config,
            external_access="trusted-external",
        ), self.memory)
        resolved = str(source.resolve())
        base = {
            "resolved_local_path": resolved,
            "folder_id": "root",
            "drive_name": "race.txt",
            "mime_type": "text/plain",
            "local_size_bytes": len("approved"),
            "drive_account_permission_id": "account123",
            "resolved_folder_id": "folder123",
        }
        first_snapshot = {
            **base,
            "local_sha256": hashlib.sha256(b"approved").hexdigest(),
        }
        changed_snapshot = {
            **base,
            "local_sha256": hashlib.sha256(b"tampered").hexdigest(),
        }
        toolbox.google_drive.upload_approval_snapshot = Mock(side_effect=(
            first_snapshot,
            first_snapshot,
            changed_snapshot,
        ))
        calls = []
        original = toolbox.tools["google_drive_upload_file"]
        toolbox.tools["google_drive_upload_file"] = Tool(
            original.name,
            original.description,
            original.parameters,
            lambda **kwargs: calls.append(kwargs) or {"uploaded": True},
        )
        arguments = {"local_path": "race.txt"}
        scope = "conversation:151"

        with toolbox.approval_context(scope):
            blocked = json.loads(toolbox.execute("google_drive_upload_file", arguments))
        self.assertTrue(self.memory.decide_approval(blocked["approval_id"], True))
        with toolbox.approval_context(scope):
            changed = json.loads(toolbox.execute("google_drive_upload_file", arguments))

        self.assertFalse(changed["ok"])
        self.assertIn("changed during the final execution check", changed["error"])
        self.assertEqual(calls, [])

    def test_approved_drive_handlers_receive_bound_account_and_folder_ids(self):
        source = self.config.workspace / "bound.txt"
        source.write_text("bound bytes", encoding="utf-8")
        toolbox = ToolBox(replace(
            self.config,
            external_access="trusted-external",
        ), self.memory)
        destination = {
            "drive_account_permission_id": "account123",
            "resolved_folder_id": "folder123",
        }
        toolbox.google_drive.approval_destination_snapshot = Mock(
            return_value=destination
        )
        toolbox.google_drive.create_folder = Mock(return_value={"id": "created123"})

        create_arguments = {"name": "Reports"}
        with toolbox.approval_context("conversation:152"):
            create_blocked = json.loads(toolbox.execute(
                "google_drive_create_folder", create_arguments
            ))
        self.assertTrue(self.memory.decide_approval(create_blocked["approval_id"], True))
        with toolbox.approval_context("conversation:152"):
            create_result = json.loads(toolbox.execute(
                "google_drive_create_folder", create_arguments
            ))
        self.assertTrue(create_result["ok"])
        self.assertEqual(
            toolbox.google_drive.create_folder.call_args.kwargs,
            {
                "expected_account_permission_id": "account123",
                "expected_parent_folder_id": "folder123",
            },
        )

        upload_snapshot = {
            "resolved_local_path": str(source.resolve()),
            "folder_id": "root",
            "drive_name": "bound.txt",
            "mime_type": "text/plain",
            "local_size_bytes": len("bound bytes"),
            "local_sha256": hashlib.sha256(b"bound bytes").hexdigest(),
            **destination,
        }
        toolbox.google_drive.upload_approval_snapshot = Mock(return_value=upload_snapshot)
        toolbox.google_drive.upload_file = Mock(return_value={"id": "uploaded123"})
        upload_arguments = {"local_path": "bound.txt"}
        with toolbox.approval_context("conversation:153"):
            upload_blocked = json.loads(toolbox.execute(
                "google_drive_upload_file", upload_arguments
            ))
        self.assertTrue(self.memory.decide_approval(upload_blocked["approval_id"], True))
        with toolbox.approval_context("conversation:153"):
            upload_result = json.loads(toolbox.execute(
                "google_drive_upload_file", upload_arguments
            ))
        self.assertTrue(upload_result["ok"])
        self.assertEqual(
            toolbox.google_drive.upload_file.call_args.kwargs[
                "expected_account_permission_id"
            ],
            "account123",
        )
        self.assertEqual(
            toolbox.google_drive.upload_file.call_args.kwargs["expected_folder_id"],
            "folder123",
        )

    def test_approved_computer_write_performs_verified_readback_in_same_effect(self):
        computer_root = self.test_dir / "computer-write"
        computer_root.mkdir()
        toolbox = ToolBox(replace(
            self.config,
            computer_access="trusted-desktop",
            computer_root=computer_root,
        ), self.memory)
        arguments = {"path": "Documents/note.txt", "content": "approved note"}
        scope = "conversation:170"

        with toolbox.approval_context(scope):
            blocked = json.loads(toolbox.execute("computer_write_file", arguments))
        self.assertTrue(self.memory.decide_approval(blocked["approval_id"], True, ttl_hours=2))
        with toolbox.approval_context(scope):
            completed = json.loads(toolbox.execute("computer_write_file", arguments))

        self.assertTrue(completed["ok"])
        self.assertTrue(completed["result"]["verified_readback"])
        self.assertEqual(
            (computer_root / "Documents" / "note.txt").read_text(encoding="utf-8"),
            "approved note",
        )
        approvals = self.memory.list_approvals()
        self.assertEqual(len(approvals), 1)
        self.assertEqual(approvals[0]["status"], "consumed")

    def test_approved_sensitive_write_does_not_authorize_automatic_read(self):
        toolbox = ToolBox(replace(
            self.config,
            computer_access="trusted-desktop",
            computer_root=self.test_dir,
        ), self.memory)
        calls = []
        schema = {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        }
        toolbox.tools["computer_write_file"] = Tool(
            "computer_write_file",
            "test write",
            schema,
            lambda path: calls.append(("computer_write_file", path)) or {"path": path},
        )
        toolbox.tools["computer_read_file"] = Tool(
            "computer_read_file",
            "test read",
            schema,
            lambda path: calls.append(("computer_read_file", path)) or {"path": path},
        )
        arguments = {"path": "Documents/note.txt"}
        scope = "conversation:42"

        with toolbox.approval_context(scope):
            write_blocked = json.loads(toolbox.execute("computer_write_file", arguments))
        self.assertTrue(
            self.memory.decide_approval(write_blocked["approval_id"], True, ttl_hours=2)
        )
        with toolbox.approval_context(scope):
            write_allowed = json.loads(toolbox.execute("computer_write_file", arguments))
            read_blocked = json.loads(toolbox.execute("computer_read_file", arguments))

        self.assertTrue(write_allowed["ok"])
        self.assertFalse(read_blocked["ok"])
        self.assertTrue(read_blocked["approval_required"])
        self.assertEqual(calls, [("computer_write_file", "Documents/note.txt")])
        rows = {item["id"]: item for item in self.memory.list_approvals()}
        self.assertEqual(rows[write_blocked["approval_id"]]["status"], "consumed")
        self.assertEqual(rows[read_blocked["approval_id"]]["action"], "access_private_files")

    def test_disabled_execution_is_absent_and_rejected_on_direct_call(self):
        toolbox = ToolBox(replace(self.config, execution_mode="disabled"), self.memory)
        names = set(toolbox.tools)
        self.assertNotIn("run_process", names)
        self.assertFalse(json.loads(toolbox.execute("run_process", {"program": "python"}))["ok"])
        with patch("jarvis.tools.subprocess.Popen") as popen:
            with self.assertRaisesRegex(PermissionError, "disabled"):
                toolbox.run_process("python", ["--version"])
        popen.assert_not_called()

    def test_research_question_is_local_loop_capability_not_isolated_research(self):
        toolbox = ToolBox(self.config, self.memory)
        self.assertEqual(LOCAL_RESEARCH_TOOLS, {"research_question"})
        self.assertNotIn("research_question", UNTRUSTED_WEB_TOOLS)
        schema = next(
            item["function"] for item in toolbox.schemas
            if item["function"]["name"] == "research_question"
        )
        self.assertNotIn("required", schema["parameters"])
        self.assertEqual(
            schema["parameters"]["anyOf"],
            [{"required": ["query"]}, {"required": ["urls"]}],
        )
        self.assertEqual(
            schema["parameters"]["properties"]["urls"]["maxItems"], 5
        )
        self.assertEqual(schema["parameters"]["properties"]["max_results"]["maximum"], 5)

    def test_self_source_tools_are_explicit_read_only_and_path_bounded(self):
        source = self.test_dir / "runtime"
        package = source / "jarvis"
        tests = source / "tests"
        package.mkdir(parents=True)
        tests.mkdir()
        (package / "module.py").write_text("VALUE = 7\n", encoding="utf-8")
        (tests / "test_module.py").write_text(
            "from jarvis import module\n", encoding="utf-8"
        )
        enabled = replace(self.config, self_inspect="read-only")
        with patch("jarvis.tools.PACKAGE_ROOT", package), patch(
            "jarvis.tools.SOURCE_ROOT", source
        ):
            toolbox = ToolBox(enabled, self.memory)
            self.assertEqual(SELF_INSPECTION_TOOLS, {
                "self_source_list", "self_source_read",
            })
            self.assertTrue(SELF_INSPECTION_TOOLS.issubset(toolbox.tools))
            self.assertNotIn("self_source_write", toolbox.tools)
            self.assertEqual(
                toolbox.self_source_list("jarvis"), ["jarvis/module.py"]
            )
            read = toolbox.self_source_read("jarvis/module.py")
            self.assertEqual(read["content"], "1: VALUE = 7")
            self.assertTrue(read["read_only"])
            with self.assertRaises(PermissionError):
                toolbox.self_source_read("jarvis/../tests/test_module.py")
            with self.assertRaises(PermissionError):
                toolbox.self_source_read(str((package / "module.py").resolve()))

        disabled = ToolBox(self.config, self.memory)
        self.assertTrue(SELF_INSPECTION_TOOLS.isdisjoint(disabled.tools))
        with self.assertRaisesRegex(PermissionError, "disabled"):
            disabled.self_source_read("jarvis/module.py")

    def test_readonly_mode_removes_and_rejects_every_mutating_tool(self):
        toolbox = ToolBox(replace(self.config, autonomy="readonly"), self.memory)
        names = set(toolbox.tools)
        self.assertTrue(MUTATING_TOOLS.isdisjoint(names))
        self.assertTrue({"web_search", "web_fetch", "research_question", "list_files", "read_file", "read_files", "search_files", "recall", "session_search", "skill_list", "skill_read"}.issubset(names))
        for name, arguments in (
            ("write_file", {"path": "x.txt", "content": "blocked"}),
            ("make_directory", {"path": "new"}),
            ("copy_path", {"source": "x.txt", "destination": "y.txt"}),
            ("move_path", {"source": "x.txt", "destination": "y.txt"}),
            ("trash_path", {"path": "x.txt"}),
            ("run_process", {"program": "python", "arguments": ["--version"]}),
            ("remember", {"content": "safe reusable fact"}),
        ):
            with self.subTest(name=name):
                self.assertFalse(json.loads(toolbox.execute(name, arguments))["ok"])
        with self.assertRaisesRegex(PermissionError, "readonly"):
            toolbox.write_file("x.txt", "blocked")
        for operation in (
            lambda: toolbox.make_directory("new"),
            lambda: toolbox.copy_path("x.txt", "y.txt"),
            lambda: toolbox.move_path("x.txt", "y.txt"),
            lambda: toolbox.trash_path("x.txt"),
        ):
            with self.assertRaisesRegex(PermissionError, "readonly"):
                operation()
        with self.assertRaisesRegex(PermissionError, "readonly"):
            toolbox.remember("safe reusable fact")
        with patch("jarvis.tools.subprocess.Popen") as popen:
            with self.assertRaisesRegex(PermissionError, "readonly"):
                toolbox.run_process("python", ["--version"])
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
