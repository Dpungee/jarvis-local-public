from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from jarvis.agent import (
    Agent,
    _requests_bluetooth_inventory,
    _requests_bluetooth_metadata,
    _requests_bluetooth_profile_update,
    _requests_fresh_bluetooth_inventory,
    _required_effect_tools,
)
from jarvis.bluetooth_inventory import (
    BLUETOOTH_OBSERVATION_FRESH_SECONDS,
    MAX_BLUETOOTH_DEVICES,
    BluetoothInventory,
    BluetoothInventoryError,
    BluetoothInventoryRateLimited,
    _WINDOWS_PAIRED_BLUETOOTH_SCRIPT,
    _normalize_provider_result,
)
from jarvis.config import Config
from jarvis.memory import Memory
from jarvis.tools import ToolBox


TEMP_ROOT = Path(__file__).resolve().parents[1] / ".test-tmp"


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int = 60) -> None:
        self.value += timedelta(seconds=seconds)


def endpoint(
    raw_id: str,
    *,
    name: str = "Keyboard",
    transport: str = "classic",
    manufacturer: str | None = None,
    model_name: str | None = None,
    categories: list[str] | None = None,
    present: bool | None = None,
    connected: bool | None = None,
    paired: bool = True,
) -> dict[str, object]:
    return {
        "raw_id": raw_id,
        "transport": transport,
        "name": name,
        "paired": paired,
        "paired_evidence_available": True,
        "present": present,
        "present_evidence_available": present is not None,
        "connected": connected,
        "connected_evidence_available": connected is not None,
        "manufacturer": manufacturer,
        "model_name": model_name,
        "categories": categories or [],
    }


def provider(*devices: dict[str, object]) -> dict[str, object]:
    return {
        "provider": "windows_device_information",
        "observed_at": "2026-08-28T12:00:00+00:00",
        "devices": list(devices),
    }


class BluetoothInventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        TEMP_ROOT.mkdir(exist_ok=True)
        self.root = TEMP_ROOT / f"bluetooth-{os.getpid()}-{self._testMethodName}"
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir()
        self.clock = Clock()

    def tearDown(self) -> None:
        resolved = self.root.resolve()
        self.assertEqual(resolved.parent, TEMP_ROOT.resolve())
        shutil.rmtree(resolved)

    def inventory(self, snapshots: list[dict[str, object]]) -> BluetoothInventory:
        def enumerate_next() -> dict[str, object]:
            return snapshots.pop(0)

        return BluetoothInventory(
            self.root,
            enumerator=enumerate_next,
            clock=self.clock,
            min_check_interval_seconds=0,
        )

    def test_first_check_is_a_suppressed_baseline_and_second_new_device_alerts(self):
        first = endpoint("Bluetooth#Device_A", name="Headphones")
        second = endpoint("Bluetooth#Device_B", name="Phone", transport="low_energy")
        inventory = self.inventory([provider(first), provider(first, second)])

        baseline = inventory.check(include_os_metadata=True)
        self.assertTrue(baseline["baseline_created"])
        self.assertEqual(baseline["new_endpoints"], 0)
        self.assertFalse(baseline["devices"][0]["is_new"])
        self.assertEqual(
            baseline["security_assessment"]["attention_signal_count"], 0
        )
        self.assertFalse(
            baseline["security_assessment"]["compromise_established"]
        )

        self.clock.advance()
        changed = inventory.check(include_os_metadata=True)
        self.assertFalse(changed["baseline_created"])
        self.assertEqual(changed["new_endpoints"], 1)
        new_rows = [item for item in changed["devices"] if item["is_new"]]
        self.assertEqual(len(new_rows), 1)
        self.assertEqual(new_rows[0]["os_reported_name"], "Phone")
        signal = next(
            item
            for item in changed["security_assessment"]["signals"]
            if item["rule_id"] == "new_unreviewed_paired_endpoint"
        )
        self.assertEqual(signal["severity"], "medium")
        self.assertFalse(signal["compromise_established"])
        self.assertFalse(
            changed["security_assessment"]["automatic_containment"]["enabled"]
        )

    def test_raw_endpoint_identifiers_are_keyed_and_never_persisted_or_returned(self):
        raw_id = "BluetoothLE#BluetoothLE00:11:22:33:44:55-private"
        inventory = self.inventory([provider(endpoint(raw_id))])
        result = inventory.check(include_os_metadata=True)

        self.assertNotIn(raw_id, json.dumps(result, sort_keys=True))
        with closing(sqlite3.connect(inventory.path)) as connection:
            row = connection.execute(
                "SELECT identity_sha256, device_id FROM bluetooth_devices"
            ).fetchone()
            all_text = " ".join(
                str(value)
                for table in (
                    "bluetooth_meta",
                    "bluetooth_devices",
                    "bluetooth_checks",
                    "bluetooth_observations",
                    "bluetooth_events",
                )
                for record in connection.execute(f"SELECT * FROM {table}").fetchall()
                for value in record
            )
        assert row is not None
        self.assertRegex(row[0], r"^[0-9a-f]{64}$")
        self.assertRegex(row[1], r"^[0-9a-f]{32}$")
        self.assertNotIn(raw_id, all_text)
        self.assertFalse(result["addresses_exposed"])

    def test_os_metadata_is_redacted_by_default_and_never_inferred(self):
        inventory = self.inventory([
            provider(endpoint(
                "endpoint-private",
                name="Alex's iPhone",
                manufacturer=None,
                model_name=None,
                present=None,
                connected=None,
            ))
        ])
        default = inventory.check()
        device = default["devices"][0]
        self.assertNotIn("os_reported_name", device)
        self.assertNotIn("manufacturer", device)
        self.assertNotIn("model_name", device)
        self.assertIsNone(device["device_type"])
        self.assertIsNone(device["present"])
        self.assertFalse(device["present_evidence_available"])
        self.assertIsNone(device["connected"])
        self.assertFalse(device["connected_evidence_available"])
        self.assertTrue(device["paired_now"])
        self.assertIn("IsPaired", device["paired_evidence"])

        explicit = inventory.status(include_os_metadata=True)["devices"][0]
        self.assertEqual(explicit["os_reported_name"], "Alex's iPhone")
        self.assertIsNone(explicit["manufacturer"])
        self.assertIsNone(explicit["model_name"])
        self.assertIn("user-authored", explicit["metadata_notice"])

    def test_unpaired_or_unproven_rows_are_dropped_and_limits_fail_closed(self):
        normalized = _normalize_provider_result(provider(
            endpoint("paired", paired=True),
            endpoint("unpaired", paired=False),
        ))
        self.assertEqual([row["raw_id"] for row in normalized["devices"]], ["paired"])

        too_many = provider(*(
            endpoint(f"endpoint-{index}")
            for index in range(MAX_BLUETOOTH_DEVICES + 1)
        ))
        with self.assertRaises(BluetoothInventoryError):
            _normalize_provider_result(too_many)

    def test_profile_is_local_metadata_and_retired_endpoint_gets_review_signal(self):
        item = endpoint("endpoint-one")
        inventory = self.inventory([provider(item), provider(item)])
        baseline = inventory.check()
        device_id = baseline["devices"][0]["device_id"]
        updated = inventory.set_profile(
            device_id,
            label="Old keyboard",
            trust_state="retired",
            device_type="keyboard",
        )
        self.assertEqual(updated["label"], "Old keyboard")
        self.assertFalse(updated["access_authorized"])
        self.assertFalse(updated["control_enabled"])

        self.clock.advance()
        result = inventory.check()
        signal = next(
            row
            for row in result["security_assessment"]["signals"]
            if row["rule_id"] == "retired_endpoint_paired"
        )
        self.assertEqual(signal["severity"], "high")
        self.assertFalse(signal["compromise_established"])

    def test_address_like_os_metadata_is_redacted_before_storage_and_render(self):
        raw_values = (
            "00:11:22:33:44:55",
            "AA-BB-CC-DD-EE-FF",
            "001122334455",
            "0011.2233.4455",
            "00 11 22 33 44 55",
            "192.168.50.8",
        )
        inventory = self.inventory([
            provider(endpoint(
                "private-raw-aep-id",
                name=f"Phone {raw_values[0]}",
                manufacturer=(
                    f"Vendor {raw_values[1]} / switch {raw_values[3]}"
                ),
                model_name=(
                    f"Model {raw_values[2]} near radio {raw_values[4]}"
                ),
                categories=[f"Phone {raw_values[5]}"],
            ))
        ])
        result = inventory.check(include_os_metadata=True)
        serialized = json.dumps(result, sort_keys=True)
        for raw in raw_values:
            self.assertNotIn(raw, serialized)
        self.assertIn("[redacted address]", serialized)
        self.assertEqual(result["metadata_address_redactions"], 1)
        self.assertFalse(result["addresses_exposed"])
        self.assertTrue(result["devices"][0]["metadata_address_redacted"])

        with closing(sqlite3.connect(inventory.path)) as connection:
            row = connection.execute(
                """
                SELECT os_name, manufacturer, model_name, categories_json
                FROM bluetooth_devices
                """
            ).fetchone()
        persisted = json.dumps(row)
        for raw in raw_values:
            self.assertNotIn(raw, persisted)
        self.assertIn("[redacted address]", persisted)

    def test_address_scrub_migration_covers_dotted_and_spaced_hardware_addresses(self):
        inventory = self.inventory([provider(endpoint("migration-endpoint"))])
        inventory.check(include_os_metadata=True)
        with closing(sqlite3.connect(inventory.path)) as connection:
            connection.execute(
                """
                UPDATE bluetooth_devices SET
                    os_name='Headset 0011.2233.4455',
                    manufacturer='Vendor 00 11 22 33 44 55',
                    model_name='Model 0011.2233.4455',
                    categories_json='["Radio 00 11 22 33 44 55"]',
                    metadata_address_redacted=0
                """
            )
            connection.execute(
                "UPDATE bluetooth_meta SET value='1' WHERE key='schema_version'"
            )
            connection.commit()

        reopened = BluetoothInventory(
            self.root,
            enumerator=lambda: provider(),
            clock=self.clock,
            min_check_interval_seconds=0,
        )
        status = reopened.status(include_os_metadata=True)
        serialized = json.dumps(status, sort_keys=True)
        self.assertNotIn("0011.2233.4455", serialized)
        self.assertNotIn("00 11 22 33 44 55", serialized)
        self.assertIn("[redacted address]", serialized)
        self.assertFalse(status["addresses_exposed"])
        with closing(sqlite3.connect(inventory.path)) as connection:
            stored = connection.execute(
                """
                SELECT os_name, manufacturer, model_name, categories_json,
                       metadata_address_redacted
                FROM bluetooth_devices
                """
            ).fetchone()
        stored_text = json.dumps(stored)
        self.assertNotIn("0011.2233.4455", stored_text)
        self.assertNotIn("00 11 22 33 44 55", stored_text)
        self.assertTrue(stored[-1])

    def test_future_schema_is_rejected_before_any_database_mutation(self):
        future_root = self.root / "future-schema"
        future_root.mkdir()
        database = future_root / "bluetooth-inventory.db"
        with closing(sqlite3.connect(database)) as connection:
            connection.executescript("""
                CREATE TABLE bluetooth_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                INSERT INTO bluetooth_meta(key, value)
                VALUES ('schema_version', '999');
                CREATE TABLE bluetooth_devices (
                    device_id TEXT PRIMARY KEY,
                    os_name TEXT
                );
                INSERT INTO bluetooth_devices(device_id, os_name)
                VALUES ('future-row', 'Headset 0011.2233.4455');
            """)
        before = {
            item.name: item.read_bytes()
            for item in future_root.iterdir()
            if item.is_file()
        }

        with self.assertRaisesRegex(
            BluetoothInventoryError,
            "schema is newer",
        ):
            BluetoothInventory(
                future_root,
                enumerator=lambda: provider(),
                clock=self.clock,
                min_check_interval_seconds=0,
            )

        after = {
            item.name: item.read_bytes()
            for item in future_root.iterdir()
            if item.is_file()
        }
        self.assertEqual(after, before)
        with closing(sqlite3.connect(database)) as connection:
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(bluetooth_devices)"
                ).fetchall()
            }
            value = connection.execute(
                "SELECT os_name FROM bluetooth_devices WHERE device_id='future-row'"
            ).fetchone()[0]
            schema = connection.execute(
                "SELECT value FROM bluetooth_meta WHERE key='schema_version'"
            ).fetchone()[0]
        self.assertEqual(columns, {"device_id", "os_name"})
        self.assertEqual(value, "Headset 0011.2233.4455")
        self.assertEqual(schema, "999")

    def test_unavailable_bluetooth_store_does_not_break_unrelated_tools(self):
        workspace = self.root / "workspace"
        data_dir = self.root / "tool-data"
        workspace.mkdir()
        data_dir.mkdir()
        (workspace / "ordinary.txt").write_text("available", encoding="utf-8")
        config = replace(
            Config.load(),
            workspace=workspace,
            data_dir=data_dir,
            bluetooth_access="paired-readonly",
        )

        with Memory(data_dir / "jarvis.db") as memory, patch(
            "jarvis.tools.BluetoothInventory",
            side_effect=BluetoothInventoryError(
                "Bluetooth database schema is newer than this Jarvis build"
            ),
        ):
            toolbox = ToolBox(config, memory)
            ordinary = json.loads(toolbox.execute("list_files", {"path": "."}))
            bluetooth = json.loads(toolbox.execute(
                "bluetooth_inventory", {"action": "status"}
            ))

        self.assertTrue(ordinary["ok"])
        self.assertIsNone(toolbox.bluetooth_inventory_store)
        self.assertFalse(bluetooth["ok"])
        self.assertIn("Paired Bluetooth inventory is unavailable", bluetooth["error"])

    def test_return_after_absence_is_not_classified_as_never_before_seen(self):
        item = endpoint("returning-endpoint")
        inventory = self.inventory([provider(item), provider(), provider(item)])
        baseline = inventory.check()
        device_id = baseline["devices"][0]["device_id"]
        self.clock.advance()
        inventory.check()
        self.clock.advance()
        returned = inventory.check()

        self.assertEqual(returned["new_endpoints"], 0)
        device = next(
            row for row in returned["devices"] if row["device_id"] == device_id
        )
        self.assertFalse(device["is_new"])
        self.assertNotIn(
            "new_unreviewed_paired_endpoint",
            {
                row["rule_id"]
                for row in returned["security_assessment"]["signals"]
            },
        )
        event_types = {
            row["event_type"]
            for row in inventory.events(device_id=device_id)["events"]
        }
        self.assertIn("paired_endpoint_returned", event_types)

    def test_stale_observation_is_not_described_as_current_or_used_for_trust_alert(self):
        item = endpoint("stale-endpoint", connected=True, present=True)
        inventory = self.inventory([provider(item)])
        baseline = inventory.check()
        device_id = baseline["devices"][0]["device_id"]
        inventory.set_profile(device_id, trust_state="watch")
        self.clock.advance(BLUETOOTH_OBSERVATION_FRESH_SECONDS + 1)

        stale = inventory.status()
        self.assertEqual(stale["freshness"]["state"], "stale")
        self.assertEqual(stale["paired_now"], 0)
        self.assertEqual(stale["paired_in_last_check"], 1)
        device = stale["devices"][0]
        self.assertFalse(device["paired_now"])
        self.assertTrue(device["paired_in_last_check"])
        self.assertIsNone(device["present"])
        self.assertTrue(device["present_in_last_check"])
        self.assertIsNone(device["connected"])
        self.assertTrue(device["connected_in_last_check"])
        rules = {
            row["rule_id"] for row in stale["security_assessment"]["signals"]
        }
        self.assertIn("observation_stale", rules)
        self.assertNotIn("watched_endpoint_paired", rules)

    def test_watch_or_retired_label_requires_a_later_observation(self):
        item = endpoint("profile-timing-endpoint")
        inventory = self.inventory([provider(item), provider(item), provider(item)])
        baseline = inventory.check()
        device_id = baseline["devices"][0]["device_id"]
        inventory.set_profile(device_id, trust_state="retired")

        before_new_check = inventory.status()
        self.assertNotIn(
            "retired_endpoint_paired",
            {
                row["rule_id"]
                for row in before_new_check["security_assessment"]["signals"]
            },
        )
        equal_time_check = inventory.check()
        self.assertNotIn(
            "retired_endpoint_paired",
            {
                row["rule_id"]
                for row in equal_time_check["security_assessment"]["signals"]
            },
        )
        self.clock.advance(1)
        later_check = inventory.check()
        self.assertIn(
            "retired_endpoint_paired",
            {
                row["rule_id"]
                for row in later_check["security_assessment"]["signals"]
            },
        )

    def test_pending_discovery_survives_restart_and_suppresses_baseline_and_return(self):
        first = endpoint("private-baseline-aep", name="Existing keyboard")
        added = endpoint("private-new-aep", name="New headset")
        inventory = self.inventory([
            provider(first),
            provider(first, added),
            provider(),
            provider(added),
        ])

        inventory.check()
        self.assertEqual(inventory.pending_alerts()["pending_count"], 0)
        self.clock.advance()
        inventory.check()
        pending = inventory.pending_alerts()
        self.assertEqual(pending["pending_count"], 1)
        self.assertEqual(
            pending["alerts"][0]["event_type"],
            "new_paired_endpoint_observed",
        )
        serialized = json.dumps(pending, sort_keys=True)
        self.assertNotIn("private-baseline-aep", serialized)
        self.assertNotIn("private-new-aep", serialized)
        self.assertFalse(pending["addresses_exposed"])

        reopened = BluetoothInventory(
            self.root,
            enumerator=lambda: provider(),
            clock=self.clock,
            min_check_interval_seconds=0,
        )
        self.assertEqual(reopened.pending_alerts()["alerts"], pending["alerts"])
        alert = pending["alerts"][0]
        inventory.acknowledge_alert(
            event_id=alert["event_id"],
            receipt_id=alert["receipt_id"],
        )
        self.clock.advance()
        inventory.check()
        self.clock.advance()
        inventory.check()
        self.assertEqual(inventory.pending_alerts()["pending_count"], 0)
        self.assertIn(
            "paired_endpoint_returned",
            {row["event_type"] for row in inventory.events()["events"]},
        )

    def test_alert_acknowledgement_is_exact_idempotent_and_non_replaying(self):
        item = endpoint("private-alert-aep")
        inventory = self.inventory([provider(), provider(item)])
        inventory.check()
        self.clock.advance()
        inventory.check()
        alert = inventory.pending_alerts()["alerts"][0]

        wrong_receipt = "0" * 32
        if wrong_receipt == alert["receipt_id"]:
            wrong_receipt = "1" * 32
        with self.assertRaises(KeyError):
            inventory.acknowledge_alert(
                event_id=alert["event_id"],
                receipt_id=wrong_receipt,
            )
        self.assertEqual(inventory.pending_alerts()["pending_count"], 1)
        first = inventory.acknowledge_alert(
            event_id=alert["event_id"],
            receipt_id=alert["receipt_id"],
        )
        self.assertTrue(first["changed"])
        self.assertEqual(inventory.pending_alerts()["pending_count"], 0)

        self.clock.advance()
        reopened = BluetoothInventory(
            self.root,
            enumerator=lambda: provider(),
            clock=self.clock,
            min_check_interval_seconds=0,
        )
        duplicate = reopened.acknowledge_alert(
            event_id=alert["event_id"],
            receipt_id=alert["receipt_id"],
        )
        self.assertFalse(duplicate["changed"])
        self.assertEqual(duplicate["acknowledged_at"], first["acknowledged_at"])
        self.assertEqual(reopened.pending_alerts()["pending_count"], 0)
        for invalid in ("A" * 32, "0" * 31, "0" * 33, "../receipt"):
            with self.assertRaises(ValueError):
                reopened.acknowledge_alert(
                    event_id=alert["event_id"],
                    receipt_id=invalid,
                )

    def test_pending_alert_capacity_and_ttl_expire_explicitly(self):
        a = endpoint("capacity-a")
        b = endpoint("capacity-b")
        c = endpoint("capacity-c")
        inventory = self.inventory([
            provider(),
            provider(a),
            provider(a, b),
            provider(a, b, c),
        ])
        with patch(
            "jarvis.bluetooth_inventory.MAX_PENDING_BLUETOOTH_ALERTS", 2
        ):
            for _ in range(4):
                inventory.check()
                self.clock.advance()
            pending = inventory.pending_alerts()
        self.assertEqual(pending["pending_count"], 2)
        with closing(sqlite3.connect(inventory.path)) as connection:
            capacity = connection.execute(
                """
                SELECT COUNT(*) FROM bluetooth_alert_receipts
                WHERE state='expired' AND resolution_reason='capacity'
                """
            ).fetchone()[0]
        self.assertEqual(capacity, 1)

        self.clock.advance(31 * 24 * 60 * 60)
        expired = inventory.pending_alerts()
        self.assertEqual(expired["pending_count"], 0)
        with closing(sqlite3.connect(inventory.path)) as connection:
            ttl = connection.execute(
                """
                SELECT COUNT(*) FROM bluetooth_alert_receipts
                WHERE state='expired' AND resolution_reason='ttl'
                """
            ).fetchone()[0]
        self.assertEqual(ttl, 2)

    def test_provider_failure_does_not_create_a_false_clean_baseline(self):
        def fail() -> dict[str, object]:
            raise RuntimeError("private provider detail")

        inventory = BluetoothInventory(
            self.root,
            enumerator=fail,
            clock=self.clock,
            min_check_interval_seconds=0,
        )
        with self.assertRaisesRegex(BluetoothInventoryError, "provider failed"):
            inventory.check()
        status = inventory.status()
        self.assertIsNone(status["last_check_at"])
        self.assertEqual(
            status["security_assessment"]["signals"][0]["rule_id"],
            "observation_not_established",
        )
        with closing(sqlite3.connect(inventory.path)) as connection:
            row = connection.execute(
                "SELECT status, error_code FROM bluetooth_checks"
            ).fetchone()
        self.assertEqual(row, ("failed", "provider_failed"))

    def test_cross_process_lease_blocks_concurrent_check(self):
        entered = threading.Event()
        release = threading.Event()
        failures: list[BaseException] = []

        def slow_provider() -> dict[str, object]:
            entered.set()
            release.wait(5)
            return provider(endpoint("slow-endpoint"))

        first = BluetoothInventory(
            self.root,
            enumerator=slow_provider,
            clock=self.clock,
            min_check_interval_seconds=0,
        )
        second = BluetoothInventory(
            self.root,
            enumerator=lambda: provider(),
            clock=self.clock,
            min_check_interval_seconds=0,
        )

        def run_first() -> None:
            try:
                first.check()
            except BaseException as exc:  # pragma: no cover - surfaced below
                failures.append(exc)

        worker = threading.Thread(target=run_first)
        worker.start()
        self.assertTrue(entered.wait(2))
        try:
            with self.assertRaises(BluetoothInventoryRateLimited):
                second.check()
        finally:
            release.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])

    def test_fixed_windows_script_contains_no_pairing_control_or_rf_watcher(self):
        lowered = _WINDOWS_PAIRED_BLUETOOTH_SCRIPT.casefold()
        self.assertIn("getdeviceselectorfrompairingstate", lowered)
        for forbidden in (
            "pairasync", "unpairasync", "bluetoothleadvertisementwatcher",
            "gattcharacteristic", "writevalueasync", "radio.setstateasync",
        ):
            self.assertNotIn(forbidden, lowered)


class BluetoothToolTests(unittest.TestCase):
    def setUp(self) -> None:
        TEMP_ROOT.mkdir(exist_ok=True)
        self.root = TEMP_ROOT / f"bluetooth-tool-{os.getpid()}-{self._testMethodName}"
        if self.root.exists():
            shutil.rmtree(self.root)
        self.workspace = self.root / "workspace"
        self.data = self.root / "data"
        self.workspace.mkdir(parents=True)
        self.data.mkdir()
        self.memory = Memory(self.data / "memory.db")
        self.config = replace(
            Config.load(),
            workspace=self.workspace,
            data_dir=self.data,
            bluetooth_access="disabled",
            network_access="disabled",
            memory_embeddings="disabled",
            vault_dir=None,
        )

    def tearDown(self) -> None:
        self.memory.close()
        resolved = self.root.resolve()
        self.assertEqual(resolved.parent, TEMP_ROOT.resolve())
        shutil.rmtree(resolved)

    def test_tool_is_disabled_by_default_and_exposes_only_readonly_endpoint_actions(self):
        disabled = ToolBox(self.config, self.memory)
        self.assertNotIn("bluetooth_inventory", disabled.tools)

        with patch("jarvis.tools.BluetoothInventory") as inventory_type:
            store = inventory_type.return_value
            store.status.return_value = {"devices": []}
            store.check.return_value = {"devices": []}
            store.list_devices.return_value = {"devices": []}
            store.device_detail.return_value = {"device": {"device_id": "abc"}}
            store.events.return_value = {"events": []}
            store.set_profile.return_value = {
                "device_id": "abc",
                "control_enabled": False,
            }
            enabled = ToolBox(
                replace(self.config, bluetooth_access="paired-readonly"),
                self.memory,
            )
            self.assertIn("bluetooth_inventory", enabled.tools)
            actions = set(
                enabled.tools["bluetooth_inventory"]
                .parameters["properties"]["action"]["enum"]
            )
            self.assertEqual(
                actions, {"status", "check", "list", "detail", "history", "profile"}
            )
            self.assertNotIn("pair", actions)
            self.assertNotIn("connect", actions)
            self.assertNotIn("scan_nearby", actions)

            enabled.bluetooth_inventory("status")
            store.status.assert_called_once_with(include_os_metadata=False)
            enabled.bluetooth_inventory("check", include_os_metadata=True)
            store.check.assert_called_once_with(include_os_metadata=True)
            enabled.bluetooth_inventory("detail", device_id="abc", event_limit=8)
            store.device_detail.assert_called_once_with(
                "abc", event_limit=8, include_os_metadata=False
            )
            profile = enabled.bluetooth_inventory(
                "profile",
                device_id="abc",
                label="Headset",
                trust_state="recognized",
            )
            self.assertTrue(profile["operator_metadata_only"])
            self.assertFalse(profile["authority_added"])
            self.assertFalse(profile["access_granted"])
            self.assertFalse(profile["control_enabled"])

    def test_bluetooth_routing_is_explicit_and_meta_or_negated_text_is_inert(self):
        for prompt in (
            "What Bluetooth devices are paired right now?",
            "Check whether my headphones are connected over Bluetooth",
            "Show my saved Bluetooth device inventory",
        ):
            with self.subTest(prompt=prompt):
                self.assertTrue(_requests_bluetooth_inventory(prompt))
                tools, reason = _required_effect_tools(
                    prompt, requires_coding=False, allow_external_mutation=False
                )
                self.assertEqual(tools, frozenset({"bluetooth_inventory"}))
                self.assertIn("Bluetooth", str(reason))
        for prompt in (
            "Do not check Bluetooth devices; explain how Bluetooth works.",
            'Explain the phrase "show my paired Bluetooth devices".',
            "Rewrite: what Bluetooth devices are connected?",
            "Here is an example: `check my Bluetooth devices`.",
        ):
            with self.subTest(prompt=prompt):
                self.assertFalse(_requests_bluetooth_inventory(prompt))
        self.assertTrue(_requests_fresh_bluetooth_inventory(
            "What Bluetooth devices are paired right now?"
        ))
        self.assertFalse(_requests_fresh_bluetooth_inventory(
            "Show my saved Bluetooth device inventory"
        ))
        self.assertTrue(_requests_bluetooth_metadata(
            "Check the model and manufacturer of my Bluetooth devices"
        ))
        for prompt in (
            "Tell me the Bluetooth device name",
            "What type is my Bluetooth device?",
            "Show details for my Bluetooth accessory",
            "Tell me if the Bluetooth device label says headset",
            "Can you mark whether my Bluetooth device is connected?",
        ):
            with self.subTest(read_only_prompt=prompt):
                self.assertFalse(_requests_bluetooth_profile_update(prompt))
        for prompt in (
            "Rename this Bluetooth device to Office keyboard",
            "Set the Bluetooth endpoint trust state to recognized",
            "Bluetooth device: label it Desk headset",
            "Mark this Bluetooth accessory as watch",
        ):
            with self.subTest(profile_prompt=prompt):
                self.assertTrue(_requests_bluetooth_profile_update(prompt))

    def test_agent_forces_saved_read_and_metadata_boundaries(self):
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
                                "name": "bluetooth_inventory",
                                "arguments": {
                                    "action": "check",
                                    "include_os_metadata": True,
                                },
                            },
                        }],
                    ),
                    Response(role="assistant", content="Saved paired-device evidence reviewed."),
                ]

            def models(self, refresh=True):
                return ["qwen3.5:9b"]

            def chat(self, messages, tools, model, context_length, **kwargs):
                return self.responses.pop(0)

        class Box:
            schemas = [{
                "type": "function",
                "function": {
                    "name": "bluetooth_inventory",
                    "description": "paired Bluetooth inventory",
                    "parameters": {"type": "object", "properties": {}},
                },
            }]

            def __init__(self):
                self.calls = []

            def execute(self, name, arguments):
                self.calls.append((name, arguments))
                return json.dumps({"ok": True, "result": {"devices": []}})

        box = Box()
        agent = Agent(
            replace(self.config, bluetooth_access="paired-readonly", max_steps=4),
            self.memory,
            client=Client(),
            coding_review=False,
            coding_planning=False,
        )
        agent.toolbox = box
        result = agent.run("Show my saved Bluetooth device inventory")
        self.assertEqual(result.status, "complete", result.reason)
        self.assertEqual(len(box.calls), 1)
        self.assertEqual(box.calls[0][1]["action"], "status")
        self.assertIs(box.calls[0][1]["include_os_metadata"], False)

    def test_current_paired_question_uses_deterministic_read(self):
        class Client:
            def models(self, refresh=True):
                return ["qwen3.5:9b"]

            def chat(self, *args, **kwargs):
                raise AssertionError("fresh Bluetooth inventory must not rely on a model")

        class Box:
            schemas = [{
                "type": "function",
                "function": {
                    "name": "bluetooth_inventory",
                    "description": "paired Bluetooth inventory",
                    "parameters": {"type": "object", "properties": {}},
                },
            }]

            def __init__(self):
                self.calls = []

            def execute(self, name, arguments):
                self.calls.append((name, arguments))
                return json.dumps({
                    "ok": True,
                    "result": {
                        "last_check_at": "2026-08-28T12:00:00+00:00",
                        "paired_now": 1,
                        "devices": [{
                            "device_id": "a" * 32,
                            "display_name": "Bluetooth endpoint aaaaaa",
                            "transports": ["classic"],
                            "paired_now": True,
                            "connected_evidence_available": False,
                        }],
                    },
                })

        box = Box()
        agent = Agent(
            replace(self.config, bluetooth_access="paired-readonly", max_steps=4),
            self.memory,
            client=Client(),
            coding_review=False,
            coding_planning=False,
        )
        agent.toolbox = box
        result = agent.run("What Bluetooth devices are paired right now?")
        self.assertEqual(result.status, "complete", result.reason)
        self.assertEqual(box.calls[0][1]["action"], "check")
        self.assertIs(box.calls[0][1]["include_os_metadata"], False)
        self.assertIn("connection evidence unavailable", str(result))
        self.assertIn("did not scan nearby radios", str(result))


if __name__ == "__main__":
    unittest.main()
