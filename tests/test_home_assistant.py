from __future__ import annotations

import json
import os
import shutil
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from jarvis.agent import (
    Agent,
    _HOME_DEVICE_CONTROL_INTENT,
    _HOME_DEVICE_STATUS_INTENT,
    _NETWORK_INVENTORY_INTENT,
    _required_effect_tools,
)
from jarvis.config import Config
from jarvis.home_assistant import (
    HomeAssistantProvider,
    normalize_home_assistant_url,
)
from jarvis.memory import Memory
from jarvis.tools import ToolBox


TEMP_ROOT = Path(__file__).resolve().parent / ".tmp"
TEMP_ROOT.mkdir(exist_ok=True)


class RecordingHomeAssistant(HomeAssistantProvider):
    def __init__(self, entities=("remote.example_tv",)) -> None:
        super().__init__(
            "https://192.168.50.2:8123",
            "test-token-" + "x" * 24,
            tuple(entities),
            timeout_seconds=1,
        )
        self.requests: list[tuple[str, str, dict | None]] = []
        self.activities: dict[str, str | None] = {
            entity: None for entity in self.allowed_entities
        }

    def _request(self, method, path, payload=None):
        self.requests.append((method, path, payload))
        if method == "POST":
            if path == "/api/services/remote/turn_on":
                self.activities[payload["entity_id"]] = payload["activity"]
            return []
        entity = path.removeprefix("/api/states/")
        return {
            "entity_id": entity,
            "state": "on",
            "attributes": {
                "friendly_name": entity.removeprefix("remote.")
                .replace("_", " ")
                .title(),
                "current_activity": self.activities[entity],
            },
        }


class NetworkRecordingHomeAssistant(HomeAssistantProvider):
    def __init__(self) -> None:
        super().__init__(
            "http://127.0.0.1:8123",
            "test-token-" + "x" * 24,
            (),
            timeout_seconds=1,
        )

    def _request(self, method, path, payload=None):
        self.assert_request = (method, path, payload)
        return [
            {
                "entity_id": "device_tracker.example_tv",
                "state": "home",
                "last_changed": "2026-08-28T10:00:00+00:00",
                "attributes": {
                    "source_type": "router",
                    "friendly_name": "Example TV",
                    "hostname": "google-tv",
                    "ip": "192.168.1.40",
                    "mac": "AA-BB-CC-DD-EE-40",
                    "icon": "mdi:television",
                },
            },
            {
                "entity_id": "device_tracker.private_phone_location",
                "state": "home",
                "attributes": {
                    "source_type": "gps",
                    "friendly_name": "Private GPS tracker",
                    "latitude": 40.0,
                    "longitude": -75.0,
                },
            },
            {
                "entity_id": "sensor.example_tv_link_rate",
                "state": "433",
                "attributes": {"unit_of_measurement": "Mbps"},
            },
            {
                "entity_id": "sensor.example_tv_signal_strength",
                "state": "82",
                "attributes": {"unit_of_measurement": "%"},
            },
            {
                "entity_id": "sensor.example_tv_link_type",
                "state": "5GHz",
                "attributes": {},
            },
            {
                "entity_id": "sensor.router_download_today",
                "state": "2048",
                "attributes": {"unit_of_measurement": "MB"},
            },
        ]


class HomeAssistantProviderTests(unittest.TestCase):
    def test_origin_is_private_literal_and_canonical(self):
        self.assertEqual(
            normalize_home_assistant_url("https://192.168.50.2:8123/"),
            "https://192.168.50.2:8123",
        )
        self.assertEqual(
            normalize_home_assistant_url("http://localhost:8123"),
            "http://localhost:8123",
        )
        for unsafe in (
            "https://example.com",
            "http://homeassistant.local:8123",
            "http://user:pass@192.168.50.2:8123",
            "https://192.168.50.2:8123/api/states",
            "file:///tmp/home-assistant",
        ):
            with self.subTest(url=unsafe), self.assertRaises(ValueError):
                normalize_home_assistant_url(unsafe)

    def test_plaintext_is_rejected_for_non_loopback_before_opener_creation(self):
        for unsafe in (
            "http://192.168.50.2:8123",
            "http://169.254.20.5:8123",
            "http://[fd00::2]:8123",
        ):
            with self.subTest(url=unsafe), patch(
                "jarvis.home_assistant.urllib.request.build_opener"
            ) as build_opener:
                with self.assertRaisesRegex(ValueError, "must use HTTPS"):
                    HomeAssistantProvider(
                        unsafe,
                        "test-token-" + "x" * 24,
                        ("remote.example_tv",),
                    )
                build_opener.assert_not_called()

    def test_https_lan_and_plaintext_loopback_remain_supported(self):
        for supported, expected in (
            ("https://192.168.50.2:8123", "https://192.168.50.2:8123"),
            ("https://[fd00::2]:8123", "https://[fd00::2]:8123"),
            ("http://127.0.0.1:8123", "http://127.0.0.1:8123"),
            ("http://[::1]:8123", "http://[::1]:8123"),
            ("http://localhost:8123", "http://localhost:8123"),
        ):
            with self.subTest(url=supported):
                self.assertEqual(normalize_home_assistant_url(supported), expected)

    def test_status_reads_only_allowlisted_entities_and_never_exposes_token(self):
        provider = RecordingHomeAssistant((
            "remote.example_tv",
            "remote.bedroom_tv",
        ))
        result = provider.status()
        self.assertEqual(
            [item[1] for item in provider.requests],
            [
                "/api/states/remote.example_tv",
                "/api/states/remote.bedroom_tv",
            ],
        )
        self.assertEqual(len(result["devices"]), 2)
        self.assertFalse(result["credentials_exposed"])
        self.assertNotIn(provider.token, json.dumps(result))
        with self.assertRaises(PermissionError):
            provider._state("remote.unlisted_tv")

    def test_spotify_launch_uses_exact_android_tv_package_and_readback(self):
        provider = RecordingHomeAssistant()
        snapshot = provider.approval_snapshot(
            "Example TV", "open_app", "Spotify"
        )
        self.assertEqual(snapshot["resolved_entity"], "remote.example_tv")
        self.assertEqual(snapshot["resolved_app"], "com.spotify.tv.android")
        result = provider.control(
            entity_id=snapshot["resolved_entity"],
            action=snapshot["resolved_action"],
            app=snapshot["resolved_app"],
        )
        self.assertIn(
            (
                "POST",
                "/api/services/remote/turn_on",
                {
                    "entity_id": "remote.example_tv",
                    "activity": "com.spotify.tv.android",
                },
            ),
            provider.requests,
        )
        self.assertTrue(result["command_accepted"])
        self.assertTrue(result["readback_completed"])
        self.assertTrue(result["effect_verified"])
        self.assertEqual(
            result["effect_verification_basis"], "current_activity_exact_match"
        )
        self.assertEqual(result["current_activity"], "com.spotify.tv.android")
        self.assertNotIn(provider.token, json.dumps(result))

    def test_navigation_is_fixed_allowlist_and_unlisted_targets_fail(self):
        provider = RecordingHomeAssistant()
        result = provider.control(
            entity_id="remote.example_tv", action="home"
        )
        self.assertEqual(result["action"], "home")
        self.assertTrue(result["command_accepted"])
        self.assertTrue(result["readback_completed"])
        self.assertIsNone(result["effect_verified"])
        self.assertEqual(
            result["effect_verification_basis"],
            "remote_effect_not_observable_from_entity_state",
        )
        self.assertIn(
            (
                "POST",
                "/api/services/remote/send_command",
                {"entity_id": "remote.example_tv", "command": "HOME"},
            ),
            provider.requests,
        )
        with self.assertRaises(ValueError):
            provider.approval_snapshot("Example TV", "shell", None)
        with self.assertRaises(PermissionError):
            provider.control(entity_id="remote.unlisted_tv", action="home")
        with self.assertRaises(ValueError):
            provider.normalize_app("; rm -rf / ;")

    def test_app_launch_mismatch_is_not_effect_verified(self):
        provider = RecordingHomeAssistant()
        mismatched_state = {
            "entity_id": "remote.example_tv",
            "state": "on",
            "attributes": {
                "friendly_name": "Example TV",
                "current_activity": "com.google.android.youtube.tv",
            },
        }
        with (
            patch.object(
                provider,
                "_request",
                side_effect=[[], *[mismatched_state for _item in range(4)]],
            ),
            patch("jarvis.home_assistant.time.sleep"),
        ):
            result = provider.control(
                entity_id="remote.example_tv",
                action="open_app",
                app="Spotify",
            )

        self.assertTrue(result["command_accepted"])
        self.assertTrue(result["readback_completed"])
        self.assertFalse(result["effect_verified"])
        self.assertEqual(
            result["effect_verification_basis"], "current_activity_did_not_match"
        )

    def test_netgear_telemetry_is_read_only_bounded_and_honest_about_bandwidth(self):
        provider = NetworkRecordingHomeAssistant()
        result = provider.network_telemetry()
        self.assertEqual(provider.assert_request, ("GET", "/api/states", None))
        self.assertEqual(result["connected_devices"], 1)
        self.assertEqual(result["known_devices"], 1)
        device = result["devices"][0]
        self.assertEqual(device["friendly_name"], "Example TV")
        self.assertEqual(device["device_type"], "TV or streaming device")
        self.assertEqual(device["link_type"], "5GHz")
        self.assertEqual(device["link_rate_mbps"], 433.0)
        self.assertEqual(device["signal_percent"], 82.0)
        self.assertNotIn("latitude", json.dumps(result))
        self.assertNotIn("longitude", json.dumps(result))
        self.assertIn("not actual data usage", result["bandwidth_scope"])
        self.assertEqual(
            result["router_metrics"]["sensor.router_download_today"],
            {"metric": "download_today", "value": 2048.0, "unit": "MB"},
        )
        self.assertNotIn(provider.token, json.dumps(result))


class HomeDeviceToolAndRoutingTests(unittest.TestCase):
    def setUp(self):
        self.test_dir = TEMP_ROOT / f"home-device-{os.getpid()}-{self._testMethodName}"
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir)
        self.test_dir.mkdir()
        self.workspace = self.test_dir / "workspace"
        self.data_dir = self.test_dir / "data"
        self.workspace.mkdir()
        self.data_dir.mkdir()
        self.config = replace(
            Config.load(),
            workspace=self.workspace,
            data_dir=self.data_dir,
            home_assistant_access="disabled",
            memory_embeddings="disabled",
            vault_dir=None,
        )
        self.memory = Memory(self.data_dir / "test.db")

    def tearDown(self):
        self.memory.close()
        resolved = self.test_dir.resolve()
        self.assertEqual(resolved.parent, TEMP_ROOT.resolve())
        shutil.rmtree(resolved)

    def paired_config(self):
        return replace(
            self.config,
            home_assistant_access="paired",
            home_assistant_url="https://192.168.50.2:8123",
            home_assistant_token="test-token-" + "x" * 24,
            home_assistant_entities=("remote.example_tv",),
        )

    def test_tools_are_absent_until_pairing_is_configured(self):
        disabled = ToolBox(self.config, self.memory)
        self.assertNotIn("home_device_status", disabled.tools)
        self.assertNotIn("home_device_control", disabled.tools)
        with patch("jarvis.tools.HomeAssistantProvider") as provider_type:
            enabled = ToolBox(self.paired_config(), self.memory)
        self.assertIn("home_device_status", enabled.tools)
        self.assertIn("home_device_control", enabled.tools)
        provider_type.assert_called_once()

    def test_netgear_readonly_mode_enriches_inventory_without_device_control(self):
        config = replace(
            self.config,
            network_access="private-lan",
            home_assistant_network_access="netgear-readonly",
            home_assistant_url="http://127.0.0.1:8123",
            home_assistant_token="test-token-" + "x" * 24,
            home_assistant_entities=(),
        )
        with (
            patch("jarvis.tools.NetworkInventory") as inventory_type,
            patch("jarvis.tools.HomeAssistantProvider") as provider_type,
        ):
            inventory_type.return_value.list_devices.return_value = {
                "devices": [], "known_devices": 0
            }
            provider_type.return_value.network_telemetry.return_value = {
                "provider": "home_assistant_netgear",
                "devices": [{"friendly_name": "Test TV"}],
            }
            toolbox = ToolBox(config, self.memory)
            result = toolbox.network_inventory("list", include_offline=True)
        self.assertIn("network_inventory", toolbox.tools)
        self.assertNotIn("home_device_status", toolbox.tools)
        self.assertNotIn("home_device_control", toolbox.tools)
        self.assertEqual(
            result["router_telemetry"]["provider"], "home_assistant_netgear"
        )
        provider_type.return_value.network_telemetry.assert_called_once_with()

    def test_exact_device_action_and_app_are_bound_to_one_shot_approval(self):
        provider = RecordingHomeAssistant()
        with patch("jarvis.tools.HomeAssistantProvider", return_value=provider):
            toolbox = ToolBox(self.paired_config(), self.memory)
        arguments = {
            "device": "Example TV",
            "action": "open_app",
            "app": "Spotify",
        }
        with toolbox.approval_context("conversation:501"):
            blocked = json.loads(toolbox.execute("home_device_control", arguments))
        self.assertTrue(blocked["approval_required"])
        request = next(
            item for item in self.memory.list_approvals()
            if item["id"] == blocked["approval_id"]
        )
        self.assertIn("remote.example_tv", request["resource"])
        self.assertIn("com.spotify.tv.android", request["resource"])
        self.assertNotIn(provider.token, request["resource"])
        self.assertTrue(
            self.memory.decide_approval(blocked["approval_id"], True, ttl_hours=2)
        )
        with toolbox.approval_context("conversation:501"):
            allowed = json.loads(toolbox.execute("home_device_control", arguments))
        self.assertTrue(allowed["ok"], allowed)
        self.assertTrue(allowed["result"]["readback_completed"])
        self.assertTrue(allowed["result"]["effect_verified"])

        changed = {**arguments, "app": "YouTube"}
        with toolbox.approval_context("conversation:501"):
            blocked_changed = json.loads(
                toolbox.execute("home_device_control", changed)
            )
        self.assertTrue(blocked_changed["approval_required"])
        self.assertNotEqual(blocked_changed["approval_id"], blocked["approval_id"])

    def test_routing_distinguishes_control_status_and_network_inventory(self):
        control = "Open Spotify on my Google TV"
        self.assertIsNotNone(_HOME_DEVICE_CONTROL_INTENT.search(control))
        tools, reason = _required_effect_tools(
            control, requires_coding=False, allow_external_mutation=False
        )
        self.assertEqual(tools, frozenset({"home_device_control"}))
        self.assertIn("home-device", reason)

        status = "What is playing on the living room TV?"
        self.assertIsNotNone(_HOME_DEVICE_STATUS_INTENT.search(status))
        tools, _reason = _required_effect_tools(
            status, requires_coding=False, allow_external_mutation=False
        )
        self.assertEqual(tools, frozenset({"home_device_status"}))

        combined = (
            "Control a device connected to my network: open Spotify on my Google TV"
        )
        self.assertIsNotNone(_HOME_DEVICE_CONTROL_INTENT.search(combined))
        self.assertIsNotNone(_NETWORK_INVENTORY_INTENT.search(combined))
        tools, _reason = _required_effect_tools(
            combined, requires_coding=False, allow_external_mutation=False
        )
        self.assertEqual(tools, frozenset({"home_device_control"}))

        for ordinary in (
            "Explain how smart TVs work",
            "Which Google TV should I buy?",
            "Write a guide to Home Assistant",
        ):
            self.assertIsNone(_HOME_DEVICE_CONTROL_INTENT.search(ordinary))
            self.assertIsNone(_HOME_DEVICE_STATUS_INTENT.search(ordinary))

    def test_agent_offers_only_home_device_tools_for_control_request(self):
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
                                "name": "home_device_control",
                                "arguments": {
                                    "device": "Example TV",
                                    "action": "open_app",
                                    "app": "Spotify",
                                },
                            }
                        }],
                    ),
                    Response(
                        role="assistant",
                        content="Spotify is open on the living-room TV.",
                    ),
                ]

            def models(self, refresh=True):
                return ["qwen3.5:9b"]

            def chat(self, messages, tools, model, context_length, **kwargs):
                self.requests.append({"messages": messages, "tools": tools})
                return self.responses.pop(0)

        class HomeToolBox:
            def __init__(self):
                self.calls = []
                self.schemas = [
                    {
                        "type": "function",
                        "function": {
                            "name": name,
                            "description": name,
                            "parameters": {"type": "object", "properties": {}},
                        },
                    }
                    for name in ("home_device_status", "home_device_control")
                ]

            def execute(self, name, arguments):
                self.calls.append((name, arguments))
                return json.dumps({
                    "ok": True,
                    "result": {
                        "entity_id": "remote.example_tv",
                        "action": "open_app",
                        "app": "com.spotify.tv.android",
                        "command_accepted": True,
                        "readback_completed": True,
                        "effect_verified": True,
                        "effect_verification_basis": "current_activity_exact_match",
                    },
                })

        client = Client()
        toolbox = HomeToolBox()
        agent = Agent(
            replace(self.paired_config(), max_steps=4),
            self.memory,
            client=client,
            coding_review=False,
            coding_planning=False,
        )
        agent.toolbox = toolbox
        result = agent.run("Open Spotify on my Google TV")
        self.assertEqual(result.status, "complete", result.reason)
        self.assertEqual(
            [name for name, _arguments in toolbox.calls],
            ["home_device_control"],
        )
        offered = {
            schema["function"]["name"] for schema in client.requests[0]["tools"]
        }
        self.assertEqual(
            offered, {"home_device_status", "home_device_control"}
        )
        self.assertIn("Spotify is open", str(result))


if __name__ == "__main__":
    unittest.main()
