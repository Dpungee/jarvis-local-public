from __future__ import annotations

import copy
import json
import os
import shutil
import sqlite3
import threading
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jarvis.network_inventory import (
    NETWORK_INVENTORY_SCHEMA_VERSION,
    NetworkInventory,
    NetworkInventoryError,
    NetworkInventoryRateLimited,
)
from jarvis.network_defense import verify_assessment_receipt


TEMP_ROOT = Path(__file__).resolve().parent / ".tmp"
TEMP_ROOT.mkdir(exist_ok=True)


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class MutableDiscovery:
    def __init__(self, value: dict) -> None:
        self.value = value

    def __call__(self, _max_hosts: int) -> dict:
        return copy.deepcopy(self.value)


def interface(
    *,
    index: int = 7,
    guid: str = "{11111111-2222-3333-4444-555555555555}",
    alias: str = "Wi-Fi",
    address: str = "192.168.50.2",
    prefix_length: int = 24,
    gateway_ipv4: str = "192.168.50.1",
    gateway_mac: str = "00-11-22-33-44-55",
    adapter_mac: str = "00-AA-BB-CC-DD-EE",
    hardware_interface: bool = True,
) -> dict:
    return {
        "interface_index": index,
        "interface_guid": guid,
        "interface_alias": alias,
        "address": address,
        "prefix_length": prefix_length,
        "scan_range": f"{address.rsplit('.', 1)[0]}.0/{prefix_length}",
        "scan_cidr": f"{address.rsplit('.', 1)[0]}.0/{prefix_length}",
        "gateway": gateway_ipv4,
        "gateway_ipv4": gateway_ipv4,
        "gateway_mac": gateway_mac,
        "mac": adapter_mac,
        "adapter_mac": adapter_mac,
        "hardware_interface": hardware_interface,
        "network_category": "Private",
        "profile_name": "Owned test LAN",
    }


def observation(
    ipv4: str,
    mac: str | None,
    *,
    interface_index: int = 7,
    hostname: str | None = None,
    visibility: str = "active",
    neighbor_state: str = "Reachable",
) -> dict:
    return {
        "interface_index": interface_index,
        "ipv4": ipv4,
        "mac": mac,
        "hostname": hostname,
        "visibility": visibility,
        "neighbor_state": neighbor_state,
        "actively_reachable": visibility in {"active", "active_probe", "local_host"},
        "cached": visibility == "neighbor_cache",
    }


def discovery(*observations: dict, interfaces: list[dict] | None = None) -> dict:
    return {
        "interfaces": copy.deepcopy(interfaces or [interface()]),
        "observations": [copy.deepcopy(item) for item in observations],
        "candidate_hosts": 254,
        "range_truncated": False,
        "method": "deterministic safety test",
    }


class NetworkInventorySafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.test_dir = TEMP_ROOT / f"network-safety-{os.getpid()}-{self._testMethodName}"
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir)
        self.test_dir.mkdir()
        self.clock = MutableClock(datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc))
        self.discovery = MutableDiscovery(discovery())
        self.inventory = NetworkInventory(
            self.test_dir,
            discoverer=self.discovery,
            clock=self.clock,
            min_scan_interval_seconds=0,
            max_scans_per_hour=0,
        )

    def tearDown(self) -> None:
        resolved = self.test_dir.resolve()
        self.assertEqual(resolved.parent, TEMP_ROOT.resolve())
        shutil.rmtree(resolved)

    def pair(self, inventory: NetworkInventory | None = None) -> dict:
        target = inventory or self.inventory
        scope = target.pair_scope(
            interface_index=7,
            owns_or_administers=True,
            display_name="Owned test LAN",
        )
        self.assertTrue(scope.get("scope_id"))
        return scope

    def test_scan_requires_an_operator_paired_scope(self) -> None:
        with self.assertRaises(NetworkInventoryError):
            self.inventory.scan()
        with self.assertRaises((NetworkInventoryError, PermissionError, ValueError)):
            self.inventory.pair_scope(
                interface_index=7,
                owns_or_administers=False,
                display_name="Not authorized",
            )
        self.assertEqual(self.inventory.list_scopes()["scopes"], [])

    def test_future_schema_is_rejected_before_any_database_mutation(self) -> None:
        future_dir = self.test_dir / "future-schema"
        future_dir.mkdir()
        path = future_dir / "network-inventory.db"
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("CREATE TABLE future_only(value TEXT)")
            connection.execute("INSERT INTO future_only(value) VALUES ('preserve-me')")
            connection.execute(
                f"PRAGMA user_version={NETWORK_INVENTORY_SCHEMA_VERSION + 1}"
            )
            connection.commit()
        before = path.read_bytes()
        with self.assertRaisesRegex(NetworkInventoryError, "newer"):
            NetworkInventory(
                future_dir,
                discoverer=self.discovery,
                clock=self.clock,
                min_scan_interval_seconds=0,
                max_scans_per_hour=0,
            )
        self.assertEqual(path.read_bytes(), before)
        with closing(sqlite3.connect(path)) as connection:
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0],
                NETWORK_INVENTORY_SCHEMA_VERSION + 1,
            )
            self.assertEqual(
                connection.execute("SELECT value FROM future_only").fetchone()[0],
                "preserve-me",
            )
        self.assertFalse(path.with_name(path.name + "-wal").exists())
        self.assertFalse(path.with_name(path.name + "-shm").exists())

    def test_scope_is_revalidated_against_exact_adapter_cidr_and_gateway_mac(self) -> None:
        mutations = {
            "adapter index": {"interface_index": 12},
            "adapter GUID": {"interface_guid": "{AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE}"},
            "CIDR": {
                "address": "192.168.51.2",
                "scan_range": "192.168.51.0/24",
                "scan_cidr": "192.168.51.0/24",
            },
            "gateway IPv4": {"gateway": "192.168.50.254", "gateway_ipv4": "192.168.50.254"},
            "gateway MAC": {"gateway_mac": "00-11-22-33-44-99"},
        }
        for label, changes in mutations.items():
            with self.subTest(label=label):
                case_dir = self.test_dir / label.replace(" ", "-")
                case_dir.mkdir()
                source = MutableDiscovery(discovery())
                inventory = NetworkInventory(
                    case_dir,
                    discoverer=source,
                    clock=self.clock,
                    min_scan_interval_seconds=0,
                    max_scans_per_hour=0,
                )
                scope = self.pair(inventory)
                changed = interface()
                changed.update(changes)
                source.value = discovery(interfaces=[changed])
                with self.assertRaises(NetworkInventoryError):
                    inventory.scan(scope_id=scope["scope_id"])

    def test_virtual_public_and_wrong_interface_data_are_rejected(self) -> None:
        public_adapter = interface(
            index=8,
            guid="{88888888-2222-3333-4444-555555555555}",
            address="8.8.8.8",
            gateway_ipv4="8.8.8.1",
            gateway_mac="00-11-22-33-44-88",
        )
        virtual_adapter = interface(
            index=9,
            guid="{99999999-2222-3333-4444-555555555555}",
            alias="Virtual Switch",
            address="192.168.90.2",
            gateway_ipv4="192.168.90.1",
            gateway_mac="00-11-22-33-44-90",
            hardware_interface=False,
        )
        self.discovery.value = discovery(
            observation("192.168.50.20", "10-20-30-40-50-60", hostname="valid"),
            observation(
                "192.168.50.21", "10-20-30-40-50-61", interface_index=9,
                hostname="wrong-interface",
            ),
            observation("192.168.60.22", "10-20-30-40-50-62", hostname="wrong-cidr"),
            observation("8.8.8.8", "10-20-30-40-50-63", interface_index=8),
            interfaces=[interface(), public_adapter, virtual_adapter],
        )
        candidates = self.inventory.scope_candidates()["candidates"]
        by_index = {item["interface_index"]: item for item in candidates}
        self.assertTrue(by_index[7]["eligible"])
        # Ineligible adapters may be omitted completely or returned explicitly as
        # ineligible; neither representation may make them pairable.
        self.assertFalse(by_index.get(8, {}).get("eligible", False))
        self.assertFalse(by_index.get(9, {}).get("eligible", False))

        scope = self.pair()
        result = self.inventory.scan(
            scope_id=scope["scope_id"], include_identifiers=True
        )
        self.assertEqual([item["ipv4"] for item in result["devices"]], ["192.168.50.20"])

    def test_cache_only_observation_is_never_online_and_never_extends_continuity(self) -> None:
        active = observation("192.168.50.30", "10-20-30-40-50-70")
        cached = observation(
            "192.168.50.30",
            "10-20-30-40-50-70",
            visibility="neighbor_cache",
            neighbor_state="Stale",
        )
        self.discovery.value = discovery(active)
        scope = self.pair()
        first = self.inventory.scan(scope_id=scope["scope_id"], include_identifiers=True)
        self.assertTrue(first["devices"][0]["visible_now"])

        self.clock.value += timedelta(minutes=1)
        self.discovery.value = discovery(cached)
        second = self.inventory.scan(scope_id=scope["scope_id"], include_identifiers=True)
        self.assertFalse(second["devices"][0]["visible_now"])
        self.assertIsNone(second["devices"][0]["continuous_visible_seconds"])

        self.clock.value += timedelta(minutes=1)
        third = self.inventory.scan(scope_id=scope["scope_id"], include_identifiers=True)
        self.assertFalse(third["devices"][0]["visible_now"])
        self.assertIsNone(third["devices"][0]["continuous_visible_seconds"])

    def test_non_unicast_and_out_of_scope_addresses_are_never_persisted(self) -> None:
        rejected = (
            "8.8.8.8",
            "169.254.1.5",
            "224.0.0.1",
            "255.255.255.255",
            "192.168.50.255",
        )
        self.discovery.value = discovery(
            observation("192.168.50.40", "10-20-30-40-50-80"),
            *(observation(address, f"10-20-30-40-50-{index:02X}") for index, address in enumerate(rejected, 1)),
        )
        scope = self.pair()
        result = self.inventory.scan(
            scope_id=scope["scope_id"], include_identifiers=True
        )
        self.assertEqual([item["ipv4"] for item in result["devices"]], ["192.168.50.40"])
        stored = json.dumps(
            self.inventory.list_devices(include_identifiers=True), sort_keys=True
        )
        for address in rejected:
            self.assertNotIn(address, stored)

    def test_multicast_mac_is_rejected_and_local_mac_is_explicitly_unstable(self) -> None:
        self.discovery.value = discovery(
            observation("192.168.50.41", "01-00-5E-00-00-01"),
            observation("192.168.50.42", "02-11-22-33-44-55"),
        )
        scope = self.pair()
        result = self.inventory.scan(
            scope_id=scope["scope_id"], include_identifiers=True
        )
        by_ip = {item["ipv4"]: item for item in result["devices"]}
        self.assertIsNone(by_ip["192.168.50.41"]["mac"])
        local = by_ip["192.168.50.42"]
        self.assertEqual(local["identity_confidence"], "limited")
        self.assertEqual(
            local["identity_basis"],
            "locally administered MAC address (may be randomized or spoofed)",
        )

    def test_stale_ip_only_record_does_not_transfer_identity_or_profile(self) -> None:
        self.discovery.value = discovery(
            observation("192.168.50.47", None, hostname="first-occupant")
        )
        scope = self.pair()
        first = self.inventory.scan(scope_id=scope["scope_id"])
        first_id = first["devices"][0]["device_id"]
        self.inventory.set_profile(
            first_id, label="Old device", trust_state="recognized"
        )

        self.clock.value += timedelta(minutes=16)
        self.discovery.value = discovery(
            observation("192.168.50.47", None, hostname="later-occupant")
        )
        second = self.inventory.scan(scope_id=scope["scope_id"])
        current = next(item for item in second["devices"] if item["visible_now"])
        self.assertNotEqual(current["device_id"], first_id)
        self.assertIsNone(current["label"])
        self.assertEqual(current["trust_state"], "unreviewed")

    def test_observation_never_auto_upgrades_trust_or_access(self) -> None:
        device = observation("192.168.50.43", "10-20-30-40-50-83")
        self.discovery.value = discovery(device)
        scope = self.pair()
        first = self.inventory.scan(scope_id=scope["scope_id"])
        self.clock.value += timedelta(seconds=1)
        second = self.inventory.scan(scope_id=scope["scope_id"])
        current = second["devices"][0]
        self.assertEqual(current["trust_state"], "unreviewed")
        self.assertFalse(current["enrolled"])
        self.assertFalse(current["access_authorized"])
        self.assertEqual(first["devices"][0]["device_id"], current["device_id"])

    def test_profile_label_and_allowed_trust_state_never_grant_authority(self) -> None:
        self.discovery.value = discovery(
            observation("192.168.50.44", "10-20-30-40-50-84")
        )
        scope = self.pair()
        device_id = self.inventory.scan(scope_id=scope["scope_id"])["devices"][0]["device_id"]
        profile = self.inventory.set_profile(
            device_id,
            label="enrolled administrator control node",
            trust_state="recognized",
            device_type="server",
        )
        self.assertEqual(profile["trust_state"], "recognized")
        self.assertFalse(profile["enrolled"])
        self.assertFalse(profile["access_authorized"])
        for forbidden in ("enrolled", "authorized", "trusted"):
            with self.subTest(trust_state=forbidden):
                with self.assertRaises(ValueError):
                    self.inventory.set_profile(device_id, trust_state=forbidden)

    def test_raw_identifiers_are_absent_unless_explicitly_requested(self) -> None:
        ip = "192.168.50.45"
        mac = "10:20:30:40:50:85"
        hostname = "private-bedroom-device"
        self.discovery.value = discovery(observation(ip, mac, hostname=hostname))
        scope = self.pair()
        redacted = self.inventory.scan(scope_id=scope["scope_id"])
        encoded = json.dumps(redacted, sort_keys=True)
        for secret in (ip, mac, mac.replace(":", "-"), hostname):
            self.assertNotIn(secret, encoded)

        detailed = self.inventory.device_detail(
            redacted["devices"][0]["device_id"], include_identifiers=False
        )
        local_only_views = (
            detailed,
            self.inventory.list_devices(include_identifiers=False),
            self.inventory.status(include_identifiers=False),
            self.inventory.events(include_identifiers=False),
        )
        for view in local_only_views:
            view_text = json.dumps(view, sort_keys=True)
            for secret in (ip, mac, mac.replace(":", "-"), hostname):
                self.assertNotIn(secret, view_text)
        explicit = self.inventory.list_devices(include_identifiers=True)
        explicit_text = json.dumps(explicit, sort_keys=True)
        self.assertIn(ip, explicit_text)
        self.assertIn(mac, explicit_text)
        self.assertIn(hostname, explicit_text)

    def test_security_receipt_survives_restart_without_network_activity(self) -> None:
        ip = "192.168.50.48"
        mac = "10-20-30-40-50-88"
        hostname = "private-security-receipt"
        self.discovery.value = discovery(observation(ip, mac, hostname=hostname))
        scope = self.pair()
        completed = self.inventory.scan(scope_id=scope["scope_id"])
        original = completed["security_assessment"]

        discovery_calls: list[int] = []

        def forbidden_discovery(max_hosts: int) -> dict:
            discovery_calls.append(max_hosts)
            raise AssertionError("reading a security receipt must not scan the network")

        reopened = NetworkInventory(
            self.test_dir,
            discoverer=forbidden_discovery,
            clock=self.clock,
            min_scan_interval_seconds=0,
            max_scans_per_hour=0,
        )
        restored = reopened.security_assessment()
        history = reopened.security_assessment_history()
        self.assertEqual(restored["assessment_id"], original["assessment_id"])
        self.assertEqual(len(history["assessments"]), 1)
        self.assertEqual(history["verified_receipts"], 1)
        self.assertEqual(history["integrity_failures"], [])
        self.assertTrue(verify_assessment_receipt(history["assessments"][0]))
        self.assertFalse(history["network_activity_performed"])
        self.assertEqual(discovery_calls, [])
        encoded = json.dumps(history, sort_keys=True)
        for private_value in (ip, mac, mac.replace("-", ":"), hostname):
            self.assertNotIn(private_value, encoded)

    def test_new_device_incident_is_durable_exact_and_non_disruptive(self) -> None:
        self.discovery.value = discovery(
            observation("192.168.50.70", "10-20-30-40-50-A0", hostname="baseline")
        )
        scope = self.pair()
        baseline = self.inventory.scan(scope_id=scope["scope_id"])
        self.assertTrue(baseline["security_summary"]["baseline_created"])
        self.assertEqual(self.inventory.pending_incidents()["pending_count"], 0)

        self.clock.value += timedelta(minutes=1)
        self.discovery.value = discovery(
            observation("192.168.50.70", "10-20-30-40-50-A0", hostname="baseline"),
            observation("192.168.50.71", "10-20-30-40-50-A1", hostname="new-phone"),
        )
        completed = self.inventory.scan(scope_id=scope["scope_id"])
        pending = self.inventory.pending_incidents(include_identifiers=True)
        self.assertEqual(pending["pending_count"], 1)
        self.assertEqual(len(pending["incidents"]), 1)
        alert = pending["incidents"][0]
        self.assertEqual(alert["severity"], "medium")
        self.assertEqual(alert["category"], "asset_change")
        self.assertFalse(alert["compromise_established"])
        self.assertEqual(alert["automatic_actions"], [])
        self.assertGreaterEqual(len(alert["actions_not_taken"]), 3)
        self.assertTrue(alert["device"]["display_name"].startswith("Observed device "))
        self.assertNotIn("hostname", alert["device"])
        self.assertNotIn("ipv4", alert["device"])
        self.assertNotIn("mac", alert["device"])
        self.assertIn("not proof", alert["assessment"])
        self.assertEqual(
            alert["assessment_id"], completed["security_assessment"]["assessment_id"]
        )
        redacted = self.inventory.pending_incidents()
        self.assertNotIn("hostname", redacted["incidents"][0]["device"])
        self.assertNotIn("ipv4", redacted["incidents"][0]["device"])
        self.assertNotIn("mac", redacted["incidents"][0]["device"])
        self.assertNotIn("new-phone", json.dumps(pending, sort_keys=True))

        annotated = self.inventory.record_incident_actions(
            incident_id=alert["incident_id"],
            actions=[{
                "tool_id": "netstat-flow",
                "title": "Reviewed current connection metadata",
                "status": "completed",
                "receipt_id": "a" * 32,
            }],
        )
        self.assertEqual(len(annotated["automatic_actions"]), 1)
        self.assertEqual(
            annotated["automatic_actions"][0]["outcome"],
            "Passive read-only check completed.",
        )
        self.assertFalse(annotated["receipt_authoritative_for_containment"])
        # Annotation is idempotent and arbitrary process output cannot be stored.
        same = self.inventory.record_incident_actions(
            incident_id=alert["incident_id"],
            actions=[{
                "tool_id": "tasklist-endpoint",
                "title": "This must not replace the first receipt",
                "status": "failed",
                "receipt_id": "b" * 32,
            }],
        )
        self.assertEqual(same["automatic_actions"], annotated["automatic_actions"])
        with self.assertRaises(ValueError):
            self.inventory.record_incident_actions(
                incident_id=alert["incident_id"],
                actions=[{
                    "tool_id": "netstat-flow",
                    "title": "bad",
                    "status": "completed; include stdout",
                    "receipt_id": "c" * 32,
                }],
            )

        reopened = NetworkInventory(
            self.test_dir,
            discoverer=lambda _max_hosts: (_ for _ in ()).throw(
                AssertionError("reading a pending incident must not scan")
            ),
            clock=self.clock,
            min_scan_interval_seconds=0,
            max_scans_per_hour=0,
        )
        restored = reopened.pending_incidents(include_identifiers=True)
        self.assertEqual(
            restored["incidents"][0]["automatic_actions"],
            annotated["automatic_actions"],
        )
        wrong_receipt = "0" * 32
        if wrong_receipt == alert["receipt_id"]:
            wrong_receipt = "1" * 32
        with self.assertRaises(KeyError):
            reopened.acknowledge_incident(
                incident_id=alert["incident_id"], receipt_id=wrong_receipt
            )
        self.assertEqual(reopened.pending_incidents()["pending_count"], 1)
        first = reopened.acknowledge_incident(
            incident_id=alert["incident_id"], receipt_id=alert["receipt_id"]
        )
        self.assertTrue(first["changed"])
        duplicate = reopened.acknowledge_incident(
            incident_id=alert["incident_id"], receipt_id=alert["receipt_id"]
        )
        self.assertFalse(duplicate["changed"])
        self.assertEqual(reopened.pending_incidents()["pending_count"], 0)

    def test_disabled_incident_mode_records_inventory_without_alerts(self) -> None:
        disabled_dir = self.test_dir / "disabled-incidents"
        inventory = NetworkInventory(
            disabled_dir,
            discoverer=self.discovery,
            clock=self.clock,
            min_scan_interval_seconds=0,
            max_scans_per_hour=0,
            incidents_enabled=False,
        )
        self.discovery.value = discovery(
            observation("192.168.50.80", "10-20-30-40-50-B0", hostname="baseline")
        )
        candidate = inventory.scope_candidates()["candidates"][0]
        scope = inventory.pair_scope(
            candidate["interface_index"], True, "Disabled alert test"
        )
        inventory.scan(scope_id=scope["scope_id"])
        self.clock.value += timedelta(minutes=1)
        self.discovery.value = discovery(
            observation("192.168.50.80", "10-20-30-40-50-B0", hostname="baseline"),
            observation("192.168.50.81", "10-20-30-40-50-B1", hostname="new-device"),
        )
        completed = inventory.scan(scope_id=scope["scope_id"])
        self.assertEqual(len(completed["devices"]), 2)
        pending = inventory.pending_incidents(include_identifiers=True)
        self.assertTrue(pending["disabled"])
        self.assertEqual(pending["pending_count"], 0)
        with self.assertRaisesRegex(PermissionError, "disabled"):
            inventory.record_incident_actions(
                incident_id="a" * 32,
                actions=[{
                    "tool_id": "netstat-flow",
                    "title": "not allowed",
                    "status": "completed",
                    "receipt_id": "b" * 32,
                }],
            )

    def test_concurrent_security_receipt_recovery_creates_exactly_one_row(self) -> None:
        self.discovery.value = discovery(
            observation("192.168.50.49", "10-20-30-40-50-89")
        )
        scope = self.pair()
        self.inventory.scan(scope_id=scope["scope_id"])
        with closing(sqlite3.connect(self.inventory.path)) as connection:
            connection.execute("DELETE FROM network_security_receipts")
            connection.commit()

        readers = [
            NetworkInventory(
                self.test_dir,
                discoverer=lambda _max_hosts: (_ for _ in ()).throw(
                    AssertionError("assessment recovery must not discover")
                ),
                clock=self.clock,
                min_scan_interval_seconds=0,
                max_scans_per_hour=0,
            )
            for _ in range(6)
        ]
        barrier = threading.Barrier(len(readers))
        assessment_ids: list[str] = []
        errors: list[BaseException] = []

        def read_receipt(reader: NetworkInventory) -> None:
            try:
                barrier.wait(timeout=5)
                assessment_ids.append(reader.security_assessment()["assessment_id"])
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        workers = [
            threading.Thread(target=read_receipt, args=(reader,), daemon=True)
            for reader in readers
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)
        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(errors, [])
        self.assertEqual(len(set(assessment_ids)), 1)
        with closing(sqlite3.connect(self.inventory.path)) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM network_security_receipts"
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_concurrent_first_run_schema_initialization_is_safe(self) -> None:
        data_dir = self.test_dir / "concurrent-first-run"
        workers_count = 8
        barrier = threading.Barrier(workers_count)
        errors: list[BaseException] = []

        def initialize() -> None:
            try:
                barrier.wait(timeout=5)
                NetworkInventory(
                    data_dir,
                    discoverer=self.discovery,
                    clock=self.clock,
                    min_scan_interval_seconds=0,
                    max_scans_per_hour=0,
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        workers = [
            threading.Thread(target=initialize, daemon=True)
            for _ in range(workers_count)
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=15)

        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(errors, [])
        with closing(sqlite3.connect(data_dir / "network-inventory.db")) as connection:
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0],
                NETWORK_INVENTORY_SCHEMA_VERSION,
            )

    def test_profile_change_creates_a_new_immutable_security_receipt(self) -> None:
        self.discovery.value = discovery(
            observation("192.168.50.50", "10-20-30-40-50-90")
        )
        scope = self.pair()
        scanned = self.inventory.scan(scope_id=scope["scope_id"])
        device_id = scanned["devices"][0]["device_id"]
        original_id = scanned["security_assessment"]["assessment_id"]
        updated = self.inventory.set_profile(device_id, trust_state="watch")
        changed_id = updated["security_assessment"]["assessment_id"]
        self.assertNotEqual(changed_id, original_id)
        history = self.inventory.security_assessment_history()
        self.assertEqual(
            {item["assessment_id"] for item in history["assessments"]},
            {original_id, changed_id},
        )
        self.assertEqual(history["verified_receipts"], 2)
        self.assertEqual(history["integrity_failures"], [])

    def test_profile_change_does_not_retroactively_claim_reappearance(self) -> None:
        self.discovery.value = discovery(
            observation("192.168.50.52", "10-20-30-40-50-92")
        )
        scope = self.pair()
        first = self.inventory.scan(scope_id=scope["scope_id"])
        device_id = first["devices"][0]["device_id"]
        self.clock.value += timedelta(hours=2)
        updated = self.inventory.set_profile(device_id, trust_state="retired")
        self.assertNotIn(
            "retired_device_reappeared",
            {
                item["rule_id"]
                for item in updated["security_assessment"]["signals"]
            },
        )
        self.clock.value += timedelta(minutes=1)
        observed_again = self.inventory.scan(scope_id=scope["scope_id"])
        self.assertIn(
            "retired_device_reappeared",
            {
                item["rule_id"]
                for item in observed_again["security_assessment"]["signals"]
            },
        )

    def test_store_freshness_transition_is_read_only_and_fail_closed(self) -> None:
        self.discovery.value = discovery(
            observation("192.168.50.53", "10-20-30-40-50-93")
        )
        scope = self.pair()
        fresh = self.inventory.scan(scope_id=scope["scope_id"])[
            "security_assessment"
        ]
        self.clock.value += timedelta(days=2)

        def forbidden_discovery(_max_hosts: int) -> dict:
            raise AssertionError("freshness assessment must not scan the network")

        self.inventory.discoverer = forbidden_discovery
        stale = self.inventory.security_assessment(scope_id=scope["scope_id"])
        self.assertNotEqual(fresh["assessment_id"], stale["assessment_id"])
        self.assertEqual(stale["coverage"]["freshness_state"], "stale")
        self.assertIn(
            "monitoring_stale", {item["rule_id"] for item in stale["signals"]}
        )
        self.assertFalse(stale["automatic_containment"]["enabled"])

    def test_security_receipts_are_isolated_by_paired_scope(self) -> None:
        second_interface = interface(
            index=8,
            guid="{88888888-2222-3333-4444-555555555555}",
            alias="Ethernet",
            address="192.168.60.2",
            gateway_ipv4="192.168.60.1",
            gateway_mac="00-11-22-33-44-66",
            adapter_mac="00-AA-BB-CC-DD-FF",
        )
        first_observation = observation(
            "192.168.50.54", "10-20-30-40-50-94", interface_index=7
        )
        second_observation = observation(
            "192.168.60.54", "10-20-30-40-60-94", interface_index=8
        )
        self.discovery.value = discovery(
            first_observation,
            second_observation,
            interfaces=[interface(), second_interface],
        )
        first_scope = self.inventory.pair_scope(
            interface_index=7,
            owns_or_administers=True,
            display_name="First LAN",
        )
        second_scope = self.inventory.pair_scope(
            interface_index=8,
            owns_or_administers=True,
            display_name="Second LAN",
        )
        first_scan = self.inventory.scan(scope_id=first_scope["scope_id"])
        self.clock.value += timedelta(seconds=1)
        second_scan = self.inventory.scan(scope_id=second_scope["scope_id"])
        first_receipt = self.inventory.security_assessment(
            scope_id=first_scope["scope_id"]
        )
        second_receipt = self.inventory.security_assessment(
            scope_id=second_scope["scope_id"]
        )
        self.assertEqual(first_receipt["scope_id"], first_scope["scope_id"])
        self.assertEqual(second_receipt["scope_id"], second_scope["scope_id"])
        self.assertNotEqual(first_receipt["assessment_id"], second_receipt["assessment_id"])
        first_ids = {
            item["device_id"]
            for item in first_receipt["evidence_snapshot"]["devices"]
        }
        second_ids = {
            item["device_id"]
            for item in second_receipt["evidence_snapshot"]["devices"]
        }
        self.assertEqual(first_ids, {first_scan["devices"][0]["device_id"]})
        self.assertEqual(second_ids, {second_scan["devices"][0]["device_id"]})
        self.assertTrue(first_ids.isdisjoint(second_ids))
        with self.assertRaisesRegex(ValueError, "explicit scope_id"):
            self.inventory.security_assessment()
        status = self.inventory.status()
        self.assertEqual(
            status["security_assessment"]["posture"],
            "scope_selection_required",
        )
        self.assertEqual(len(status["security_assessments"]), 2)
        self.assertEqual(
            {item["scope_name"] for item in status["security_assessments"]},
            {"First LAN", "Second LAN"},
        )

    def test_tampered_security_receipt_is_reported_invalid(self) -> None:
        self.discovery.value = discovery(
            observation("192.168.50.51", "10-20-30-40-50-91")
        )
        scope = self.pair()
        result = self.inventory.scan(scope_id=scope["scope_id"])
        assessment_id = result["security_assessment"]["assessment_id"]
        with closing(sqlite3.connect(self.inventory.path)) as connection:
            receipt = json.loads(connection.execute(
                "SELECT receipt_json FROM network_security_receipts WHERE assessment_id=?",
                (assessment_id,),
            ).fetchone()[0])
            receipt["conclusion"] = "This network is perfectly safe."
            connection.execute(
                "UPDATE network_security_receipts SET receipt_json=? WHERE assessment_id=?",
                (json.dumps(receipt, sort_keys=True), assessment_id),
            )
            connection.commit()
        history = self.inventory.security_assessment_history()
        self.assertEqual(history["assessments"], [])
        self.assertEqual(history["verified_receipts"], 0)
        self.assertEqual(len(history["integrity_failures"]), 1)
        self.assertEqual(
            history["integrity_failures"][0]["assessment_id"], assessment_id
        )
        status = self.inventory.status()
        self.assertEqual(status["inventory"]["known_devices"], 1)
        self.assertEqual(status["security_assessment"]["posture"], "assessment_unavailable")

    def test_receipt_database_metadata_mismatch_fails_closed(self) -> None:
        self.discovery.value = discovery(
            observation("192.168.50.56", "10-20-30-40-50-96")
        )
        scope = self.pair()
        assessment_id = self.inventory.scan(scope_id=scope["scope_id"])[
            "security_assessment"
        ]["assessment_id"]
        with closing(sqlite3.connect(self.inventory.path)) as connection:
            connection.execute(
                "UPDATE network_security_receipts SET scope_id=? WHERE assessment_id=?",
                ("f" * 32, assessment_id),
            )
            connection.commit()
        history = self.inventory.security_assessment_history()
        self.assertEqual(history["assessments"], [])
        self.assertEqual(len(history["integrity_failures"]), 1)
        status = self.inventory.status()
        self.assertEqual(status["inventory"]["known_devices"], 1)
        self.assertEqual(status["security_assessment"]["posture"], "assessment_unavailable")

    def test_receipt_database_created_at_mismatch_fails_closed(self) -> None:
        self.discovery.value = discovery(
            observation("192.168.50.57", "10-20-30-40-50-97")
        )
        scope = self.pair()
        assessment_id = self.inventory.scan(scope_id=scope["scope_id"])[
            "security_assessment"
        ]["assessment_id"]
        with closing(sqlite3.connect(self.inventory.path)) as connection:
            connection.execute(
                "UPDATE network_security_receipts SET created_at=? WHERE assessment_id=?",
                ("2099-01-01T00:00:00+00:00", assessment_id),
            )
            connection.commit()

        history = self.inventory.security_assessment_history()
        self.assertEqual(history["assessments"], [])
        self.assertEqual(len(history["integrity_failures"]), 1)
        status = self.inventory.status()
        self.assertEqual(status["inventory"]["known_devices"], 1)
        self.assertEqual(
            status["security_assessment"]["posture"], "assessment_unavailable"
        )

    def test_scan_cooldown_is_durable_across_inventory_instances(self) -> None:
        first = NetworkInventory(
            self.test_dir / "durable-rate",
            discoverer=self.discovery,
            clock=self.clock,
            min_scan_interval_seconds=60,
            max_scans_per_hour=12,
        )
        scope = self.pair(first)
        first.scan(scope_id=scope["scope_id"])
        second = NetworkInventory(
            self.test_dir / "durable-rate",
            discoverer=self.discovery,
            clock=self.clock,
            min_scan_interval_seconds=60,
            max_scans_per_hour=12,
        )
        with self.assertRaises(NetworkInventoryRateLimited) as caught:
            second.scan(scope_id=scope["scope_id"])
        self.assertGreater(caught.exception.retry_after_seconds, 0)

    def test_hourly_scan_quota_is_durable_across_inventory_instances(self) -> None:
        data_dir = self.test_dir / "durable-hourly-quota"
        first = NetworkInventory(
            data_dir,
            discoverer=self.discovery,
            clock=self.clock,
            min_scan_interval_seconds=0,
            max_scans_per_hour=2,
        )
        scope = self.pair(first)
        first.scan(scope_id=scope["scope_id"])
        self.clock.value += timedelta(seconds=1)
        first.scan(scope_id=scope["scope_id"])
        second = NetworkInventory(
            data_dir,
            discoverer=self.discovery,
            clock=self.clock,
            min_scan_interval_seconds=0,
            max_scans_per_hour=2,
        )
        self.clock.value += timedelta(seconds=1)
        with self.assertRaises(NetworkInventoryRateLimited) as caught:
            second.scan(scope_id=scope["scope_id"])
        self.assertGreater(caught.exception.retry_after_seconds, 0)

    def test_single_flight_lease_is_durable_across_inventory_instances(self) -> None:
        scan_started = threading.Event()
        release_scan = threading.Event()
        block = threading.Event()
        base = discovery(observation("192.168.50.46", "10-20-30-40-50-86"))

        def blocking_discovery(_max_hosts: int) -> dict:
            if block.is_set():
                scan_started.set()
                self.assertTrue(release_scan.wait(timeout=5))
            return copy.deepcopy(base)

        data_dir = self.test_dir / "single-flight"
        first = NetworkInventory(
            data_dir,
            discoverer=blocking_discovery,
            clock=self.clock,
            min_scan_interval_seconds=0,
            max_scans_per_hour=0,
            lease_seconds=90,
        )
        scope = self.pair(first)
        second = NetworkInventory(
            data_dir,
            discoverer=blocking_discovery,
            clock=self.clock,
            min_scan_interval_seconds=0,
            max_scans_per_hour=0,
            lease_seconds=90,
        )
        block.set()
        errors: list[BaseException] = []

        def scan_first() -> None:
            try:
                first.scan(scope_id=scope["scope_id"])
            except BaseException as exc:  # pragma: no cover - reported by assertion below
                errors.append(exc)

        worker = threading.Thread(target=scan_first, daemon=True)
        worker.start()
        self.assertTrue(scan_started.wait(timeout=5))
        try:
            with self.assertRaises(NetworkInventoryRateLimited) as caught:
                second.scan(scope_id=scope["scope_id"])
            self.assertGreater(caught.exception.retry_after_seconds, 0)
        finally:
            release_scan.set()
            worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])

    def test_existing_v1_database_migrates_without_granting_trust(self) -> None:
        migration_dir = self.test_dir / "migration"
        migration_dir.mkdir()
        path = migration_dir / "network-inventory.db"
        with closing(sqlite3.connect(path)) as connection:
            connection.executescript(
                """
                CREATE TABLE network_devices (
                    identity TEXT PRIMARY KEY,
                    mac TEXT,
                    ipv4 TEXT NOT NULL,
                    hostname TEXT,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    continuous_since TEXT NOT NULL,
                    seen_count INTEGER NOT NULL,
                    visibility TEXT NOT NULL,
                    neighbor_state TEXT NOT NULL
                );
                CREATE TABLE network_scans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    observed_at TEXT NOT NULL,
                    observed_devices INTEGER NOT NULL,
                    candidate_hosts INTEGER NOT NULL,
                    range_truncated INTEGER NOT NULL
                );
                """
            )
            timestamp = "2026-08-20T12:00:00+00:00"
            connection.execute(
                """
                INSERT INTO network_devices (
                    identity, mac, ipv4, hostname, first_seen, last_seen,
                    continuous_since, seen_count, visibility, neighbor_state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "mac:10:20:30:40:50:99", "10:20:30:40:50:99",
                    "192.168.50.99", "legacy-device", timestamp, timestamp,
                    timestamp, 3, "active", "Reachable",
                ),
            )
            connection.execute(
                """
                INSERT INTO network_scans (
                    observed_at, observed_devices, candidate_hosts, range_truncated
                ) VALUES (?, ?, ?, ?)
                """,
                (timestamp, 1, 254, 0),
            )
            connection.commit()

        migrated = NetworkInventory(
            migration_dir,
            discoverer=self.discovery,
            clock=self.clock,
            min_scan_interval_seconds=0,
            max_scans_per_hour=0,
        )
        # Legacy rows are retained for explicit recovery/audit, but quarantined
        # from the normal paired-scope inventory until fresh evidence binds them.
        self.assertEqual(migrated.list_devices()["known_devices"], 0)
        redacted = migrated.list_devices(include_unpaired=True)
        self.assertEqual(redacted["known_devices"], 1)
        device = redacted["devices"][0]
        self.assertTrue(device["device_id"])
        self.assertEqual(device["trust_state"], "unreviewed")
        self.assertFalse(device["enrolled"])
        self.assertFalse(device["access_authorized"])
        self.assertNotIn("192.168.50.99", json.dumps(redacted, sort_keys=True))
        explicit = migrated.list_devices(
            include_identifiers=True, include_unpaired=True
        )
        self.assertEqual(explicit["devices"][0]["ipv4"], "192.168.50.99")
        self.assertEqual(explicit["devices"][0]["seen_count"], 3)
        with closing(sqlite3.connect(path)) as connection:
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0],
                NETWORK_INVENTORY_SCHEMA_VERSION,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM network_security_receipts"
                ).fetchone()[0],
                0,
            )


if __name__ == "__main__":
    unittest.main()
