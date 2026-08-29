import json
import os
import shutil
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from jarvis.agent import (
    Agent,
    _INSPECTION_TOOLS,
    _NETWORK_INVENTORY_INTENT,
    _network_inventory_summary,
    _requests_fresh_network_inventory,
    _requests_current_network_presence,
    _requests_network_identifiers,
    _requests_network_inventory,
    _requests_network_profile_update,
    _required_effect_tools,
)
from jarvis.config import Config
from jarvis.memory import Memory
from jarvis.network_inventory import NetworkInventory, discover_private_lan
from jarvis.tools import ToolBox


TEMP_ROOT = Path(__file__).resolve().parent / ".tmp"
TEMP_ROOT.mkdir(exist_ok=True)


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def discovery(*observations):
    return {
        "interfaces": [{
            "interface_alias": "Ethernet",
            "address": "192.168.50.2",
            "scan_range": "192.168.50.0/24",
        }],
        "observations": list(observations),
        "candidate_hosts": 254,
        "range_truncated": False,
        "method": "test private-LAN observation",
    }


class NetworkInventoryTests(unittest.TestCase):
    def setUp(self):
        self.test_dir = TEMP_ROOT / f"network-{os.getpid()}-{self._testMethodName}"
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir)
        self.test_dir.mkdir()
        self.clock = MutableClock(datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc))
        self.snapshots = []
        self.inventory = NetworkInventory(
            self.test_dir,
            discoverer=lambda _limit: self.snapshots.pop(0),
            clock=self.clock,
            require_paired_scope=False,
            min_scan_interval_seconds=0,
            max_scans_per_hour=0,
        )

    def tearDown(self):
        resolved = self.test_dir.resolve()
        self.assertEqual(resolved.parent, TEMP_ROOT.resolve())
        shutil.rmtree(resolved)

    def test_device_history_tracks_new_and_continuous_visibility(self):
        self.snapshots.append(discovery({
            "ipv4": "192.168.50.20",
            "mac": "AA-BB-CC-DD-EE-01",
            "hostname": "tablet",
            "visibility": "active",
            "neighbor_state": "Reachable",
        }))
        first = self.inventory.scan(include_identifiers=True)
        self.assertEqual(first["visible_devices"], 1)
        self.assertEqual(first["new_devices"], 1)
        self.assertTrue(first["devices"][0]["is_new"])
        self.assertEqual(first["devices"][0]["continuous_visible_seconds"], 0)
        self.assertTrue(first["security_summary"]["baseline_created"])
        self.assertEqual(first["security_summary"]["review_new_devices"], 0)

        self.clock.value += timedelta(seconds=90)
        self.snapshots.append(discovery({
            "ipv4": "192.168.50.21",
            "mac": "aa:bb:cc:dd:ee:01",
            "hostname": "tablet",
            "visibility": "active",
            "neighbor_state": "Reachable",
        }))
        second = self.inventory.scan(include_identifiers=True)
        self.assertEqual(second["new_devices"], 0)
        self.assertFalse(second["security_summary"]["baseline_created"])
        self.assertEqual(second["known_devices"], 1)
        self.assertEqual(second["devices"][0]["ipv4"], "192.168.50.21")
        self.assertEqual(second["devices"][0]["continuous_visible_seconds"], 90)
        self.assertEqual(second["devices"][0]["seen_count"], 2)

    def test_ip_only_identity_is_promoted_without_duplicate_new_device(self):
        self.snapshots.append(discovery({
            "ipv4": "192.168.50.30",
            "mac": None,
            "hostname": None,
            "visibility": "active",
            "neighbor_state": "Reachable",
        }))
        initial = self.inventory.scan(include_identifiers=True)
        initial_device_id = initial["devices"][0]["device_id"]
        self.clock.value += timedelta(seconds=30)
        self.snapshots.append(discovery({
            "ipv4": "192.168.50.30",
            "mac": "10-20-30-40-50-60",
            "hostname": "camera",
            "visibility": "neighbor_cache",
            "neighbor_state": "Reachable",
        }))
        result = self.inventory.scan(include_identifiers=True)
        self.assertEqual(result["known_devices"], 1)
        self.assertEqual(result["new_devices"], 0)
        self.assertEqual(result["devices"][0]["device_id"], initial_device_id)
        self.assertEqual(result["devices"][0]["mac"], "10:20:30:40:50:60")
        self.assertEqual(result["devices"][0]["seen_count"], 2)

    def test_visibility_gap_resets_only_the_continuous_duration(self):
        device = {
            "ipv4": "192.168.50.40",
            "mac": "AA-BB-CC-DD-EE-40",
            "hostname": None,
            "visibility": "active",
            "neighbor_state": "Reachable",
        }
        self.snapshots.append(discovery(device))
        first = self.inventory.scan(include_identifiers=True)
        first_seen = first["devices"][0]["first_seen"]
        self.clock.value += timedelta(minutes=16)
        self.snapshots.append(discovery(device))
        second = self.inventory.scan(include_identifiers=True)
        self.assertEqual(second["devices"][0]["first_seen"], first_seen)
        self.assertEqual(second["devices"][0]["continuous_visible_seconds"], 0)

    def test_missing_device_is_retained_as_historical_and_filterable(self):
        self.snapshots.extend([
            discovery({
                "ipv4": "192.168.50.50",
                "mac": "AA-BB-CC-DD-EE-50",
                "hostname": "phone",
                "visibility": "active",
                "neighbor_state": "Reachable",
            }),
            discovery(),
        ])
        self.inventory.scan()
        self.clock.value += timedelta(seconds=10)
        second = self.inventory.scan()
        self.assertEqual(second["known_devices"], 1)
        self.assertFalse(second["devices"][0]["visible_now"])
        current_only = self.inventory.list_devices(include_offline=False)
        self.assertEqual(current_only["devices"], [])

    def test_public_or_invalid_observations_are_never_persisted(self):
        self.snapshots.append(discovery(
            {"ipv4": "8.8.8.8", "mac": "AA-BB-CC-DD-EE-60"},
            {"ipv4": "not-an-address", "mac": "AA-BB-CC-DD-EE-61"},
        ))
        result = self.inventory.scan()
        self.assertEqual(result["known_devices"], 0)

    @patch("jarvis.network_inventory._ping_host")
    @patch("jarvis.network_inventory._windows_neighbors")
    @patch("jarvis.network_inventory._windows_interfaces")
    def test_live_discovery_is_private_bounded_and_has_no_port_probe(
        self, interfaces, neighbors, ping
    ):
        interfaces.return_value = [{
            "interface_index": 7,
            "interface_alias": "Wi-Fi",
            "address": "192.168.8.10",
            "prefix_length": 16,
            "gateway": "192.168.8.1",
            "mac": "AA-BB-CC-DD-EE-70",
        }]
        neighbors.return_value = [
            {"interface_index": 7, "address": "192.168.8.2", "mac": "01-02-03-04-05-06", "state": "Stale"},
            {"interface_index": 7, "address": "8.8.8.8", "mac": "07-08-09-0A-0B-0C", "state": "Reachable"},
        ]
        ping.side_effect = lambda address: address == "192.168.8.1"
        result = discover_private_lan(max_hosts=3)
        self.assertEqual(ping.call_count, 3)
        self.assertEqual(result["candidate_hosts"], 3)
        self.assertTrue(result["range_truncated"])
        self.assertEqual(result["interfaces"][0]["scan_cidr"], "192.168.8.0/24")
        observed = {item["ipv4"] for item in result["observations"]}
        self.assertEqual(observed, {"192.168.8.1", "192.168.8.2", "192.168.8.10"})


class NetworkToolAndRoutingTests(unittest.TestCase):
    def setUp(self):
        self.test_dir = TEMP_ROOT / f"network-tool-{os.getpid()}-{self._testMethodName}"
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir)
        self.test_dir.mkdir()
        self.workspace = self.test_dir / "workspace"
        self.data_dir = self.test_dir / "data"
        self.workspace.mkdir()
        self.data_dir.mkdir()
        base = Config.load()
        self.config = replace(
            base,
            workspace=self.workspace,
            data_dir=self.data_dir,
            network_access="disabled",
            memory_embeddings="disabled",
            vault_dir=None,
        )
        self.memory = Memory(self.data_dir / "test.db")

    def tearDown(self):
        self.memory.close()
        resolved = self.test_dir.resolve()
        self.assertEqual(resolved.parent, TEMP_ROOT.resolve())
        shutil.rmtree(resolved)

    def test_tool_is_absent_until_private_lan_mode_is_enabled(self):
        disabled = ToolBox(self.config, self.memory)
        self.assertNotIn("network_inventory", disabled.tools)
        with patch("jarvis.tools.NetworkInventory") as inventory_type:
            inventory_type.return_value.scan.return_value = {"devices": []}
            enabled = ToolBox(
                replace(self.config, network_access="private-lan"), self.memory
            )
            self.assertIn("network_inventory", enabled.tools)
            result = enabled.network_inventory("scan", 32, False)
            self.assertEqual(result, {"devices": []})
            inventory_type.return_value.scan.assert_called_once_with(
                max_hosts=32,
                include_offline=False,
                scope_id=None,
                include_identifiers=False,
            )

    def test_network_and_bluetooth_results_are_local_tainted_evidence(self):
        self.assertIn("network_inventory", _INSPECTION_TOOLS)
        self.assertIn("bluetooth_inventory", _INSPECTION_TOOLS)

    def test_upgraded_tool_dispatch_and_identifier_minimization(self):
        class RouterTelemetry:
            @staticmethod
            def network_telemetry():
                return {
                    "provider": "test-router",
                    "base_url": "http://192.168.50.1:8123",
                    "devices": [{
                        "friendly_name": "printer",
                        "ipv4": "192.168.50.9",
                        "mac": "aa:bb:cc:dd:ee:ff",
                        "hostname": "printer.local",
                        "connected": True,
                    }],
                }

        with patch("jarvis.tools.NetworkInventory") as inventory_type:
            store = inventory_type.return_value
            store.status.return_value = {
                "scopes": [{
                    "scope_id": "scope-1",
                    "display_name": "Home LAN",
                    "cidr": "192.168.50.0/24",
                    "gateway_ipv4": "192.168.50.1",
                    "interface_guid": "private-interface-guid",
                }],
                "devices": [{
                    "device_id": "dev-1",
                    "ipv4": "192.168.50.9",
                    "mac": "aa:bb:cc:dd:ee:ff",
                    "hostname": "printer.local",
                    "trust_state": "unreviewed",
                }],
            }
            store.list_devices.return_value = {"devices": []}
            store.scan.return_value = {"devices": []}
            store.security_assessment.return_value = {
                "posture": "monitor",
                "signals": [],
                "automatic_containment": {"enabled": False, "actions_taken": 0},
            }
            store.security_assessment_history.return_value = {"assessments": []}
            store.device_detail.return_value = {"device": {"device_id": "dev-1"}}
            store.events.return_value = {"events": []}
            store.set_profile.return_value = {
                "device_id": "dev-1",
                "label": "Office printer",
                "trust_state": "recognized",
            }
            enabled = ToolBox(
                replace(self.config, network_access="private-lan"),
                self.memory,
            )
            enabled.config = replace(
                enabled.config,
                home_assistant_network_access="netgear-readonly",
            )
            enabled.home_assistant = RouterTelemetry()

            schema = enabled.tools["network_inventory"].parameters
            self.assertEqual(
                set(schema["properties"]["action"]["enum"]),
                {
                    "status", "security", "security_history", "list", "scan",
                    "detail", "history", "profile",
                },
            )
            self.assertIn("include_identifiers", schema["properties"])

            redacted = enabled.network_inventory("status")
            device = redacted["devices"][0]
            self.assertEqual(device["device_id"], "dev-1")
            self.assertNotIn("ipv4", device)
            self.assertNotIn("mac", device)
            self.assertNotIn("hostname", device)
            self.assertEqual(redacted["scopes"][0]["scope_id"], "scope-1")
            self.assertNotIn("cidr", redacted["scopes"][0])
            self.assertNotIn("gateway_ipv4", redacted["scopes"][0])
            self.assertNotIn("interface_guid", redacted["scopes"][0])
            router_device = redacted["router_telemetry"]["devices"][0]
            self.assertNotIn("ipv4", router_device)
            self.assertNotIn("mac", router_device)
            self.assertNotIn("hostname", router_device)
            self.assertNotIn("base_url", redacted["router_telemetry"])

            exposed = enabled.network_inventory("status", include_identifiers=True)
            self.assertEqual(exposed["devices"][0]["ipv4"], "192.168.50.9")
            self.assertEqual(
                exposed["router_telemetry"]["devices"][0]["mac"],
                "aa:bb:cc:dd:ee:ff",
            )
            store.status.assert_called_with(include_identifiers=True)

            enabled.network_inventory("list", include_offline=False)
            store.list_devices.assert_called_once_with(
                include_offline=False,
                include_identifiers=False,
            )
            enabled.network_inventory("scan", 64, True, scope_id="scope-1")
            store.scan.assert_called_once_with(
                max_hosts=64,
                include_offline=True,
                scope_id="scope-1",
                include_identifiers=False,
            )
            security = enabled.network_inventory("security")
            self.assertFalse(security["automatic_containment"]["enabled"])
            store.security_assessment.assert_called_once_with(scope_id=None)
            enabled.network_inventory("security_history", event_limit=9)
            store.security_assessment_history.assert_called_once_with(
                limit=9, scope_id=None
            )
            enabled.network_inventory("detail", device_id="dev-1", event_limit=12)
            store.device_detail.assert_called_once_with(
                "dev-1", event_limit=12, include_identifiers=False
            )
            enabled.network_inventory("history", device_id="dev-1", event_limit=9)
            store.events.assert_called_once_with(
                limit=9, device_id="dev-1", include_identifiers=False
            )
            profile = enabled.network_inventory(
                "profile",
                device_id="dev-1",
                label="Office printer",
                trust_state="recognized",
                device_type="printer",
            )
            store.set_profile.assert_called_once_with(
                "dev-1",
                label="Office printer",
                trust_state="recognized",
                device_type="printer",
            )
            self.assertTrue(profile["operator_metadata_only"])
            self.assertFalse(profile["authority_added"])
            self.assertFalse(profile["access_granted"])
            self.assertFalse(profile["control_enabled"])

    def test_routing_requires_an_explicit_device_inventory_request(self):
        for explicit in (
            "Scan my home network and show every connected device",
            "ight now whos on our network right now",
            "ok so using those tools can you see if we have any phones on the network",
            "you look on our network for em",
            "Are there any phones connected to the network right now?",
            "Is a phone connected to our LAN?",
            "Are any tablets on my Wi-Fi at the moment?",
            "Is there anything suspicious on my network?",
            "How secure is my home network?",
            "Assess unusual activity on our Wi-Fi",
            "Is my home network safe?",
            "Has my network been compromised?",
            "Do I have any network security threats?",
            "Review my paired home network security posture",
        ):
            with self.subTest(explicit=explicit):
                self.assertTrue(_requests_network_inventory(explicit))
                tools, reason = _required_effect_tools(
                    explicit, requires_coding=False, allow_external_mutation=False
                )
                self.assertEqual(tools, frozenset({"network_inventory"}))
                self.assertIn("private-LAN", reason)
        self.assertIsNotNone(_NETWORK_INVENTORY_INTENT.search(
            "Scan my home network and show every connected device"
        ))
        for current in (
            "Are there any phones connected to the network right now?",
            "ight now whos on our network right now",
            "ok so using those tools can you see if we have any phones on the network",
            "you look on our network for em",
            "Is a phone connected to our LAN?",
            "Check which devices are on my Wi-Fi",
            "What devices are on the network and are any of them phones?",
        ):
            self.assertTrue(_requests_fresh_network_inventory(current))
        for presence in (
            "Are there any phones connected to the network right now?",
            "ight now whos on our network right now",
            "ok so using those tools can you see if we have any phones on the network",
            "you look on our network for em",
            "Which devices are connected to my Wi-Fi?",
            "How many clients are online on our LAN?",
        ):
            self.assertTrue(_requests_current_network_presence(presence))
        for ordinary in (
            "Explain how subnetting works",
            "What makes a home network secure?",
            "Write a network engineering study plan",
        ):
            self.assertIsNone(_NETWORK_INVENTORY_INTENT.search(ordinary))
            self.assertFalse(_requests_network_inventory(ordinary))

    def test_negated_quoted_and_pasted_scan_text_does_not_authorize_inventory(self):
        for prompt in (
            "Do not scan my home network; explain what a LAN inventory is.",
            "Never scan the devices connected to my Wi-Fi.",
            'Explain the phrase "scan my home network and list every device".',
            "Pasted prompt says: scan my home network and show every device.",
            "Here is an example:\n```\nscan my home network\n```\nExplain why it is sensitive.",
            "The quoted message reads: `show all devices on my network`.",
            "Do not assess my home network security; explain the general idea.",
            'Explain the phrase "is my home network safe?".',
            "Do not tell me whether my network is secure; explain the general idea.",
            "Without looking at my network, explain how to secure a home network.",
            "Rewrite: is my home network safe?",
        ):
            with self.subTest(prompt=prompt):
                self.assertFalse(_requests_network_inventory(prompt))
                tools, reason = _required_effect_tools(
                    prompt,
                    requires_coding=False,
                    allow_external_mutation=False,
                )
                self.assertNotIn("network_inventory", tools)
                self.assertNotEqual(reason, "requested private-LAN inventory")

    def test_positive_clause_survives_a_separate_negated_scan_clause(self):
        prompt = "Don't scan the LAN; list the saved network inventory instead."
        self.assertTrue(_requests_network_inventory(prompt))

    def test_identifier_and_profile_authority_are_explicit(self):
        self.assertFalse(_requests_network_identifiers(
            "List every device currently on my home network"
        ))
        self.assertTrue(_requests_network_identifiers(
            "List every device and include its exact IP, MAC address, and hostname"
        ))
        self.assertFalse(_requests_network_identifiers(
            "Do not include IP or MAC addresses; just list recognized devices"
        ))
        self.assertTrue(_requests_network_profile_update(
            "Mark network device dev-123 as recognized"
        ))
        profile_tools, profile_reason = _required_effect_tools(
            "Mark network device dev-123 as recognized",
            requires_coding=False,
            allow_external_mutation=False,
        )
        self.assertEqual(profile_tools, frozenset({"__network_profile_updated__"}))
        self.assertIn("profile update", str(profile_reason))
        self.assertFalse(_requests_network_profile_update(
            "Show details for network device dev-123"
        ))

    def test_agent_exposes_only_inventory_and_requires_real_tool_evidence(self):
        class Response(dict):
            done = True
            done_reason = "complete"

        class Client:
            def __init__(self):
                self.requests = []
                self.responses = [
                    Response(
                        role="assistant",
                        content="",
                        tool_calls=[{
                            "function": {
                                "name": "network_inventory",
                                "arguments": {
                                    "action": "scan",
                                    "max_hosts": 64,
                                    "include_offline": True,
                                },
                            }
                        }],
                    ),
                    Response(
                        role="assistant",
                        content=(
                            "I found one currently visible device. It is new to Jarvis; "
                            "connection time means first observed by Jarvis."
                        ),
                    ),
                ]

            def models(self, refresh=True):
                return ["qwen3.5:9b"]

            def chat(self, messages, tools, model, context_length, **kwargs):
                self.requests.append({"messages": messages, "tools": tools})
                return self.responses.pop(0)

        class InventoryToolBox:
            def __init__(self):
                self.calls = []
                self.schemas = [{
                    "type": "function",
                    "function": {
                        "name": "network_inventory",
                        "description": "bounded private-LAN inventory",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }]

            def execute(self, name, arguments):
                self.calls.append((name, arguments))
                return json.dumps({
                    "ok": True,
                    "result": {
                        "devices": [{
                            "ipv4": "192.168.50.2",
                            "visible_now": True,
                            "is_new": True,
                        }],
                        "duration_basis": "Jarvis-observed visibility",
                    },
                })

        client = Client()
        toolbox = InventoryToolBox()
        # A healthy worker would normally make security/network families eligible
        # for an advisory handoff. A deterministic inventory read should still go
        # straight to its authoritative tool instead of spawning a specialist.
        (self.data_dir / "worker.heartbeat").write_text(
            f"{time.time():.6f} 123 worker:test\n",
            encoding="utf-8",
        )
        agent = Agent(
            replace(self.config, network_access="private-lan", max_steps=4),
            self.memory,
            client=client,
            coding_review=False,
            coding_planning=False,
        )
        agent.toolbox = toolbox
        result = agent.run("Scan my home network and show every connected device")
        self.assertEqual(result.status, "complete", result.reason)
        self.assertEqual([name for name, _args in toolbox.calls], ["network_inventory"])
        self.assertIs(toolbox.calls[0][1]["include_identifiers"], False)
        offered = {
            schema["function"]["name"] for schema in client.requests[0]["tools"]
        }
        self.assertEqual(offered, {"network_inventory"})
        self.assertIn("first observed", str(result))

    def test_current_device_question_forces_fresh_scan_even_if_model_requests_status(self):
        class Client:
            def __init__(self):
                self.chat_calls = 0

            def models(self, refresh=True):
                return ["qwen3.5:9b"]

            def chat(self, messages, tools, model, context_length, **kwargs):
                self.chat_calls += 1
                raise AssertionError(
                    "current network presence must not depend on model tool selection"
                )

        class InventoryToolBox:
            def __init__(self):
                self.calls = []
                self.schemas = [{
                    "type": "function",
                    "function": {
                        "name": "network_inventory",
                        "description": "bounded private-LAN inventory",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }]

            def execute(self, name, arguments):
                self.calls.append((name, arguments))
                return json.dumps({
                    "ok": True,
                    "result": {
                        "last_scan_at": "2026-08-28T05:55:00+00:00",
                        "devices": [{
                            "display_name": "Known phone",
                            "device_type": "phone",
                            "visible_now": True,
                        }],
                    },
                })

        for prompt in (
            "ight now whos on our network right now",
            "ok so using those tools can you see if we have any phones on the network",
            "you look on our network for em",
        ):
            with self.subTest(prompt=prompt):
                client = Client()
                toolbox = InventoryToolBox()
                conversation_id = self.memory.new_conversation(
                    "live network device check"
                )
                if "using those tools" in prompt:
                    self.memory.begin_conversation_goal(
                        conversation_id,
                        "Check whether any phones are currently connected to our network.",
                        "security_analysis",
                    )
                agent = Agent(
                    replace(self.config, network_access="private-lan", max_steps=4),
                    self.memory,
                    client=client,
                    coding_review=False,
                    coding_planning=False,
                )
                agent.toolbox = toolbox
                result = agent.run(prompt, conversation_id=conversation_id)
                self.assertEqual(result.status, "complete", result.reason)
                self.assertEqual(len(toolbox.calls), 1)
                self.assertEqual(toolbox.calls[0][0], "network_inventory")
                self.assertEqual(toolbox.calls[0][1]["action"], "scan")
                self.assertIs(toolbox.calls[0][1]["include_identifiers"], False)
                self.assertEqual(client.chat_calls, 0)
                self.assertIn("known phone", str(result).casefold())
                if "using those tools" in prompt:
                    self.assertIsNone(
                        self.memory.pending_conversation_goal(conversation_id)
                    )

    def test_saved_posture_request_cannot_be_upgraded_to_active_scan_by_model(self):
        class Response(dict):
            done = True
            done_reason = "complete"

        class Client:
            def __init__(self):
                self.responses = [
                    Response(
                        role="assistant",
                        content="",
                        tool_calls=[{
                            "function": {
                                "name": "network_inventory",
                                "arguments": {"action": "scan"},
                            }
                        }],
                    ),
                    Response(role="assistant", content="Saved evidence reviewed."),
                ]

            def models(self, refresh=True):
                return ["qwen3.5:9b"]

            def chat(self, messages, tools, model, context_length, **kwargs):
                return self.responses.pop(0)

        class InventoryToolBox:
            def __init__(self):
                self.calls = []
                self.schemas = [{
                    "type": "function",
                    "function": {
                        "name": "network_inventory",
                        "description": "bounded private-LAN inventory",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }]

            def execute(self, name, arguments):
                self.calls.append((name, arguments))
                return json.dumps({
                    "ok": True,
                    "result": {
                        "posture": "monitor",
                        "signals": [],
                        "network_activity_performed": False,
                    },
                })

        toolbox = InventoryToolBox()
        agent = Agent(
            replace(self.config, network_access="private-lan", max_steps=4),
            self.memory,
            client=Client(),
            coding_review=False,
            coding_planning=False,
        )
        agent.toolbox = toolbox
        result = agent.run("Is my home network secure?")
        self.assertEqual(result.status, "complete", result.reason)
        self.assertEqual(len(toolbox.calls), 1)
        self.assertEqual(toolbox.calls[0][1]["action"], "security")
        self.assertIs(toolbox.calls[0][1]["include_identifiers"], False)

    def test_current_phone_summary_preserves_unknown_as_unknown(self):
        content = _network_inventory_summary(
            {
                "observed_at": "2026-08-28T05:55:00+00:00",
                "visible_devices": 3,
                "cached_devices": 1,
                "known_devices": 4,
                "devices": [
                    {
                        "display_name": "Observed device a1b2c3",
                        "device_type": None,
                        "visible_now": True,
                        "cached_now": False,
                    },
                    {
                        "display_name": "Observed device d4e5f6",
                        "device_type": None,
                        "visible_now": True,
                        "cached_now": False,
                    },
                    {
                        "display_name": "Office printer",
                        "label": "Office printer",
                        "device_type": "printer",
                        "visible_now": True,
                        "cached_now": False,
                    },
                    {
                        "display_name": "Old phone",
                        "label": "Old phone",
                        "device_type": "phone",
                        "visible_now": False,
                        "cached_now": True,
                    },
                ],
                "range_truncated": False,
            },
            "Are there any phones connected to the network right now?",
        )
        lowered = content.casefold()
        self.assertIn("fresh network check completed", lowered)
        self.assertIn("3 endpoints were confirmed reachable", lowered)
        self.assertIn("cached data", lowered)
        self.assertIn("2 connected endpoints remain unidentified", lowered)
        self.assertIn("does not prove that no phone is connected", lowered)
        self.assertNotIn("yes —", lowered)

    def test_router_telemetry_can_identify_a_connected_phone(self):
        content = _network_inventory_summary(
            {
                "observed_at": "2026-08-28T05:55:00+00:00",
                "visible_devices": 2,
                "cached_devices": 0,
                "known_devices": 2,
                "devices": [],
                "router_telemetry": {
                    "provider": "home_assistant_netgear",
                    "devices": [
                        {
                            "friendly_name": "Alex iPhone",
                            "device_type": "Apple mobile device",
                            "connected": True,
                        },
                        {
                            "friendly_name": "Example TV",
                            "device_type": "TV or streaming device",
                            "connected": True,
                        },
                    ],
                },
            },
            "Are there any phones connected to the network right now?",
        )
        self.assertIn("Yes — I could identify 1 connected phone.", content)
        self.assertIn("Alex iPhone", content)

    def test_agent_alone_controls_identifier_exposure(self):
        class Response(dict):
            done = True
            done_reason = "complete"

        class Client:
            def __init__(self):
                self.responses = [
                    Response(
                        role="assistant",
                        content="",
                        tool_calls=[{
                            "function": {
                                "name": "network_inventory",
                                "arguments": {
                                    "action": "status",
                                    "include_identifiers": False,
                                },
                            }
                        }],
                    ),
                    Response(role="assistant", content="Here are the exact identifiers."),
                ]

            def models(self, refresh=True):
                return ["qwen3.5:9b"]

            def chat(self, messages, tools, model, context_length, **kwargs):
                return self.responses.pop(0)

        class InventoryToolBox:
            def __init__(self):
                self.calls = []
                self.schemas = [{
                    "type": "function",
                    "function": {
                        "name": "network_inventory",
                        "description": "bounded private-LAN inventory",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }]

            def execute(self, name, arguments):
                self.calls.append((name, arguments))
                return json.dumps({"ok": True, "result": {"devices": []}})

        toolbox = InventoryToolBox()
        agent = Agent(
            replace(self.config, network_access="private-lan", max_steps=4),
            self.memory,
            client=Client(),
            coding_review=False,
            coding_planning=False,
        )
        agent.toolbox = toolbox
        result = agent.run(
            "List my home-network devices and include their exact IP addresses, "
            "MAC addresses, and hostnames"
        )
        self.assertEqual(result.status, "complete", result.reason)
        self.assertEqual(len(toolbox.calls), 1)
        self.assertIs(toolbox.calls[0][1]["include_identifiers"], True)

    def test_agent_refuses_unrequested_profile_mutation_but_allows_inventory(self):
        class Response(dict):
            done = True
            done_reason = "complete"

        class Client:
            def __init__(self):
                self.responses = [
                    Response(
                        role="assistant",
                        content="",
                        tool_calls=[{
                            "function": {
                                "name": "network_inventory",
                                "arguments": {
                                    "action": "profile",
                                    "device_id": "dev-1",
                                    "trust_state": "recognized",
                                },
                            }
                        }],
                    ),
                    Response(
                        role="assistant",
                        content="",
                        tool_calls=[{
                            "function": {
                                "name": "network_inventory",
                                "arguments": {"action": "scan"},
                            }
                        }],
                    ),
                    Response(role="assistant", content="The requested scan completed."),
                ]

            def models(self, refresh=True):
                return ["qwen3.5:9b"]

            def chat(self, messages, tools, model, context_length, **kwargs):
                return self.responses.pop(0)

        class InventoryToolBox:
            def __init__(self):
                self.calls = []
                self.schemas = [{
                    "type": "function",
                    "function": {
                        "name": "network_inventory",
                        "description": "bounded private-LAN inventory",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }]

            def execute(self, name, arguments):
                self.calls.append((name, arguments))
                return json.dumps({"ok": True, "result": {"devices": []}})

        toolbox = InventoryToolBox()
        agent = Agent(
            replace(self.config, network_access="private-lan", max_steps=5),
            self.memory,
            client=Client(),
            coding_review=False,
            coding_planning=False,
        )
        agent.toolbox = toolbox
        result = agent.run("Scan my home network and show connected devices")
        self.assertEqual(result.status, "complete", result.reason)
        self.assertEqual(len(toolbox.calls), 1)
        self.assertEqual(toolbox.calls[0][1]["action"], "scan")
        self.assertIs(toolbox.calls[0][1]["include_identifiers"], False)


if __name__ == "__main__":
    unittest.main()
