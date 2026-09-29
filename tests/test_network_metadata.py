from __future__ import annotations

import os
import shutil
import unittest
from datetime import datetime, timezone
from pathlib import Path

from jarvis.network_inventory import NetworkInventory
from jarvis.network_metadata import (
    IeeeOuiRegistry,
    MAX_IEEE_OUI_BYTES,
    NetworkMetadataResolver,
)


TEMP_ROOT = Path(__file__).resolve().parent / ".tmp"
TEMP_ROOT.mkdir(exist_ok=True)


def registry_csv() -> bytes:
    return (
        "Registry,Assignment,Organization Name,Organization Address\n"
        "MA-L,544EF0,Roku Inc.,Example\n"
        "MA-L,C0A5E8,Intel Corporate,Example\n"
        "MA-L,C03937,GREE Electric Appliances Inc.,Example\n"
        "MA-L,5026EF,Murata Manufacturing Co. Ltd.,Example\n"
        "MA-L,3498B5,NETGEAR,Example\n"
    ).encode("utf-8")


class FakeRegistry:
    def __init__(self, vendors: dict[str, str]) -> None:
        self.vendors = vendors
        self.ready_calls = 0

    def ensure_ready(self) -> bool:
        self.ready_calls += 1
        return True

    def lookup(self, mac: object) -> str | None:
        compact = str(mac or "").replace(":", "").replace("-", "").casefold()
        if len(compact) != 12 or int(compact[:2], 16) & 0x02:
            return None
        return self.vendors.get(compact[:6])

    def status(self):
        return {
            "provider": "test-registry",
            "available": True,
            "source": "fixture",
            "error": None,
            "privacy": "fixture",
        }


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


class NetworkMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.test_dir = TEMP_ROOT / f"network-metadata-{os.getpid()}-{self._testMethodName}"
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir)
        self.test_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        resolved = self.test_dir.resolve()
        self.assertEqual(resolved.parent, TEMP_ROOT.resolve())
        shutil.rmtree(resolved)

    def test_registry_fetches_complete_list_without_receiving_an_observed_identifier(self):
        calls: list[tuple[object, ...]] = []

        def fetcher(*arguments):
            calls.append(arguments)
            return registry_csv()

        registry = IeeeOuiRegistry(
            self.test_dir,
            fetcher=fetcher,
            minimum_rows=5,
        )
        self.assertTrue(registry.ensure_ready())
        self.assertEqual(calls, [()])
        self.assertEqual(registry.lookup("54:4e:f0:11:22:33"), "Roku Inc.")
        self.assertTrue((self.test_dir / "network-metadata" / "ieee-oui.csv").is_file())
        receipt = (self.test_dir / "network-metadata" / "ieee-oui-receipt.json").read_text(
            encoding="utf-8"
        )
        self.assertIn("no observed endpoint identifier was sent", receipt)
        self.assertNotIn("54:4e:f0", receipt)

    def test_registry_rejects_truncated_or_oversized_payloads(self):
        truncated = IeeeOuiRegistry(
            self.test_dir,
            fetcher=lambda: b"Registry,Assignment,Organization Name\n",
            minimum_rows=2,
        )
        self.assertFalse(truncated.ensure_ready())
        self.assertIn("incomplete", str(truncated.status()["error"]))

        oversized_dir = self.test_dir / "oversized"
        oversized = IeeeOuiRegistry(
            oversized_dir,
            fetcher=lambda: b"x" * (MAX_IEEE_OUI_BYTES + 1),
            minimum_rows=1,
        )
        self.assertFalse(oversized.ensure_ready())
        self.assertIn("size", str(oversized.status()["error"]))

    def test_hostname_and_vendor_rules_are_conservative_and_provenanced(self):
        registry = FakeRegistry({
            "544ef0": "Roku Inc.",
            "c0a5e8": "Intel Corporate",
            "5026ef": "Murata Manufacturing Co. Ltd.",
        })
        resolver = NetworkMetadataResolver(
            self.test_dir,
            mode="hostname-oui",
            registry=registry,  # type: ignore[arg-type]
        )

        phone = resolver.resolve(
            mac="52:26:ef:00:00:01",
            hostname="Alex-iPhone",
        )
        self.assertEqual(phone["device_type"], "Phone")
        self.assertEqual(phone["device_type_source"], "local-hostname")
        self.assertIsNone(phone["manufacturer"])
        self.assertNotIn("hostname", phone)
        self.assertNotIn("mac", phone)

        roku = resolver.resolve(mac="54:4e:f0:00:00:01", hostname=None)
        self.assertEqual(roku["manufacturer"], "Roku Inc.")
        self.assertEqual(roku["device_type"], "TV or streaming device")
        self.assertEqual(roku["device_type_source"], "ieee-oui")

        ambiguous = resolver.resolve(mac="50:26:ef:00:00:01", hostname=None)
        self.assertEqual(ambiguous["manufacturer"], "Murata Manufacturing Co. Ltd.")
        self.assertIsNone(ambiguous["device_type"])
        self.assertEqual(ambiguous["device_type_confidence"], 0.0)

    def test_operator_profile_wins_over_inference(self):
        resolver = NetworkMetadataResolver(
            self.test_dir,
            mode="hostname-oui",
            registry=FakeRegistry({"544ef0": "Roku Inc."}),  # type: ignore[arg-type]
        )
        resolved = resolver.resolve(
            mac="54:4e:f0:00:00:01",
            hostname="roku-bedroom",
            operator_type="Test appliance",
        )
        self.assertEqual(resolved["device_type"], "Test appliance")
        self.assertEqual(resolved["device_type_confidence"], 1.0)
        self.assertEqual(resolved["device_type_source"], "operator-profile")

    def test_inventory_returns_derived_metadata_without_exposing_identifiers(self):
        snapshots = [discovery({
            "ipv4": "192.168.50.20",
            "mac": "54:4e:f0:00:00:01",
            "hostname": "living-room-roku",
            "visibility": "active",
            "neighbor_state": "Reachable",
        })]
        resolver = NetworkMetadataResolver(
            self.test_dir,
            mode="hostname-oui",
            registry=FakeRegistry({"544ef0": "Roku Inc."}),  # type: ignore[arg-type]
        )
        inventory = NetworkInventory(
            self.test_dir,
            discoverer=lambda _limit: snapshots.pop(0),
            clock=lambda: datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc),
            require_paired_scope=False,
            min_scan_interval_seconds=0,
            max_scans_per_hour=0,
            metadata_resolver=resolver,
        )
        result = inventory.scan(include_identifiers=False)
        device = result["devices"][0]
        self.assertEqual(device["manufacturer"], "Roku Inc.")
        self.assertEqual(device["device_type"], "TV or streaming device")
        self.assertGreaterEqual(device["device_type_confidence"], 0.9)
        self.assertNotIn("ipv4", device)
        self.assertNotIn("mac", device)
        self.assertNotIn("hostname", device)
        self.assertTrue(result["device_metadata"]["enabled"])


if __name__ == "__main__":
    unittest.main()
