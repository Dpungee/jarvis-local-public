from __future__ import annotations

import base64
import json
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from jarvis.agent import AgentRunCancelled
from jarvis.config import Config
from jarvis.memory import Memory
from jarvis.ollama_client import OllamaError
from jarvis.network_inventory import NetworkInventoryRateLimited
from jarvis.public_presence_store import PublicPresenceStopped, PublicPresenceStore
from jarvis.presence import (
    NetworkInventoryScanBusy,
    PresenceHTTPServer,
    PresenceRuntime,
    _interactive_approval_retry,
    normalize_presence_host,
    normalize_presence_port,
    normalize_request_host,
    safe_presence_product_comparison,
    safe_presence_network_payload,
    safe_presence_text,
    safe_companion_suggestion,
)


class _FakeRuntime:
    def __init__(self) -> None:
        self.runtime_epoch = "a" * 32
        self.last_event_after = None
        self.decisions = []
        self.always_decisions = []
        self.session_decisions = []
        self.revoked_grants = []
        self.deleted_conversations = []
        self.controls = []
        self.companion_state = {
            "mode": "observe",
            "paused": False,
            "auto_suggest": False,
            "excluded_apps": ["private.exe"],
            "available": True,
            "current": None,
            "rules": [],
            "raw_screens_persisted": False,
        }
        self.companion_controls = []
        self.companion_suggestion = {
            "id": "c" * 32,
            "text": "Want me to tighten the outline into three clear sections?",
            "expires_at": time.time() + 45,
        }
        self.companion_suggestion_decisions = []
        self.public_presence_state = {
            "configured_enabled": False,
            "control": {
                "enabled": False,
                "paused": True,
                "emergency_stopped": False,
                "effective_state": "disabled",
                "can_external_action": False,
            },
            "effective_state": "disabled",
            "process_running": False,
            "connected_platforms": 0,
            "publishing_available": False,
            "external_communication": False,
            "private_bridge": "Closed + sanitized",
            "error": None,
        }
        self.public_presence_controls = []
        self.network_status_reads = 0
        self.network_scan_calls = []
        self.network_pair_calls = []
        self.network_unpair_calls = []
        self.network_profile_calls = []
        self.network_incident_acknowledgements = []
        self.network_state = {
            "enabled": True,
            "available": True,
            "scan_in_progress": False,
            "can_scan": True,
            "error": None,
            "scopes": [{
                "scope_id": "scope-home",
                "display_name": "Home Wi-Fi",
                "interface_index": 7,
                "interface_alias": "Wi-Fi",
                "cidr": "192.168.50.0/24",
                "gateway_ipv4": "192.168.50.1",
                "active": True,
            }],
            "scope_candidates": [{
                "interface_index": 7,
                "interface_alias": "Wi-Fi",
                "address": "192.168.50.10",
                "scan_cidr": "192.168.50.0/24",
                "eligible": True,
                "reason": None,
            }],
            "inventory": {
                "last_scan_at": "2026-08-28T12:00:00+00:00",
                "visible_devices": 1,
                "new_devices": 1,
                "known_devices": 1,
                "total_known_devices": 1,
                "devices": [{
                    "device_id": "device-1",
                    "display_name": "<img src=x onerror=alert(1)>",
                    "label": None,
                    "trust_state": "unreviewed",
                    "device_type": None,
                    "identity_confidence": "high",
                    "presence_state": "reachable",
                    "visible_now": True,
                    "cached_now": False,
                    "is_new": True,
                    "ipv4": "192.168.50.21",
                    "mac": "aa:bb:cc:dd:ee:ff",
                    "first_seen": "2026-08-28T11:00:00+00:00",
                    "last_seen": "2026-08-28T12:00:00+00:00",
                    "continuous_visible_seconds": 3600,
                }],
            },
            "pending_incidents": {
                "pending_count": 1,
                "incidents": [{
                    "incident_id": "1" * 32,
                    "receipt_id": "2" * 32,
                    "assessment_id": "3" * 32,
                    "signal_id": "4" * 24,
                    "created_at": "2026-08-28T12:00:00+00:00",
                    "severity": "medium",
                    "category": "asset_change",
                    "device": {
                        "device_id": "device-1",
                        "display_name": "Observed device device",
                        "device_type": "Unknown",
                    },
                    "observed_fact": "A newly observed device needs review.",
                    "assessment": "This is not proof of compromise.",
                    "confidence": "limited",
                    "compromise_established": False,
                    "evidence_summary": ["Latest paired-LAN check"],
                    "automatic_actions": [],
                    "actions_not_taken": ["No containment was performed."],
                    "recommended_action": "Identify the device.",
                    "approval": None,
                    "limitations": ["Identity evidence can be spoofed."],
                }],
                "integrity_failures": [],
            },
            "limitations": ["Observation only."],
        }
        self.bluetooth_status_reads = 0
        self.bluetooth_check_calls = 0
        self.bluetooth_profile_calls = []
        self.bluetooth_acknowledgements = []
        self.bluetooth_state = {
            "enabled": True,
            "available": True,
            "last_check_id": 9,
            "last_check_at": "2026-08-28T12:00:00+00:00",
            "paired_in_last_check": 1,
            "known_endpoints": 1,
            "new_endpoints": 1,
            "baseline_created": False,
            "nearby_rf_scan_performed": False,
            "pairing_or_control_performed": False,
            "addresses_exposed": False,
            "devices": [{
                "device_id": "b" * 64,
                "display_name": "<img src=x onerror=alert(1)>",
                "trust_state": "unreviewed",
                "device_type": None,
                "transports": ["classic"],
                "is_new": True,
                "first_seen": "2026-08-28T11:59:00+00:00",
                "last_observed_at": "2026-08-28T12:00:00+00:00",
                "connected_evidence_available": False,
            }],
            "security_assessment": {
                "posture": "review_required",
                "signals": [{
                    "device_id": "b" * 64,
                    "rule_id": "new_unreviewed_paired_endpoint",
                    "severity": "informational",
                    "summary": "A paired endpoint is new to Jarvis.",
                    "recommended_action": "Identify it before changing trust.",
                    "compromise_established": False,
                }],
                "compromise_established": False,
                "automatic_containment": {"enabled": False, "actions_taken": 0},
            },
            "pending_alerts": {
                "pending_count": 1,
                "alerts": [{
                    "event_id": 91,
                    "receipt_id": "d" * 32,
                    "device_id": "b" * 64,
                    "display_name": "<img src=x onerror=alert(1)>",
                    "observed_at": "2026-08-28T12:00:00+00:00",
                    "trust_state": "unreviewed",
                    "summary": "A paired endpoint is new to Jarvis.",
                    "evidence_boundary": "This is not proof of malicious behavior.",
                }],
                "addresses_exposed": False,
            },
        }

    def status(self):
        return {
            "runtime_epoch": self.runtime_epoch,
            "ready": True,
            "uptime_seconds": 3,
            "active_job_id": None,
            "active_jobs": [],
            "jobs": [],
            "active_agent_count": 0,
            "max_agents": 3,
            "queued_jobs": 0,
            "control": {"state": "running"},
            "pending_approvals": 0,
            "provider": {"openai_configured": True, "ollama_online": False},
            "models": {"fast": "openai:gpt-test"},
            "screen_companion": self.companion_state,
            "public_presence": self.public_presence_state,
            "fatal_error": None,
        }

    def public_presence_status(self):
        return self.public_presence_state

    def network_inventory_status(self):
        self.network_status_reads += 1
        return self.network_state

    def network_device_detail(self, device_id, *, event_limit=100):
        if device_id != "device-1":
            raise LookupError("Device was not found")
        return {
            "device": self.network_state["inventory"]["devices"][0],
            "events": [{"event_type": "reachable", "observed_at": "2026-08-28T12:00:00+00:00"}],
            "sessions": [],
            "addresses": [{"ipv4": "192.168.50.21"}],
            "event_limit": event_limit,
        }

    def pair_network_scope(self, *, interface_index, owns_or_administers, display_name=None):
        if owns_or_administers is not True:
            raise PermissionError("Ownership or administrator confirmation is required")
        self.network_pair_calls.append((interface_index, owns_or_administers, display_name))
        return {"scope": self.network_state["scopes"][0], "status": self.network_state}

    def unpair_network_scope(self, scope_id):
        self.network_unpair_calls.append(scope_id)
        return self.network_state

    def scan_network_inventory(self, *, scope_id, max_hosts):
        self.network_scan_calls.append((scope_id, max_hosts))
        return self.network_state

    def set_network_device_profile(self, *, device_id, label, trust_state, device_type):
        self.network_profile_calls.append((device_id, label, trust_state, device_type))
        device = dict(self.network_state["inventory"]["devices"][0])
        device.update({"label": label, "trust_state": trust_state, "device_type": device_type})
        return {"device": device}

    def acknowledge_network_incident(self, *, incident_id, receipt_id):
        self.network_incident_acknowledgements.append((incident_id, receipt_id))
        return {
            "incident_id": incident_id,
            "receipt_id": receipt_id,
            "acknowledged": True,
            "changed": True,
        }

    def bluetooth_inventory_status(self):
        self.bluetooth_status_reads += 1
        return self.bluetooth_state

    def bluetooth_device_detail(self, device_id, *, event_limit=100):
        if device_id != "b" * 64:
            raise LookupError("Bluetooth endpoint was not found")
        return {
            "device": self.bluetooth_state["devices"][0],
            "events": [{
                "event_type": "paired_observed",
                "observed_at": "2026-08-28T12:00:00+00:00",
            }],
            "event_limit": event_limit,
            "addresses_exposed": False,
        }

    def check_bluetooth_inventory(self):
        self.bluetooth_check_calls += 1
        return self.bluetooth_state

    def set_bluetooth_device_profile(self, *, device_id, label, trust_state, device_type):
        self.bluetooth_profile_calls.append((device_id, label, trust_state, device_type))
        device = dict(self.bluetooth_state["devices"][0])
        device.update({"label": label, "trust_state": trust_state, "device_type": device_type})
        return {"device": device}

    def acknowledge_bluetooth_alert(self, *, event_id, receipt_id):
        self.bluetooth_acknowledgements.append((event_id, receipt_id))
        return {
            "event_id": event_id,
            "receipt_id": receipt_id,
            "state": "acknowledged",
            "changed": True,
        }

    def control_public_presence(self, action):
        if action not in {
            "pause", "resume", "emergency_stop", "clear_emergency_stop",
        }:
            raise ValueError("Public Presence control action is invalid")
        if action == "resume" and (
            not self.public_presence_state["control"]["enabled"]
            or self.public_presence_state["control"]["emergency_stopped"]
        ):
            raise PublicPresenceStopped(
                "Public Presence must be enabled and not stopped before resume"
            )
        self.public_presence_controls.append(action)
        if action == "emergency_stop":
            self.public_presence_state["control"].update({
                "enabled": False,
                "paused": True,
                "emergency_stopped": True,
                "effective_state": "emergency_stopped",
            })
            self.public_presence_state["effective_state"] = "emergency_stopped"
        elif action == "clear_emergency_stop":
            self.public_presence_state["control"].update({
                "enabled": False,
                "paused": True,
                "emergency_stopped": False,
                "effective_state": "disabled",
            })
            self.public_presence_state["effective_state"] = "disabled"
        return self.public_presence_state

    def screen_companion_status(self):
        return self.companion_state

    def screen_companion_indicator_status(self):
        state = {
            key: self.companion_state[key]
            for key in ("mode", "paused", "available")
        }
        state["suggestion"] = self.companion_suggestion
        return state

    def screen_companion_action_status(self, job_id):
        if job_id != "d" * 32:
            return None
        return {
            "job_id": job_id,
            "state": "completed",
            "message": "Done — I tightened the outline.",
            "terminal": True,
            "updated_at": time.time(),
            "expires_at": time.time() + 60,
        }

    def respond_screen_companion_suggestion(self, suggestion_id, *, accept):
        self.companion_suggestion_decisions.append((suggestion_id, accept))
        self.companion_suggestion = None
        return {"accepted": accept, "job_id": "d" * 32 if accept else None}

    def set_screen_companion(self, *, mode, paused, auto_suggest, excluded_apps):
        self.companion_state.update({
            "mode": mode,
            "paused": paused,
            "auto_suggest": auto_suggest,
            "excluded_apps": excluded_apps,
        })
        return self.companion_state

    def control_screen_companion(self, *, action, mode=None):
        self.companion_controls.append((action, mode))
        if action in {"on", "resume"}:
            if self.companion_state["mode"] == "disabled":
                self.companion_state["mode"] = "observe"
            self.companion_state["paused"] = False
        elif action == "pause":
            self.companion_state["paused"] = True
        elif action == "off":
            self.companion_state.update({"mode": "disabled", "paused": True})
        elif action == "mode":
            self.companion_state.update({"mode": mode, "paused": False})
        return self.companion_state

    def screen_companion_suggest_now(self):
        return "b" * 32

    def forget_screen_companion(self):
        return 3

    def add_screen_companion_rule(self, payload):
        self.companion_rule_payload = payload
        return 41

    def set_screen_companion_rule_enabled(self, rule_id, enabled):
        self.companion_rule_state = (rule_id, enabled)
        return True

    def delete_screen_companion_rule(self, rule_id):
        self.deleted_companion_rule = rule_id
        return True

    def events_after(self, event_id):
        self.last_event_after = event_id
        return [{"id": event_id + 1, "kind": "ready", "payload": {}}]

    def latest_event_id(self):
        return 17

    def conversations(self):
        return [{
            "id": 7, "title": "Presence", "message_count": 0,
            "project_id": 1, "project_name": "Default workspace",
        }]

    def projects(self):
        return [{
            "id": 1, "name": "Default workspace", "relative_path": ".",
            "enabled": 1, "conversation_count": 1, "task_count": 0,
            "kind": "general", "description": "Main workspace",
            "folders": [], "isolated": False,
        }]

    def create_project(self, name, kind="general", description=""):
        return {
            "id": 2, "name": name, "relative_path": "@projects/test", "enabled": 1,
            "kind": kind, "description": description,
            "folders": ["code", "research", "documents", "images", "datasets", "exports"],
            "isolated": True,
        }

    def artifacts(self, project_id):
        if project_id != 1:
            raise ValueError("Project does not exist or is disabled")
        return [{
            "name": "report.md", "relative_path": "reports/report.md",
            "kind": "document", "size": 42, "modified_at": 1_777_000_000,
        }]

    def artifact_image(self, project_id, relative_path):
        if project_id != 1 or relative_path != "images/result.png":
            raise ValueError("Artifact image is unavailable")
        return base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        ), "image/png"

    def schedule_overview(self):
        return {
            "tasks": [{
                "id": 9, "status": "queued", "prompt": "Review the workspace",
                "updated_at": "2026-08-20T12:00:00+00:00", "project_id": 1,
                "specialist_key": "coding",
            }],
            "learning_topics": [],
            "backlog": [],
        }

    def create_conversation(self, title):
        self.title = title
        return 7

    def conversation_messages(self, conversation_id):
        if conversation_id != 7:
            raise ValueError("Conversation does not exist")
        return []

    def delete_conversation(self, conversation_id):
        if conversation_id != 7:
            raise LookupError("Conversation does not exist")
        self.deleted_conversations.append(conversation_id)
        return {"id": conversation_id, "project_id": 1}

    def submit(self, conversation_id, prompt, model, attachments=None):
        self.submitted = (conversation_id, prompt, model, attachments)
        return "a" * 32

    def cancel(self, job_id):
        return job_id == "a" * 32

    def approvals(self):
        return []

    def persistent_approvals(self):
        return []

    def decide_approval(self, approval_id, approve):
        self.decisions.append((approval_id, approve))
        return True

    def decide_approval_always(self, approval_id):
        self.always_decisions.append(approval_id)
        return 12

    def decide_approval_for_session(self, approval_id):
        self.session_decisions.append(approval_id)
        return 13

    def revoke_persistent_approval(self, grant_id):
        self.revoked_grants.append(grant_id)
        return True

    def set_control(self, state, reason):
        self.controls.append((state, reason))


class PresenceHelpersTests(unittest.TestCase):
    def test_project_creation_builds_a_typed_isolated_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            data = root / "data"
            workspace.mkdir()
            data.mkdir()
            config = Config(
                root=root,
                workspace=workspace,
                data_dir=data,
                soul_path=root / "SOUL.md",
                model="auto",
                fast_model="openai:gpt-test",
                reasoning_model="openai:gpt-test",
                coding_model="openai:gpt-test",
                deep_model="openai:gpt-test",
                ollama_url="http://127.0.0.1:11434",
                ollama_api_key=None,
                max_steps=5,
                context_length=4096,
                command_timeout=30,
                autonomy="autonomous",
            )
            runtime = PresenceRuntime(config)

            project = runtime.create_project(
                "Evidence Review",
                "research",
                "Keep sources, briefs, and supporting files together.",
            )

            project_root = root / "workspace-projects" / "evidence-review"
            self.assertTrue(project["isolated"])
            self.assertEqual(project["kind"], "research")
            self.assertEqual(
                set(project["folders"]),
                {"code", "research", "documents", "images", "datasets", "exports"},
            )
            for folder in project["folders"]:
                self.assertTrue((project_root / folder).is_dir())
            self.assertTrue((project_root / "PROJECT.md").is_file())
            manifest = json.loads(
                (project_root / ".jarvis-project.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["kind"], "research")
            conversation = runtime.create_conversation("Sources", project["id"])
            with Memory(data / "jarvis.db") as memory:
                self.assertEqual(
                    memory.conversation_project(conversation)["relative_path"],
                    "@projects/evidence-review",
                )

    def test_interactive_approval_retry_binds_exact_assistant_marker(self):
        approval = {"id": 7, "scope": "conversation:12", "task_id": None}
        messages = [
            {"role": "user", "content": "scan my drive"},
            {
                "role": "assistant",
                "content": "Incomplete: Approval request #7 is waiting for an operator decision. Review it.",
            },
            {"role": "user", "content": "unrelated later message"},
        ]

        self.assertEqual(
            _interactive_approval_retry(approval, messages),
            (12, "scan my drive"),
        )
        self.assertIsNone(_interactive_approval_retry(
            {**approval, "id": 8}, messages
        ))
        self.assertIsNone(_interactive_approval_retry(
            {**approval, "task_id": 3}, messages
        ))

    def test_approving_interactive_request_automatically_resumes_exact_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            data = root / "data"
            workspace.mkdir()
            data.mkdir()
            config = Config(
                root=root,
                workspace=workspace,
                data_dir=data,
                soul_path=root / "SOUL.md",
                model="auto",
                fast_model="openai:gpt-test",
                reasoning_model="openai:gpt-test",
                coding_model="openai:gpt-test",
                deep_model="openai:gpt-test",
                ollama_url="http://127.0.0.1:11434",
                ollama_api_key=None,
                max_steps=5,
                context_length=4096,
                command_timeout=30,
                autonomy="autonomous",
            )
            with Memory(data / "jarvis.db") as memory:
                conversation_id = memory.new_conversation("Storage cleanup")
                prompt = "find what is using space on my drive"
                memory.add_message(conversation_id, "user", prompt)
                _allowed, approval_id = memory.authorize_or_request(
                    "access_private_files",
                    '{"tool":"computer_storage_report"}',
                    "This inspects file sizes outside the workspace.",
                    approval_scope=f"conversation:{conversation_id}",
                )
                memory.add_message(
                    conversation_id,
                    "assistant",
                    f"Incomplete: Approval request #{approval_id} is waiting for an operator decision. Review it.",
                )
            runtime = PresenceRuntime(config)
            with patch.object(runtime, "submit", return_value="a" * 32) as submit:
                self.assertTrue(runtime.decide_approval(approval_id, True))

            submit.assert_called_once_with(conversation_id, prompt, "auto")
            with Memory(data / "jarvis.db") as memory:
                self.assertEqual(memory.get_approval(approval_id)["status"], "approved")

    def test_approving_always_resumes_and_creates_exact_grant(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            data = root / "data"
            workspace.mkdir()
            data.mkdir()
            config = Config(
                root=root,
                workspace=workspace,
                data_dir=data,
                soul_path=root / "SOUL.md",
                model="auto",
                fast_model="openai:gpt-test",
                reasoning_model="openai:gpt-test",
                coding_model="openai:gpt-test",
                deep_model="openai:gpt-test",
                ollama_url="http://127.0.0.1:11434",
                ollama_api_key=None,
                max_steps=5,
                context_length=4096,
                command_timeout=30,
                autonomy="autonomous",
            )
            with Memory(data / "jarvis.db") as memory:
                conversation_id = memory.new_conversation("Storage cleanup")
                prompt = "inspect this exact folder"
                memory.add_message(conversation_id, "user", prompt)
                resource = json.dumps({
                    "tool": "computer_list_files",
                    "arguments_sha256": "a" * 64,
                    "arguments": {
                        "path": "C:/Users/test",
                        "recursive": False,
                        "resolved_path": "C:/Users/test",
                    },
                }, separators=(",", ":"))
                _allowed, approval_id = memory.authorize_or_request(
                    "access_private_files",
                    resource,
                    "This inspects the exact folder shown.",
                    approval_scope=f"conversation:{conversation_id}",
                )
                memory.add_message(
                    conversation_id,
                    "assistant",
                    f"Incomplete: Approval request #{approval_id} is waiting for an operator decision. Review it.",
                )
            runtime = PresenceRuntime(config)
            with patch.object(runtime, "submit", return_value="b" * 32) as submit:
                grant_id = runtime.decide_approval_always(approval_id)

            self.assertIsInstance(grant_id, int)
            submit.assert_called_once_with(conversation_id, prompt, "auto")
            with Memory(data / "jarvis.db") as memory:
                self.assertEqual(
                    memory.list_persistent_approvals(include_revoked=False)[0]["id"],
                    grant_id,
                )
                self.assertTrue(memory.revoke_persistent_approval(grant_id))
                _allowed, session_approval_id = memory.authorize_or_request(
                    "access_private_files",
                    resource,
                    "This inspects the exact folder shown.",
                    approval_scope=f"conversation:{conversation_id}",
                )
                memory.add_message(
                    conversation_id,
                    "assistant",
                    f"Incomplete: Approval request #{session_approval_id} is waiting for an operator decision. Review it.",
                )

            with patch.object(runtime, "submit", return_value="c" * 32) as submit:
                session_grant_id = runtime.decide_approval_for_session(
                    session_approval_id
                )
            self.assertIsInstance(session_grant_id, int)
            submit.assert_called_once_with(conversation_id, prompt, "auto")
            with Memory(data / "jarvis.db") as memory:
                session_grant = memory.list_persistent_approvals(
                    include_revoked=False
                )[0]
                self.assertEqual(session_grant["id"], session_grant_id)
                self.assertEqual(session_grant["grant_kind"], "session")
                self.assertEqual(
                    session_grant["scope"], f"conversation:{conversation_id}"
                )

    def test_pairing_codes_are_one_time_hashed_and_sessions_are_revocable(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "jarvis.db"
            with Memory(database) as memory:
                pairing = memory.create_presence_pairing_code("test phone")
                raw_code = pairing["code"]
                session = memory.consume_presence_pairing_code(raw_code)
                self.assertIsNotNone(session)
                self.assertIsNone(memory.consume_presence_pairing_code(raw_code))
                self.assertTrue(memory.authenticate_presence_session(session["token"]))
                listed = memory.list_presence_sessions()
                self.assertEqual(listed[0]["session_id"], session["session_id"])
                self.assertNotIn("token", listed[0])
                self.assertTrue(memory.revoke_presence_session(session["session_id"]))
                self.assertFalse(memory.authenticate_presence_session(session["token"]))
            with Memory(database) as reopened:
                self.assertFalse(reopened.authenticate_presence_session(session["token"]))
            database_bytes = database.read_bytes()
            self.assertNotIn(raw_code.replace("-", "").encode("ascii"), database_bytes)
            self.assertNotIn(session["token"].encode("ascii"), database_bytes)

    def test_pairing_code_race_issues_exactly_one_session(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "jarvis.db"
            with Memory(database) as memory:
                pairing = memory.create_presence_pairing_code("race")
            barrier = threading.Barrier(3)
            results = []
            errors = []

            def consume():
                try:
                    with Memory(database) as contender:
                        barrier.wait(timeout=3)
                        results.append(
                            contender.consume_presence_pairing_code(pairing["code"])
                        )
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=consume) for _ in range(2)]
            for thread in threads:
                thread.start()
            barrier.wait(timeout=3)
            for thread in threads:
                thread.join(timeout=5)
            self.assertEqual(errors, [])
            self.assertEqual(sum(result is not None for result in results), 1)
            with Memory(database) as memory:
                self.assertEqual(len(memory.list_presence_sessions()), 1)

    def test_expired_pairing_code_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "jarvis.db"
            with Memory(database) as memory:
                pairing = memory.create_presence_pairing_code("expired")
                memory.db.execute(
                    "UPDATE presence_pairing_codes SET expires_at=? WHERE id=?",
                    ("2000-01-01T00:00:00+00:00", pairing["pairing_id"]),
                )
                self.assertIsNone(memory.consume_presence_pairing_code(pairing["code"]))

    def test_host_and_port_fail_closed(self):
        self.assertEqual(normalize_presence_host("LOCALHOST"), "localhost")
        self.assertEqual(normalize_presence_port("8787"), 8787)
        for host in ("0.0.0.0", "192.168.1.10", "jarvis.example"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                normalize_presence_host(host)
        for port in (True, 80, 70000, "invalid"):
            with self.subTest(port=port), self.assertRaises(ValueError):
                normalize_presence_port(port)

    def test_request_host_parser_accepts_exact_hosts_and_rejects_ambiguity(self):
        self.assertEqual(normalize_request_host("LOCALHOST:8787"), "localhost")
        self.assertEqual(normalize_request_host("[::1]:8787"), "::1")
        for value in (
            "",
            "user@localhost",
            "localhost/path",
            "::1",
            "*.example.com",
            "[::1]trailing",
            "[::1]:8787:extra",
            "[" + "[\\" * 1_000,
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_request_host(value)

    def test_display_text_is_redacted_and_bounded(self):
        secret = "sk-proj-" + "A" * 32
        rendered = safe_presence_text(f"token={secret}\x00" + "x" * 100, 60)
        self.assertNotIn(secret, rendered)
        self.assertNotIn("\x00", rendered)
        self.assertLessEqual(len(rendered), 60)

    def test_companion_suggestion_is_plain_optional_and_discards_system_noise(self):
        rendered = safe_companion_suggestion(
            "Suggestion: **Review the outline and turn it into three clear sections.**\n\n"
            "Incomplete: No public source page was fetched successfully.",
            "claude.exe",
        )
        self.assertEqual(
            rendered,
            "Want me to review the outline and turn it into three clear sections?",
        )
        fallback = safe_companion_suggestion(
            "Incomplete: No allowed source URL was provided.",
            "photoshop.exe",
        )
        self.assertEqual(
            fallback,
            "Want me to help with the next useful step in photoshop?",
        )
        self.assertLessEqual(len(rendered), 180)

    def test_internal_companion_conversation_cannot_reopen_in_operator_chat(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / "data"
            data.mkdir()
            with Memory(data / "jarvis.db") as memory:
                operator_id = memory.new_conversation("Operator chat")
                memory.add_message(operator_id, "assistant", "Visible answer")
                internal_id = memory.new_conversation("Screen Companion")
                memory.mark_screen_companion_conversation(internal_id)
                memory.add_message(internal_id, "user", "Private companion analysis")

            runtime = PresenceRuntime(SimpleNamespace(data_dir=data))
            self.assertEqual(
                runtime.conversation_messages(operator_id)[-1]["content"],
                "Visible answer",
            )
            self.assertNotIn(
                internal_id,
                {int(row["id"]) for row in runtime.conversations()},
            )
            with self.assertRaisesRegex(ValueError, "internal"):
                runtime.conversation_messages(internal_id)

    def test_invalid_public_database_fails_closed_without_breaking_presence(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / "data"
            data.mkdir()
            db = sqlite3.connect(data / "public_presence.db")
            try:
                db.execute("CREATE TABLE foreign_application(secret TEXT)")
                db.commit()
            finally:
                db.close()
            runtime = PresenceRuntime(SimpleNamespace(data_dir=data))
            status = runtime.public_presence_status()
            self.assertEqual(status["effective_state"], "unavailable")
            self.assertTrue(status["control"]["emergency_stopped"])
            self.assertFalse(status["publishing_available"])
            self.assertIsInstance(runtime.runtime_epoch, str)

    def test_public_presence_controls_persist_across_runtime_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / "data"
            data.mkdir()
            config = SimpleNamespace(
                data_dir=data,
                public_presence_enabled=True,
                presence_max_agents=1,
            )
            store = PublicPresenceStore(data / "public_presence.db")
            store.set_enabled(True, actor="test:operator")
            store.set_paused(False, actor="test:operator")

            first = PresenceRuntime(config)
            self.assertEqual(
                first.public_presence_status()["effective_state"],
                "foundation_ready",
            )
            paused = first.control_public_presence("pause")
            self.assertEqual(paused["effective_state"], "paused")

            second = PresenceRuntime(config)
            self.assertTrue(second.public_presence_status()["control"]["paused"])
            resumed = second.control_public_presence("resume")
            self.assertEqual(resumed["effective_state"], "foundation_ready")
            stopped = second.control_public_presence("emergency_stop")
            self.assertEqual(stopped["effective_state"], "emergency_stopped")

            third = PresenceRuntime(config)
            persisted = third.public_presence_status()
            self.assertTrue(persisted["control"]["emergency_stopped"])
            cleared = third.control_public_presence("clear_emergency_stop")
            self.assertEqual(cleared["effective_state"], "disabled")
            self.assertFalse(cleared["control"]["enabled"])
            self.assertTrue(cleared["control"]["paused"])

            fourth = PresenceRuntime(config)
            final = fourth.public_presence_status()
            self.assertEqual(final["effective_state"], "disabled")
            self.assertFalse(final["control"]["enabled"])
            self.assertTrue(final["control"]["paused"])
            self.assertFalse(final["control"]["emergency_stopped"])

    def test_product_comparison_payload_is_bounded_safe_and_deduplicated(self):
        product = {
            "name": "<img src=x onerror=alert(1)>",
            "source_url": "https://seller.example/item",
            "source_kind": "seller",
            "seller": "Example",
            "manufacturer": None,
            "price_text": "$20",
            "currency": "USD",
            "availability": "In stock",
            "key_specs": ["blue"],
            "why_fit": "bounded",
            "tradeoff": "unknown",
            "observed_at": "2026-08-27T10:00:00-04:00",
            "image_url": "https://tracker.example/pixel.gif",
        }
        rendered = safe_presence_product_comparison({
            "ranking": "First choice",
            "products": [
                product,
                dict(product),
                {**product, "name": "Unsafe", "source_url": "javascript:alert(1)"},
                {**product, "name": "Credentials", "source_url": "https://user:pass@example.com/item"},
            ],
        })

        self.assertEqual(len(rendered["products"]), 1)
        self.assertEqual(rendered["products"][0]["name"], product["name"])
        self.assertIsNone(rendered["products"][0]["image_url"])
        self.assertEqual(rendered["products"][0]["source_url"], product["source_url"])

    def test_runtime_rejects_unknown_job_cancellation(self):
        runtime = PresenceRuntime(SimpleNamespace())
        self.assertFalse(runtime.cancel("a" * 32))

    def test_each_runtime_has_a_distinct_event_epoch(self):
        first = PresenceRuntime(SimpleNamespace())
        second = PresenceRuntime(SimpleNamespace())
        self.assertRegex(first.runtime_epoch, r"^[0-9a-f]{32}$")
        self.assertNotEqual(first.runtime_epoch, second.runtime_epoch)

    def test_artifact_index_is_metadata_only_bounded_and_skips_private_trees(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            data = root / "data"
            workspace.mkdir()
            data.mkdir()
            (workspace / "report.md").write_text("private report body", encoding="utf-8")
            (workspace / "agent.py").write_text("print('ok')", encoding="utf-8")
            (workspace / ".secret.txt").write_text("hidden", encoding="utf-8")
            (workspace / "data").mkdir()
            (workspace / "data" / "jarvis.db").write_bytes(b"database")
            config = Config(
                root=root,
                workspace=workspace,
                data_dir=data,
                soul_path=root / "SOUL.md",
                model="auto",
                fast_model="openai:gpt-test",
                reasoning_model="openai:gpt-test",
                coding_model="openai:gpt-test",
                deep_model="openai:gpt-test",
                ollama_url="http://127.0.0.1:11434",
                ollama_api_key=None,
                max_steps=5,
                context_length=4096,
                command_timeout=30,
                autonomy="autonomous",
            )
            runtime = PresenceRuntime(config)
            artifacts = runtime.artifacts(1)
            by_path = {row["relative_path"]: row for row in artifacts}
            self.assertEqual(set(by_path), {"agent.py", "report.md"})
            self.assertEqual(by_path["agent.py"]["kind"], "code")
            self.assertEqual(by_path["report.md"]["kind"], "document")
            self.assertNotIn("private report body", json.dumps(artifacts))
            self.assertLessEqual(len(artifacts), 200)

    def test_artifact_image_is_verified_and_path_traversal_is_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            data = root / "data"
            workspace.mkdir()
            data.mkdir()
            image_dir = workspace / "images"
            image_dir.mkdir()
            image_bytes = base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
            )
            (image_dir / "result.png").write_bytes(image_bytes)
            (root / "outside.png").write_bytes(image_bytes)
            config = Config(
                root=root,
                workspace=workspace,
                data_dir=data,
                soul_path=root / "SOUL.md",
                model="auto",
                fast_model="openai:gpt-test",
                reasoning_model="openai:gpt-test",
                coding_model="openai:gpt-test",
                deep_model="openai:gpt-test",
                ollama_url="http://127.0.0.1:11434",
                ollama_api_key=None,
                max_steps=5,
                context_length=4096,
                command_timeout=30,
                autonomy="autonomous",
            )
            runtime = PresenceRuntime(config)

            body, mime = runtime.artifact_image(1, "images/result.png")

            self.assertEqual(body, image_bytes)
            self.assertEqual(mime, "image/png")
            for bad in ("../outside.png", "images/../../outside.png", "/outside.png"):
                with self.subTest(path=bad), self.assertRaises(ValueError):
                    runtime.artifact_image(1, bad)

    def test_provider_rejection_is_an_incomplete_assistant_reply_not_raw_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            data = root / "data"
            workspace.mkdir()
            data.mkdir()
            config = Config(
                root=root,
                workspace=workspace,
                data_dir=data,
                soul_path=root / "SOUL.md",
                model="auto",
                fast_model="openai:gpt-5.6-luna",
                reasoning_model="openai:gpt-5.6-terra",
                coding_model="openai:gpt-5.6-sol",
                deep_model="openai:gpt-5.6-sol",
                ollama_url="http://127.0.0.1:11434",
                ollama_api_key=None,
                max_steps=5,
                context_length=4096,
                command_timeout=30,
                autonomy="autonomous",
                presence_max_agents=1,
            )

            class RejectedAgent:
                def __init__(self, *_args, **_kwargs):
                    self.client = SimpleNamespace(
                        provider_status={
                            "openai_configured": True,
                            "openai_healthy": None,
                        }
                    )

                def run(self, *_args, **_kwargs):
                    self.client.provider_status["openai_healthy"] = False
                    raise OllamaError(
                        "OpenAI model provider request failed with HTTP 400: raw detail",
                        status_code=400,
                    )

            with patch("jarvis.presence.Agent", RejectedAgent):
                runtime = PresenceRuntime(config)
                runtime.start()
                try:
                    conversation_id = runtime.create_conversation("Provider failure")
                    job_id = runtime.submit(conversation_id, "research this", "auto")
                    deadline = time.time() + 2
                    terminal = None
                    while time.time() < deadline:
                        terminal = next(
                            (
                                event
                                for event in runtime.events_after(0, limit=500)
                                if event["payload"].get("job_id") == job_id
                                and event["kind"] in {"assistant", "error"}
                            ),
                            None,
                        )
                        if terminal is not None:
                            break
                        time.sleep(0.01)
                    self.assertIsNotNone(terminal)
                    self.assertEqual(terminal["kind"], "assistant")
                    self.assertEqual(terminal["payload"]["status"], "incomplete")
                    rendered = terminal["payload"]["content"]
                    self.assertNotIn("HTTP 400", rendered)
                    self.assertNotIn("raw detail", rendered)
                    self.assertIn("request was preserved", rendered.casefold())
                    self.assertFalse(runtime.status()["provider"]["openai_healthy"])
                finally:
                    runtime.shutdown()

    def test_runtime_runs_isolated_projects_concurrently_and_cancels_one_job(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            data = root / "data"
            workspace.mkdir()
            data.mkdir()
            config = Config(
                root=root,
                workspace=workspace,
                data_dir=data,
                soul_path=root / "SOUL.md",
                model="auto",
                fast_model="openai:gpt-5.6-luna",
                reasoning_model="openai:gpt-5.6-terra",
                coding_model="openai:gpt-5.6-sol",
                deep_model="openai:gpt-5.6-sol",
                ollama_url="http://127.0.0.1:11434",
                ollama_api_key=None,
                max_steps=5,
                context_length=4096,
                command_timeout=30,
                autonomy="autonomous",
                presence_max_agents=2,
            )
            entered = threading.Event()
            release = threading.Event()
            lock = threading.Lock()
            workspaces = {}
            running = 0

            class Result(str):
                status = "complete"
                reason = None
                approval_id = None
                model = "openai:gpt-test"

            clients = []

            class ConcurrentAgent:
                def __init__(self, task_config, _memory, _event=None, **kwargs):
                    self.config = task_config
                    supplied = kwargs.get("client")
                    self.client = supplied or SimpleNamespace(
                        provider_status={"openai_configured": True},
                    )
                    clients.append((supplied, self.client))

                def run(self, prompt, *, cancellation_guard, **_kwargs):
                    nonlocal running
                    with lock:
                        workspaces[prompt] = self.config.workspace
                        running += 1
                        if running == 2:
                            entered.set()
                    while not release.wait(0.01):
                        if cancellation_guard():
                            raise AgentRunCancelled("cancelled")
                    if cancellation_guard():
                        raise AgentRunCancelled("cancelled")
                    return Result(f"done {prompt}")

            with patch("jarvis.presence.Agent", ConcurrentAgent):
                runtime = PresenceRuntime(config)
                runtime.start()
                try:
                    alpha = runtime.create_project("Alpha")
                    beta = runtime.create_project("Beta")
                    first_conversation = runtime.create_conversation(
                        "First", alpha["id"]
                    )
                    second_conversation = runtime.create_conversation(
                        "Second", beta["id"]
                    )
                    first_job = runtime.submit(first_conversation, "alpha", "auto")
                    with self.assertRaisesRegex(RuntimeError, "already has"):
                        runtime.submit(first_conversation, "alpha again", "fast")
                    second_job = runtime.submit(second_conversation, "beta", "auto")
                    self.assertTrue(entered.wait(2))
                    status = runtime.status()
                    self.assertEqual(status["runtime_epoch"], runtime.runtime_epoch)
                    self.assertEqual(status["active_agent_count"], 2)
                    self.assertIsNone(clients[0][0])
                    self.assertIs(clients[1][0], clients[0][1])
                    self.assertIs(clients[2][0], clients[0][1])
                    self.assertEqual(
                        {item["job_id"] for item in status["active_jobs"]},
                        {first_job, second_job},
                    )
                    self.assertEqual(
                        {item["job_id"] for item in status["jobs"]},
                        {first_job, second_job},
                    )
                    self.assertTrue(runtime.cancel(first_job))
                    time.sleep(0.05)
                    release.set()
                    deadline = time.time() + 2
                    events = []
                    while time.time() < deadline:
                        events = runtime.events_after(0, limit=500)
                        terminal = {
                            event["payload"].get("job_id")
                            for event in events
                            if event["kind"] in {"assistant", "cancelled", "error"}
                        }
                        if {first_job, second_job}.issubset(terminal):
                            break
                        time.sleep(0.01)
                    by_job = {
                        event["payload"].get("job_id"): event["kind"]
                        for event in events
                        if event["kind"] in {"assistant", "cancelled", "error"}
                    }
                    self.assertEqual(by_job[first_job], "cancelled")
                    self.assertEqual(by_job[second_job], "assistant")
                    self.assertNotEqual(workspaces["alpha"], workspaces["beta"])
                    with Memory(data / "jarvis.db") as memory:
                        cancelled_messages = memory.recent_messages(
                            first_conversation, limit=5
                        )
                    self.assertEqual(cancelled_messages[-1]["role"], "assistant")
                    self.assertEqual(
                        cancelled_messages[-1]["content"], "Request stopped."
                    )
                finally:
                    release.set()
                    runtime.shutdown()

    def test_queued_cancel_emits_once_and_persists_terminal_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            data = root / "data"
            workspace.mkdir()
            data.mkdir()
            config = Config(
                root=root,
                workspace=workspace,
                data_dir=data,
                soul_path=root / "SOUL.md",
                model="auto",
                fast_model="openai:gpt-5.6-luna",
                reasoning_model="openai:gpt-5.6-terra",
                coding_model="openai:gpt-5.6-sol",
                deep_model="openai:gpt-5.6-sol",
                ollama_url="http://127.0.0.1:11434",
                ollama_api_key=None,
                max_steps=5,
                context_length=4096,
                command_timeout=30,
                autonomy="autonomous",
                presence_max_agents=1,
            )
            entered = threading.Event()
            release = threading.Event()
            ran_prompts: list[str] = []

            class Result(str):
                status = "complete"
                reason = None
                approval_id = None
                model = "openai:gpt-test"

            class BlockingAgent:
                def __init__(self, _config, _memory, _event=None, **kwargs):
                    self.client = kwargs.get("client") or SimpleNamespace(
                        provider_status={"openai_configured": True},
                    )

                def run(self, prompt, **_kwargs):
                    ran_prompts.append(prompt)
                    entered.set()
                    release.wait(2)
                    return Result(f"done {prompt}")

            with patch("jarvis.presence.Agent", BlockingAgent):
                runtime = PresenceRuntime(config)
                runtime.start()
                try:
                    first_conversation = runtime.create_conversation("First")
                    queued_conversation = runtime.create_conversation("Queued")
                    runtime.submit(first_conversation, "block the worker", "auto")
                    self.assertTrue(entered.wait(2))
                    queued_job = runtime.submit(
                        queued_conversation,
                        "never execute this",
                        "auto",
                    )
                    event_cursor = runtime.latest_event_id()

                    self.assertTrue(runtime.cancel(queued_job))

                    terminal = [
                        event
                        for event in runtime.events_after(event_cursor, limit=50)
                        if event["payload"].get("job_id") == queued_job
                        and event["kind"] in {"assistant", "cancelled", "error"}
                    ]
                    self.assertEqual(len(terminal), 1)
                    self.assertEqual(terminal[0]["kind"], "cancelled")
                    self.assertEqual(
                        terminal[0]["payload"]["message"],
                        "Request cancelled before execution",
                    )
                    self.assertFalse(runtime.cancel(queued_job))
                    with Memory(data / "jarvis.db") as memory:
                        self.assertEqual(
                            memory.get_presence_job(queued_job)["status"],
                            "cancelled",
                        )
                        messages = memory.recent_messages(
                            queued_conversation,
                            limit=10,
                        )
                    self.assertEqual(messages[-1]["role"], "assistant")
                    self.assertEqual(
                        messages[-1]["content"],
                        "Request cancelled before execution.",
                    )

                    release.set()
                    deadline = time.time() + 2
                    while time.time() < deadline:
                        if any(
                            event["kind"] == "assistant"
                            and event["payload"].get("conversation_id")
                            == first_conversation
                            for event in runtime.events_after(0, limit=500)
                        ):
                            break
                        time.sleep(0.01)
                    time.sleep(0.05)
                    queued_terminal_events = [
                        event
                        for event in runtime.events_after(0, limit=500)
                        if event["payload"].get("job_id") == queued_job
                        and event["kind"] in {"assistant", "cancelled", "error"}
                    ]
                    self.assertEqual(len(queued_terminal_events), 1)
                    self.assertEqual(ran_prompts, ["block the worker"])
                finally:
                    release.set()
                    runtime.shutdown()

    def test_queued_presence_job_survives_restart_and_runs_exactly_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            data = root / "data"
            workspace.mkdir()
            data.mkdir()
            config = Config(
                root=root,
                workspace=workspace,
                data_dir=data,
                soul_path=root / "SOUL.md",
                model="auto",
                fast_model="openai:gpt-5.6-luna",
                reasoning_model="openai:gpt-5.6-terra",
                coding_model="openai:gpt-5.6-sol",
                deep_model="openai:gpt-5.6-sol",
                ollama_url="http://127.0.0.1:11434",
                ollama_api_key=None,
                max_steps=5,
                context_length=4096,
                command_timeout=30,
                autonomy="autonomous",
                presence_max_agents=1,
            )
            job_id = "d" * 32
            with Memory(data / "jarvis.db") as memory:
                conversation_id = memory.new_conversation("Durable request")
                memory.create_presence_job(
                    job_id,
                    conversation_id=conversation_id,
                    project_id=1,
                    prompt="preserved prompt",
                    model_override="fast",
                )

            runs = []

            class Result(str):
                status = "complete"
                reason = None
                approval_id = None
                model = "openai:gpt-test"

            class DurableAgent:
                def __init__(self, *_args, **_kwargs):
                    self.client = SimpleNamespace(
                        provider_status={"openai_configured": True}
                    )

                def run(self, prompt, **_kwargs):
                    runs.append(prompt)
                    return Result("completed once")

            with patch("jarvis.presence.Agent", DurableAgent):
                first = PresenceRuntime(config)
                first.start()
                try:
                    deadline = time.time() + 2
                    while time.time() < deadline:
                        with Memory(data / "jarvis.db") as memory:
                            row = memory.get_presence_job(job_id)
                        if row and row["status"] == "completed":
                            break
                        time.sleep(0.01)
                    self.assertEqual(row["status"], "completed")
                    self.assertEqual(runs, ["preserved prompt"])
                finally:
                    first.shutdown()

                second = PresenceRuntime(config)
                second.start()
                try:
                    time.sleep(0.05)
                    self.assertEqual(runs, ["preserved prompt"])
                finally:
                    second.shutdown()

    def test_running_presence_job_is_interrupted_not_replayed_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            data = root / "data"
            workspace.mkdir()
            data.mkdir()
            config = Config(
                root=root,
                workspace=workspace,
                data_dir=data,
                soul_path=root / "SOUL.md",
                model="auto",
                fast_model="openai:gpt-5.6-luna",
                reasoning_model="openai:gpt-5.6-terra",
                coding_model="openai:gpt-5.6-sol",
                deep_model="openai:gpt-5.6-sol",
                ollama_url="http://127.0.0.1:11434",
                ollama_api_key=None,
                max_steps=5,
                context_length=4096,
                command_timeout=30,
                autonomy="autonomous",
                presence_max_agents=1,
            )
            job_id = "e" * 32
            with Memory(data / "jarvis.db") as memory:
                conversation_id = memory.new_conversation("Interrupted request")
                memory.create_presence_job(
                    job_id,
                    conversation_id=conversation_id,
                    project_id=1,
                    prompt="possibly consequential prompt",
                    model_override="auto",
                )
                self.assertTrue(
                    memory.claim_presence_job(job_id, "presence:old-runtime")
                )

            runs = []

            class ProbeOnlyAgent:
                def __init__(self, *_args, **_kwargs):
                    self.client = SimpleNamespace(
                        provider_status={"openai_configured": True}
                    )

                def run(self, prompt, **_kwargs):
                    runs.append(prompt)
                    raise AssertionError("Interrupted work must not be replayed")

            with patch("jarvis.presence.Agent", ProbeOnlyAgent):
                runtime = PresenceRuntime(config)
                runtime.start()
                try:
                    with Memory(data / "jarvis.db") as memory:
                        row = memory.get_presence_job(job_id)
                        messages = memory.recent_messages(conversation_id, limit=5)
                    self.assertEqual(row["status"], "interrupted")
                    self.assertIn("not replayed automatically", row["last_error"])
                    self.assertEqual(runs, [])
                    self.assertIn(
                        "not replayed automatically",
                        messages[-1]["content"],
                    )
                finally:
                    runtime.shutdown()

    def test_presence_forwards_redacted_deltas_before_authoritative_final_message(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            data = root / "data"
            workspace.mkdir()
            data.mkdir()
            config = Config(
                root=root,
                workspace=workspace,
                data_dir=data,
                soul_path=root / "SOUL.md",
                model="auto",
                fast_model="openai:gpt-test",
                reasoning_model="openai:gpt-test",
                coding_model="openai:gpt-test",
                deep_model="openai:gpt-test",
                ollama_url="http://127.0.0.1:11434",
                ollama_api_key=None,
                max_steps=5,
                context_length=4096,
                command_timeout=30,
                autonomy="autonomous",
                presence_max_agents=1,
            )
            secret = "sk-proj-" + "A" * 32

            class Result(str):
                status = "complete"
                reason = None
                approval_id = None
                model = "openai:gpt-test"

            class StreamingAgent:
                def __init__(self, *_args, **_kwargs):
                    self.client = SimpleNamespace(
                        provider_status={"openai_configured": True}
                    )
                    self.on_event = _args[2] if len(_args) > 2 else (lambda _message: None)

                def run(self, _prompt, *, stream_callback, **_kwargs):
                    self.on_event("processing - step 7")
                    stream_callback("Hello ")
                    stream_callback(f"api_key={secret}")
                    return Result(f"Hello api_key={secret}")

            with patch("jarvis.presence.Agent", StreamingAgent):
                runtime = PresenceRuntime(config)
                runtime.start()
                try:
                    conversation_id = runtime.create_conversation("Streaming")
                    job_id = runtime.submit(conversation_id, "hello", "fast")
                    deadline = time.time() + 2
                    relevant = []
                    while time.time() < deadline:
                        relevant = [
                            event for event in runtime.events_after(0, limit=500)
                            if event["payload"].get("job_id") == job_id
                            and event["kind"] in {"assistant_delta", "assistant"}
                        ]
                        if any(event["kind"] == "assistant" for event in relevant):
                            break
                        time.sleep(0.01)
                    self.assertEqual(
                        [event["kind"] for event in relevant],
                        ["assistant_delta", "assistant_delta", "assistant"],
                    )
                    rendered = json.dumps(relevant)
                    self.assertNotIn(secret, rendered)
                    self.assertIn("[redacted]", rendered.casefold())
                    activities = [
                        event for event in runtime.events_after(0, limit=500)
                        if event["payload"].get("job_id") == job_id
                        and event["kind"] == "activity"
                    ]
                    self.assertEqual(
                        activities[-1]["payload"]["message"],
                        "processing - step 7",
                    )
                finally:
                    runtime.shutdown()


class PresenceNetworkRuntimeTests(unittest.TestCase):
    class Store:
        def __init__(self):
            self.list_calls = []
            self.scan_calls = []
            self.pair_calls = []
            self.pending_incident_calls = []
            self.incident_acknowledgements = []
            self.incident_actions = []

        def list_devices(self, **kwargs):
            self.list_calls.append(kwargs)
            return {
                "devices": [{
                    "device_id": "device-1",
                    "display_name": "Laptop",
                    "trust_state": "recognized",
                }],
                "known_devices": 1,
                "total_known_devices": 1,
                "last_scan_at": None,
            }

        def list_scopes(self):
            return {"scopes": [{
                "scope_id": "scope-home",
                "cidr": "192.168.50.0/24",
                "active": True,
            }]}

        def status(self, *, include_identifiers=False):
            return {
                "paired": True,
                "scopes": self.list_scopes()["scopes"],
                "inventory": self.list_devices(
                    include_offline=True,
                    include_identifiers=include_identifiers,
                ),
                "scan_policy": {"max_hosts": 512},
                "pending_incidents": self.pending_incidents(
                    include_identifiers=include_identifiers
                ),
            }

        def pending_incidents(
            self, *, limit=50, assessment_id=None, include_identifiers=False
        ):
            self.pending_incident_calls.append(
                (limit, assessment_id, include_identifiers)
            )
            if assessment_id is None:
                return {"pending_count": 0, "incidents": [], "integrity_failures": []}
            return {
                "pending_count": 1,
                "integrity_failures": [],
                "incidents": [{
                    "incident_id": "1" * 32,
                    "receipt_id": "2" * 32,
                    "assessment_id": assessment_id,
                    "signal_id": "4" * 24,
                    "created_at": "2026-08-28T12:00:00+00:00",
                    "severity": "medium",
                    "category": "asset_change",
                    "device": {
                        "device_id": "a" * 32,
                        "display_name": "Observed device aaaaaa",
                        "device_type": "Unknown",
                    },
                    "observed_fact": "A new device needs review.",
                    "assessment": "This is not proof of compromise.",
                    "confidence": "limited",
                    "compromise_established": False,
                    "evidence_summary": ["Latest paired-LAN check"],
                    "automatic_actions": (
                        self.incident_actions[-1][1]
                        if self.incident_actions else []
                    ),
                    "actions_not_taken": ["No containment was performed."],
                    "recommended_action": "Identify it before changing access.",
                    "approval": None,
                    "limitations": ["Identity evidence can be spoofed."],
                }],
            }

        def record_incident_actions(self, *, incident_id, actions):
            durable = [{
                "tool_id": row["tool_id"],
                "title": row["title"],
                "outcome": "Passive read-only check completed.",
                "receipt_id": row["receipt_id"],
            } for row in actions]
            self.incident_actions.append((incident_id, durable))
            return {"incident_id": incident_id, "automatic_actions": durable}

        def acknowledge_incident(self, *, incident_id, receipt_id):
            self.incident_acknowledgements.append((incident_id, receipt_id))
            return {
                "incident_id": incident_id,
                "receipt_id": receipt_id,
                "acknowledged": True,
                "changed": True,
            }

        def scope_candidates(self):
            return {"candidates": [{"interface_index": 7, "eligible": True}]}

        def pair_scope(self, **kwargs):
            self.pair_calls.append(kwargs)
            return {"scope_id": "scope-home", "active": True}

        def unpair_scope(self, _scope_id):
            return True

        def scan(self, **kwargs):
            self.scan_calls.append(kwargs)
            return {"devices": []}

        def set_profile(self, device_id, label, trust_state, device_type):
            return {
                "device_id": device_id,
                "label": label,
                "trust_state": trust_state,
                "device_type": device_type,
            }

        def device_detail(self, device_id, event_limit=100, include_identifiers=False):
            return {"device": {"device_id": device_id}, "events": [], "event_limit": event_limit}

    @staticmethod
    def runtime(store=None, *, enabled=True):
        runtime = PresenceRuntime.__new__(PresenceRuntime)
        runtime.config = SimpleNamespace(
            network_access="private-lan" if enabled else "disabled",
            network_monitor_enabled=False,
            network_monitor_interval_seconds=300,
            network_defense_mode="alert-only",
            network_incident_popups_enabled=True,
            autonomy="autonomous",
            data_dir=Path("data"),
        )
        runtime._network_inventory = store
        runtime._network_inventory_error = None
        runtime._network_scan_lock = threading.Lock()
        runtime._network_monitor_thread = None
        runtime._network_monitor_last_check_at = None
        runtime._network_monitor_last_error = None
        runtime._network_security_registry = None
        runtime._network_security_registry_scope_key = ()
        runtime._network_security_registry_error = None
        runtime._network_security_registry_report = None
        runtime._background_control_state_override = "running"
        runtime._shutdown = threading.Event()
        runtime.emit = lambda *_args, **_kwargs: None
        return runtime

    def test_status_reads_history_and_adapters_without_scanning(self):
        store = self.Store()
        status = self.runtime(store).network_inventory_status()
        self.assertTrue(status["enabled"])
        self.assertTrue(status["can_scan"])
        self.assertEqual(store.scan_calls, [])
        self.assertEqual(
            store.list_calls,
            [{"include_offline": True, "include_identifiers": True}],
        )
        self.assertEqual(status["inventory"]["devices"][0]["device_id"], "device-1")

    def test_disabled_status_is_inert_and_pair_requires_exact_attestation(self):
        store = self.Store()
        status = self.runtime(store, enabled=False).network_inventory_status()
        self.assertFalse(status["enabled"])
        self.assertEqual(store.list_calls, [])
        runtime = self.runtime(store)
        with self.assertRaisesRegex(PermissionError, "own or administer"):
            runtime.pair_network_scope(
                interface_index=7,
                owns_or_administers=False,
                display_name="Home",
            )
        self.assertEqual(store.pair_calls, [])

    def test_explicit_scan_is_single_flight_and_bounded_to_selected_scope(self):
        store = self.Store()
        runtime = self.runtime(store)
        runtime._network_scan_lock.acquire()
        try:
            with self.assertRaises(NetworkInventoryScanBusy):
                runtime.scan_network_inventory(scope_id="scope-home", max_hosts=64)
        finally:
            runtime._network_scan_lock.release()
        self.assertEqual(store.scan_calls, [])
        status = runtime.scan_network_inventory(scope_id="scope-home", max_hosts=64)
        self.assertEqual(
            store.scan_calls,
            [{
                "max_hosts": 64,
                "include_offline": True,
                "scope_id": "scope-home",
                "include_identifiers": True,
            }],
        )
        self.assertTrue(status["can_scan"])

    def test_background_monitor_reuses_bounded_paired_scope_scan(self):
        store = self.Store()
        runtime = self.runtime(store)
        runtime.config.network_monitor_enabled = True
        completed = runtime._network_monitor_once()
        self.assertEqual(completed, 1)
        self.assertEqual(
            store.scan_calls,
            [{
                "max_hosts": 512,
                "include_offline": True,
                "scope_id": "scope-home",
                "include_identifiers": True,
            }],
        )
        self.assertIsNotNone(runtime._network_monitor_last_check_at)
        self.assertIsNone(runtime._network_monitor_last_error)

    def test_network_monitor_obeys_pause_and_emergency_stop(self):
        store = self.Store()
        runtime = self.runtime(store)
        runtime.config.network_monitor_enabled = True
        for state in ("paused", "stopped"):
            with self.subTest(state=state):
                runtime._background_control_state_override = state
                self.assertEqual(runtime._network_monitor_once(), 0)
                self.assertIn(state, runtime._network_monitor_last_error)
        self.assertEqual(store.scan_calls, [])
        with self.assertRaisesRegex(PermissionError, "emergency stop"):
            runtime.scan_network_inventory(scope_id="scope-home", max_hosts=64)

    def test_new_device_scan_emits_bounded_evidence_popup_payload(self):
        store = self.Store()
        store.scan = lambda **_kwargs: {
            "scan_id": 42,
            "scope_id": "scope-home",
            "scope_name": "Home",
            "observed_at": "2026-08-28T12:00:00+00:00",
            "devices": [{
                "device_id": "a" * 32,
                "display_name": "Observed device aaaaaa",
                "hostname": "phone.local",
                "ipv4": "192.168.50.55",
                "mac": "10:20:30:40:50:95",
                "identity_confidence": "moderate",
                "is_new": True,
            }],
            "security_summary": {"baseline_created": False},
            "security_assessment": {
                "assessment_id": "3" * 32,
                "posture": "review_required",
                "signals": [{
                    "device_id": "a" * 32,
                    "rule_id": "new_unreviewed_device",
                    "severity": "medium",
                    "summary": "A new device needs review.",
                    "recommended_action": "Identify it before changing access.",
                    "compromise_established": False,
                }],
                "automatic_containment": {"enabled": False, "actions_taken": 0},
            },
        }
        runtime = self.runtime(store)
        emitted = []
        runtime.emit = lambda kind, **payload: emitted.append((kind, payload))
        runtime.scan_network_inventory(scope_id="scope-home", max_hosts=64)
        self.assertEqual(len(emitted), 2)
        kind, payload = emitted[0]
        self.assertEqual(kind, "network_inventory_updated")
        self.assertFalse(payload["baseline_created"])
        self.assertEqual(payload["scan_id"], 42)
        self.assertEqual(payload["observed_at"], "2026-08-28T12:00:00+00:00")
        self.assertEqual(payload["new_devices"][0]["hostname"], "phone.local")
        self.assertFalse(
            payload["security_assessment"]["automatic_containment"]["enabled"]
        )
        self.assertEqual(payload["pending_incidents"]["pending_count"], 1)
        self.assertEqual(emitted[1][0], "network_defense_incident")
        incident = emitted[1][1]["incident"]
        self.assertFalse(incident["compromise_established"])
        self.assertEqual(incident["automatic_actions"], [])
        self.assertNotIn("hostname", json.dumps(incident).casefold())

    def test_safe_readonly_defense_runs_only_passive_receipted_snapshot(self):
        store = self.Store()
        store.scan = lambda **_kwargs: {
            "scan_id": 43,
            "scope_id": "scope-home",
            "scope_name": "Home",
            "observed_at": "2026-08-28T12:01:00+00:00",
            "devices": [],
            "security_summary": {"baseline_created": False},
            "security_assessment": {"assessment_id": "3" * 32},
        }

        class PassiveRegistry:
            manifests = (SimpleNamespace(
                tool_id="netstat-flow",
                display_name="Local connection-table inspection",
            ),)

            def __init__(self):
                self.calls = []

            def run_passive_snapshot(self, **kwargs):
                self.calls.append(kwargs)
                return {
                    "results": [{
                        "tool_id": "netstat-flow",
                        "operation_id": "list-connections",
                        "tier": "passive_read_only",
                        "receipt_id": "9" * 32,
                        "status": "completed",
                    }],
                    "active_probes_executed": 0,
                    "containment_actions_executed": 0,
                    "raw_output_returned": False,
                    "receipts_verified": True,
                    "receipt_scope": "batch_passive_local_metadata",
                }

        registry = PassiveRegistry()
        runtime = self.runtime(store)
        runtime.config.network_defense_mode = "safe-readonly"
        emitted = []
        runtime.emit = lambda kind, **payload: emitted.append((kind, payload))
        with patch.object(runtime, "_network_defense_registry", return_value=registry):
            runtime.scan_network_inventory(scope_id="scope-home", max_hosts=64)
        self.assertEqual(len(registry.calls), 1)
        self.assertEqual(registry.calls[0]["max_steps"], 3)
        self.assertNotIn("include_active", registry.calls[0])
        self.assertEqual(len(store.incident_actions), 1)
        attached = store.incident_actions[0][1][0]
        self.assertEqual(attached["receipt_id"], "9" * 32)
        self.assertTrue(attached["title"].startswith("Batch passive snapshot"))
        self.assertNotIn("stdout", json.dumps(attached).casefold())
        popup = next(
            payload["incident"]
            for kind, payload in emitted
            if kind == "network_defense_incident"
        )
        self.assertEqual(popup["automatic_actions"], store.incident_actions[0][1])

    def test_popup_preference_hides_events_without_deleting_incidents(self):
        store = self.Store()
        store.scan = lambda **_kwargs: {
            "scan_id": 45,
            "scope_id": "scope-home",
            "scope_name": "Home",
            "observed_at": "2026-08-28T12:03:00+00:00",
            "devices": [],
            "security_summary": {"baseline_created": False},
            "security_assessment": {"assessment_id": "3" * 32},
        }
        runtime = self.runtime(store)
        runtime.config.network_incident_popups_enabled = False
        emitted = []
        runtime.emit = lambda kind, **payload: emitted.append((kind, payload))

        status = runtime.scan_network_inventory(
            scope_id="scope-home", max_hosts=64
        )

        self.assertEqual([kind for kind, _payload in emitted], [
            "network_inventory_updated",
        ])
        self.assertEqual(status["pending_incidents"]["pending_count"], 0)
        self.assertEqual(
            emitted[0][1]["pending_incidents"]["pending_count"], 1
        )

    def test_disabled_defense_mode_emits_no_incident_popup(self):
        store = self.Store()
        store.scan = lambda **_kwargs: {
            "scan_id": 44,
            "scope_id": "scope-home",
            "scope_name": "Home",
            "observed_at": "2026-08-28T12:02:00+00:00",
            "devices": [],
            "security_summary": {"baseline_created": False},
            "security_assessment": {"assessment_id": "3" * 32},
        }
        runtime = self.runtime(store)
        runtime.config.network_defense_mode = "disabled"
        emitted = []
        runtime.emit = lambda kind, **payload: emitted.append((kind, payload))
        status = runtime.scan_network_inventory(
            scope_id="scope-home", max_hosts=64
        )
        self.assertEqual([kind for kind, _payload in emitted], [
            "network_inventory_updated"
        ])
        self.assertEqual(status["pending_incidents"]["pending_count"], 0)
        self.assertTrue(status["pending_incidents"]["disabled"])

    def test_network_profile_changes_respect_global_readonly_mode(self):
        store = self.Store()
        runtime = self.runtime(store)
        runtime.config.autonomy = "readonly"
        with self.assertRaisesRegex(PermissionError, "readonly mode"):
            runtime.set_network_device_profile(
                device_id="device-1",
                label="Laptop",
                trust_state="recognized",
                device_type="computer",
            )

    def test_network_payload_is_bounded_and_redacted(self):
        secret = "sk-proj-" + "A" * 40
        payload = safe_presence_network_payload({
            "hostname": "<img onerror=alert(1)>",
            "secret": secret,
            "extra": ["x"] * 5_000,
        })
        self.assertEqual(payload["hostname"], "<img onerror=alert(1)>")
        self.assertNotIn(secret, json.dumps(payload))
        self.assertEqual(len(payload["extra"]), 4_096)


class PresenceBluetoothRuntimeTests(unittest.TestCase):
    class Store:
        def __init__(self):
            self.status_calls = []
            self.check_calls = []
            self.profile_calls = []
            self.pending_calls = []
            self.acknowledgements = []

        @staticmethod
        def snapshot():
            return {
                "last_check_id": 17,
                "last_check_at": "2026-08-28T12:00:00+00:00",
                "paired_in_last_check": 1,
                "known_endpoints": 1,
                "new_endpoints": 1,
                "baseline_created": False,
                "nearby_rf_scan_performed": False,
                "pairing_or_control_performed": False,
                "addresses_exposed": False,
                "devices": [{
                    "device_id": "c" * 64,
                    "display_name": "Wireless keyboard",
                    "trust_state": "unreviewed",
                    "transports": ["classic"],
                    "is_new": True,
                    "first_seen": "2026-08-28T11:59:00+00:00",
                    "last_observed_at": "2026-08-28T12:00:00+00:00",
                    "connected_evidence_available": False,
                }],
                "security_assessment": {
                    "posture": "review_required",
                    "signals": [{
                        "device_id": "c" * 64,
                        "rule_id": "new_unreviewed_paired_endpoint",
                        "severity": "informational",
                        "summary": "A paired endpoint is new to Jarvis.",
                        "recommended_action": "Identify it before changing trust.",
                        "compromise_established": False,
                    }],
                    "compromise_established": False,
                    "automatic_containment": {"enabled": False, "actions_taken": 0},
                },
            }

        def status(self, *, include_os_metadata=False):
            self.status_calls.append(include_os_metadata)
            return self.snapshot()

        def check(self, *, include_os_metadata=False):
            self.check_calls.append(include_os_metadata)
            return self.snapshot()

        def pending_alerts(self, *, limit=50):
            self.pending_calls.append(limit)
            return {
                "pending_count": 1,
                "alerts": [{
                    "event_id": 17,
                    "receipt_id": "e" * 32,
                    "device_id": "c" * 64,
                    "display_name": "Wireless keyboard",
                    "observed_at": "2026-08-28T12:00:00+00:00",
                }],
                "addresses_exposed": False,
            }

        def acknowledge_alert(self, *, event_id, receipt_id):
            self.acknowledgements.append((event_id, receipt_id))
            return {
                "event_id": event_id,
                "receipt_id": receipt_id,
                "state": "acknowledged",
                "changed": True,
            }

        def set_profile(self, device_id, *, label, trust_state, device_type):
            self.profile_calls.append((device_id, label, trust_state, device_type))
            return {
                "device_id": device_id,
                "label": label,
                "trust_state": trust_state,
                "device_type": device_type,
            }

        def device_detail(self, device_id, *, event_limit, include_os_metadata=False):
            return {
                "device": {"device_id": device_id},
                "events": [],
                "event_limit": event_limit,
                "addresses_exposed": False,
            }

    @staticmethod
    def runtime(store=None, *, enabled=True):
        runtime = PresenceRuntime.__new__(PresenceRuntime)
        runtime.config = SimpleNamespace(
            bluetooth_access="paired-readonly" if enabled else "disabled",
            bluetooth_monitor_enabled=False,
            bluetooth_monitor_interval_seconds=60,
            autonomy="autonomous",
        )
        runtime._bluetooth_inventory = store
        runtime._bluetooth_inventory_error = None
        runtime._bluetooth_check_lock = threading.Lock()
        runtime._bluetooth_monitor_thread = None
        runtime._bluetooth_monitor_last_check_at = None
        runtime._bluetooth_monitor_last_error = None
        runtime._background_control_state_override = "running"
        runtime._shutdown = threading.Event()
        runtime.emit = lambda *_args, **_kwargs: None
        return runtime

    def test_status_is_read_only_and_disabled_mode_is_inert(self):
        store = self.Store()
        status = self.runtime(store).bluetooth_inventory_status()
        self.assertEqual(store.status_calls, [True])
        self.assertEqual(store.pending_calls, [50])
        self.assertEqual(store.check_calls, [])
        self.assertFalse(status["addresses_exposed"])
        self.assertFalse(status["nearby_rf_scan_performed"])
        disabled = self.runtime(store, enabled=False).bluetooth_inventory_status()
        self.assertFalse(disabled["enabled"])
        self.assertEqual(store.status_calls, [True])

    def test_check_emits_bounded_first_observed_event_without_addresses(self):
        store = self.Store()
        runtime = self.runtime(store)
        emitted = []
        runtime.emit = lambda kind, **payload: emitted.append((kind, payload))
        status = runtime.check_bluetooth_inventory()
        self.assertEqual(store.check_calls, [True])
        self.assertEqual(emitted[0][0], "bluetooth_inventory_updated")
        payload = emitted[0][1]
        self.assertEqual(payload["check_id"], 17)
        self.assertEqual(payload["new_devices"][0]["device_id"], "c" * 64)
        self.assertFalse(payload["security_assessment"]["compromise_established"])
        rendered = json.dumps({"event": payload, "status": status}).casefold()
        self.assertNotIn("bluetoothaddress", rendered)
        self.assertNotIn("aa:bb:cc:dd:ee:ff", rendered)

    def test_monitor_reuses_same_paired_readonly_check(self):
        store = self.Store()
        runtime = self.runtime(store)
        runtime.config.bluetooth_monitor_enabled = True
        self.assertTrue(runtime._bluetooth_monitor_once())
        self.assertEqual(store.check_calls, [True])
        self.assertIsNotNone(runtime._bluetooth_monitor_last_check_at)
        self.assertIsNone(runtime._bluetooth_monitor_last_error)

    def test_bluetooth_monitor_obeys_pause_and_emergency_stop(self):
        store = self.Store()
        runtime = self.runtime(store)
        for state in ("paused", "stopped"):
            with self.subTest(state=state):
                runtime._background_control_state_override = state
                self.assertFalse(runtime._bluetooth_monitor_once())
                self.assertIn(state, runtime._bluetooth_monitor_last_error)
        self.assertEqual(store.check_calls, [])

    def test_profile_changes_respect_global_readonly_mode(self):
        store = self.Store()
        runtime = self.runtime(store)
        runtime.config.autonomy = "readonly"
        with self.assertRaisesRegex(PermissionError, "readonly mode"):
            runtime.set_bluetooth_device_profile(
                device_id="c" * 64,
                label="Keyboard",
                trust_state="recognized",
                device_type="keyboard",
            )
        self.assertEqual(store.profile_calls, [])
        runtime.config.autonomy = "autonomous"
        result = runtime.set_bluetooth_device_profile(
            device_id="c" * 64,
            label="Keyboard",
            trust_state="recognized",
            device_type="keyboard",
        )
        self.assertEqual(result["device"]["label"], "Keyboard")

    def test_durable_alert_acknowledgement_uses_exact_receipt(self):
        store = self.Store()
        runtime = self.runtime(store)
        result = runtime.acknowledge_bluetooth_alert(
            event_id=17,
            receipt_id="e" * 32,
        )
        self.assertTrue(result["changed"])
        self.assertEqual(store.acknowledgements, [(17, "e" * 32)])


class PresenceHTTPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = _FakeRuntime()
        self.server = PresenceHTTPServer(("127.0.0.1", 0), self.runtime)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, path, *, payload=None, headers=None, method=None):
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request_headers = dict(headers or {})
        if body is not None:
            request_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base + path,
            data=body,
            headers=request_headers,
            method=method or ("POST" if body is not None else "GET"),
        )
        return urllib.request.urlopen(request, timeout=2)

    def test_health_assets_and_security_headers(self):
        with self.request("/api/health") as response:
            payload = json.load(response)
            self.assertTrue(payload["ready"])
            self.assertEqual(payload["service"], "jarvis-presence")
            self.assertEqual(payload["runtime_epoch"], self.runtime.runtime_epoch)
            self.assertEqual(response.headers["X-Frame-Options"], "DENY")
            self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])
        with self.request("/") as response:
            page = response.read().decode("utf-8")
            self.assertIn("JARVIS Presence", page)
            self.assertIn("/presence.js", page)
            self.assertIn('id="agent-tabs"', page)
            self.assertIn('id="agent-detail"', page)
            self.assertIn('id="home-mode"', page)
            self.assertIn('data-view="projects"', page)
            self.assertIn('data-view="artifacts"', page)
            self.assertIn('data-view="scheduled"', page)
            self.assertIn('data-view="dispatch"', page)
            self.assertIn('data-view="devices"', page)
            self.assertIn('data-view="companion"', page)
            self.assertIn('data-view="customize"', page)
            self.assertIn('id="pinned-projects"', page)
            self.assertIn('id="utility-view"', page)
            self.assertIn('id="project-dialog"', page)
            self.assertIn('id="project-name"', page)
            self.assertIn('id="project-kind"', page)
            self.assertIn('id="project-description"', page)
            self.assertIn('id="project-context"', page)
            self.assertIn('id="delete-chat-dialog"', page)
            self.assertIn('id="confirm-delete-chat"', page)
            self.assertIn('id="attach-image"', page)
            self.assertIn('id="image-input"', page)
            self.assertIn('id="split-view"', page)
            self.assertIn('id="secondary-pane"', page)
            self.assertIn('id="secondary-conversation"', page)
            self.assertIn('id="secondary-composer"', page)
            self.assertIn('id="secondary-stop"', page)
            self.assertIn('accept="image/png,image/jpeg,image/webp,image/gif"', page)
            self.assertIn("AGENTS", page)
            self.assertIn("Public Presence", page)
        with self.request("/presence.js") as response:
            script = response.read().decode("utf-8")
            self.assertIn("function showProgress", script)
            self.assertIn("function appendAssistantDelta", script)
            self.assertIn("function finalizeAssistantStream", script)
            self.assertIn("function addImageFiles", script)
            self.assertIn("function renderMessageContent", script)
            self.assertIn("function renderLinkedText", script)
            self.assertIn("function safeHttpUrl", script)
            self.assertIn("function renderProductComparison", script)
            self.assertIn('link.rel = "noopener noreferrer"', script)
            self.assertIn("document.createTextNode", script)
            self.assertNotIn("innerHTML", script)
            self.assertIn("product_comparison", script)
            self.assertIn("No verified image", script)
            self.assertIn("staleObservation", script)
            self.assertIn("jarvis-image:", script)
            self.assertIn("/api/artifacts/image", script)
            self.assertIn("images: sentImages.map", script)
            self.assertIn('event.kind === "assistant_delta"', script)
            self.assertIn("currentJobId() ? 150 : 700", script)
            self.assertIn("Live activity", script)
            self.assertIn("function renderAgentTabs", script)
            self.assertIn("function messageTarget", script)
            self.assertIn("function loadSecondaryConversation", script)
            self.assertIn("function newSecondaryConversation", script)
            self.assertIn("function setSplitView", script)
            self.assertIn("function submitSecondaryPrompt", script)
            self.assertIn("function cancelSecondaryActive", script)
            self.assertIn("conversation_id: state.secondaryConversationId", script)
            self.assertIn('localStorage.setItem("jarvis.presence.split-view"', script)
            self.assertIn("eventTarget", script)
            self.assertIn("active_task_prompt", script)
            self.assertIn("specialist.participating", script)
            self.assertIn("Reported for this request", script)
            self.assertIn("Last task (", script)
            self.assertIn("Specialist agents", script)
            self.assertIn("function adoptRuntimeEpoch", script)
            self.assertIn("function reconcileRuntimeState", script)
            self.assertIn("function isHistoricalConversationEvent", script)
            self.assertIn("if (isHistoricalConversationEvent(event)) continue", script)
            self.assertIn("createdAt < state.pageStartedAt - 2", script)
            self.assertIn("function providerLabel", script)
            self.assertIn("OpenAI configured · unverified", script)
            self.assertIn("OpenAI circuit open", script)
            self.assertIn("state.activeJobs = new Map", script)
            global_terminal_clear = script.index(
                "state.activeJobs.delete(payload.conversation_id);"
            )
            selected_conversation_render = script.index(
                'if (event.kind === "assistant" && belongsHere)'
            )
            self.assertLess(global_terminal_clear, selected_conversation_render)
            self.assertIn("function openUtility", script)
            self.assertIn("function renderPinnedProjects", script)
            self.assertIn("openProjectInChat(projectId).catch(showError)", script)
            self.assertIn("function renderArtifacts", script)
            self.assertIn("function renderSchedule", script)
            self.assertIn("function renderNetworkInventory", script)
            self.assertIn("function paintNetworkInventory", script)
            self.assertIn("function renderNetworkDefense", script)
            self.assertIn("function showNewNetworkDeviceAlerts", script)
            self.assertIn("jarvis.network.first-observed-alerts.v1", script)
            self.assertIn("event.created_at", script)
            self.assertIn("First observed by Jarvis", script)
            self.assertIn("Reachable in last check", script)
            self.assertIn("Limited observation", script)
            self.assertIn("Evidence freshness:", script)
            self.assertIn("function scanNetworkInventory", script)
            self.assertIn("function openNetworkDevice", script)
            self.assertIn("/api/network-inventory", script)
            self.assertIn("I own or administer this network", script)
            self.assertIn("Opening this page never scans", script)
            self.assertIn("Cached evidence only", script)
            self.assertIn("Not observed in the last check", script)
            self.assertIn("No automatic blocking is performed", script)
            self.assertIn("Automatic action: None", script)
            self.assertIn("Compromise is not established", script)
            self.assertIn('id="new-network-device-dialog"', page)
            self.assertIn("Device first observed by Jarvis", page)
            self.assertIn("function renderBluetoothInventorySection", script)
            self.assertIn("function showNewBluetoothDeviceAlerts", script)
            self.assertIn("function acknowledgeVisibleBluetoothAlerts", script)
            self.assertIn("jarvis.bluetooth.first-observed-alerts.v1", script)
            self.assertIn('event.kind === "bluetooth_inventory_updated"', script)
            self.assertIn("/api/bluetooth-inventory", script)
            self.assertIn("/api/bluetooth-inventory/alerts/acknowledge", script)
            self.assertIn("never scans nearby unpaired radios", script)
            self.assertIn("Connection evidence unavailable", script)
            self.assertIn('id="new-bluetooth-device-dialog"', page)
            self.assertIn("Bluetooth device first observed by Jarvis", page)
            self.assertIn("function renderCompanion", script)
            self.assertIn("/api/screen-companion", script)
            self.assertIn("function renderPublicPresence", script)
            self.assertIn("/api/public-presence", script)
            self.assertIn("No public listener, account connection, social API, or publishing method", script)
            self.assertIn("Approve always", script)
            self.assertIn("Approve for this session", script)
            self.assertIn("function decideApprovalAlways", script)
            self.assertIn("function decideApprovalForSession", script)
            self.assertIn("function revokeApprovalGrant", script)
            self.assertIn("function submitProject", script)
            self.assertIn("function requestConversationDelete", script)
            self.assertIn("function confirmConversationDelete", script)
            self.assertIn("function updateProjectChrome", script)
            self.assertIn('method: "DELETE"', script)
            self.assertIn("row.project_id === state.projectId", script)
            self.assertNotIn("window.prompt", script)
        with self.request("/presence.css") as response:
            stylesheet = response.read().decode("utf-8")
            self.assertIn(".message.streaming .content::after", stylesheet)
            self.assertIn("@keyframes stream-caret", stylesheet)
            self.assertIn(".product-grid", stylesheet)
            self.assertIn("repeat(auto-fit, minmax(230px, 1fr))", stylesheet)
            self.assertIn(".product-image-placeholder", stylesheet)
            self.assertIn(".product-link", stylesheet)
            self.assertIn(".network-device-grid", stylesheet)
            self.assertIn(".network-attestation", stylesheet)
            self.assertIn(".network-detail-facts", stylesheet)
            self.assertIn(".network-defense-signal", stylesheet)
            self.assertIn(".new-network-device-card", stylesheet)
            self.assertIn(".secondary-pane", stylesheet)
            self.assertIn("body.split-view .workspace", stylesheet)
            self.assertIn("repeat(2, minmax(0, 1fr))", stylesheet)
            self.assertIn(".secondary-messages", stylesheet)
            self.assertIn(".conversation-delete", stylesheet)
            self.assertIn(".project-overview", stylesheet)
            self.assertIn(".project-folder-list", stylesheet)
            for selector, row in (
                (".topbar", 1),
                (".agent-tabs", 2),
                (".agent-detail", 3),
                (".messages", 4),
                (".composer-area", 5),
            ):
                with self.subTest(selector=selector):
                    rule = stylesheet.split(selector + " {", 1)[1].split("}", 1)[0]
                    self.assertIn(f"grid-row: {row};", rule)
            self.assertIn("overflow: hidden;", stylesheet.split(".workspace {", 1)[1].split("}", 1)[0])

    def test_conversation_and_chat_routes(self):
        with self.request("/api/projects") as response:
            self.assertEqual(json.load(response)["projects"][0]["id"], 1)
        with self.request("/api/artifacts?project_id=1") as response:
            payload = json.load(response)
            self.assertEqual(payload["project_id"], 1)
            self.assertEqual(payload["artifacts"][0]["relative_path"], "reports/report.md")
        with self.request(
            "/api/artifacts/image?project_id=1&path=images%2Fresult.png"
        ) as response:
            self.assertEqual(response.headers["Content-Type"], "image/png")
            self.assertTrue(response.read().startswith(b"\x89PNG\r\n\x1a\n"))
        with self.request("/api/schedule") as response:
            self.assertEqual(json.load(response)["tasks"][0]["status"], "queued")
        with self.request(
            "/api/projects",
            payload={"name": "Test", "kind": "research", "description": "Sources"},
        ) as response:
            self.assertEqual(response.status, 201)
            project = json.load(response)["project"]
            self.assertEqual(project["id"], 2)
            self.assertEqual(project["kind"], "research")
        with self.request("/api/conversations", payload={"title": "Test"}) as response:
            self.assertEqual(response.status, 201)
            self.assertEqual(json.load(response)["conversation_id"], 7)
        with self.request(
            "/api/chat",
            payload={"conversation_id": 7, "prompt": "hello", "model": "fast"},
        ) as response:
            self.assertEqual(response.status, 202)
            self.assertEqual(json.load(response)["job_id"], "a" * 32)
        self.assertEqual(self.runtime.submitted, (7, "hello", "fast", None))

        encoded = "iVBORw0KGgo="
        with self.request(
            "/api/chat",
            payload={
                "conversation_id": 7,
                "prompt": "what is in this image?",
                "model": "auto",
                "images": [{"name": "screen.png", "mime": "image/png", "data": encoded}],
            },
        ) as response:
            self.assertEqual(response.status, 202)
        self.assertEqual(self.runtime.submitted[0:3], (7, "what is in this image?", "auto"))
        self.assertEqual(self.runtime.submitted[3][0]["data"], encoded)

        with self.request(
            "/api/conversations/7", method="DELETE"
        ) as response:
            deleted = json.load(response)
        self.assertTrue(deleted["deleted"])
        self.assertEqual(deleted["conversation_id"], 7)
        self.assertEqual(self.runtime.deleted_conversations, [7])

    def test_network_inventory_routes_are_explicit_bounded_and_attested(self):
        with self.request("/api/network-inventory") as response:
            status = json.load(response)
        self.assertTrue(status["enabled"])
        self.assertEqual(status["inventory"]["devices"][0]["device_id"], "device-1")
        self.assertEqual(self.runtime.network_status_reads, 1)
        self.assertEqual(self.runtime.network_scan_calls, [])

        with self.request(
            "/api/network-inventory/device?device_id=device-1&event_limit=25"
        ) as response:
            detail = json.load(response)
        self.assertEqual(detail["device"]["device_id"], "device-1")
        self.assertEqual(detail["event_limit"], 25)

        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request(
                "/api/network-inventory/scopes/pair",
                payload={
                    "interface_index": 7,
                    "owns_or_administers": False,
                    "display_name": "Home",
                },
            )
        self.assertEqual(raised.exception.code, 403)
        self.assertEqual(self.runtime.network_pair_calls, [])

        with self.request(
            "/api/network-inventory/scopes/pair",
            payload={
                "interface_index": 7,
                "owns_or_administers": True,
                "display_name": "Home",
            },
        ) as response:
            self.assertEqual(response.status, 201)
        self.assertEqual(self.runtime.network_pair_calls, [(7, True, "Home")])

        with self.request(
            "/api/network-inventory/scan",
            payload={"scope_id": "scope-home", "max_hosts": 128},
        ) as response:
            self.assertTrue(json.load(response)["status"]["enabled"])
        self.assertEqual(self.runtime.network_scan_calls, [("scope-home", 128)])

        with self.request(
            "/api/network-inventory/devices/profile",
            payload={
                "device_id": "device-1",
                "label": "Living room TV",
                "trust_state": "recognized",
                "device_type": "television",
            },
        ) as response:
            self.assertEqual(json.load(response)["device"]["label"], "Living room TV")
        self.assertEqual(
            self.runtime.network_profile_calls,
            [("device-1", "Living room TV", "recognized", "television")],
        )

        with self.request(
            "/api/network-defense/incidents/acknowledge",
            payload={"incident_id": "1" * 32, "receipt_id": "2" * 32},
        ) as response:
            acknowledged = json.load(response)["incident"]
        self.assertTrue(acknowledged["changed"])
        self.assertEqual(
            self.runtime.network_incident_acknowledgements,
            [("1" * 32, "2" * 32)],
        )

        with self.request(
            "/api/network-inventory/scopes/unpair",
            payload={"scope_id": "scope-home"},
        ) as response:
            self.assertTrue(json.load(response)["status"]["enabled"])
        self.assertEqual(self.runtime.network_unpair_calls, ["scope-home"])

    def test_network_inventory_route_validation_and_safe_ui_boundary(self):
        for payload in (
            {"scope_id": "scope-home", "max_hosts": True},
            {"scope_id": "scope-home", "max_hosts": 0},
            {"scope_id": "scope-home", "max_hosts": 513},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    self.request("/api/network-inventory/scan", payload=payload)
                self.assertEqual(raised.exception.code, 400)
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request(
                "/api/network-inventory/scopes/pair",
                payload={"interface_index": 7, "owns_or_administers": "yes"},
            )
        self.assertEqual(raised.exception.code, 400)
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request(
                "/api/network-inventory/device?device_id=device-1&event_limit=101"
            )
        self.assertEqual(raised.exception.code, 400)
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request(
                "/api/network-defense/incidents/acknowledge",
                payload={"incident_id": {"unsafe": True}, "receipt_id": "2" * 32},
            )
        self.assertEqual(raised.exception.code, 400)
        self.assertEqual(self.runtime.network_scan_calls, [])

        with self.request("/api/network-inventory") as response:
            rendered = response.read().decode("utf-8")
        self.assertIn("<img src=x onerror=alert(1)>", rendered)
        with self.request("/presence.js") as response:
            script = response.read().decode("utf-8")
        self.assertIn("heading.textContent = networkDeviceName(device)", script)
        self.assertNotIn("innerHTML", script)

    def test_bluetooth_inventory_routes_are_readonly_bounded_and_safe(self):
        device_id = "b" * 64
        with self.request("/api/bluetooth-inventory") as response:
            status = json.load(response)
        self.assertTrue(status["enabled"])
        self.assertFalse(status["addresses_exposed"])
        self.assertEqual(self.runtime.bluetooth_status_reads, 1)
        self.assertEqual(self.runtime.bluetooth_check_calls, 0)

        with self.request(
            f"/api/bluetooth-inventory/device?device_id={device_id}&event_limit=25"
        ) as response:
            detail = json.load(response)
        self.assertEqual(detail["device"]["device_id"], device_id)
        self.assertEqual(detail["event_limit"], 25)

        with self.request("/api/bluetooth-inventory/check", payload={}) as response:
            checked = json.load(response)["status"]
        self.assertEqual(checked["last_check_id"], 9)
        self.assertEqual(self.runtime.bluetooth_check_calls, 1)

        with self.request(
            "/api/bluetooth-inventory/alerts/acknowledge",
            payload={"event_id": 91, "receipt_id": "d" * 32},
        ) as response:
            acknowledged = json.load(response)["alert"]
        self.assertTrue(acknowledged["changed"])
        self.assertEqual(
            self.runtime.bluetooth_acknowledgements,
            [(91, "d" * 32)],
        )

        with self.request(
            "/api/bluetooth-inventory/devices/profile",
            payload={
                "device_id": device_id,
                "label": "Office keyboard",
                "trust_state": "recognized",
                "device_type": "keyboard",
            },
        ) as response:
            profile = json.load(response)["device"]
        self.assertEqual(profile["label"], "Office keyboard")
        self.assertEqual(
            self.runtime.bluetooth_profile_calls,
            [(device_id, "Office keyboard", "recognized", "keyboard")],
        )

        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request(
                f"/api/bluetooth-inventory/device?device_id={device_id}&event_limit=101"
            )
        self.assertEqual(raised.exception.code, 400)
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request(
                "/api/bluetooth-inventory/alerts/acknowledge",
                payload={"event_id": True, "receipt_id": "d" * 32},
            )
        self.assertEqual(raised.exception.code, 400)
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request(
                "/api/bluetooth-inventory/devices/profile",
                payload={"device_id": device_id, "label": {"unsafe": True}},
            )
        self.assertEqual(raised.exception.code, 400)

        with self.request("/api/bluetooth-inventory") as response:
            rendered = response.read().decode("utf-8")
        self.assertIn("<img src=x onerror=alert(1)>", rendered)
        with self.request("/presence.js") as response:
            script = response.read().decode("utf-8")
        self.assertIn("title.textContent = bluetoothDeviceName(device)", script)
        self.assertNotIn("innerHTML", script)

    def test_network_scan_rate_limit_is_a_bounded_retry_response(self):
        def limited(**_kwargs):
            raise NetworkInventoryRateLimited(
                "Please wait briefly before checking this network again",
                retry_after_seconds=37,
            )

        self.runtime.scan_network_inventory = limited
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request(
                "/api/network-inventory/scan",
                payload={"scope_id": "scope-home", "max_hosts": 64},
            )
        self.assertEqual(raised.exception.code, 429)
        payload = json.loads(raised.exception.read().decode("utf-8"))
        self.assertEqual(payload["retry_after_seconds"], 37)
        self.assertIn("wait briefly", payload["error"])

    def test_screen_companion_routes(self):
        with self.request("/api/screen-companion") as response:
            self.assertEqual(json.load(response)["mode"], "observe")
        with self.request("/api/screen-companion/indicator") as response:
            indicator = json.load(response)
            self.assertEqual(indicator["mode"], "observe")
            self.assertEqual(
                indicator["suggestion"]["text"],
                "Want me to tighten the outline into three clear sections?",
            )
            self.assertNotIn("current", indicator)
            self.assertNotIn("rules", indicator)
        with self.request(
            "/api/screen-companion/suggestions/" + "c" * 32 + "/accept",
            payload={},
        ) as response:
            accepted = json.load(response)
            self.assertTrue(accepted["accepted"])
            self.assertEqual(accepted["job_id"], "d" * 32)
        with self.request(
            "/api/screen-companion/actions/" + "d" * 32
        ) as response:
            action = json.load(response)["action"]
            self.assertEqual(action["state"], "completed")
            self.assertTrue(action["terminal"])
        with self.assertRaises(urllib.error.HTTPError) as missing:
            self.request("/api/screen-companion/actions/" + "e" * 32)
        self.assertEqual(missing.exception.code, 404)
        self.assertEqual(
            self.runtime.companion_suggestion_decisions,
            [("c" * 32, True)],
        )
        with self.request(
            "/api/screen-companion/state",
            payload={
                "mode": "suggest",
                "paused": False,
                "auto_suggest": True,
                "excluded_apps": ["vault.exe"],
            },
        ) as response:
            self.assertEqual(json.load(response)["state"]["mode"], "suggest")
        with self.request("/api/screen-companion/suggest", payload={}) as response:
            self.assertEqual(response.status, 202)
            self.assertEqual(json.load(response)["job_id"], "b" * 32)
        with self.request(
            "/api/screen-companion/control", payload={"action": "pause"}
        ) as response:
            self.assertTrue(json.load(response)["state"]["paused"])
        self.assertEqual(self.runtime.companion_controls, [("pause", None)])
        with self.request("/api/screen-companion/forget", payload={}) as response:
            self.assertEqual(json.load(response)["forgotten_receipts"], 3)
        with self.request(
            "/api/screen-companion/rules",
            payload={"trigger_app": "chrome.exe", "action_prompt": "Help"},
        ) as response:
            self.assertEqual(response.status, 201)
            self.assertEqual(json.load(response)["rule_id"], 41)
        with self.request(
            "/api/screen-companion/rules/41/disable", payload={}
        ) as response:
            self.assertTrue(json.load(response)["changed"])
        self.assertEqual(self.runtime.companion_rule_state, (41, False))
        with self.request(
            "/api/screen-companion/rules/41/delete", payload={}
        ) as response:
            self.assertTrue(json.load(response)["changed"])
        self.assertEqual(self.runtime.deleted_companion_rule, 41)

    def test_public_presence_routes_are_status_and_control_only(self):
        with self.request("/api/public-presence") as response:
            status = json.load(response)
        self.assertFalse(status["configured_enabled"])
        self.assertFalse(status["publishing_available"])
        self.assertFalse(status["external_communication"])

        with self.request(
            "/api/public-presence/control", payload={"action": "emergency_stop"}
        ) as response:
            stopped = json.load(response)["status"]
        self.assertEqual(stopped["effective_state"], "emergency_stopped")
        with self.request(
            "/api/public-presence/control",
            payload={"action": "clear_emergency_stop"},
        ) as response:
            cleared = json.load(response)["status"]
        self.assertEqual(cleared["effective_state"], "disabled")
        self.assertFalse(cleared["control"]["enabled"])
        self.assertTrue(cleared["control"]["paused"])
        self.assertFalse(cleared["control"]["emergency_stopped"])
        self.assertEqual(
            self.runtime.public_presence_controls,
            ["emergency_stop", "clear_emergency_stop"],
        )

    def test_public_presence_invalid_control_is_bad_request(self):
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request(
                "/api/public-presence/control",
                payload={"action": "enable_and_publish"},
            )
        self.assertEqual(raised.exception.code, 400)
        self.assertEqual(self.runtime.public_presence_controls, [])

    def test_public_presence_resume_while_disabled_conflicts(self):
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request(
                "/api/public-presence/control", payload={"action": "resume"}
            )
        self.assertEqual(raised.exception.code, 409)
        self.assertEqual(self.runtime.public_presence_controls, [])

    def test_screen_companion_state_rejects_non_list_exclusions(self):
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request(
                "/api/screen-companion/state",
                payload={
                    "mode": "observe",
                    "paused": False,
                    "auto_suggest": False,
                    "excluded_apps": "vault.exe",
                },
            )
        self.assertEqual(raised.exception.code, 400)

    def test_screen_companion_state_rejects_non_boolean_switches(self):
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request(
                "/api/screen-companion/state",
                payload={
                    "mode": "observe",
                    "paused": "false",
                    "auto_suggest": False,
                    "excluded_apps": [],
                },
            )
        self.assertEqual(raised.exception.code, 400)

    def test_approval_always_and_revoke_routes(self):
        with self.request("/api/approvals") as response:
            payload = json.load(response)
        self.assertEqual(payload["approvals"], [])
        self.assertEqual(payload["persistent_approvals"], [])
        with self.request(
            "/api/approvals/7/approve-always", payload={}
        ) as response:
            self.assertEqual(json.load(response)["grant_id"], 12)
        with self.request(
            "/api/approvals/8/approve-session", payload={}
        ) as response:
            self.assertEqual(json.load(response)["grant_id"], 13)
        with self.request(
            "/api/approval-grants/12/revoke", payload={}
        ) as response:
            self.assertTrue(json.load(response)["changed"])
        self.assertEqual(self.runtime.always_decisions, [7])
        self.assertEqual(self.runtime.session_decisions, [8])
        self.assertEqual(self.runtime.revoked_grants, [12])

    def test_artifacts_require_a_bounded_positive_project_id(self):
        for value in ("", "0", "-1", "1.5", "99999999999999999999"):
            with self.subTest(value=value):
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    self.request(f"/api/artifacts?project_id={value}")
                self.assertEqual(raised.exception.code, 400)

    def test_event_epoch_mismatch_resets_stale_cursor(self):
        stale_epoch = "b" * 32
        with self.request(f"/api/events?after=999&epoch={stale_epoch}") as response:
            payload = json.load(response)
        self.assertEqual(payload["runtime_epoch"], self.runtime.runtime_epoch)
        self.assertTrue(payload["cursor_reset"])
        self.assertEqual(payload["latest_event_id"], 17)
        self.assertEqual(payload["events"][0]["id"], 1)
        self.assertEqual(self.runtime.last_event_after, 0)

        with self.request(
            f"/api/events?after=41&epoch={self.runtime.runtime_epoch}"
        ) as response:
            payload = json.load(response)
        self.assertFalse(payload["cursor_reset"])
        self.assertEqual(payload["events"][0]["id"], 42)
        self.assertEqual(self.runtime.last_event_after, 41)

    def test_cross_origin_mutation_is_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request(
                "/api/conversations",
                payload={"title": "blocked"},
                headers={"Origin": "https://evil.example"},
            )
        self.assertEqual(raised.exception.code, 403)

    def test_dns_rebinding_host_is_rejected_for_reads_and_mutations(self):
        headers = {"Host": "attacker.invalid:8787"}
        with self.assertRaises(urllib.error.HTTPError) as read_error:
            self.request("/api/conversations", headers=headers)
        self.assertEqual(read_error.exception.code, 403)

        with self.assertRaises(urllib.error.HTTPError) as write_error:
            self.request(
                "/api/conversations",
                payload={"title": "blocked"},
                headers={
                    **headers,
                    "Origin": "http://attacker.invalid:8787",
                },
            )
        self.assertEqual(write_error.exception.code, 403)

    def test_same_hostname_on_a_different_origin_port_is_rejected(self):
        host = f"127.0.0.1:{self.server.server_port}"
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request(
                "/api/conversations",
                payload={"title": "blocked"},
                headers={"Host": host, "Origin": "http://127.0.0.1:65534"},
            )
        self.assertEqual(raised.exception.code, 403)

    def test_non_json_mutation_is_rejected(self):
        request = urllib.request.Request(
            self.base + "/api/conversations",
            data=b"title=blocked",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=2)
        self.assertEqual(raised.exception.code, 400)

    def test_remote_host_requires_pairing_and_accepts_one_live_session(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp = tempfile.TemporaryDirectory()
        data = Path(self.temp.name)
        self.runtime.config = SimpleNamespace(data_dir=data)
        with Memory(data / "jarvis.db") as memory:
            pairing = memory.create_presence_pairing_code("browser")
        self.server = PresenceHTTPServer(
            ("127.0.0.1", 0),
            self.runtime,
            trusted_hosts=("jarvis.example",),
            remote_access="paired",
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        remote = {"Host": "jarvis.example"}
        with self.request("/", headers=remote) as response:
            self.assertEqual(response.status, 200)
        with self.assertRaises(urllib.error.HTTPError) as missing:
            self.request("/api/status", headers=remote)
        self.assertEqual(missing.exception.code, 401)
        with self.request(
            "/api/pair",
            payload={"code": pairing["code"]},
            headers={**remote, "Origin": "https://jarvis.example"},
        ) as response:
            self.assertEqual(response.status, 201)
            session = json.load(response)
        with self.request(
            "/api/status",
            headers={**remote, "Authorization": f"Bearer {session['token']}"},
        ) as response:
            self.assertTrue(json.load(response)["ready"])
        with Memory(data / "jarvis.db") as memory:
            memory.revoke_presence_session(session["session_id"])
        with self.assertRaises(urllib.error.HTTPError) as revoked:
            self.request(
                "/api/status",
                headers={**remote, "Authorization": f"Bearer {session['token']}"},
            )
        self.assertEqual(revoked.exception.code, 401)
        self.temp.cleanup()

    def test_remote_host_is_rejected_while_remote_access_is_disabled(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.server = PresenceHTTPServer(
            ("127.0.0.1", 0), self.runtime,
            trusted_hosts=("jarvis.example",), remote_access="disabled",
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        with self.assertRaises(urllib.error.HTTPError) as blocked:
            self.request("/", headers={"Host": "jarvis.example"})
        self.assertEqual(blocked.exception.code, 403)


if __name__ == "__main__":
    unittest.main()
