from __future__ import annotations

import ipaddress
import hashlib
import json
import os
import re
import secrets
import socket
import sqlite3
import struct
import subprocess
import threading
import ctypes
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator
from uuid import uuid4

from .network_defense import (
    NETWORK_DEFENSE_RULESET_VERSION,
    assess_network_defense,
    verify_assessment_receipt,
)


MAX_SCAN_HOSTS = 512
DEFAULT_SCAN_HOSTS = 512
CONTINUITY_GAP_SECONDS = 15 * 60
NETWORK_INVENTORY_SCHEMA_VERSION = 4
DEFAULT_SCAN_COOLDOWN_SECONDS = 15
DEFAULT_MAX_SCANS_PER_HOUR = 12
DEFAULT_SCAN_LEASE_SECONDS = 90
MAX_EVENT_HISTORY = 5_000
MAX_OBSERVATION_HISTORY = 20_000
MAX_SECURITY_RECEIPTS = 2_000
MAX_PENDING_NETWORK_INCIDENTS = 256
MAX_NETWORK_INCIDENT_RECEIPTS = 2_000
NETWORK_INCIDENT_TTL_DAYS = 30
TRUST_STATES = frozenset({"unreviewed", "recognized", "watch", "retired"})
_INVENTORY_INITIALIZE_LOCK = threading.Lock()
_PRIVATE_V4 = tuple(
    ipaddress.ip_network(value)
    for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)


class NetworkInventoryError(RuntimeError):
    """A bounded private-LAN inventory could not be collected safely."""


class NetworkInventoryRateLimited(NetworkInventoryError):
    """A durable scan cooldown or another live scanner blocked this attempt."""

    def __init__(self, message: str, *, retry_after_seconds: int) -> None:
        super().__init__(message)
        self.retry_after_seconds = max(1, int(retry_after_seconds))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def _is_rfc1918(address: ipaddress.IPv4Address) -> bool:
    return any(address in network for network in _PRIVATE_V4)


def _normalize_mac(value: Any) -> str | None:
    text = str(value or "").strip().replace("-", ":").casefold()
    compact = text.replace(":", "")
    if len(compact) != 12 or any(character not in "0123456789abcdef" for character in compact):
        return None
    if compact in {"0" * 12, "f" * 12}:
        return None
    # Group/multicast addresses cannot identify one endpoint. Reject them rather
    # than allowing a crafted neighbor-cache row to merge unrelated devices.
    if int(compact[:2], 16) & 0x01:
        return None
    return ":".join(compact[index:index + 2] for index in range(0, 12, 2))


def _mac_identity(mac: str | None) -> tuple[str, str]:
    if not mac:
        return "limited", "IPv4 observation only (addresses can be reassigned)"
    first_octet = int(mac.split(":", 1)[0], 16)
    if first_octet & 0x02:
        return (
            "limited",
            "locally administered MAC address (may be randomized or spoofed)",
        )
    return "moderate", "globally administered MAC address (can still be spoofed)"


def _network_for_interface(address: ipaddress.IPv4Address, prefix: int) -> tuple[ipaddress.IPv4Network, bool]:
    actual = ipaddress.ip_network(f"{address}/{prefix}", strict=False)
    if actual.prefixlen < 24:
        return ipaddress.ip_network(f"{address}/24", strict=False), True
    return actual, False


def _host_in_scope(value: Any, network: ipaddress.IPv4Network) -> ipaddress.IPv4Address | None:
    try:
        address = ipaddress.ip_address(str(value or ""))
    except ValueError:
        return None
    if (
        not isinstance(address, ipaddress.IPv4Address)
        or not _is_rfc1918(address)
        or address not in network
        or address in {network.network_address, network.broadcast_address}
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
    ):
        return None
    return address


def _powershell_json(script: str, *, timeout: float = 12.0) -> Any:
    if os.name != "nt":
        raise NetworkInventoryError("Private-LAN discovery currently requires Windows")
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    executable = (
        system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    )
    if not executable.is_file():
        raise NetworkInventoryError("Windows PowerShell is unavailable")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            [
                str(executable), "-NoLogo", "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass", "-Command", script,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            creationflags=flags,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NetworkInventoryError("Windows network inventory command failed") from exc
    if completed.returncode != 0:
        raise NetworkInventoryError("Windows network inventory command returned an error")
    output = completed.stdout.lstrip("\ufeff").strip()
    if not output:
        return []
    try:
        return json.loads(output)
    except json.JSONDecodeError as exc:
        raise NetworkInventoryError("Windows network inventory returned invalid data") from exc


def _windows_interfaces() -> list[dict[str, Any]]:
    script = r"""
$ErrorActionPreference = 'Stop'
$rows = foreach ($cfg in Get-NetIPConfiguration | Where-Object { $_.NetAdapter.Status -eq 'Up' }) {
  $profile = Get-NetConnectionProfile -InterfaceIndex $cfg.InterfaceIndex -ErrorAction SilentlyContinue
  foreach ($addr in @($cfg.IPv4Address)) {
    if ($null -ne $addr -and $addr.IPAddress -notlike '127.*') {
      [pscustomobject]@{
        interface_index = [int]$cfg.InterfaceIndex
        interface_alias = [string]$cfg.InterfaceAlias
        interface_guid = [string]$cfg.NetAdapter.InterfaceGuid
        interface_description = [string]$cfg.NetAdapter.InterfaceDescription
        address = [string]$addr.IPAddress
        prefix_length = [int]$addr.PrefixLength
        gateway = [string](@($cfg.IPv4DefaultGateway)[0].NextHop)
        mac = [string]$cfg.NetAdapter.LinkLayerAddress
        hardware_interface = [bool]$cfg.NetAdapter.HardwareInterface
        network_category = [string]$profile.NetworkCategory
        profile_name = [string]$profile.Name
      }
    }
  }
}
@($rows) | ConvertTo-Json -Compress -Depth 3
"""
    value = _powershell_json(script)
    return value if isinstance(value, list) else [value]


def _windows_neighbors() -> list[dict[str, Any]]:
    script = r"""
$ErrorActionPreference = 'Stop'
$rows = Get-NetNeighbor -AddressFamily IPv4 | Where-Object {
  $_.State -notin @('Unreachable','Incomplete') -and $_.IPAddress -ne '0.0.0.0'
} | ForEach-Object {
  [pscustomobject]@{
    interface_index = [int]$_.InterfaceIndex
    address = [string]$_.IPAddress
    mac = [string]$_.LinkLayerAddress
    state = [string]$_.State
  }
}
@($rows) | ConvertTo-Json -Compress -Depth 3
"""
    value = _powershell_json(script)
    return value if isinstance(value, list) else [value]


def _ping_host(address: str) -> bool:
    if os.name != "nt":
        return False
    handle = None
    try:
        iphlpapi = ctypes.WinDLL("iphlpapi")
        iphlpapi.IcmpCreateFile.restype = ctypes.c_void_p
        iphlpapi.IcmpSendEcho.argtypes = (
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_void_p,
            ctypes.c_ushort,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_ulong,
        )
        iphlpapi.IcmpSendEcho.restype = ctypes.c_ulong
        iphlpapi.IcmpCloseHandle.argtypes = (ctypes.c_void_p,)
        iphlpapi.IcmpCloseHandle.restype = ctypes.c_bool
        handle = iphlpapi.IcmpCreateFile()
        if not handle or handle == ctypes.c_void_p(-1).value:
            return False
        destination = struct.unpack("=I", socket.inet_aton(address))[0]
        reply = ctypes.create_string_buffer(128)
        replies = iphlpapi.IcmpSendEcho(
            handle,
            destination,
            None,
            0,
            None,
            reply,
            len(reply),
            300,
        )
        return bool(replies)
    except (OSError, ValueError):
        return False
    finally:
        if handle and handle != ctypes.c_void_p(-1).value:
            try:
                iphlpapi.IcmpCloseHandle(handle)
            except (NameError, OSError):
                pass


def _interface_candidate(row: dict[str, Any]) -> dict[str, Any] | None:
    try:
        address = ipaddress.ip_address(str(row.get("address") or ""))
        prefix = int(row.get("prefix_length"))
    except (ValueError, TypeError):
        return None
    if (
        not isinstance(address, ipaddress.IPv4Address)
        or not _is_rfc1918(address)
        or row.get("hardware_interface") is False
        or not 1 <= prefix <= 30
    ):
        return None
    scan_network, truncated = _network_for_interface(address, prefix)
    gateway_text = str(row.get("gateway") or "").strip()
    gateway = _host_in_scope(gateway_text, scan_network)
    return {
        "interface_index": int(row.get("interface_index") or 0),
        "interface_alias": str(row.get("interface_alias") or "")[:200],
        "interface_guid": str(row.get("interface_guid") or "").strip().casefold()[:80],
        "interface_description": str(row.get("interface_description") or "")[:300],
        "address": str(address),
        "prefix_length": prefix,
        "scan_cidr": str(scan_network),
        "range_truncated": truncated,
        "gateway_ipv4": str(gateway) if gateway is not None else None,
        "adapter_mac": _normalize_mac(row.get("mac")),
        "network_category": str(row.get("network_category") or "Unknown")[:40],
        "profile_name": str(row.get("profile_name") or "")[:200] or None,
    }


def _gateway_mac(
    neighbors: list[dict[str, Any]], interface_index: int, gateway_ipv4: str | None
) -> str | None:
    if not gateway_ipv4:
        return None
    for row in neighbors:
        try:
            same_interface = int(row.get("interface_index") or 0) == int(interface_index)
        except (TypeError, ValueError):
            continue
        if same_interface and str(row.get("address") or "") == gateway_ipv4:
            return _normalize_mac(row.get("mac"))
    return None


def discover_private_lan(
    max_hosts: int = DEFAULT_SCAN_HOSTS,
    *,
    scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Collect bounded presence evidence from one paired, directly attached LAN.

    With ``scope`` supplied, route identity is validated before the first probe.
    The low-level helper keeps its unscoped form for deterministic diagnostics;
    ``NetworkInventory.scan`` never permits that form in normal operation.
    """
    if isinstance(max_hosts, bool) or not 1 <= int(max_hosts) <= MAX_SCAN_HOSTS:
        raise ValueError(f"max_hosts must be between 1 and {MAX_SCAN_HOSTS}")
    budget = int(max_hosts)
    raw_interfaces = _windows_interfaces()
    candidates_by_index = {
        candidate["interface_index"]: candidate
        for candidate in (_interface_candidate(row) for row in raw_interfaces)
        if candidate is not None
    }
    if scope is not None:
        wanted_index = int(scope.get("interface_index") or 0)
        selected = candidates_by_index.get(wanted_index)
        if selected is None:
            raise NetworkInventoryError("The paired network adapter is not currently available")
        exact_fields = (
            ("interface_guid", "interface_guid"),
            ("scan_cidr", "cidr"),
            ("gateway_ipv4", "gateway_ipv4"),
            ("adapter_mac", "adapter_mac"),
            ("profile_name", "profile_name"),
        )
        for current_key, stored_key in exact_fields:
            current_value = str(selected.get(current_key) or "").strip().casefold()
            stored_value = str(scope.get(stored_key) or "").strip().casefold()
            if not current_value or current_value != stored_value:
                raise NetworkInventoryError(
                    "The current route no longer matches the paired network; pair it again"
                )
        # Gateway identity is checked from already-local cache before any ICMP is sent.
        neighbors_before = _windows_neighbors()
        current_gateway_mac = _gateway_mac(
            neighbors_before, wanted_index, selected["gateway_ipv4"]
        )
        if (
            not current_gateway_mac
            or current_gateway_mac != _normalize_mac(scope.get("gateway_mac"))
        ):
            raise NetworkInventoryError(
                "The gateway identity no longer matches the paired network; scanning stopped"
            )
        interfaces = [selected]
    else:
        interfaces = list(candidates_by_index.values())
    if not interfaces:
        raise NetworkInventoryError(
            "No directly connected physical RFC1918 IPv4 interface was found"
        )

    probe_candidates: list[str] = []
    networks: dict[int, ipaddress.IPv4Network] = {}
    truncated = False
    for row in interfaces:
        network = ipaddress.ip_network(row["scan_cidr"], strict=True)
        networks[row["interface_index"]] = network
        truncated = truncated or bool(row.get("range_truncated"))
        for host in network.hosts():
            if str(host) == row["address"]:
                continue
            if len(probe_candidates) >= budget:
                truncated = True
                break
            probe_candidates.append(str(host))
        if len(probe_candidates) >= budget:
            break

    # Native ICMP avoids spawning processes. Workers and candidates are bounded.
    with ThreadPoolExecutor(
        max_workers=min(32, max(1, len(probe_candidates)))
    ) as executor:
        responsive_flags = list(executor.map(_ping_host, probe_candidates))
    responsive = {
        address
        for address, is_up in zip(probe_candidates, responsive_flags)
        if is_up
    }

    observations: dict[tuple[int, str], dict[str, Any]] = {}
    for row in _windows_neighbors():
        try:
            interface_index = int(row.get("interface_index") or 0)
        except (ValueError, TypeError):
            continue
        network = networks.get(interface_index)
        if network is None:
            continue
        address = _host_in_scope(row.get("address"), network)
        if address is None:
            continue
        address_text = str(address)
        state = str(row.get("state") or "Unknown")[:40]
        actively_reachable = address_text in responsive
        observations[(interface_index, address_text)] = {
            "ipv4": address_text,
            "mac": _normalize_mac(row.get("mac")),
            "hostname": None,
            "visibility": "active_probe" if actively_reachable else "neighbor_cache",
            "neighbor_state": state,
            "actively_reachable": actively_reachable,
            "cached": True,
        }
    first_interface = interfaces[0]
    for address in responsive:
        observations.setdefault((first_interface["interface_index"], address), {
            "ipv4": address,
            "mac": None,
            "hostname": None,
            "visibility": "active_probe",
            "neighbor_state": "Reachable",
            "actively_reachable": True,
            "cached": False,
        })
    for row in interfaces:
        observations[(row["interface_index"], row["address"])] = {
            "ipv4": row["address"],
            "mac": row["adapter_mac"],
            "hostname": socket.gethostname()[:255] or None,
            "visibility": "local_host",
            "neighbor_state": "Local",
            "actively_reachable": True,
            "cached": False,
        }

    return {
        "interfaces": interfaces,
        "observations": sorted(
            observations.values(),
            key=lambda item: ipaddress.ip_address(item["ipv4"]),
        ),
        "candidate_hosts": len(probe_candidates),
        "responsive_hosts": len(responsive),
        "range_truncated": truncated,
        "method": (
            "one bounded ICMP echo per paired-scope candidate plus the local "
            "Windows neighbor cache"
        ),
    }


class NetworkInventory:
    """Versioned, scope-bound device observation history.

    Discovery is evidence, never authority: MAC/IP records do not enroll a device,
    grant access, or prove ownership. Raw identifiers are local-only by default.
    """

    def __init__(
        self,
        data_dir: Path,
        *,
        discoverer: Callable[..., dict[str, Any]] = discover_private_lan,
        clock: Callable[[], datetime] = _utc_now,
        min_scan_interval_seconds: int = DEFAULT_SCAN_COOLDOWN_SECONDS,
        max_scans_per_hour: int = DEFAULT_MAX_SCANS_PER_HOUR,
        lease_seconds: int = DEFAULT_SCAN_LEASE_SECONDS,
        require_paired_scope: bool = True,
        incidents_enabled: bool = True,
    ) -> None:
        self.path = Path(data_dir).resolve() / "network-inventory.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.discoverer = discoverer
        self.clock = clock
        self.min_scan_interval_seconds = max(0, int(min_scan_interval_seconds))
        self.max_scans_per_hour = max(0, int(max_scans_per_hour))
        self.lease_seconds = max(10, min(int(lease_seconds), 600))
        self.require_paired_scope = bool(require_paired_scope)
        self.incidents_enabled = bool(incidents_enabled)
        self._lock = threading.Lock()
        # Presence, the worker, and CLI commands can all start together. Keep
        # their idempotent first-run schema work serial within this process;
        # SQLite's busy retry below covers the equivalent cross-process race.
        with _INVENTORY_INITIALIZE_LOCK:
            self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        for attempt in range(100):
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).casefold() or attempt == 99:
                    connection.close()
                    raise
                time.sleep(0.05)
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
        return {
            str(row["name"])
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }

    @classmethod
    def _add_column(
        cls, connection: sqlite3.Connection, table: str, declaration: str
    ) -> None:
        name = declaration.split(None, 1)[0]
        if name not in cls._columns(connection, table):
            try:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {declaration}")
            except sqlite3.OperationalError as exc:
                # Another process can complete the same idempotent migration
                # after our schema read but before this ALTER acquires its lock.
                if name in cls._columns(connection, table):
                    return
                raise exc

    def _initialize(self) -> None:
        self._reject_future_schema_readonly()
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS network_devices (
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
                CREATE TABLE IF NOT EXISTS network_scans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    observed_at TEXT NOT NULL,
                    observed_devices INTEGER NOT NULL,
                    candidate_hosts INTEGER NOT NULL,
                    range_truncated INTEGER NOT NULL
                );
            """)
            for declaration in (
                "device_uuid TEXT",
                "scope_id TEXT",
                "label TEXT",
                "trust_state TEXT NOT NULL DEFAULT 'unreviewed'",
                "device_type TEXT",
                "identity_confidence TEXT NOT NULL DEFAULT 'limited'",
                "identity_basis TEXT NOT NULL DEFAULT 'IPv4 observation only'",
                "last_active_seen TEXT",
                "profile_updated_at TEXT",
            ):
                self._add_column(connection, "network_devices", declaration)
            for declaration in (
                "scope_id TEXT",
                "status TEXT NOT NULL DEFAULT 'completed'",
                "completed_at TEXT",
                "responsive_hosts INTEGER NOT NULL DEFAULT 0",
                "method TEXT NOT NULL DEFAULT ''",
                "error_code TEXT",
            ):
                self._add_column(connection, "network_scans", declaration)
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS network_scopes (
                    scope_id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    interface_index INTEGER NOT NULL,
                    interface_guid TEXT NOT NULL,
                    interface_alias TEXT NOT NULL,
                    cidr TEXT NOT NULL,
                    gateway_ipv4 TEXT NOT NULL,
                    gateway_mac TEXT NOT NULL,
                    adapter_mac TEXT NOT NULL,
                    profile_name TEXT NOT NULL,
                    network_category TEXT NOT NULL,
                    paired_at TEXT NOT NULL,
                    last_validated_at TEXT,
                    active INTEGER NOT NULL DEFAULT 1,
                    ownership_attested INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS network_device_addresses (
                    device_uuid TEXT NOT NULL,
                    ipv4 TEXT NOT NULL,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    seen_count INTEGER NOT NULL,
                    PRIMARY KEY(device_uuid, ipv4)
                );
                CREATE TABLE IF NOT EXISTS network_presence_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_uuid TEXT NOT NULL,
                    scope_id TEXT,
                    started_at TEXT NOT NULL,
                    last_reachable_at TEXT NOT NULL,
                    ended_at TEXT,
                    observation_count INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS network_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scan_id INTEGER,
                    device_uuid TEXT,
                    scope_id TEXT,
                    observed_at TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    details_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS network_observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scan_id INTEGER NOT NULL,
                    device_uuid TEXT NOT NULL,
                    scope_id TEXT,
                    observed_at TEXT NOT NULL,
                    ipv4 TEXT NOT NULL,
                    mac TEXT,
                    hostname TEXT,
                    evidence TEXT NOT NULL,
                    actively_reachable INTEGER NOT NULL,
                    cached INTEGER NOT NULL,
                    neighbor_state TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS network_scan_leases (
                    scope_id TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    leased_until TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS network_security_receipts (
                    assessment_id TEXT PRIMARY KEY,
                    scan_id INTEGER,
                    scope_id TEXT,
                    ruleset_version TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    input_sha256 TEXT NOT NULL,
                    receipt_sha256 TEXT NOT NULL,
                    receipt_json TEXT NOT NULL,
                    UNIQUE(scan_id, ruleset_version, input_sha256)
                );
                CREATE TABLE IF NOT EXISTS network_incident_alerts (
                    incident_id TEXT PRIMARY KEY
                        CHECK(length(incident_id)=32 AND
                              incident_id NOT GLOB '*[^0-9a-f]*'),
                    receipt_id TEXT NOT NULL UNIQUE
                        CHECK(length(receipt_id)=32 AND
                              receipt_id NOT GLOB '*[^0-9a-f]*'),
                    assessment_id TEXT NOT NULL,
                    signal_id TEXT NOT NULL,
                    scope_id TEXT,
                    device_uuid TEXT,
                    created_at TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    category TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending'
                        CHECK(state IN ('pending', 'acknowledged', 'expired')),
                    resolved_at TEXT,
                    resolution_reason TEXT,
                    payload_sha256 TEXT NOT NULL
                        CHECK(length(payload_sha256)=64 AND
                              payload_sha256 NOT GLOB '*[^0-9a-f]*'),
                    payload_json TEXT NOT NULL,
                    UNIQUE(assessment_id, signal_id),
                    CHECK(
                        (state='pending' AND resolved_at IS NULL AND
                         resolution_reason IS NULL)
                        OR
                        (state!='pending' AND resolved_at IS NOT NULL AND
                         resolution_reason IS NOT NULL)
                    ),
                    FOREIGN KEY(assessment_id)
                        REFERENCES network_security_receipts(assessment_id)
                        ON DELETE CASCADE
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_network_devices_uuid
                    ON network_devices(device_uuid);
                CREATE INDEX IF NOT EXISTS idx_network_observations_scan
                    ON network_observations(scan_id, device_uuid);
                CREATE INDEX IF NOT EXISTS idx_network_events_device
                    ON network_events(device_uuid, id DESC);
                CREATE INDEX IF NOT EXISTS idx_network_sessions_device
                    ON network_presence_sessions(device_uuid, id DESC);
                CREATE INDEX IF NOT EXISTS idx_network_scans_scope
                    ON network_scans(scope_id, id DESC);
                CREATE INDEX IF NOT EXISTS idx_network_security_receipts_scope
                    ON network_security_receipts(scope_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_network_incident_alerts_state
                    ON network_incident_alerts(state, created_at DESC);
            """)
            rows = connection.execute(
                "SELECT identity, device_uuid, mac, ipv4, first_seen, last_seen "
                "FROM network_devices"
            ).fetchall()
            for row in rows:
                device_uuid = str(row["device_uuid"] or "").strip() or uuid4().hex
                confidence, basis = _mac_identity(_normalize_mac(row["mac"]))
                connection.execute(
                    "UPDATE network_devices SET device_uuid=?, "
                    "identity_confidence=?, identity_basis=? WHERE identity=?",
                    (device_uuid, confidence, basis, str(row["identity"])),
                )
                connection.execute(
                    """
                    INSERT INTO network_device_addresses (
                        device_uuid, ipv4, first_seen, last_seen, seen_count
                    ) VALUES (?, ?, ?, ?, 1)
                    ON CONFLICT(device_uuid, ipv4) DO NOTHING
                    """,
                    (
                        device_uuid,
                        str(row["ipv4"]),
                        str(row["first_seen"]),
                        str(row["last_seen"]),
                    ),
                )
            connection.execute(f"PRAGMA user_version={NETWORK_INVENTORY_SCHEMA_VERSION}")

    def _reject_future_schema_readonly(self) -> None:
        """Refuse a future database before WAL, DDL, or migration can touch it."""
        if not self.path.exists():
            return
        uri = f"{self.path.as_uri()}?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=10)
            try:
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            finally:
                connection.close()
        except (sqlite3.DatabaseError, TypeError, ValueError) as exc:
            raise NetworkInventoryError(
                "Network inventory database could not be inspected safely"
            ) from exc
        if version > NETWORK_INVENTORY_SCHEMA_VERSION:
            raise NetworkInventoryError(
                "Network inventory database schema is newer than this Jarvis build"
            )

    def scope_candidates(self) -> dict[str, Any]:
        if self.discoverer is discover_private_lan:
            raw_interfaces = _windows_interfaces()
            neighbors = _windows_neighbors()
        else:
            sample = self.discoverer(1)
            raw_interfaces = (
                sample.get("interfaces", []) if isinstance(sample, dict) else []
            )
            neighbors = []
        candidates: list[dict[str, Any]] = []
        for row in raw_interfaces:
            candidate = _interface_candidate(row)
            if candidate is None:
                continue
            gateway_mac = _normalize_mac(row.get("gateway_mac")) or _gateway_mac(
                neighbors, candidate["interface_index"], candidate["gateway_ipv4"]
            )
            eligible = bool(
                candidate["interface_guid"]
                and candidate["adapter_mac"]
                and candidate["gateway_ipv4"]
            )
            reason = (
                "Ready to pair; the gateway will be verified again before scanning."
                if eligible
                else "A physical adapter, stable adapter ID, and local gateway are required."
            )
            candidates.append({
                **candidate,
                "gateway_mac": gateway_mac,
                "eligible": eligible,
                "reason": reason,
            })
        return {
            "candidates": candidates,
            "notice": (
                "Pair only a network you own or administer. Merely having a private "
                "IP address does not establish authorization."
            ),
        }

    @staticmethod
    def _render_scope(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "scope_id": str(row["scope_id"]),
            "display_name": str(row["display_name"]),
            "interface_index": int(row["interface_index"]),
            "interface_alias": str(row["interface_alias"]),
            "cidr": str(row["cidr"]),
            "gateway_ipv4": str(row["gateway_ipv4"]),
            "paired_at": str(row["paired_at"]),
            "last_validated_at": row["last_validated_at"],
            "active": bool(row["active"]),
            "ownership_attested": bool(row["ownership_attested"]),
        }

    def list_scopes(self) -> dict[str, Any]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM network_scopes ORDER BY active DESC, paired_at DESC"
            ).fetchall()
        return {"scopes": [self._render_scope(row) for row in rows]}

    def pair_scope(
        self,
        interface_index: int,
        owns_or_administers: bool,
        display_name: str | None = None,
    ) -> dict[str, Any]:
        if isinstance(interface_index, bool) or int(interface_index) <= 0:
            raise ValueError("interface_index must be a positive integer")
        if owns_or_administers is not True:
            raise PermissionError(
                "Pairing requires an explicit statement that you own or administer this network"
            )
        candidate = next(
            (
                item
                for item in self.scope_candidates()["candidates"]
                if item["interface_index"] == int(interface_index) and item["eligible"]
            ),
            None,
        )
        if candidate is None:
            raise NetworkInventoryError("That physical private-LAN adapter is not pairable")
        gateway_mac = candidate.get("gateway_mac")
        if not gateway_mac:
            # This one packet is part of the explicit pairing action, not a scan.
            _ping_host(str(candidate["gateway_ipv4"]))
            gateway_mac = _gateway_mac(
                _windows_neighbors(),
                candidate["interface_index"],
                candidate["gateway_ipv4"],
            )
        if not gateway_mac:
            raise NetworkInventoryError(
                "Jarvis could not verify the gateway identity, so this network was not paired"
            )
        now_text = _iso(self.clock().astimezone(timezone.utc))
        name = str(display_name or candidate["profile_name"] or candidate["interface_alias"])
        name = name.strip()[:80] or "Owned network"
        with self._lock, self._connect() as connection:
            existing = connection.execute(
                """
                SELECT * FROM network_scopes
                WHERE interface_guid=? AND cidr=? AND gateway_ipv4=? AND gateway_mac=?
                """,
                (
                    candidate["interface_guid"], candidate["scan_cidr"],
                    candidate["gateway_ipv4"], gateway_mac,
                ),
            ).fetchone()
            scope_id = str(existing["scope_id"]) if existing else uuid4().hex
            connection.execute(
                """
                INSERT INTO network_scopes (
                    scope_id, display_name, interface_index, interface_guid,
                    interface_alias, cidr, gateway_ipv4, gateway_mac, adapter_mac,
                    profile_name, network_category, paired_at, last_validated_at,
                    active, ownership_attested
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1)
                ON CONFLICT(scope_id) DO UPDATE SET
                    display_name=excluded.display_name,
                    interface_index=excluded.interface_index,
                    interface_alias=excluded.interface_alias,
                    adapter_mac=excluded.adapter_mac,
                    profile_name=excluded.profile_name,
                    network_category=excluded.network_category,
                    last_validated_at=excluded.last_validated_at,
                    active=1,
                    ownership_attested=1
                """,
                (
                    scope_id, name, candidate["interface_index"],
                    candidate["interface_guid"], candidate["interface_alias"],
                    candidate["scan_cidr"], candidate["gateway_ipv4"], gateway_mac,
                    candidate["adapter_mac"], str(candidate["profile_name"] or ""),
                    candidate["network_category"],
                    str(existing["paired_at"]) if existing else now_text,
                    now_text,
                ),
            )
            row = connection.execute(
                "SELECT * FROM network_scopes WHERE scope_id=?", (scope_id,)
            ).fetchone()
        assert row is not None
        return self._render_scope(row)

    def unpair_scope(self, scope_id: str) -> bool:
        normalized = str(scope_id or "").strip().casefold()
        if not normalized:
            raise ValueError("scope_id is required")
        with self._lock, self._connect() as connection:
            changed = connection.execute(
                "UPDATE network_scopes SET active=0 WHERE scope_id=? AND active=1",
                (normalized,),
            ).rowcount
            connection.execute(
                "DELETE FROM network_scan_leases WHERE scope_id=?", (normalized,)
            )
        return bool(changed)

    def _scope(self, scope_id: str | None) -> dict[str, Any] | None:
        with self._connect() as connection:
            if scope_id:
                row = connection.execute(
                    "SELECT * FROM network_scopes WHERE scope_id=? AND active=1",
                    (str(scope_id).strip().casefold(),),
                ).fetchone()
                if row is None:
                    raise NetworkInventoryError("The selected owned-network scope is not paired")
                return dict(row)
            rows = connection.execute(
                "SELECT * FROM network_scopes WHERE active=1 ORDER BY paired_at"
            ).fetchall()
        if len(rows) == 1:
            return dict(rows[0])
        if len(rows) > 1:
            raise NetworkInventoryError("Choose which paired network Jarvis should check")
        if self.require_paired_scope:
            raise NetworkInventoryError(
                "Pair a network you own or administer before Jarvis checks devices"
            )
        return None

    @staticmethod
    def _validate_collected_scope(
        scope: dict[str, Any], collected: dict[str, Any]
    ) -> None:
        interfaces = collected.get("interfaces")
        if not isinstance(interfaces, list):
            raise NetworkInventoryError("Network discovery did not identify its adapter")
        wanted_index = int(scope.get("interface_index") or 0)
        raw = next(
            (
                item for item in interfaces
                if isinstance(item, dict)
                and int(item.get("interface_index") or 0) == wanted_index
            ),
            None,
        )
        if raw is None:
            raise NetworkInventoryError("Discovery did not use the paired network adapter")
        current = _interface_candidate(raw)
        if current is None:
            raise NetworkInventoryError("The paired adapter is no longer a physical private LAN")
        gateway_mac = _normalize_mac(raw.get("gateway_mac"))
        checks = (
            (current.get("interface_guid"), scope.get("interface_guid")),
            (current.get("scan_cidr"), scope.get("cidr")),
            (current.get("gateway_ipv4"), scope.get("gateway_ipv4")),
            (current.get("adapter_mac"), scope.get("adapter_mac")),
            (gateway_mac, scope.get("gateway_mac")),
        )
        if any(
            not str(current_value or "").strip()
            or str(current_value).strip().casefold()
            != str(stored_value or "").strip().casefold()
            for current_value, stored_value in checks
        ):
            raise NetworkInventoryError(
                "The current adapter, route, or gateway no longer matches the paired network"
            )

    @staticmethod
    def _synthetic_scope(collected: dict[str, Any]) -> dict[str, Any]:
        interfaces = collected.get("interfaces")
        if not isinstance(interfaces, list) or not interfaces:
            raise NetworkInventoryError("Synthetic discovery did not declare its private scope")
        item = interfaces[0]
        cidr = str(item.get("scan_cidr") or item.get("scan_range") or "")
        network = ipaddress.ip_network(cidr, strict=False)
        if not isinstance(network, ipaddress.IPv4Network) or not _is_rfc1918(network.network_address + 1):
            raise NetworkInventoryError("Synthetic discovery scope must be RFC1918 IPv4")
        return {
            "scope_id": "test-unpaired-scope",
            "display_name": "Test private LAN",
            "cidr": str(network),
            "interface_index": int(item.get("interface_index") or 1),
        }

    def _acquire_scan(self, scope_id: str, now: datetime) -> tuple[str, int]:
        owner = uuid4().hex
        now_text = _iso(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            lease = connection.execute(
                "SELECT leased_until FROM network_scan_leases WHERE scope_id=?",
                (scope_id,),
            ).fetchone()
            if lease is not None:
                remaining = int((_parse_time(str(lease["leased_until"])) - now).total_seconds())
                if remaining > 0:
                    raise NetworkInventoryRateLimited(
                        "A network check is already running", retry_after_seconds=remaining
                    )
            last = connection.execute(
                """
                SELECT completed_at FROM network_scans
                WHERE scope_id=? AND status='completed'
                ORDER BY id DESC LIMIT 1
                """,
                (scope_id,),
            ).fetchone()
            if self.min_scan_interval_seconds and last and last["completed_at"]:
                elapsed = int((now - _parse_time(str(last["completed_at"]))).total_seconds())
                if elapsed < self.min_scan_interval_seconds:
                    retry = self.min_scan_interval_seconds - max(0, elapsed)
                    raise NetworkInventoryRateLimited(
                        "Please wait briefly before checking this network again",
                        retry_after_seconds=retry,
                    )
            if self.max_scans_per_hour:
                since = _iso(now - timedelta(hours=1))
                count = int(connection.execute(
                    """
                    SELECT COUNT(*) FROM network_scans
                    WHERE scope_id=? AND status='completed' AND completed_at>=?
                    """,
                    (scope_id, since),
                ).fetchone()[0])
                if count >= self.max_scans_per_hour:
                    oldest = connection.execute(
                        """
                        SELECT completed_at FROM network_scans
                        WHERE scope_id=? AND status='completed' AND completed_at>=?
                        ORDER BY completed_at LIMIT 1
                        """,
                        (scope_id, since),
                    ).fetchone()
                    retry = 60
                    if oldest and oldest["completed_at"]:
                        retry = max(
                            1,
                            int(
                                (
                                    _parse_time(str(oldest["completed_at"]))
                                    + timedelta(hours=1)
                                    - now
                                ).total_seconds()
                            ),
                        )
                    raise NetworkInventoryRateLimited(
                        "The hourly network-check limit has been reached",
                        retry_after_seconds=retry,
                    )
            leased_until = _iso(now + timedelta(seconds=self.lease_seconds))
            connection.execute(
                """
                INSERT INTO network_scan_leases(scope_id, owner, leased_until)
                VALUES (?, ?, ?)
                ON CONFLICT(scope_id) DO UPDATE SET
                    owner=excluded.owner, leased_until=excluded.leased_until
                """,
                (scope_id, owner, leased_until),
            )
            cursor = connection.execute(
                """
                INSERT INTO network_scans (
                    observed_at, observed_devices, candidate_hosts, range_truncated,
                    scope_id, status, responsive_hosts, method
                ) VALUES (?, 0, 0, 0, ?, 'running', 0, '')
                """,
                (now_text, scope_id),
            )
            scan_id = int(cursor.lastrowid)
        return owner, scan_id

    def _release_scan(self, scope_id: str, owner: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM network_scan_leases WHERE scope_id=? AND owner=?",
                (scope_id, owner),
            )

    @staticmethod
    def _identity(scope_id: str, observation: dict[str, Any]) -> str:
        mac = _normalize_mac(observation.get("mac"))
        return f"{scope_id}:mac:{mac}" if mac else f"{scope_id}:ip:{observation['ipv4']}"

    @staticmethod
    def _event(
        connection: sqlite3.Connection,
        *,
        scan_id: int | None,
        device_uuid: str | None,
        scope_id: str,
        observed_at: str,
        event_type: str,
        summary: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO network_events (
                scan_id, device_uuid, scope_id, observed_at,
                event_type, summary, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scan_id, device_uuid, scope_id, observed_at,
                event_type[:60], summary[:500],
                json.dumps(details or {}, sort_keys=True, separators=(",", ":")),
            ),
        )

    def scan(
        self,
        *,
        max_hosts: int = DEFAULT_SCAN_HOSTS,
        include_offline: bool = True,
        scope_id: str | None = None,
        include_identifiers: bool = False,
    ) -> dict[str, Any]:
        if isinstance(max_hosts, bool) or not 1 <= int(max_hosts) <= MAX_SCAN_HOSTS:
            raise ValueError(f"max_hosts must be between 1 and {MAX_SCAN_HOSTS}")
        now = self.clock().astimezone(timezone.utc)
        scope = self._scope(scope_id)
        collected: dict[str, Any] | None = None
        if scope is None:
            collected = self.discoverer(int(max_hosts))
            scope = self._synthetic_scope(collected)
        scope_key = str(scope["scope_id"])
        owner, scan_id = self._acquire_scan(scope_key, now)
        try:
            if collected is None:
                if self.discoverer is discover_private_lan:
                    collected = self.discoverer(int(max_hosts), scope=scope)
                else:
                    collected = self.discoverer(int(max_hosts))
                    self._validate_collected_scope(scope, collected)
            observations = collected.get("observations")
            if not isinstance(observations, list):
                raise NetworkInventoryError("Network discoverer returned invalid observations")
            result = self._persist_scan(
                scope,
                collected,
                observations,
                scan_id=scan_id,
                now=now,
                include_offline=include_offline,
                include_identifiers=include_identifiers,
            )
            try:
                result["security_assessment"] = self._record_security_receipt(
                    scope_id=scope_id
                )
            except (OSError, RuntimeError, TypeError, ValueError, sqlite3.Error):
                # A valid inventory observation remains valid if the separate,
                # read-only assessment layer cannot write its receipt.
                result["security_assessment"] = {
                    "posture": "assessment_unavailable",
                    "conclusion": (
                        "Network evidence was saved, but its security assessment receipt "
                        "could not be generated. No containment action was taken."
                    ),
                    "signals": [],
                    "automatic_containment": {"enabled": False, "actions_taken": 0},
                }
            return result
        except Exception as exc:
            with self._connect() as connection:
                connection.execute(
                    """
                    UPDATE network_scans SET status='failed', completed_at=?, error_code=?
                    WHERE id=? AND status='running'
                    """,
                    (_iso(self.clock().astimezone(timezone.utc)), type(exc).__name__, scan_id),
                )
            raise
        finally:
            self._release_scan(scope_key, owner)

    def _persist_scan(
        self,
        scope: dict[str, Any],
        collected: dict[str, Any],
        observations: list[Any],
        *,
        scan_id: int,
        now: datetime,
        include_offline: bool,
        include_identifiers: bool,
    ) -> dict[str, Any]:
        now_text = _iso(now)
        scope_id = str(scope["scope_id"])
        network = ipaddress.ip_network(str(scope["cidr"]), strict=True)
        prepared: list[dict[str, Any]] = []
        mac_counts: dict[str, int] = {}
        for observation in observations[: MAX_SCAN_HOSTS + 8]:
            if not isinstance(observation, dict):
                continue
            raw_interface_index = observation.get("interface_index")
            if raw_interface_index is not None:
                try:
                    if int(raw_interface_index) != int(scope.get("interface_index") or 0):
                        continue
                except (TypeError, ValueError):
                    continue
            address = _host_in_scope(observation.get("ipv4"), network)
            if address is None:
                continue
            mac = _normalize_mac(observation.get("mac"))
            if mac:
                mac_counts[mac] = mac_counts.get(mac, 0) + 1
            actively_reachable = bool(
                observation.get("actively_reachable")
                if "actively_reachable" in observation
                else str(observation.get("visibility") or "") in {"active", "active_probe", "local_host"}
            )
            prepared.append({
                "ipv4": str(address),
                "mac": mac,
                "hostname": str(observation.get("hostname") or "")[:255] or None,
                "visibility": str(observation.get("visibility") or "unknown")[:40],
                "neighbor_state": str(observation.get("neighbor_state") or "Unknown")[:40],
                "actively_reachable": actively_reachable,
                "cached": bool(observation.get("cached")) or not actively_reachable,
            })
        for item in prepared:
            if item["mac"] and mac_counts.get(item["mac"], 0) > 1:
                item["mac_conflict"] = item["mac"]
                item["mac"] = None

        active_now: set[str] = set()
        cached_now: set[str] = set()
        observed_now: set[str] = set()
        new_now: set[str] = set()
        scan_events: list[dict[str, Any]] = []
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior_scan = connection.execute(
                """
                SELECT id, completed_at FROM network_scans
                WHERE scope_id=? AND status='completed' AND id<>?
                ORDER BY id DESC LIMIT 1
                """,
                (scope_id, scan_id),
            ).fetchone()
            baseline_created = prior_scan is None
            prior_active: set[str] = set()
            if prior_scan is not None:
                prior_active = {
                    str(row["device_uuid"])
                    for row in connection.execute(
                        """
                        SELECT device_uuid FROM network_observations
                        WHERE scan_id=? AND actively_reachable=1
                        """,
                        (int(prior_scan["id"]),),
                    ).fetchall()
                }
            for clean in prepared:
                identity = self._identity(scope_id, clean)
                prior = connection.execute(
                    "SELECT * FROM network_devices WHERE identity=?", (identity,)
                ).fetchone()
                promoted = False
                # An IP-only identity is deliberately short-lived. After a real
                # observation gap, DHCP reuse must create a new opaque device
                # record instead of inheriting another machine's history/labels.
                if prior is not None and not clean["mac"] and not prior["mac"]:
                    ip_age = (
                        now - _parse_time(str(prior["last_seen"]))
                    ).total_seconds()
                    if ip_age > CONTINUITY_GAP_SECONDS:
                        old_uuid = str(prior["device_uuid"])
                        connection.execute(
                            "UPDATE network_devices SET identity=? WHERE identity=?",
                            (f"{identity}:expired:{old_uuid[:12]}", identity),
                        )
                        self._event(
                            connection, scan_id=scan_id, device_uuid=old_uuid,
                            scope_id=scope_id, observed_at=now_text,
                            event_type="ip_identity_expired",
                            summary=(
                                "An IP-only identity expired after an observation gap; "
                                "a later occupant will not inherit its trust labels"
                            ),
                        )
                        prior = None
                if prior is None and clean["mac"]:
                    ip_identity = f"{scope_id}:ip:{clean['ipv4']}"
                    candidate = connection.execute(
                        "SELECT * FROM network_devices WHERE identity=?", (ip_identity,)
                    ).fetchone()
                    if candidate is None:
                        candidate = connection.execute(
                            """
                            SELECT * FROM network_devices
                            WHERE scope_id IS NULL AND ipv4=? AND mac IS NULL
                            ORDER BY last_seen DESC LIMIT 1
                            """,
                            (clean["ipv4"],),
                        ).fetchone()
                    if candidate is not None:
                        age = (now - _parse_time(str(candidate["last_seen"]))).total_seconds()
                        if age <= CONTINUITY_GAP_SECONDS:
                            prior = candidate
                            promoted = True
                if prior is None and clean["mac"]:
                    prior = connection.execute(
                        """
                        SELECT * FROM network_devices
                        WHERE scope_id IS NULL AND mac=?
                        ORDER BY last_seen DESC LIMIT 1
                        """,
                        (clean["mac"],),
                    ).fetchone()
                    promoted = prior is not None

                if prior is None:
                    device_uuid = uuid4().hex
                    first_seen = now_text
                    continuous_since = now_text
                    seen_count = 1
                    label = device_type = profile_updated_at = None
                    trust_state = "unreviewed"
                    new_now.add(device_uuid)
                    event_type = "baseline_observed" if baseline_created else "new_device_observed"
                    summary = (
                        "Device included in the initial observation baseline"
                        if baseline_created
                        else "A device not previously recorded in this paired scope was observed"
                    )
                    self._event(
                        connection, scan_id=scan_id, device_uuid=device_uuid,
                        scope_id=scope_id, observed_at=now_text,
                        event_type=event_type, summary=summary,
                    )
                    scan_events.append({"device_id": device_uuid, "event_type": event_type, "summary": summary})
                else:
                    device_uuid = str(prior["device_uuid"])
                    first_seen = str(prior["first_seen"])
                    seen_count = int(prior["seen_count"]) + 1
                    label = prior["label"]
                    device_type = prior["device_type"]
                    trust_state = str(prior["trust_state"] or "unreviewed")
                    profile_updated_at = prior["profile_updated_at"]
                    was_active = device_uuid in prior_active
                    last_active = str(prior["last_active_seen"] or "")
                    within_gap = bool(
                        last_active
                        and (now - _parse_time(last_active)).total_seconds()
                        <= CONTINUITY_GAP_SECONDS
                    )
                    continuous_since = (
                        str(prior["continuous_since"])
                        if clean["actively_reachable"] and was_active and within_gap
                        else (now_text if clean["actively_reachable"] else str(prior["continuous_since"]))
                    )
                    if str(prior["ipv4"]) != clean["ipv4"]:
                        summary = "The latest observed IPv4 address for this device changed"
                        self._event(
                            connection, scan_id=scan_id, device_uuid=device_uuid,
                            scope_id=scope_id, observed_at=now_text,
                            event_type="identifier_changed", summary=summary,
                        )
                        scan_events.append({"device_id": device_uuid, "event_type": "identifier_changed", "summary": summary})
                    if promoted:
                        connection.execute(
                            "DELETE FROM network_devices WHERE identity=?",
                            (str(prior["identity"]),),
                        )
                        self._event(
                            connection, scan_id=scan_id, device_uuid=device_uuid,
                            scope_id=scope_id, observed_at=now_text,
                            event_type="identity_strengthened",
                            summary="A recent IP-only record gained MAC evidence; trust was not changed",
                        )

                confidence, basis = _mac_identity(clean["mac"])
                if clean.get("mac_conflict"):
                    confidence = "limited"
                    basis = "conflicting duplicate MAC evidence; correlated by IPv4 only"
                last_active_seen = now_text if clean["actively_reachable"] else (
                    str(prior["last_active_seen"]) if prior is not None and prior["last_active_seen"] else None
                )
                connection.execute(
                    """
                    INSERT INTO network_devices (
                        identity, device_uuid, scope_id, mac, ipv4, hostname,
                        first_seen, last_seen, continuous_since, last_active_seen,
                        seen_count, visibility, neighbor_state, label, trust_state,
                        device_type, identity_confidence, identity_basis,
                        profile_updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(identity) DO UPDATE SET
                        device_uuid=excluded.device_uuid,
                        scope_id=excluded.scope_id,
                        mac=excluded.mac,
                        ipv4=excluded.ipv4,
                        hostname=COALESCE(excluded.hostname, network_devices.hostname),
                        last_seen=excluded.last_seen,
                        continuous_since=excluded.continuous_since,
                        last_active_seen=COALESCE(excluded.last_active_seen, network_devices.last_active_seen),
                        seen_count=excluded.seen_count,
                        visibility=excluded.visibility,
                        neighbor_state=excluded.neighbor_state,
                        identity_confidence=excluded.identity_confidence,
                        identity_basis=excluded.identity_basis
                    """,
                    (
                        identity, device_uuid, scope_id, clean["mac"], clean["ipv4"],
                        clean["hostname"], first_seen, now_text, continuous_since,
                        last_active_seen, seen_count, clean["visibility"],
                        clean["neighbor_state"], label, trust_state, device_type,
                        confidence, basis, profile_updated_at,
                    ),
                )
                observed_now.add(device_uuid)
                if clean["actively_reachable"]:
                    active_now.add(device_uuid)
                elif clean["cached"]:
                    cached_now.add(device_uuid)
                connection.execute(
                    """
                    INSERT INTO network_observations (
                        scan_id, device_uuid, scope_id, observed_at, ipv4, mac,
                        hostname, evidence, actively_reachable, cached, neighbor_state
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        scan_id, device_uuid, scope_id, now_text, clean["ipv4"],
                        clean["mac"], clean["hostname"], clean["visibility"],
                        1 if clean["actively_reachable"] else 0,
                        1 if clean["cached"] else 0, clean["neighbor_state"],
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO network_device_addresses (
                        device_uuid, ipv4, first_seen, last_seen, seen_count
                    ) VALUES (?, ?, ?, ?, 1)
                    ON CONFLICT(device_uuid, ipv4) DO UPDATE SET
                        last_seen=excluded.last_seen,
                        seen_count=network_device_addresses.seen_count+1
                    """,
                    (device_uuid, clean["ipv4"], now_text, now_text),
                )
                if clean["actively_reachable"]:
                    open_session = connection.execute(
                        """
                        SELECT * FROM network_presence_sessions
                        WHERE device_uuid=? AND ended_at IS NULL
                        ORDER BY id DESC LIMIT 1
                        """,
                        (device_uuid,),
                    ).fetchone()
                    if open_session is not None and device_uuid in prior_active:
                        connection.execute(
                            """
                            UPDATE network_presence_sessions
                            SET last_reachable_at=?, observation_count=observation_count+1
                            WHERE id=?
                            """,
                            (now_text, int(open_session["id"])),
                        )
                    else:
                        if open_session is not None:
                            connection.execute(
                                "UPDATE network_presence_sessions SET ended_at=? WHERE id=?",
                                (str(open_session["last_reachable_at"]), int(open_session["id"])),
                            )
                        connection.execute(
                            """
                            INSERT INTO network_presence_sessions (
                                device_uuid, scope_id, started_at, last_reachable_at,
                                ended_at, observation_count
                            ) VALUES (?, ?, ?, ?, NULL, 1)
                            """,
                            (device_uuid, scope_id, now_text, now_text),
                        )
                        if prior is not None and device_uuid not in prior_active:
                            summary = "The device responded during this check"
                            self._event(
                                connection, scan_id=scan_id, device_uuid=device_uuid,
                                scope_id=scope_id, observed_at=now_text,
                                event_type="presence_reachable", summary=summary,
                            )
                            scan_events.append({"device_id": device_uuid, "event_type": "presence_reachable", "summary": summary})

            for device_uuid in sorted(prior_active - active_now):
                connection.execute(
                    """
                    UPDATE network_presence_sessions
                    SET ended_at=last_reachable_at
                    WHERE device_uuid=? AND ended_at IS NULL
                    """,
                    (device_uuid,),
                )
                summary = (
                    "The device did not respond during this check; current presence is unknown"
                )
                self._event(
                    connection, scan_id=scan_id, device_uuid=device_uuid,
                    scope_id=scope_id, observed_at=now_text,
                    event_type="presence_not_observed", summary=summary,
                )
                scan_events.append({"device_id": device_uuid, "event_type": "presence_not_observed", "summary": summary})

            if scope_id != "test-unpaired-scope":
                connection.execute(
                    "UPDATE network_scopes SET last_validated_at=? WHERE scope_id=?",
                    (now_text, scope_id),
                )
            connection.execute(
                """
                UPDATE network_scans SET
                    status='completed', completed_at=?, observed_devices=?,
                    candidate_hosts=?, range_truncated=?, responsive_hosts=?, method=?
                WHERE id=?
                """,
                (
                    now_text, len(observed_now), int(collected.get("candidate_hosts") or 0),
                    1 if collected.get("range_truncated") else 0,
                    int(collected.get("responsive_hosts") or len(active_now)),
                    str(collected.get("method") or "private-LAN observation")[:300],
                    scan_id,
                ),
            )
            connection.execute(
                """
                DELETE FROM network_events WHERE id NOT IN (
                    SELECT id FROM network_events ORDER BY id DESC LIMIT ?
                )
                """,
                (MAX_EVENT_HISTORY,),
            )
            connection.execute(
                """
                DELETE FROM network_observations WHERE id NOT IN (
                    SELECT id FROM network_observations ORDER BY id DESC LIMIT ?
                )
                """,
                (MAX_OBSERVATION_HISTORY,),
            )
            rows = connection.execute(
                """
                SELECT * FROM network_devices
                WHERE scope_id=? OR (?='test-unpaired-scope' AND scope_id IS NULL)
                ORDER BY first_seen, device_uuid LIMIT 4096
                """,
                (scope_id, scope_id),
            ).fetchall()
        devices = self._render_rows(
            rows, active=active_now, cached=cached_now, new=new_now,
            now=now, include_identifiers=include_identifiers,
        )
        if not include_offline:
            devices = [item for item in devices if item["visible_now"]]
        limited = [item for item in devices if item["identity_confidence"] == "limited"]
        return {
            "scan_id": scan_id,
            "scope_id": scope_id,
            "scope_name": str(scope.get("display_name") or "Paired network"),
            "observed_at": now_text,
            "devices": devices,
            "visible_devices": len([item for item in devices if item["visible_now"]]),
            "cached_devices": len([item for item in devices if item["cached_now"]]),
            "new_devices": len([item for item in devices if item["is_new"]]),
            "known_devices": len(devices),
            "total_known_devices": len(rows),
            "interfaces": collected.get("interfaces", []) if include_identifiers else [],
            "candidate_hosts": int(collected.get("candidate_hosts") or 0),
            "range_truncated": bool(collected.get("range_truncated")),
            "method": str(collected.get("method") or "private-LAN observation"),
            "events": scan_events,
            "duration_basis": (
                "Jarvis-observed active reachability; this is not authoritative "
                "router Wi-Fi/DHCP association time."
            ),
            "security_summary": {
                "baseline_created": baseline_created,
                "review_new_devices": 0 if baseline_created else len(new_now),
                "limited_identity_devices": len(limited),
                "coverage_complete_for_selected_range": not bool(collected.get("range_truncated")),
                "advice": (
                    "Initial baseline created. Label devices you recognize; discovery alone does not trust them."
                    if baseline_created
                    else (
                        "Review newly observed devices. A trust label never grants device access."
                        if new_now
                        else "No new device record was created in this check."
                    )
                ),
            },
            "limitations": self._limitations(),
        }

    @staticmethod
    def _limitations() -> list[str]:
        return [
            "Only the explicitly paired physical LAN scope is checked.",
            "A cached neighbor is shown as cached, not online; sleeping or ICMP-blocking devices may be missed.",
            "Other VLANs, guest networks, client-isolated Wi-Fi, and IPv6 are not checked.",
            "No ports, services, credentials, packets, private files, or vulnerabilities are inspected.",
            "IP and MAC evidence can be reassigned or spoofed and never grants trust or access.",
        ]

    def _last_scan_sets(
        self, connection: sqlite3.Connection, scope_id: str | None = None
    ) -> tuple[sqlite3.Row | None, set[str], set[str]]:
        if scope_id:
            last = connection.execute(
                """
                SELECT * FROM network_scans
                WHERE status='completed' AND scope_id=? ORDER BY id DESC LIMIT 1
                """,
                (scope_id,),
            ).fetchone()
        else:
            last = connection.execute(
                "SELECT * FROM network_scans WHERE status='completed' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if last is None:
            return None, set(), set()
        observations = connection.execute(
            "SELECT device_uuid, actively_reachable, cached FROM network_observations WHERE scan_id=?",
            (int(last["id"]),),
        ).fetchall()
        active = {str(row["device_uuid"]) for row in observations if row["actively_reachable"]}
        cached = {
            str(row["device_uuid"])
            for row in observations
            if row["cached"] and not row["actively_reachable"]
        }
        return last, active, cached

    def list_devices(
        self,
        *,
        include_offline: bool = True,
        include_identifiers: bool = False,
        include_unpaired: bool = False,
    ) -> dict[str, Any]:
        now = self.clock().astimezone(timezone.utc)
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                (
                    "SELECT * FROM network_devices "
                    + ("" if include_unpaired else "WHERE scope_id IS NOT NULL ")
                    + "ORDER BY last_seen DESC, device_uuid LIMIT 4096"
                )
            ).fetchall()
            last_scan, active, cached = self._last_scan_sets(connection)
        devices = self._render_rows(
            rows, active=active, cached=cached, new=set(), now=now,
            include_identifiers=include_identifiers,
        )
        if not include_offline:
            devices = [item for item in devices if item["visible_now"]]
        return {
            "last_scan_at": str(last_scan["completed_at"]) if last_scan else None,
            "devices": devices,
            "visible_devices": len([item for item in devices if item["visible_now"]]),
            "cached_devices": len([item for item in devices if item["cached_now"]]),
            "known_devices": len(devices),
            "last_scan_id": int(last_scan["id"]) if last_scan else None,
            "last_scan_scope_id": str(last_scan["scope_id"] or "") if last_scan else None,
            "coverage_complete_for_selected_range": (
                not bool(last_scan["range_truncated"]) if last_scan else None
            ),
            "candidate_hosts": int(last_scan["candidate_hosts"]) if last_scan else 0,
            "responsive_hosts": int(last_scan["responsive_hosts"]) if last_scan else 0,
            "visibility_basis": (
                "Reachable or cached at the last completed check; run a fresh check for current evidence."
            ),
            "duration_basis": (
                "Historical Jarvis reachability observations, not router association time."
            ),
            "limitations": self._limitations(),
        }

    def status(self, *, include_identifiers: bool = False) -> dict[str, Any]:
        inventory = self.list_devices(
            include_offline=True, include_identifiers=include_identifiers
        )
        scopes = self.list_scopes()["scopes"]
        active_scopes = [item for item in scopes if item.get("active")]
        security_assessments: list[dict[str, Any]] = []
        for scope in active_scopes:
            try:
                assessment = self.security_assessment(scope_id=str(scope["scope_id"]))
            except (OSError, RuntimeError, TypeError, ValueError, sqlite3.Error):
                continue
            assessment = dict(assessment)
            assessment["scope_id"] = str(scope["scope_id"])
            assessment["scope_name"] = str(
                scope.get("display_name") or "Paired network"
            )
            security_assessments.append(assessment)
        try:
            if len(active_scopes) > 1:
                raise ValueError("Multiple active network scopes require selection")
            security_assessment = (
                security_assessments[0]
                if security_assessments
                else self.security_assessment()
            )
        except (OSError, RuntimeError, TypeError, ValueError, sqlite3.Error):
            security_assessment = {
                "posture": (
                    "scope_selection_required"
                    if len(active_scopes) > 1
                    else "assessment_unavailable"
                ),
                "highest_severity": "none",
                "attention_signal_count": 0,
                "signals": [],
                "conclusion": (
                    "Multiple paired networks are active. Review the separately "
                    "named assessment for each network; Jarvis will not present one "
                    "network's evidence as the posture of every network."
                    if len(active_scopes) > 1
                    else "Stored inventory remains available, but its security receipt "
                    "could not be verified. No containment action was taken."
                ),
                "automatic_containment": {"enabled": False, "actions_taken": 0},
            }
        if not include_identifiers:
            scopes = [
                {
                    key: value
                    for key, value in item.items()
                    if key in {
                        "scope_id", "display_name", "paired_at",
                        "last_validated_at", "active", "ownership_attested",
                    }
                }
                for item in scopes
            ]
        try:
            pending_incidents = self.pending_incidents(
                limit=50, include_identifiers=include_identifiers
            )
        except (OSError, RuntimeError, TypeError, ValueError, sqlite3.Error):
            pending_incidents = {
                "pending_count": 0,
                "incidents": [],
                "integrity_failures": [{"reason": "incident_store_unavailable"}],
                "alerts_survive_restart": True,
                "automatic_containment": False,
            }
        return {
            "paired": any(item["active"] for item in scopes),
            "scopes": scopes,
            "inventory": inventory,
            "security_assessment": security_assessment,
            "security_assessments": security_assessments,
            "pending_incidents": pending_incidents,
            "scan_policy": {
                "max_hosts": MAX_SCAN_HOSTS,
                "cooldown_seconds": self.min_scan_interval_seconds,
                "max_scans_per_hour": self.max_scans_per_hour,
                "active_probe": "one bounded ICMP echo per candidate",
                "ports_or_services_scanned": False,
            },
        }

    def _security_input_snapshot(
        self, scope_id: str | None = None
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Read one scope's evidence from a single SQLite snapshot."""
        requested_scope = str(scope_id or "").strip().casefold() or None
        now = self.clock().astimezone(timezone.utc)
        with self._connect() as connection:
            connection.execute("BEGIN")
            if requested_scope is not None:
                exists = connection.execute(
                    "SELECT 1 FROM network_scopes WHERE scope_id=?",
                    (requested_scope,),
                ).fetchone()
                if exists is None:
                    raise KeyError("Network scope was not found")
                selected_scope = requested_scope
            else:
                active_scope_rows = connection.execute(
                    """
                    SELECT scope_id FROM network_scopes WHERE active=1
                    ORDER BY paired_at DESC LIMIT 2
                    """
                ).fetchall()
                if len(active_scope_rows) > 1:
                    raise ValueError(
                        "Multiple active network scopes require an explicit scope_id"
                    )
                latest = (
                    active_scope_rows[0] if active_scope_rows else connection.execute(
                    """
                    SELECT scope_id FROM network_scans
                    WHERE status='completed' AND scope_id IS NOT NULL
                    ORDER BY id DESC LIMIT 1
                    """
                    ).fetchone()
                )
                if latest is None:
                    latest = connection.execute(
                        """
                        SELECT scope_id FROM network_scopes
                        ORDER BY active DESC, paired_at DESC LIMIT 1
                        """
                    ).fetchone()
                selected_scope = (
                    str(latest["scope_id"]) if latest is not None else None
                )
            if selected_scope is None:
                last_scan = None
                rows: list[sqlite3.Row] = []
                active: set[str] = set()
                cached: set[str] = set()
                event_rows: list[sqlite3.Row] = []
                baseline_scan = False
            else:
                last_scan, active, cached = self._last_scan_sets(
                    connection, selected_scope
                )
                rows = connection.execute(
                    """
                    SELECT * FROM network_devices WHERE scope_id=?
                    ORDER BY last_seen DESC, device_uuid LIMIT 4096
                    """,
                    (selected_scope,),
                ).fetchall()
                if last_scan is None:
                    event_rows = []
                    baseline_scan = False
                else:
                    event_rows = connection.execute(
                        """
                        SELECT id, device_uuid, observed_at, event_type
                        FROM network_events WHERE scope_id=? AND scan_id=?
                        ORDER BY id DESC LIMIT 1024
                        """,
                        (selected_scope, int(last_scan["id"])),
                    ).fetchall()
                    completed_count = connection.execute(
                        """
                        SELECT COUNT(*) FROM network_scans
                        WHERE scope_id=? AND status='completed' AND id<=?
                        """,
                        (selected_scope, int(last_scan["id"])),
                    ).fetchone()[0]
                    baseline_scan = int(completed_count) == 1
        devices = self._render_rows(
            rows,
            active=active,
            cached=cached,
            new=set(),
            now=now,
            include_identifiers=False,
        )
        inventory = {
            "last_scan_at": str(last_scan["completed_at"]) if last_scan else None,
            "last_scan_id": int(last_scan["id"]) if last_scan else None,
            "last_scan_scope_id": selected_scope,
            "coverage_complete_for_selected_range": (
                not bool(last_scan["range_truncated"]) if last_scan else None
            ),
            "candidate_hosts": int(last_scan["candidate_hosts"]) if last_scan else 0,
            "responsive_hosts": int(last_scan["responsive_hosts"]) if last_scan else 0,
            "baseline_scan": baseline_scan,
            "devices": devices,
        }
        events = [
            {
                "event_id": int(row["id"]),
                "device_id": row["device_uuid"],
                "observed_at": str(row["observed_at"]),
                "event_type": str(row["event_type"]),
            }
            for row in event_rows
        ]
        return inventory, events

    def _record_security_receipt(
        self, *, scope_id: str | None = None
    ) -> dict[str, Any]:
        inventory, event_rows = self._security_input_snapshot(scope_id)
        assessment = assess_network_defense(
            inventory,
            event_rows,
            now=self.clock().astimezone(timezone.utc),
        )
        with self._lock, self._connect() as connection:
            existing = connection.execute(
                """
                SELECT assessment_id, scan_id, scope_id, ruleset_version,
                       created_at, input_sha256, receipt_sha256, receipt_json
                FROM network_security_receipts WHERE assessment_id=?
                """,
                (str(assessment["assessment_id"]),),
            ).fetchone()
            if existing is not None:
                stored = self._verified_security_receipt(existing)
                if self.incidents_enabled:
                    self._record_incident_alerts(connection, stored)
                self._maintain_incident_retention(
                    connection, now=self.clock().astimezone(timezone.utc)
                )
                return stored
            connection.execute(
                """
                INSERT OR IGNORE INTO network_security_receipts (
                    assessment_id, scan_id, scope_id, ruleset_version,
                    created_at, input_sha256, receipt_sha256, receipt_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(assessment["assessment_id"]),
                    inventory.get("last_scan_id"),
                    inventory.get("last_scan_scope_id"),
                    NETWORK_DEFENSE_RULESET_VERSION,
                    str(assessment["generated_at"]),
                    str(assessment["input_sha256"]),
                    str(assessment["receipt_sha256"]),
                    json.dumps(assessment, sort_keys=True, separators=(",", ":")),
                ),
            )
            row = connection.execute(
                """
                SELECT assessment_id, scan_id, scope_id, ruleset_version,
                       created_at, input_sha256, receipt_sha256, receipt_json
                FROM network_security_receipts WHERE assessment_id=?
                """,
                (str(assessment["assessment_id"]),),
            ).fetchone()
            if row is not None:
                stored = self._verified_security_receipt(row)
                if self.incidents_enabled:
                    self._record_incident_alerts(connection, stored)
                self._maintain_incident_retention(
                    connection, now=self.clock().astimezone(timezone.utc)
                )
            connection.execute(
                """
                DELETE FROM network_security_receipts
                WHERE assessment_id NOT IN (
                    SELECT assessment_id FROM network_security_receipts
                    ORDER BY created_at DESC, assessment_id DESC LIMIT ?
                )
                """,
                (MAX_SECURITY_RECEIPTS,),
            )
        if row is None:
            raise NetworkInventoryError("Network security receipt was not stored")
        return self._verified_security_receipt(row)

    @staticmethod
    def _incident_text(value: Any, limit: int) -> str:
        raw = str(value or "")
        raw = re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", raw)
        raw = re.sub(r"[\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]", "", raw)
        text = " ".join(raw.split())
        return text[:limit]

    @classmethod
    def _incident_device(
        cls, connection: sqlite3.Connection, device_id: str | None
    ) -> dict[str, Any] | None:
        if not device_id or not re.fullmatch(r"[0-9a-f]{32}", device_id):
            return None
        row = connection.execute(
            """
            SELECT device_uuid, label, device_type,
                   identity_confidence, identity_basis
            FROM network_devices WHERE device_uuid=?
            """,
            (device_id,),
        ).fetchone()
        if row is None:
            return {
                "device_id": device_id,
                "display_name": f"Observed device {device_id[:6]}",
                "device_type": "Unknown",
                "manufacturer": "Unknown",
                "identity_confidence": "limited",
                "identity_basis": "Device details were unavailable",
            }
        label = cls._incident_text(row["label"], 120)
        opaque_name = f"Observed device {device_id[:6]}"
        return {
            "device_id": str(row["device_uuid"]),
            "display_name": label or opaque_name,
            "device_type": cls._incident_text(row["device_type"], 80) or "Unknown",
            "manufacturer": "Unknown",
            "identity_confidence": cls._incident_text(
                row["identity_confidence"], 24
            ) or "limited",
            "identity_basis": cls._incident_text(row["identity_basis"], 240),
        }

    @classmethod
    def _incident_payload(
        cls,
        connection: sqlite3.Connection,
        assessment: dict[str, Any],
        signal: dict[str, Any],
    ) -> dict[str, Any]:
        evidence = signal.get("evidence")
        evidence = evidence if isinstance(evidence, dict) else {}
        evidence_summary: list[str] = []
        presence = cls._incident_text(evidence.get("presence_state"), 40)
        trust = cls._incident_text(evidence.get("trust_state"), 40)
        if presence:
            evidence_summary.append(f"Latest observed presence: {presence}")
        if trust:
            evidence_summary.append(f"Operator trust label: {trust}")
        if evidence.get("observed_at"):
            evidence_summary.append(
                "Signal observed at " + cls._incident_text(evidence["observed_at"], 80)
            )
        if evidence.get("last_active_seen"):
            evidence_summary.append(
                "Last active evidence at "
                + cls._incident_text(evidence["last_active_seen"], 80)
            )
        if not evidence_summary:
            evidence_summary.append(
                "Derived from the latest integrity-checked Jarvis network assessment receipt"
            )
        device_id = cls._incident_text(signal.get("device_id"), 64) or None
        observed_fact = cls._incident_text(signal.get("summary"), 500)
        return {
            "assessment_id": cls._incident_text(assessment.get("assessment_id"), 64),
            "signal_id": cls._incident_text(signal.get("signal_id"), 64),
            "scope_id": cls._incident_text(assessment.get("scope_id"), 64) or None,
            "created_at": cls._incident_text(assessment.get("generated_at"), 80),
            "severity": cls._incident_text(signal.get("severity"), 24) or "medium",
            "category": cls._incident_text(signal.get("category"), 80) or "network_review",
            "device": cls._incident_device(connection, device_id),
            "observed_fact": observed_fact,
            "assessment": (
                "Jarvis detected a review signal. This is a hypothesis requiring "
                "corroboration, not proof that the device or network is compromised."
            ),
            "confidence": cls._incident_text(signal.get("confidence"), 24) or "limited",
            "compromise_established": False,
            "evidence_summary": evidence_summary[:8],
            "automatic_actions": [],
            "actions_not_taken": [
                "No device was blocked, disconnected, quarantined, or modified.",
                "No router, firewall, account, credential, or endpoint setting was changed.",
                "No exploit, credential attack, evasion, persistence, or destructive test was attempted.",
            ],
            "recommended_action": cls._incident_text(
                signal.get("recommended_action"), 500
            ),
            "approval": None,
            "limitations": [
                "Network names, IP addresses, and MAC addresses can be stale or spoofed.",
                "Inventory evidence does not inspect traffic contents, services, vulnerabilities, or endpoint state.",
                "Containment requires stronger corroboration, exact scope, rollback, and operator approval.",
            ],
        }

    def _record_incident_alerts(
        self, connection: sqlite3.Connection, assessment: dict[str, Any]
    ) -> None:
        assessment_id = str(assessment.get("assessment_id") or "")
        if not re.fullmatch(r"[0-9a-f]{32}", assessment_id):
            return
        signals = assessment.get("signals")
        if not isinstance(signals, list):
            return
        for signal in signals[:128]:
            if not isinstance(signal, dict):
                continue
            signal_id = str(signal.get("signal_id") or "")
            severity = str(signal.get("severity") or "").casefold()
            category = str(signal.get("category") or "").casefold()
            if (
                not re.fullmatch(r"[0-9a-f]{24}", signal_id)
                or severity not in {"medium", "high"}
                or category not in {"asset_change", "operator_policy", "threat_detection"}
                or signal.get("compromise_established") is not False
            ):
                continue
            incident_id = hashlib.sha256(
                f"{assessment_id}:{signal_id}".encode("ascii")
            ).hexdigest()[:32]
            payload = self._incident_payload(connection, assessment, signal)
            payload["incident_id"] = incident_id
            payload_json = json.dumps(
                payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            )
            payload_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
            connection.execute(
                """
                INSERT OR IGNORE INTO network_incident_alerts(
                    incident_id, receipt_id, assessment_id, signal_id, scope_id,
                    device_uuid, created_at, severity, category, state,
                    payload_sha256, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    incident_id,
                    secrets.token_hex(16),
                    assessment_id,
                    signal_id,
                    payload.get("scope_id"),
                    (payload.get("device") or {}).get("device_id"),
                    payload.get("created_at") or _iso(self.clock().astimezone(timezone.utc)),
                    severity,
                    category,
                    payload_sha256,
                    payload_json,
                ),
            )

    def _maintain_incident_retention(
        self, connection: sqlite3.Connection, *, now: datetime
    ) -> None:
        now_text = _iso(now)
        cutoff = _iso(now - timedelta(days=NETWORK_INCIDENT_TTL_DAYS))
        connection.execute(
            """
            UPDATE network_incident_alerts SET
                state='expired', resolved_at=?, resolution_reason='ttl'
            WHERE state='pending' AND created_at < ?
            """,
            (now_text, cutoff),
        )
        overflow = connection.execute(
            """
            SELECT incident_id FROM network_incident_alerts
            WHERE state='pending' ORDER BY created_at DESC, incident_id DESC
            LIMIT -1 OFFSET ?
            """,
            (MAX_PENDING_NETWORK_INCIDENTS,),
        ).fetchall()
        if overflow:
            connection.executemany(
                """
                UPDATE network_incident_alerts SET
                    state='expired', resolved_at=?, resolution_reason='capacity'
                WHERE incident_id=? AND state='pending'
                """,
                ((now_text, str(row["incident_id"])) for row in overflow),
            )
        connection.execute(
            """
            DELETE FROM network_incident_alerts
            WHERE incident_id NOT IN (
                SELECT incident_id FROM network_incident_alerts
                ORDER BY created_at DESC, incident_id DESC LIMIT ?
            )
            """,
            (MAX_NETWORK_INCIDENT_RECEIPTS,),
        )

    @staticmethod
    def _verified_security_receipt(row: sqlite3.Row) -> dict[str, Any]:
        stored = json.loads(str(row["receipt_json"]))
        if not isinstance(stored, dict) or not verify_assessment_receipt(stored):
            raise NetworkInventoryError("Network security receipt failed integrity verification")
        row_metadata = (
            str(row["assessment_id"]),
            row["scan_id"],
            row["scope_id"],
            str(row["ruleset_version"]),
            str(row["created_at"]),
            str(row["input_sha256"]),
            str(row["receipt_sha256"]),
        )
        receipt_metadata = (
            str(stored["assessment_id"]),
            stored.get("scan_id"),
            stored.get("scope_id"),
            str(stored["ruleset_version"]),
            str(stored["generated_at"]),
            str(stored["input_sha256"]),
            str(stored["receipt_sha256"]),
        )
        if row_metadata != receipt_metadata:
            raise NetworkInventoryError(
                "Network security receipt metadata failed integrity verification"
            )
        return stored

    def security_assessment(self, *, scope_id: str | None = None) -> dict[str, Any]:
        """Read or create one deterministic, identifier-free evidence receipt.

        This performs no network activity and never blocks, quarantines, or changes
        a device. The receipt's digest detects accidental/casual corruption; it is
        not a cryptographic attestation against a privileged local attacker.
        """
        return self._record_security_receipt(scope_id=scope_id)

    def security_assessment_history(
        self, *, limit: int = 50, scope_id: str | None = None
    ) -> dict[str, Any]:
        if isinstance(limit, bool) or not 1 <= int(limit) <= 100:
            raise ValueError("Security assessment history limit must be between 1 and 100")
        normalized_scope = str(scope_id or "").strip().casefold() or None
        with self._connect() as connection:
            if normalized_scope is None:
                rows = connection.execute(
                    """
                    SELECT assessment_id, scan_id, scope_id, ruleset_version,
                           created_at, input_sha256, receipt_sha256, receipt_json
                    FROM network_security_receipts
                    ORDER BY created_at DESC, assessment_id DESC LIMIT ?
                    """,
                    (int(limit),),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT assessment_id, scan_id, scope_id, ruleset_version,
                           created_at, input_sha256, receipt_sha256, receipt_json
                    FROM network_security_receipts WHERE scope_id=?
                    ORDER BY created_at DESC, assessment_id DESC LIMIT ?
                    """,
                    (normalized_scope, int(limit)),
                ).fetchall()
        assessments: list[dict[str, Any]] = []
        integrity_failures: list[dict[str, Any]] = []
        for row in rows:
            try:
                value = self._verified_security_receipt(row)
            except (json.JSONDecodeError, KeyError, NetworkInventoryError):
                value = None
            if isinstance(value, dict):
                assessments.append(value)
            else:
                integrity_failures.append({
                    "assessment_id": str(row["assessment_id"]),
                    "created_at": str(row["created_at"]),
                    "reason": "receipt_integrity_failed",
                })
        return {
            "assessments": assessments,
            "verified_receipts": len(assessments),
            "integrity_failures": integrity_failures,
            "network_activity_performed": False,
        }

    @staticmethod
    def _verified_incident_alert(row: sqlite3.Row) -> dict[str, Any]:
        try:
            payload = json.loads(str(row["payload_json"]))
        except (json.JSONDecodeError, TypeError) as exc:
            raise NetworkInventoryError(
                "Network incident payload is invalid"
            ) from exc
        if not isinstance(payload, dict):
            raise NetworkInventoryError("Network incident payload is invalid")
        payload_json = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
        payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        if payload_hash != str(row["payload_sha256"]):
            raise NetworkInventoryError(
                "Network incident payload failed integrity verification"
            )
        if (
            payload.get("incident_id") != str(row["incident_id"])
            or payload.get("assessment_id") != str(row["assessment_id"])
            or payload.get("signal_id") != str(row["signal_id"])
            or payload.get("scope_id") != row["scope_id"]
            or payload.get("severity") != str(row["severity"])
            or payload.get("category") != str(row["category"])
            or payload.get("compromise_established") is not False
        ):
            raise NetworkInventoryError(
                "Network incident metadata failed integrity verification"
            )
        device = payload.get("device")
        payload_device_id = device.get("device_id") if isinstance(device, dict) else None
        if payload_device_id != row["device_uuid"]:
            raise NetworkInventoryError(
                "Network incident device binding failed integrity verification"
            )
        automatic_actions = payload.get("automatic_actions")
        if not isinstance(automatic_actions, list) or len(automatic_actions) > 32:
            raise NetworkInventoryError("Network incident action receipt is invalid")
        result = dict(payload)
        result.update({
            "receipt_id": str(row["receipt_id"]),
            "state": str(row["state"]),
            "resolved_at": row["resolved_at"],
            "resolution_reason": row["resolution_reason"],
            "receipt_integrity_checked": True,
            "receipt_authoritative_for_containment": False,
        })
        return result

    def pending_incidents(
        self,
        *,
        limit: int = 50,
        assessment_id: str | None = None,
        include_identifiers: bool = False,
    ) -> dict[str, Any]:
        if not self.incidents_enabled:
            return {
                "pending_count": 0,
                "incidents": [],
                "integrity_failures": [],
                "alerts_survive_restart": True,
                "automatic_containment": False,
                "disabled": True,
            }
        if isinstance(limit, bool) or not 1 <= int(limit) <= 100:
            raise ValueError("Incident limit must be between 1 and 100")
        normalized_assessment = str(assessment_id or "").strip().casefold() or None
        if normalized_assessment is not None and not re.fullmatch(
            r"[0-9a-f]{32}", normalized_assessment
        ):
            raise ValueError("assessment_id must be exactly 32 lowercase hex characters")
        now = self.clock().astimezone(timezone.utc)
        with self._lock, self._connect() as connection:
            self._maintain_incident_retention(connection, now=now)
            params: tuple[Any, ...]
            where = "WHERE state='pending'"
            if normalized_assessment is not None:
                where += " AND assessment_id=?"
                params = (normalized_assessment, int(limit))
            else:
                params = (int(limit),)
            rows = connection.execute(
                f"""
                SELECT * FROM network_incident_alerts
                {where}
                ORDER BY created_at ASC, incident_id ASC LIMIT ?
                """,
                params,
            ).fetchall()
            total = int(connection.execute(
                "SELECT COUNT(*) FROM network_incident_alerts WHERE state='pending'"
            ).fetchone()[0])
        incidents: list[dict[str, Any]] = []
        integrity_failures: list[dict[str, Any]] = []
        for row in rows:
            try:
                incident = self._verified_incident_alert(row)
                if not include_identifiers and isinstance(incident.get("device"), dict):
                    incident["device"] = {
                        key: value
                        for key, value in incident["device"].items()
                        if key not in {"hostname", "ipv4", "mac"}
                    }
                incidents.append(incident)
            except NetworkInventoryError:
                integrity_failures.append({
                    "incident_id": str(row["incident_id"]),
                    "reason": "receipt_integrity_failed",
                })
        return {
            "pending_count": total,
            "incidents": incidents,
            "integrity_failures": integrity_failures,
            "alerts_survive_restart": True,
            "automatic_containment": False,
        }

    def record_incident_actions(
        self, *, incident_id: str, actions: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Attach bounded passive-diagnostic receipts to one pending alert.

        This surface intentionally accepts statuses rather than arbitrary prose,
        never changes an approval field, and cannot assert compromise.  Tool
        output is not copied into the alert; the durable receipt is the audit
        reference.
        """
        if not self.incidents_enabled:
            raise PermissionError("Network-defense incidents are disabled")
        incident = str(incident_id or "").strip().casefold()
        if not re.fullmatch(r"[0-9a-f]{32}", incident):
            raise ValueError("incident_id must be exactly 32 lowercase hex characters")
        if not isinstance(actions, list) or not 1 <= len(actions) <= 12:
            raise ValueError("actions must contain between 1 and 12 receipts")
        outcomes = {
            "completed": "Passive read-only check completed.",
            "failed": "Passive read-only check failed closed.",
            "unavailable": "Passive read-only check was unavailable.",
            "skipped": "Passive read-only check was safely skipped.",
        }
        normalized: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for action in actions:
            if not isinstance(action, dict):
                raise ValueError("Each incident action must be an object")
            tool_id = self._incident_text(action.get("tool_id"), 120).casefold()
            receipt_id = self._incident_text(action.get("receipt_id"), 32).casefold()
            status = self._incident_text(action.get("status"), 24).casefold()
            title = self._incident_text(action.get("title"), 240)
            if not re.fullmatch(r"[a-z][a-z0-9_-]{0,119}", tool_id):
                raise ValueError("Incident action tool_id is invalid")
            if not re.fullmatch(r"[0-9a-f]{32}", receipt_id):
                raise ValueError("Incident action receipt_id is invalid")
            if status not in outcomes or not title:
                raise ValueError("Incident action status or title is invalid")
            dedupe_key = (tool_id, receipt_id)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            normalized.append({
                "tool_id": tool_id,
                "title": title,
                "outcome": outcomes[status],
                "receipt_id": receipt_id,
            })
        if not normalized:
            raise ValueError("actions did not contain a unique receipt")
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM network_incident_alerts WHERE incident_id=?",
                (incident,),
            ).fetchone()
            if row is None:
                raise KeyError("Network incident was not found")
            verified = self._verified_incident_alert(row)
            if str(row["state"]) != "pending":
                raise PermissionError("Only pending network incidents may be annotated")
            existing = verified.get("automatic_actions")
            if isinstance(existing, list) and existing:
                return verified
            payload = {
                key: value
                for key, value in verified.items()
                if key not in {
                    "receipt_id", "state", "resolved_at", "resolution_reason",
                    "receipt_integrity_checked",
                    "receipt_authoritative_for_containment",
                }
            }
            payload["automatic_actions"] = normalized
            payload_json = json.dumps(
                payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            )
            payload_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
            connection.execute(
                """
                UPDATE network_incident_alerts
                SET payload_sha256=?, payload_json=?
                WHERE incident_id=? AND state='pending'
                """,
                (payload_sha256, payload_json, incident),
            )
            updated = connection.execute(
                "SELECT * FROM network_incident_alerts WHERE incident_id=?",
                (incident,),
            ).fetchone()
        assert updated is not None
        return self._verified_incident_alert(updated)

    def acknowledge_incident(
        self, *, incident_id: str, receipt_id: str
    ) -> dict[str, Any]:
        incident = str(incident_id or "").strip().casefold()
        receipt = str(receipt_id or "").strip().casefold()
        if not re.fullmatch(r"[0-9a-f]{32}", incident):
            raise ValueError("incident_id must be exactly 32 lowercase hex characters")
        if not re.fullmatch(r"[0-9a-f]{32}", receipt):
            raise ValueError("receipt_id must be exactly 32 lowercase hex characters")
        now = self.clock().astimezone(timezone.utc)
        with self._lock, self._connect() as connection:
            self._maintain_incident_retention(connection, now=now)
            row = connection.execute(
                """
                SELECT * FROM network_incident_alerts
                WHERE incident_id=? AND receipt_id=?
                """,
                (incident, receipt),
            ).fetchone()
            if row is None:
                raise KeyError("Network incident receipt was not found")
            verified = self._verified_incident_alert(row)
            if str(row["state"]) == "pending":
                connection.execute(
                    """
                    UPDATE network_incident_alerts SET
                        state='acknowledged', resolved_at=?,
                        resolution_reason='operator_acknowledged'
                    WHERE incident_id=? AND receipt_id=? AND state='pending'
                    """,
                    (_iso(now), incident, receipt),
                )
                changed = True
            else:
                changed = False
        return {
            "incident_id": incident,
            "receipt_id": receipt,
            "acknowledged": True,
            "changed": changed,
            "previous_state": verified["state"],
        }

    def set_profile(
        self,
        device_id: str,
        label: str | None = None,
        trust_state: str | None = None,
        device_type: str | None = None,
    ) -> dict[str, Any]:
        normalized = str(device_id or "").strip().casefold()
        if not normalized:
            raise ValueError("device_id is required")
        if label is None and trust_state is None and device_type is None:
            raise ValueError("Provide at least one profile field")
        clean_label = None if label is None else str(label).strip()[:120] or None
        clean_type = None if device_type is None else str(device_type).strip()[:80] or None
        clean_trust = None if trust_state is None else str(trust_state).strip().casefold()
        if clean_trust is not None and clean_trust not in TRUST_STATES:
            raise ValueError("trust_state must be unreviewed, recognized, watch, or retired")
        now_text = _iso(self.clock().astimezone(timezone.utc))
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM network_devices WHERE device_uuid=?", (normalized,)
            ).fetchone()
            if row is None:
                raise KeyError("Device was not found")
            next_label = row["label"] if label is None else clean_label
            next_type = row["device_type"] if device_type is None else clean_type
            next_trust = str(row["trust_state"]) if clean_trust is None else clean_trust
            connection.execute(
                """
                UPDATE network_devices SET
                    label=?, device_type=?, trust_state=?, profile_updated_at=?
                WHERE device_uuid=?
                """,
                (next_label, next_type, next_trust, now_text, normalized),
            )
            self._event(
                connection, scan_id=None, device_uuid=normalized,
                scope_id=str(row["scope_id"] or "legacy"), observed_at=now_text,
                event_type="profile_updated",
                summary="Operator-updated local device labels; access authority was unchanged",
            )
            updated = connection.execute(
                "SELECT * FROM network_devices WHERE device_uuid=?", (normalized,)
            ).fetchone()
        assert updated is not None
        rendered = self._render_rows(
            [updated], active=set(), cached=set(), new=set(),
            now=self.clock().astimezone(timezone.utc), include_identifiers=False,
        )[0]
        try:
            rendered["security_assessment"] = self._record_security_receipt(
                scope_id=str(updated["scope_id"] or "") or None
            )
        except (OSError, RuntimeError, TypeError, ValueError, sqlite3.Error):
            rendered["security_assessment"] = {
                "posture": "assessment_unavailable",
                "signals": [],
                "automatic_containment": {"enabled": False, "actions_taken": 0},
            }
        return rendered

    def events(
        self,
        *,
        limit: int = 100,
        device_id: str | None = None,
        include_identifiers: bool = False,
    ) -> dict[str, Any]:
        if isinstance(limit, bool) or not 1 <= int(limit) <= 500:
            raise ValueError("limit must be between 1 and 500")
        params: tuple[Any, ...]
        where = ""
        if device_id:
            where = "WHERE e.device_uuid=?"
            params = (str(device_id).strip().casefold(), int(limit))
        else:
            params = (int(limit),)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT e.*, d.label, d.hostname, d.ipv4, d.mac
                FROM network_events e
                LEFT JOIN network_devices d ON d.device_uuid=e.device_uuid
                {where}
                ORDER BY e.id DESC LIMIT ?
                """,
                params,
            ).fetchall()
        rendered = []
        for row in rows:
            item = {
                "event_id": int(row["id"]),
                "device_id": row["device_uuid"],
                "observed_at": str(row["observed_at"]),
                "event_type": str(row["event_type"]),
                "summary": str(row["summary"]),
                "display_name": str(row["label"] or "") or (
                    f"Observed device {str(row['device_uuid'] or '')[:6]}"
                ),
            }
            if include_identifiers:
                item.update({"hostname": row["hostname"], "ipv4": row["ipv4"], "mac": row["mac"]})
            rendered.append(item)
        return {"events": rendered}

    def device_detail(
        self,
        device_id: str,
        *,
        event_limit: int = 100,
        include_identifiers: bool = False,
    ) -> dict[str, Any]:
        normalized = str(device_id or "").strip().casefold()
        if not normalized:
            raise ValueError("device_id is required")
        if isinstance(event_limit, bool) or not 1 <= int(event_limit) <= 500:
            raise ValueError("event_limit must be between 1 and 500")
        now = self.clock().astimezone(timezone.utc)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM network_devices WHERE device_uuid=?", (normalized,)
            ).fetchone()
            if row is None:
                raise KeyError("Device was not found")
            last_scan, active, cached = self._last_scan_sets(
                connection, str(row["scope_id"] or "") or None
            )
            sessions = connection.execute(
                """
                SELECT started_at, last_reachable_at, ended_at, observation_count
                FROM network_presence_sessions WHERE device_uuid=?
                ORDER BY id DESC LIMIT 100
                """,
                (normalized,),
            ).fetchall()
            addresses = connection.execute(
                """
                SELECT ipv4, first_seen, last_seen, seen_count
                FROM network_device_addresses WHERE device_uuid=?
                ORDER BY last_seen DESC LIMIT 100
                """,
                (normalized,),
            ).fetchall()
        device = self._render_rows(
            [row], active=active, cached=cached, new=set(), now=now,
            include_identifiers=include_identifiers,
        )[0]
        return {
            "device": device,
            "events": self.events(
                limit=event_limit, device_id=normalized,
                include_identifiers=include_identifiers,
            )["events"],
            "sessions": [dict(item) for item in sessions],
            "addresses": [dict(item) for item in addresses] if include_identifiers else [],
            "address_count": len(addresses),
            "last_scan_at": str(last_scan["completed_at"]) if last_scan else None,
        }

    @staticmethod
    def _render_rows(
        rows: list[sqlite3.Row],
        *,
        active: set[str],
        cached: set[str],
        new: set[str],
        now: datetime,
        include_identifiers: bool,
    ) -> list[dict[str, Any]]:
        rendered: list[dict[str, Any]] = []
        for row in rows:
            device_uuid = str(row["device_uuid"])
            reachable = device_uuid in active
            cached_now = device_uuid in cached and not reachable
            duration = None
            if reachable:
                duration = max(
                    0,
                    int(
                        (now - _parse_time(str(row["continuous_since"]))).total_seconds()
                    ),
                )
            label = str(row["label"] or "").strip() or None
            device_type = str(row["device_type"] or "").strip() or None
            item: dict[str, Any] = {
                "device_id": device_uuid,
                "display_name": label or (
                    f"{device_type} {device_uuid[:6]}" if device_type else f"Observed device {device_uuid[:6]}"
                ),
                "label": label,
                "trust_state": str(row["trust_state"] or "unreviewed"),
                "device_type": device_type,
                "identity_confidence": str(row["identity_confidence"] or "limited"),
                "identity_basis": str(row["identity_basis"] or "limited observation"),
                "presence_state": "reachable" if reachable else ("cached" if cached_now else "unobserved"),
                "visible_now": reachable,
                "cached_now": cached_now,
                "is_new": device_uuid in new,
                "first_seen": str(row["first_seen"]),
                "last_seen": str(row["last_seen"]),
                "last_active_seen": row["last_active_seen"],
                "profile_updated_at": row["profile_updated_at"],
                "continuous_visible_seconds": duration,
                "seen_count": int(row["seen_count"]),
                "visibility": str(row["visibility"]),
                "neighbor_state": str(row["neighbor_state"]),
                "enrolled": False,
                "access_authorized": False,
                "trust_notice": "This label does not enroll the device or grant Jarvis access.",
            }
            if include_identifiers:
                item.update({
                    "ipv4": str(row["ipv4"]),
                    "mac": row["mac"],
                    "hostname": row["hostname"],
                })
                if not label and row["hostname"]:
                    item["display_name"] = str(row["hostname"])
            rendered.append(item)
        return rendered
