from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from uuid import uuid4

from .redaction import redact_secrets


MAX_APPROVED_NETWORKS = 32
MAX_APPROVED_ROOTS = 16
MAX_EXECUTABLE_BYTES = 128 * 1024 * 1024
MAX_CAPTURE_BYTES = 1_000_000
MAX_RETURN_CHARACTERS = 16_000
MAX_RECEIPTS = 10_000
MAX_RUNBOOK_STEPS = 32
NETWORK_TOOL_RECEIPT_SCHEMA_VERSION = 1
_RECEIPT_INITIALIZE_LOCK = threading.RLock()
_IDENTIFIER = re.compile(r"[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*\Z")
_DNS_NAME = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z",
    re.IGNORECASE,
)
_RFC1918 = tuple(
    ipaddress.ip_network(value)
    for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
_IPV6_ULA = ipaddress.ip_network("fc00::/7")
_CATEGORY_ORDER = (
    "inventory",
    "dns",
    "tls",
    "packet_flow",
    "vulnerability_assessment",
    "wireless_bluetooth",
    "endpoint_telemetry",
    "firewall_router",
)


class NetworkToolError(ValueError):
    """A defensive network-tool request failed closed."""


class AutonomyTier(str, Enum):
    PASSIVE_READ_ONLY = "passive_read_only"
    BOUNDED_ACTIVE_PROBE = "bounded_active_probe"
    STATE_CHANGING_CONTAINMENT = "state_changing_containment"


_TIER_ORDER = {
    AutonomyTier.PASSIVE_READ_ONLY: 0,
    AutonomyTier.BOUNDED_ACTIVE_PROBE: 1,
    AutonomyTier.STATE_CHANGING_CONTAINMENT: 2,
}


@dataclass(frozen=True)
class OperationSpec:
    operation_id: str
    description: str
    tier: AutonomyTier
    target_kind: str = "none"  # none, host, or network
    default_ports: tuple[int, ...] = ()
    accepts_server_name: bool = False
    executable: bool = True
    timeout_seconds: int = 20
    argv_builder: Callable[[str, Mapping[str, Any]], tuple[str, ...]] | None = None


@dataclass(frozen=True)
class DefensiveToolManifest:
    tool_id: str
    display_name: str
    category: str
    executable_names: tuple[str, ...]
    priority: int
    operations: tuple[OperationSpec, ...]


@dataclass(frozen=True)
class InstalledDefensiveTool:
    manifest: DefensiveToolManifest
    executable_path: str
    executable_sha256: str
    executable_size: int
    executable_mtime_ns: int


@dataclass(frozen=True)
class ExecutionPlan:
    plan_sha256: str
    tool_id: str
    operation_id: str
    category: str
    tier: AutonomyTier
    target: str | None
    arguments: Mapping[str, Any]
    executable_path: str
    executable_sha256: str
    executable_size: int
    executable_mtime_ns: int
    argv: tuple[str, ...]
    timeout_seconds: int
    executable: bool
    safe_automatic: bool

    def public(self) -> dict[str, Any]:
        return {
            "plan_sha256": self.plan_sha256,
            "tool_id": self.tool_id,
            "operation_id": self.operation_id,
            "category": self.category,
            "tier": self.tier.value,
            "target": self.target,
            "argv": list(self.argv),
            "timeout_seconds": self.timeout_seconds,
            "execution_allowed": self.executable
            and self.tier is not AutonomyTier.STATE_CHANGING_CONTAINMENT,
            "requires_operator_approval": (
                not self.safe_automatic
            ),
            "safe_automatic": self.safe_automatic,
        }


def _nmap_inventory(executable: str, arguments: Mapping[str, Any]) -> tuple[str, ...]:
    return (
        executable, "-sn", "-n", "--max-retries", "1",
        "--host-timeout", "30s", str(arguments["target"]),
    )


def _nslookup(executable: str, arguments: Mapping[str, Any]) -> tuple[str, ...]:
    return executable, str(arguments["target"])


def _openssl_tls(executable: str, arguments: Mapping[str, Any]) -> tuple[str, ...]:
    target = str(arguments["target"])
    port = int(arguments["port"])
    connect = f"[{target}]:{port}" if ":" in target else f"{target}:{port}"
    command = [executable, "s_client", "-connect", connect, "-brief"]
    server_name = arguments.get("server_name")
    if server_name:
        command.extend(("-servername", str(server_name)))
    return tuple(command)


def _netstat(executable: str, _arguments: Mapping[str, Any]) -> tuple[str, ...]:
    return executable, "-ano"


def _nmap_service_inventory(
    executable: str, arguments: Mapping[str, Any]
) -> tuple[str, ...]:
    ports = ",".join(str(port) for port in arguments["ports"])
    return (
        executable, "-sV", "--version-light", "-n", "-T3",
        "--max-retries", "1", "--host-timeout", "60s",
        "-p", ports, str(arguments["target"]),
    )


def _netsh_wireless(executable: str, _arguments: Mapping[str, Any]) -> tuple[str, ...]:
    return executable, "wlan", "show", "interfaces"


def _pnputil_bluetooth(executable: str, _arguments: Mapping[str, Any]) -> tuple[str, ...]:
    return executable, "/enum-devices", "/class", "Bluetooth", "/connected"


def _tasklist(executable: str, _arguments: Mapping[str, Any]) -> tuple[str, ...]:
    return executable, "/FO", "CSV", "/NH"


def _netsh_firewall(executable: str, _arguments: Mapping[str, Any]) -> tuple[str, ...]:
    return executable, "advfirewall", "show", "allprofiles"


def _arp_cache(executable: str, arguments: Mapping[str, Any]) -> tuple[str, ...]:
    return executable, "-a", str(arguments["target"])


BUILTIN_DEFENSIVE_TOOLS: tuple[DefensiveToolManifest, ...] = (
    DefensiveToolManifest(
        "nmap-inventory", "Nmap private-network inventory", "inventory",
        ("nmap", "nmap.exe"), 20,
        (OperationSpec(
            "ping-sweep", "Bounded host discovery without port or script scanning.",
            AutonomyTier.BOUNDED_ACTIVE_PROBE, "network", timeout_seconds=45,
            argv_builder=_nmap_inventory,
        ),),
    ),
    DefensiveToolManifest(
        "nslookup-dns", "NSLookup private-host lookup", "dns",
        ("nslookup", "nslookup.exe"), 20,
        (OperationSpec(
            "reverse-lookup", "Resolve one literal owned private address.",
            AutonomyTier.BOUNDED_ACTIVE_PROBE, "host", timeout_seconds=15,
            argv_builder=_nslookup,
        ),),
    ),
    DefensiveToolManifest(
        "openssl-tls", "OpenSSL TLS inspection", "tls",
        ("openssl", "openssl.exe"), 20,
        (OperationSpec(
            "inspect-certificate", "Inspect a TLS handshake on one owned private host.",
            AutonomyTier.BOUNDED_ACTIVE_PROBE, "host", (443,), True,
            timeout_seconds=20, argv_builder=_openssl_tls,
        ),),
    ),
    DefensiveToolManifest(
        "netstat-flow", "Local connection-table inspection", "packet_flow",
        ("netstat", "netstat.exe"), 10,
        (OperationSpec(
            "list-connections", "Read the local connection and listener table.",
            AutonomyTier.PASSIVE_READ_ONLY, argv_builder=_netstat,
        ),),
    ),
    DefensiveToolManifest(
        "nmap-service-assessment", "Nmap bounded service assessment",
        "vulnerability_assessment", ("nmap", "nmap.exe"), 20,
        (OperationSpec(
            "safe-service-inventory",
            "Identify services on a bounded port set; scripts and payload actions are unavailable.",
            AutonomyTier.BOUNDED_ACTIVE_PROBE, "host",
            (22, 53, 80, 443, 445, 3389), timeout_seconds=70,
            argv_builder=_nmap_service_inventory,
        ),),
    ),
    DefensiveToolManifest(
        "netsh-wireless", "Windows wireless visibility", "wireless_bluetooth",
        ("netsh", "netsh.exe"), 10,
        (OperationSpec(
            "show-wireless-interface", "Read the current Windows WLAN interface state.",
            AutonomyTier.PASSIVE_READ_ONLY, argv_builder=_netsh_wireless,
        ),),
    ),
    DefensiveToolManifest(
        "pnputil-bluetooth", "Windows Bluetooth device visibility",
        "wireless_bluetooth", ("pnputil", "pnputil.exe"), 20,
        (OperationSpec(
            "list-connected-bluetooth",
            "Read Windows Plug-and-Play records for connected Bluetooth-class devices.",
            AutonomyTier.PASSIVE_READ_ONLY, argv_builder=_pnputil_bluetooth,
        ),),
    ),
    DefensiveToolManifest(
        "tasklist-endpoint", "Windows endpoint process telemetry",
        "endpoint_telemetry", ("tasklist", "tasklist.exe"), 10,
        (OperationSpec(
            "list-processes", "Read bounded local process telemetry.",
            AutonomyTier.PASSIVE_READ_ONLY, argv_builder=_tasklist,
        ),),
    ),
    DefensiveToolManifest(
        "netsh-firewall", "Windows firewall visibility and containment planning",
        "firewall_router", ("netsh", "netsh.exe"), 10,
        (
            OperationSpec(
                "show-firewall-profiles", "Read Windows Firewall profile state.",
                AutonomyTier.PASSIVE_READ_ONLY, argv_builder=_netsh_firewall,
            ),
            OperationSpec(
                "containment-preview",
                "Plan operator-reviewed containment; this registry never applies it.",
                AutonomyTier.STATE_CHANGING_CONTAINMENT, "host",
                executable=False, argv_builder=None,
            ),
        ),
    ),
    DefensiveToolManifest(
        "arp-router-cache", "Local router-neighbor cache visibility",
        "firewall_router", ("arp", "arp.exe"), 20,
        (OperationSpec(
            "show-owned-neighbor", "Read a cached entry for one owned private host.",
            AutonomyTier.PASSIVE_READ_ONLY, "host", argv_builder=_arp_cache,
        ),),
    ),
)

# Safe automation is independently closed over exact, code-reviewed built-in
# manifest objects.  A connector cannot grant itself automatic authority merely
# by labeling an operation "passive_read_only".
_SAFE_AUTOMATIC_MANIFESTS = {
    manifest.tool_id: manifest for manifest in BUILTIN_DEFENSIVE_TOOLS
}


def _safe_automatic_operation(
    manifest: DefensiveToolManifest, operation: OperationSpec
) -> bool:
    approved = _SAFE_AUTOMATIC_MANIFESTS.get(manifest.tool_id)
    return bool(
        approved is not None
        and manifest == approved
        and operation in approved.operations
        and operation.tier is AutonomyTier.PASSIVE_READ_ONLY
        and operation.executable
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _is_owned_private_network(network: ipaddress._BaseNetwork) -> bool:
    if isinstance(network, ipaddress.IPv4Network):
        return any(network.subnet_of(scope) for scope in _RFC1918)
    return isinstance(network, ipaddress.IPv6Network) and network.subnet_of(_IPV6_ULA)


def _bounded_networks(values: Iterable[str]) -> tuple[ipaddress._BaseNetwork, ...]:
    source = tuple(values)
    if not 1 <= len(source) <= MAX_APPROVED_NETWORKS:
        raise NetworkToolError("Configure 1 to 32 owned private network scopes")
    result: list[ipaddress._BaseNetwork] = []
    for value in source:
        try:
            network = ipaddress.ip_network(str(value), strict=True)
        except ValueError as exc:
            raise NetworkToolError("Owned network scopes must be canonical CIDRs") from exc
        if not _is_owned_private_network(network):
            raise NetworkToolError("Owned network scopes must be RFC1918 IPv4 or IPv6 ULA")
        if network not in result:
            result.append(network)
    return tuple(result)


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _hash_file(path: Path, expected: os.stat_result | None = None) -> tuple[str, os.stat_result]:
    before = os.lstat(path)
    attributes = getattr(before, "st_file_attributes", 0)
    if (
        stat.S_ISLNK(before.st_mode)
        or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        or not stat.S_ISREG(before.st_mode)
        or not 0 < before.st_size <= MAX_EXECUTABLE_BYTES
    ):
        raise NetworkToolError("Defensive executable is not a bounded ordinary file")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    after = os.lstat(path)
    identity = lambda row: (row.st_dev, row.st_ino, row.st_size, row.st_mtime_ns)
    if identity(before) != identity(after) or (expected and identity(before) != identity(expected)):
        raise NetworkToolError("Defensive executable changed during verification")
    return digest.hexdigest(), after


class DefensiveNetworkToolRegistry:
    """Approved, scope-bound defensive tools with no exploit or install path."""

    def __init__(
        self,
        data_dir: Path,
        *,
        owned_networks: Iterable[str],
        approved_executable_roots: Iterable[Path],
        manifests: Sequence[DefensiveToolManifest] = BUILTIN_DEFENSIVE_TOOLS,
        which: Callable[[str], str | None] = shutil.which,
        runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.owned_networks = _bounded_networks(owned_networks)
        raw_roots = tuple(Path(value) for value in approved_executable_roots)
        if not 1 <= len(raw_roots) <= MAX_APPROVED_ROOTS:
            raise NetworkToolError("Configure 1 to 16 approved executable roots")
        roots: list[Path] = []
        for raw_root in raw_roots:
            try:
                details = os.lstat(raw_root)
                resolved = raw_root.resolve(strict=True)
            except OSError as exc:
                raise NetworkToolError(
                    "Approved executable roots must be existing directories"
                ) from exc
            if (
                stat.S_ISLNK(details.st_mode)
                or getattr(details, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                or not stat.S_ISDIR(details.st_mode)
                or resolved == Path(resolved.anchor)
            ):
                raise NetworkToolError(
                    "Approved executable roots must be narrow ordinary directories"
                )
            if resolved not in roots:
                roots.append(resolved)
        self.approved_roots = tuple(roots)
        self.manifests = self._validate_manifests(manifests)
        self.which = which
        self.runner = runner
        self.clock = clock
        self.receipt_path = Path(data_dir).resolve() / "network-security-tools.db"
        self.receipt_path.parent.mkdir(parents=True, exist_ok=True)
        with _RECEIPT_INITIALIZE_LOCK:
            self._initialize_receipts()

    @staticmethod
    def _validate_manifests(
        manifests: Sequence[DefensiveToolManifest],
    ) -> tuple[DefensiveToolManifest, ...]:
        if not manifests:
            raise NetworkToolError("At least one approved defensive manifest is required")
        seen: set[str] = set()
        result: list[DefensiveToolManifest] = []
        for manifest in manifests:
            if (
                not _IDENTIFIER.fullmatch(manifest.tool_id)
                or manifest.tool_id in seen
                or manifest.category not in _CATEGORY_ORDER
                or not manifest.executable_names
                or not manifest.operations
            ):
                raise NetworkToolError("Defensive tool manifest is invalid or duplicated")
            seen.add(manifest.tool_id)
            operation_ids: set[str] = set()
            for operation in manifest.operations:
                if (
                    not _IDENTIFIER.fullmatch(operation.operation_id)
                    or operation.operation_id in operation_ids
                    or operation.target_kind not in {"none", "host", "network"}
                    or not 1 <= operation.timeout_seconds <= 120
                    or operation.tier is AutonomyTier.STATE_CHANGING_CONTAINMENT
                    and operation.executable
                    or operation.executable and operation.argv_builder is None
                ):
                    raise NetworkToolError("Defensive operation manifest is invalid")
                operation_ids.add(operation.operation_id)
            result.append(manifest)
        return tuple(sorted(result, key=lambda row: (row.priority, row.tool_id)))

    @contextmanager
    def _connect(self):
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(self.receipt_path, timeout=10)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=10000")
            for attempt in range(100):
                try:
                    connection.execute("PRAGMA journal_mode=WAL")
                    break
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc).casefold() or attempt == 99:
                        raise
                    time.sleep(0.05)
            yield connection
            connection.commit()
        except Exception:
            if connection is not None:
                connection.rollback()
            raise
        finally:
            if connection is not None:
                connection.close()

    def _initialize_receipts(self) -> None:
        self._reject_future_receipt_schema()
        with self._connect() as connection:
            schema = self._read_receipt_schema(connection)
            if schema is not None and schema > NETWORK_TOOL_RECEIPT_SCHEMA_VERSION:
                raise NetworkToolError(
                    "Network-tool receipt database is newer than this Jarvis build"
                )
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS network_tool_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS network_tool_receipts (
                    receipt_id TEXT PRIMARY KEY,
                    plan_sha256 TEXT NOT NULL,
                    tool_id TEXT NOT NULL,
                    operation_id TEXT NOT NULL,
                    category TEXT NOT NULL,
                    tier TEXT NOT NULL,
                    target TEXT,
                    executable_sha256 TEXT NOT NULL,
                    argv_sha256 TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    status TEXT NOT NULL,
                    exit_code INTEGER,
                    stdout_sha256 TEXT,
                    stderr_sha256 TEXT,
                    stdout_bytes INTEGER NOT NULL DEFAULT 0,
                    stderr_bytes INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT
                );
                CREATE INDEX IF NOT EXISTS network_tool_receipts_time_idx
                    ON network_tool_receipts(started_at DESC);
            """)
            connection.execute(
                """
                INSERT INTO network_tool_meta(key, value)
                VALUES ('schema_version', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (str(NETWORK_TOOL_RECEIPT_SCHEMA_VERSION),),
            )

    @staticmethod
    def _read_receipt_schema(connection: sqlite3.Connection) -> int | None:
        table = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type='table' AND name='network_tool_meta'
            """
        ).fetchone()
        if table is None:
            return None
        row = connection.execute(
            "SELECT value FROM network_tool_meta WHERE key='schema_version'"
        ).fetchone()
        if row is None:
            return None
        try:
            return int(row["value"] if isinstance(row, sqlite3.Row) else row[0])
        except (TypeError, ValueError, OverflowError) as exc:
            raise NetworkToolError("Network-tool receipt schema version is invalid") from exc

    def _reject_future_receipt_schema(self) -> None:
        if not self.receipt_path.exists():
            return
        try:
            connection = sqlite3.connect(
                f"{self.receipt_path.as_uri()}?mode=ro", uri=True, timeout=10
            )
            connection.row_factory = sqlite3.Row
            try:
                schema = self._read_receipt_schema(connection)
            finally:
                connection.close()
        except NetworkToolError:
            raise
        except sqlite3.DatabaseError as exc:
            raise NetworkToolError(
                "Network-tool receipt database could not be inspected safely"
            ) from exc
        if schema is not None and schema > NETWORK_TOOL_RECEIPT_SCHEMA_VERSION:
            raise NetworkToolError(
                "Network-tool receipt database is newer than this Jarvis build"
            )

    def _fingerprint(
        self,
        manifest: DefensiveToolManifest,
        candidate: str,
    ) -> InstalledDefensiveTool:
        candidate_path = Path(candidate)
        before_link = os.lstat(candidate_path)
        if stat.S_ISLNK(before_link.st_mode) or getattr(
            before_link, "st_file_attributes", 0
        ) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            raise NetworkToolError("Approved defensive executables cannot be links")
        path = candidate_path.resolve(strict=True)
        if not any(_within(path, root) for root in self.approved_roots):
            raise NetworkToolError("Defensive executable is outside approved roots")
        allowed_names = {name.casefold() for name in manifest.executable_names}
        if path.name.casefold() not in allowed_names:
            raise NetworkToolError("Discovered executable name does not match its manifest")
        digest, details = _hash_file(path)
        return InstalledDefensiveTool(
            manifest, str(path), digest, int(details.st_size), int(details.st_mtime_ns)
        )

    def discover(self) -> tuple[InstalledDefensiveTool, ...]:
        installed: list[InstalledDefensiveTool] = []
        for manifest in self.manifests:
            candidate = next(
                (found for name in manifest.executable_names if (found := self.which(name))),
                None,
            )
            if candidate is None:
                continue
            try:
                installed.append(self._fingerprint(manifest, candidate))
            except (OSError, NetworkToolError):
                continue
        return tuple(installed)

    def discovery_report(self) -> dict[str, Any]:
        installed = self.discover()
        found = {row.manifest.tool_id: row for row in installed}
        return {
            "owned_networks": [str(row) for row in self.owned_networks],
            "installed": [
                {
                    "tool_id": row.manifest.tool_id,
                    "display_name": row.manifest.display_name,
                    "category": row.manifest.category,
                    "executable_path": row.executable_path,
                    "executable_sha256": row.executable_sha256,
                    "operations": [
                        {
                            "operation_id": operation.operation_id,
                            "tier": operation.tier.value,
                            "execution_allowed": operation.executable,
                            "safe_automatic": _safe_automatic_operation(
                                row.manifest, operation
                            ),
                        }
                        for operation in row.manifest.operations
                    ],
                }
                for row in installed
            ],
            "unavailable": [
                manifest.tool_id
                for manifest in self.manifests
                if manifest.tool_id not in found
            ],
            "installs_or_downloads_performed": False,
            "offensive_features_enabled": False,
        }

    def _owned_target(self, value: Any, kind: str) -> str:
        text = str(value or "").strip()
        try:
            if kind == "host":
                address = ipaddress.ip_address(text)
                if not any(address in network for network in self.owned_networks):
                    raise NetworkToolError("Target is outside configured owned networks")
                if address.is_loopback or address.is_link_local or address.is_multicast:
                    raise NetworkToolError("Target is not a usable owned-network host")
                for network in self.owned_networks:
                    if address in network and isinstance(network, ipaddress.IPv4Network):
                        if address in {network.network_address, network.broadcast_address}:
                            raise NetworkToolError("Target is not a usable owned-network host")
                return str(address)
            network = ipaddress.ip_network(text, strict=True)
        except ValueError as exc:
            raise NetworkToolError("Targets must be literal canonical IP addresses or CIDRs") from exc
        if not any(
            network.version == owned.version and network.subnet_of(owned)
            for owned in self.owned_networks
        ):
            raise NetworkToolError("Target is outside configured owned networks")
        return str(network)

    @staticmethod
    def _operation(
        installed: InstalledDefensiveTool, operation_id: str
    ) -> OperationSpec:
        for operation in installed.manifest.operations:
            if operation.operation_id == operation_id:
                return operation
        raise NetworkToolError("Unknown defensive tool operation")

    def _validated_arguments(
        self, operation: OperationSpec, arguments: Mapping[str, Any]
    ) -> dict[str, Any]:
        if not isinstance(arguments, Mapping):
            raise NetworkToolError("Defensive tool arguments must be an object")
        allowed = ({"target"} if operation.target_kind != "none" else set())
        if operation.default_ports:
            allowed.add("ports")
        if operation.accepts_server_name:
            allowed.add("server_name")
        if set(arguments) - allowed:
            raise NetworkToolError("Unknown defensive tool argument")
        clean: dict[str, Any] = {}
        if operation.target_kind != "none":
            if "target" not in arguments:
                raise NetworkToolError("This operation requires an owned-network target")
            clean["target"] = self._owned_target(
                arguments["target"], operation.target_kind
            )
        if operation.default_ports:
            raw_ports = arguments.get("ports", operation.default_ports)
            if (
                not isinstance(raw_ports, (list, tuple))
                or not 1 <= len(raw_ports) <= 16
                or any(
                    isinstance(port, bool)
                    or not isinstance(port, int)
                    or not 1 <= port <= 65_535
                    for port in raw_ports
                )
            ):
                raise NetworkToolError("ports must contain 1 to 16 numeric TCP ports")
            clean["ports"] = tuple(sorted(set(raw_ports)))
            if operation.operation_id == "inspect-certificate" and len(clean["ports"]) != 1:
                raise NetworkToolError("TLS inspection requires exactly one port")
            if operation.operation_id == "inspect-certificate":
                clean["port"] = clean.pop("ports")[0]
        if operation.accepts_server_name and "server_name" in arguments:
            name = str(arguments["server_name"] or "").strip().casefold()
            if _DNS_NAME.fullmatch(name) is None:
                raise NetworkToolError("server_name must be a bounded DNS name")
            clean["server_name"] = name
        return clean

    @staticmethod
    def _plan_digest(payload: Mapping[str, Any]) -> str:
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def _plan_installed_operation(
        self,
        installed: InstalledDefensiveTool,
        operation_id: str,
        arguments: Mapping[str, Any] | None,
    ) -> ExecutionPlan:
        tool_id = installed.manifest.tool_id
        operation = self._operation(installed, operation_id)
        clean = self._validated_arguments(operation, arguments or {})
        argv = (
            operation.argv_builder(installed.executable_path, clean)
            if operation.argv_builder is not None
            else ()
        )
        payload = {
            "tool_id": tool_id,
            "operation_id": operation_id,
            "category": installed.manifest.category,
            "tier": operation.tier.value,
            "target": clean.get("target"),
            "arguments": clean,
            "executable_path": installed.executable_path,
            "executable_sha256": installed.executable_sha256,
            "executable_size": installed.executable_size,
            "executable_mtime_ns": installed.executable_mtime_ns,
            "argv": argv,
            "timeout_seconds": operation.timeout_seconds,
            "executable": operation.executable,
            "safe_automatic": _safe_automatic_operation(
                installed.manifest, operation
            ),
        }
        digest = self._plan_digest(payload)
        return ExecutionPlan(digest, tool_id, operation_id, installed.manifest.category,
            operation.tier, clean.get("target"), clean, installed.executable_path,
            installed.executable_sha256, installed.executable_size,
            installed.executable_mtime_ns, argv, operation.timeout_seconds,
            operation.executable, payload["safe_automatic"])

    def plan_operation(
        self,
        tool_id: str,
        operation_id: str,
        arguments: Mapping[str, Any] | None = None,
    ) -> ExecutionPlan:
        installed = next(
            (row for row in self.discover() if row.manifest.tool_id == tool_id), None
        )
        if installed is None:
            raise NetworkToolError("Approved defensive tool is not installed")
        return self._plan_installed_operation(installed, operation_id, arguments)

    def plan_runbook(
        self,
        categories: Iterable[str],
        *,
        target: str | None = None,
        include_active: bool = False,
        include_containment_preview: bool = False,
    ) -> dict[str, Any]:
        requested = set(categories)
        if not requested or requested - set(_CATEGORY_ORDER):
            raise NetworkToolError("Runbook categories are missing or unsupported")
        installed = self.discover()
        steps: list[ExecutionPlan] = []
        unavailable: list[str] = []
        for category in _CATEGORY_ORDER:
            if category not in requested:
                continue
            candidates: list[tuple[InstalledDefensiveTool, OperationSpec]] = []
            for tool in installed:
                if tool.manifest.category != category:
                    continue
                for operation in tool.manifest.operations:
                    if operation.tier is AutonomyTier.BOUNDED_ACTIVE_PROBE and not include_active:
                        continue
                    if (
                        operation.tier is AutonomyTier.STATE_CHANGING_CONTAINMENT
                        and not include_containment_preview
                    ):
                        continue
                    if operation.target_kind != "none" and target is None:
                        continue
                    candidates.append((tool, operation))
            candidates.sort(key=lambda item: (
                _TIER_ORDER[item[1].tier], item[0].manifest.priority,
                item[0].manifest.tool_id, item[1].operation_id,
            ))
            if not candidates:
                unavailable.append(category)
                continue
            tiers_added: set[AutonomyTier] = set()
            for tool, operation in candidates:
                if operation.tier in tiers_added:
                    continue
                arguments = {"target": target} if operation.target_kind != "none" else {}
                steps.append(self._plan_installed_operation(
                    tool, operation.operation_id, arguments
                ))
                tiers_added.add(operation.tier)
                if len(steps) >= MAX_RUNBOOK_STEPS:
                    break
        return {
            "steps": [step.public() for step in steps],
            "unavailable_categories": unavailable,
            "deterministic": True,
            "executed": False,
            "disruptive_actions_executed": False,
        }

    def select_passive_steps(
        self,
        *,
        categories: Iterable[str] | None = None,
        target: str | None = None,
        max_steps: int = 8,
    ) -> tuple[ExecutionPlan, ...]:
        """Select only fixed read-only steps; never active or containment work."""
        if isinstance(max_steps, bool) or not 1 <= int(max_steps) <= 16:
            raise ValueError("max_steps must be between 1 and 16")
        requested = set(categories or _CATEGORY_ORDER)
        if not requested or requested - set(_CATEGORY_ORDER):
            raise NetworkToolError("Passive snapshot categories are unsupported")
        candidates: list[
            tuple[int, str, str, InstalledDefensiveTool, str | None]
        ] = []
        for tool in self.discover():
            if tool.manifest.category not in requested:
                continue
            for operation in tool.manifest.operations:
                if (
                    operation.tier is not AutonomyTier.PASSIVE_READ_ONLY
                    or not _safe_automatic_operation(tool.manifest, operation)
                    or not operation.executable
                    or operation.target_kind != "none" and target is None
                ):
                    continue
                candidates.append((
                    tool.manifest.priority,
                    tool.manifest.tool_id,
                    operation.operation_id,
                    tool,
                    target if operation.target_kind != "none" else None,
                ))
        selected: list[ExecutionPlan] = []
        for _priority, _tool_id, operation_id, tool, step_target in sorted(
            candidates, key=lambda item: item[:3]
        ):
            arguments = {"target": step_target} if step_target is not None else {}
            selected.append(self._plan_installed_operation(
                tool, operation_id, arguments
            ))
            if len(selected) >= int(max_steps):
                break
        return tuple(selected)

    def run_passive_snapshot(
        self,
        *,
        categories: Iterable[str] | None = None,
        target: str | None = None,
        max_steps: int = 8,
    ) -> dict[str, Any]:
        """Execute a deterministic passive-only snapshot with metadata receipts."""
        plans = self.select_passive_steps(
            categories=categories, target=target, max_steps=max_steps
        )
        results: list[dict[str, Any]] = []
        for plan in plans:
            result = self.execute_plan(plan, include_output=False)
            results.append({
                "tool_id": plan.tool_id,
                "operation_id": plan.operation_id,
                "tier": plan.tier.value,
                "receipt_id": result["receipt_id"],
                "status": result["status"],
                "exit_code": result["exit_code"],
                "stdout_bytes": result["stdout_bytes"],
                "stdout_sha256": result["stdout_sha256"],
            })
        verified = self.verify_passive_snapshot_results(results)
        if not verified:
            raise NetworkToolError("Passive snapshot receipts failed verification")
        return {
            "results": results,
            "selected_steps": len(plans),
            "executed_steps": len(results),
            "active_probes_executed": 0,
            "containment_actions_executed": 0,
            "raw_output_returned": False,
            "receipts_verified": True,
            "receipt_scope": "batch_passive_local_metadata",
        }

    def verify_passive_snapshot_results(
        self, results: Sequence[Mapping[str, Any]]
    ) -> bool:
        """Verify batch receipt rows against audited automatic operations."""
        if (
            not isinstance(results, Sequence)
            or isinstance(results, (str, bytes, bytearray))
            or not 0 <= len(results) <= 16
            or any(not isinstance(result, Mapping) for result in results)
        ):
            return False
        if not results:
            return True
        rows = {
            str(row["receipt_id"]): row
            for row in self.receipts(limit=min(500, max(100, len(results) * 4)))[
                "receipts"
            ]
        }
        manifests = {manifest.tool_id: manifest for manifest in self.manifests}
        for result in results:
            receipt_id = str(result.get("receipt_id") or "")
            row = rows.get(receipt_id)
            if row is None:
                return False
            tool_id = str(result.get("tool_id") or "")
            operation_id = str(result.get("operation_id") or "")
            manifest = manifests.get(tool_id)
            if manifest is None:
                return False
            installed_shape = InstalledDefensiveTool(
                manifest, "", "", 0, 0
            )
            try:
                operation = self._operation(installed_shape, operation_id)
            except NetworkToolError:
                return False
            if not _safe_automatic_operation(manifest, operation):
                return False
            if (
                str(row.get("tool_id") or "") != tool_id
                or str(row.get("operation_id") or "") != operation_id
                or str(row.get("tier") or "")
                != AutonomyTier.PASSIVE_READ_ONLY.value
                or str(row.get("status") or "") != str(result.get("status") or "")
                or str(result.get("tier") or "")
                != AutonomyTier.PASSIVE_READ_ONLY.value
            ):
                return False
        return True

    def _receipt_start(self, plan: ExecutionPlan, status: str, error_code: str | None) -> str:
        receipt_id = uuid4().hex
        now_text = _iso(self.clock())
        argv_hash = hashlib.sha256(
            json.dumps(list(plan.argv), separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO network_tool_receipts(
                    receipt_id, plan_sha256, tool_id, operation_id, category,
                    tier, target, executable_sha256, argv_sha256, started_at,
                    completed_at, status, error_code
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (receipt_id, plan.plan_sha256, plan.tool_id, plan.operation_id,
                 plan.category, plan.tier.value, plan.target,
                 plan.executable_sha256, argv_hash, now_text,
                 now_text if status != "running" else None, status, error_code),
            )
        return receipt_id

    def _receipt_finish(
        self,
        receipt_id: str,
        *,
        status: str,
        exit_code: int | None,
        stdout: bytes,
        stderr: bytes,
        error_code: str | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE network_tool_receipts SET
                    completed_at=?, status=?, exit_code=?, stdout_sha256=?,
                    stderr_sha256=?, stdout_bytes=?, stderr_bytes=?, error_code=?
                WHERE receipt_id=?
                """,
                (_iso(self.clock()), status, exit_code,
                 hashlib.sha256(stdout).hexdigest(),
                 hashlib.sha256(stderr).hexdigest(), len(stdout), len(stderr),
                 error_code, receipt_id),
            )
            connection.execute(
                """
                DELETE FROM network_tool_receipts WHERE receipt_id NOT IN (
                    SELECT receipt_id FROM network_tool_receipts
                    ORDER BY started_at DESC, receipt_id DESC LIMIT ?
                )
                """,
                (MAX_RECEIPTS,),
            )

    def _validate_plan_contract(
        self, plan: ExecutionPlan
    ) -> DefensiveToolManifest:
        manifest = next(
            (row for row in self.manifests if row.tool_id == plan.tool_id), None
        )
        if manifest is None:
            raise NetworkToolError("Execution plan references an unknown manifest")
        installed_shape = InstalledDefensiveTool(
            manifest,
            plan.executable_path,
            plan.executable_sha256,
            plan.executable_size,
            plan.executable_mtime_ns,
        )
        operation = self._operation(installed_shape, plan.operation_id)
        clean = self._validated_arguments(operation, dict(plan.arguments))
        expected_argv = (
            operation.argv_builder(plan.executable_path, clean)
            if operation.argv_builder is not None
            else ()
        )
        if (
            plan.category != manifest.category
            or plan.tier is not operation.tier
            or plan.target != clean.get("target")
            or dict(plan.arguments) != clean
            or plan.argv != expected_argv
            or plan.timeout_seconds != operation.timeout_seconds
            or plan.executable is not operation.executable
            or plan.safe_automatic
            is not _safe_automatic_operation(manifest, operation)
        ):
            raise NetworkToolError("Execution plan does not match its approved manifest")
        return manifest

    def execute_plan(
        self,
        plan: ExecutionPlan,
        *,
        active_authorized: bool = False,
        include_output: bool = False,
    ) -> dict[str, Any]:
        manifest = self._validate_plan_contract(plan)
        payload = {
            "tool_id": plan.tool_id, "operation_id": plan.operation_id,
            "category": plan.category, "tier": plan.tier.value,
            "target": plan.target, "arguments": dict(plan.arguments),
            "executable_path": plan.executable_path,
            "executable_sha256": plan.executable_sha256,
            "executable_size": plan.executable_size,
            "executable_mtime_ns": plan.executable_mtime_ns,
            "argv": plan.argv, "timeout_seconds": plan.timeout_seconds,
            "executable": plan.executable,
            "safe_automatic": plan.safe_automatic,
        }
        if self._plan_digest(payload) != plan.plan_sha256:
            raise NetworkToolError("Execution plan integrity check failed")
        if plan.tier is AutonomyTier.STATE_CHANGING_CONTAINMENT or not plan.executable:
            receipt = self._receipt_start(plan, "denied", "containment_plan_only")
            raise PermissionError(
                f"State-changing containment is plan-only in this registry (receipt {receipt})"
            )
        if not plan.safe_automatic and not active_authorized:
            error_code = (
                "active_approval_required"
                if plan.tier is AutonomyTier.BOUNDED_ACTIVE_PROBE
                else "unreviewed_manifest_approval_required"
            )
            receipt = self._receipt_start(plan, "denied", error_code)
            raise PermissionError(
                f"This operation requires explicit approval (receipt {receipt})"
            )
        try:
            current_tool = self._fingerprint(manifest, plan.executable_path)
        except (OSError, NetworkToolError):
            receipt = self._receipt_start(plan, "denied", "executable_changed")
            raise PermissionError(f"Defensive executable changed (receipt {receipt})")
        if (
            current_tool.executable_sha256 != plan.executable_sha256
            or current_tool.executable_size != plan.executable_size
            or current_tool.executable_mtime_ns != plan.executable_mtime_ns
        ):
            receipt = self._receipt_start(plan, "denied", "executable_changed")
            raise PermissionError(f"Defensive executable changed (receipt {receipt})")
        receipt = self._receipt_start(plan, "running", None)
        safe_environment = {
            key: value for key, value in os.environ.items()
            if key.casefold() in {"systemroot", "windir", "temp", "tmp"}
        }
        try:
            completed = self.runner(
                list(plan.argv), shell=False, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
                timeout=plan.timeout_seconds, cwd=None, env=safe_environment,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            self._receipt_finish(receipt, status="failed", exit_code=None,
                stdout=b"", stderr=b"", error_code="execution_failed")
            raise NetworkToolError("Defensive tool execution failed") from exc
        except Exception as exc:
            self._receipt_finish(receipt, status="failed", exit_code=None,
                stdout=b"", stderr=b"", error_code="runner_failed")
            raise NetworkToolError("Defensive tool runner failed") from exc
        stdout = completed.stdout if isinstance(completed.stdout, bytes) else str(
            completed.stdout or ""
        ).encode("utf-8", errors="replace")
        stderr = completed.stderr if isinstance(completed.stderr, bytes) else str(
            completed.stderr or ""
        ).encode("utf-8", errors="replace")
        if len(stdout) > MAX_CAPTURE_BYTES or len(stderr) > MAX_CAPTURE_BYTES:
            self._receipt_finish(receipt, status="failed", exit_code=completed.returncode,
                stdout=stdout[:MAX_CAPTURE_BYTES], stderr=stderr[:MAX_CAPTURE_BYTES],
                error_code="output_limit_exceeded")
            raise NetworkToolError("Defensive tool output exceeded the bounded limit")
        status = "completed" if completed.returncode == 0 else "failed"
        self._receipt_finish(receipt, status=status, exit_code=completed.returncode,
            stdout=stdout, stderr=stderr,
            error_code=None if completed.returncode == 0 else "nonzero_exit")
        response: dict[str, Any] = {
            "receipt_id": receipt,
            "status": status,
            "exit_code": int(completed.returncode),
            "stdout_bytes": len(stdout),
            "stderr_bytes": len(stderr),
            "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
            "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
            "shell_used": False,
            "disruptive_action_executed": False,
        }
        if include_output:
            stdout_text = redact_secrets(stdout.decode("utf-8", errors="replace"))
            stderr_text = redact_secrets(stderr.decode("utf-8", errors="replace"))
            response.update({
                "stdout": stdout_text[:MAX_RETURN_CHARACTERS],
                "stderr": stderr_text[:MAX_RETURN_CHARACTERS],
                "stdout_truncated": len(stdout_text) > MAX_RETURN_CHARACTERS,
                "stderr_truncated": len(stderr_text) > MAX_RETURN_CHARACTERS,
            })
        return response

    def receipts(self, *, limit: int = 100) -> dict[str, Any]:
        if isinstance(limit, bool) or not 1 <= int(limit) <= 500:
            raise ValueError("limit must be between 1 and 500")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM network_tool_receipts
                ORDER BY started_at DESC, receipt_id DESC LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        return {"receipts": [dict(row) for row in rows]}
