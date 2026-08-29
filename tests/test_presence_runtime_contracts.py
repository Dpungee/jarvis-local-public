from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from jarvis.bluetooth_inventory import BluetoothInventory
from jarvis.config import Config
from jarvis.memory import Memory
from jarvis.network_inventory import NetworkInventory
from jarvis.presence import PresenceRuntime


def _network_snapshot() -> dict[str, object]:
    return {
        "interfaces": [{
            "interface_index": 7,
            "interface_alias": "Ethernet",
            "interface_guid": "test-private-interface",
            "interface_description": "Test adapter",
            "address": "192.168.50.2",
            "prefix_length": 24,
            "gateway": "192.168.50.1",
            "gateway_mac": "00-11-22-33-44-55",
            "mac": "10-20-30-40-50-60",
            "hardware_interface": True,
            "network_category": "Private",
            "profile_name": "Test home LAN",
        }],
        "observations": [{
            "ipv4": "192.168.50.20",
            "mac": "AA-BB-CC-DD-EE-01",
            "hostname": "test-tablet",
            "visibility": "active_probe",
            "neighbor_state": "Reachable",
            "actively_reachable": True,
            "cached": True,
        }],
        "candidate_hosts": 1,
        "responsive_hosts": 1,
        "range_truncated": False,
        "method": "deterministic test observation",
    }


def _bluetooth_snapshot() -> dict[str, object]:
    return {
        "provider": "windows_device_information",
        "observed_at": "2026-08-28T12:00:00+00:00",
        "devices": [{
            "raw_id": "Bluetooth#Deterministic_Test_Keyboard",
            "transport": "classic",
            "name": "Test Keyboard",
            "paired": True,
            "paired_evidence_available": True,
            "present": True,
            "present_evidence_available": True,
            "connected": True,
            "connected_evidence_available": True,
            "manufacturer": "Test Manufacturer",
            "model_name": "Test Model",
            "categories": ["keyboard"],
        }],
    }


class PresenceRuntimeContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.data = self.root / "data"
        self.workspace.mkdir()
        self.data.mkdir()
        self.config = Config(
            root=self.root,
            workspace=self.workspace,
            data_dir=self.data,
            soul_path=self.root / "SOUL.md",
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
            ollama_enabled=False,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_control_status_and_approval_contracts_survive_restart(self) -> None:
        runtime = PresenceRuntime(self.config)
        runtime.set_control("paused", "operator requested a pause")
        with self.assertRaisesRegex(ValueError, "running, paused, or stopped"):
            runtime.set_control("maybe")

        with Memory(self.data / "jarvis.db") as memory:
            allowed, approval_id = memory.authorize_or_request(
                "access_private_files",
                '{"arguments":{"path":"C:/bounded"},"tool":"computer_list_files"}',
                "This inspects the exact folder shown.",
                approval_scope="foreground",
            )
        self.assertFalse(allowed)
        status = runtime.status()
        self.assertTrue({
            "runtime_epoch", "ready", "control", "pending_approvals",
            "specialists", "models", "screen_companion", "public_presence",
            "fatal_error",
        }.issubset(status))
        self.assertEqual(status["control"]["state"], "paused")
        self.assertEqual(status["pending_approvals"], 1)

        approvals = runtime.approvals()
        self.assertEqual(len(approvals), 1)
        self.assertEqual(
            set(approvals[0]),
            {
                "id", "created_at", "updated_at", "action", "resource",
                "reason", "status", "expires_at", "decided_at", "task_id",
                "scope", "persistent_eligible",
            },
        )
        self.assertEqual(approvals[0]["id"], approval_id)
        self.assertEqual(approvals[0]["status"], "pending")
        self.assertFalse(runtime.decide_approval(9_223_372_036_854_775_807, False))
        self.assertTrue(runtime.decide_approval(approval_id, False))

        restarted = PresenceRuntime(self.config)
        restarted_status = restarted.status()
        self.assertEqual(restarted_status["control"]["state"], "paused")
        self.assertEqual(restarted_status["pending_approvals"], 0)
        self.assertEqual(restarted.approvals()[0]["status"], "denied")

    def test_projects_and_schedule_overview_are_durable_and_bounded(self) -> None:
        runtime = PresenceRuntime(self.config)
        project = runtime.create_project(
            "Contract Review",
            "research",
            "A durable route-facing project.",
        )
        with self.assertRaisesRegex(ValueError, "must not be empty"):
            runtime.create_project("   ")
        with self.assertRaisesRegex(ValueError, "Project type"):
            runtime.create_project("Invalid type", "unknown")

        with Memory(self.data / "jarvis.db") as memory:
            task_id = memory.add_task(
                "Prepare the next bounded review",
                project_id=project["id"],
            )
            topic_id = memory.add_learning_topic("Network defense evidence", 48)
            subject_id = memory.approve_subject("Local AI reliability")
            backlog_id = memory.add_backlog_item(
                "research",
                subject_id,
                "Produce a dated brief.",
                interval_hours=168,
            )

        restarted = PresenceRuntime(self.config)
        projects = restarted.projects()
        stored = next(row for row in projects if row["id"] == project["id"])
        self.assertTrue({
            "id", "name", "relative_path", "enabled", "conversation_count",
            "task_count", "kind", "description", "folders", "isolated",
        }.issubset(stored))
        self.assertEqual(stored["kind"], "research")
        self.assertEqual(stored["task_count"], 1)
        self.assertTrue(stored["isolated"])

        overview = restarted.schedule_overview()
        self.assertEqual(set(overview), {"tasks", "learning_topics", "backlog"})
        task = next(row for row in overview["tasks"] if row["id"] == task_id)
        topic = next(
            row for row in overview["learning_topics"] if row["id"] == topic_id
        )
        backlog = next(
            row for row in overview["backlog"] if row["id"] == backlog_id
        )
        self.assertEqual(
            set(task),
            {"id", "status", "prompt", "updated_at", "project_id", "specialist_key"},
        )
        self.assertEqual(
            set(topic),
            {"id", "topic", "interval_hours", "next_run", "enabled"},
        )
        self.assertEqual(
            set(backlog),
            {"id", "kind", "subject", "next_run", "enabled"},
        )
        self.assertEqual(task["project_id"], project["id"])
        self.assertEqual(topic["interval_hours"], 48)
        self.assertEqual(backlog["subject"], "Local AI reliability")

    def test_network_inventory_route_is_durable_and_fails_closed(self) -> None:
        disabled = PresenceRuntime(self.config)
        disabled_status = disabled.network_inventory_status()
        self.assertTrue({
            "enabled", "available", "can_scan", "scopes", "scope_candidates",
            "inventory", "security_assessment", "pending_incidents", "monitor",
            "scan_policy", "limitations", "error",
        }.issubset(disabled_status))
        self.assertFalse(disabled_status["enabled"])
        self.assertFalse(disabled_status["available"])
        with self.assertRaises(PermissionError):
            disabled.scan_network_inventory(scope_id=None)

        enabled_config = replace(self.config, network_access="private-lan")
        runtime = PresenceRuntime(enabled_config)
        runtime._network_inventory = NetworkInventory(
            self.data,
            discoverer=lambda _limit: _network_snapshot(),
            min_scan_interval_seconds=0,
            max_scans_per_hour=0,
        )
        with self.assertRaises(PermissionError):
            runtime.pair_network_scope(
                interface_index=7,
                owns_or_administers=False,
            )
        paired = runtime.pair_network_scope(
            interface_index=7,
            owns_or_administers=True,
            display_name="Test LAN",
        )
        scope_id = paired["scope"]["scope_id"]
        scanned = runtime.scan_network_inventory(scope_id=scope_id, max_hosts=8)
        self.assertTrue(scanned["enabled"])
        self.assertTrue(scanned["available"])
        self.assertEqual(scanned["inventory"]["known_devices"], 1)
        device_id = scanned["inventory"]["devices"][0]["device_id"]
        profiled = runtime.set_network_device_profile(
            device_id=device_id,
            label="Office tablet",
            trust_state="recognized",
            device_type="tablet",
        )
        self.assertEqual(profiled["device"]["label"], "Office tablet")
        with self.assertRaisesRegex(ValueError, "trust_state"):
            runtime.set_network_device_profile(
                device_id=device_id,
                label=None,
                trust_state="trusted-with-control",
                device_type=None,
            )

        restarted = PresenceRuntime(enabled_config)
        restarted._network_inventory = NetworkInventory(
            self.data,
            discoverer=lambda _limit: _network_snapshot(),
            min_scan_interval_seconds=0,
            max_scans_per_hour=0,
        )
        durable = restarted.network_device_detail(device_id)
        self.assertEqual(durable["device"]["label"], "Office tablet")
        self.assertEqual(durable["device"]["trust_state"], "recognized")
        self.assertEqual(restarted.network_inventory_status()["inventory"]["known_devices"], 1)

        readonly = PresenceRuntime(replace(enabled_config, autonomy="readonly"))
        readonly._network_inventory = restarted._network_inventory
        with self.assertRaises(PermissionError):
            readonly.set_network_device_profile(
                device_id=device_id,
                label="Blocked",
                trust_state=None,
                device_type=None,
            )

    def test_bluetooth_inventory_route_is_durable_and_fails_closed(self) -> None:
        disabled = PresenceRuntime(self.config)
        disabled_status = disabled.bluetooth_inventory_status()
        self.assertTrue({
            "enabled", "available", "check_in_progress", "devices",
            "known_endpoints", "security_assessment", "monitor", "limitations",
            "error",
        }.issubset(disabled_status))
        self.assertFalse(disabled_status["enabled"])
        self.assertFalse(disabled_status["available"])
        with self.assertRaises(PermissionError):
            disabled.check_bluetooth_inventory()

        enabled_config = replace(self.config, bluetooth_access="paired-readonly")
        runtime = PresenceRuntime(enabled_config)
        runtime._bluetooth_inventory = BluetoothInventory(
            self.data,
            enumerator=_bluetooth_snapshot,
            min_check_interval_seconds=0,
        )
        checked = runtime.check_bluetooth_inventory()
        self.assertTrue(checked["enabled"])
        self.assertTrue(checked["available"])
        self.assertEqual(checked["known_endpoints"], 1)
        self.assertFalse(checked["nearby_rf_scan_performed"])
        self.assertFalse(checked["pairing_or_control_performed"])
        self.assertFalse(checked["addresses_exposed"])
        device_id = checked["devices"][0]["device_id"]
        profile = runtime.set_bluetooth_device_profile(
            device_id=device_id,
            label="Desk keyboard",
            trust_state="recognized",
            device_type="keyboard",
        )
        self.assertEqual(profile["device"]["label"], "Desk keyboard")
        with self.assertRaisesRegex(ValueError, "trust_state"):
            runtime.set_bluetooth_device_profile(
                device_id=device_id,
                label=None,
                trust_state="controls-computer",
                device_type=None,
            )

        restarted = PresenceRuntime(enabled_config)
        restarted._bluetooth_inventory = BluetoothInventory(
            self.data,
            enumerator=_bluetooth_snapshot,
            min_check_interval_seconds=0,
        )
        durable = restarted.bluetooth_device_detail(device_id)
        self.assertEqual(durable["device"]["label"], "Desk keyboard")
        self.assertEqual(durable["device"]["trust_state"], "recognized")
        self.assertEqual(restarted.bluetooth_inventory_status()["known_endpoints"], 1)

        readonly = PresenceRuntime(replace(enabled_config, autonomy="readonly"))
        readonly._bluetooth_inventory = restarted._bluetooth_inventory
        with self.assertRaises(PermissionError):
            readonly.set_bluetooth_device_profile(
                device_id=device_id,
                label="Blocked",
                trust_state=None,
                device_type=None,
            )

    def test_companion_controls_and_rules_are_durable_and_validate_inputs(self) -> None:
        runtime = PresenceRuntime(self.config)
        state = runtime.set_screen_companion(
            mode="suggest",
            paused=False,
            auto_suggest=True,
            excluded_apps=["KeePass.exe", "keepass.exe"],
        )
        self.assertTrue({
            "mode", "paused", "auto_suggest", "excluded_apps", "updated_at",
            "learning",
        }.issubset(state))
        self.assertEqual(state["mode"], "suggest")
        self.assertEqual(state["excluded_apps"], ["keepass.exe"])
        with self.assertRaisesRegex(ValueError, "switches must be boolean"):
            runtime.set_screen_companion(
                mode="observe",
                paused="no",  # type: ignore[arg-type]
                auto_suggest=False,
                excluded_apps=[],
            )
        with self.assertRaisesRegex(ValueError, "action must be"):
            runtime.control_screen_companion(action="toggle")

        rule_id = runtime.add_screen_companion_rule({
            "trigger_app": "word.exe",
            "title_contains": "outline",
            "action_prompt": "Offer to research the current outline topic.",
            "action_mode": "suggest",
            "cooldown_seconds": 60,
        })
        self.assertTrue(runtime.set_screen_companion_rule_enabled(rule_id, False))
        self.assertFalse(runtime.set_screen_companion_rule_enabled(999_999, True))
        with self.assertRaisesRegex(ValueError, "prompt"):
            runtime.add_screen_companion_rule({
                "trigger_app": "word.exe",
                "action_prompt": "",
            })

        restarted = PresenceRuntime(self.config)
        with Memory(self.data / "jarvis.db") as memory:
            stored_state = memory.screen_companion_state()
            rules = memory.list_screen_companion_rules()
        self.assertEqual(stored_state["mode"], "suggest")
        self.assertFalse(stored_state["paused"])
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["id"], rule_id)
        self.assertFalse(rules[0]["enabled"])

        resumed = restarted.control_screen_companion(action="resume")
        self.assertEqual(resumed["mode"], "suggest")
        self.assertFalse(resumed["paused"])
        self.assertTrue(restarted.delete_screen_companion_rule(rule_id))
        self.assertFalse(restarted.delete_screen_companion_rule(rule_id))
        with Memory(self.data / "jarvis.db") as memory:
            self.assertEqual(memory.list_screen_companion_rules(), [])


if __name__ == "__main__":
    unittest.main()
