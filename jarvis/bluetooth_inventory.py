from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import subprocess
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator
from uuid import uuid4


BLUETOOTH_INVENTORY_SCHEMA_VERSION = 3
MAX_BLUETOOTH_DEVICES = 256
MAX_BLUETOOTH_EVENTS = 5_000
MAX_BLUETOOTH_OBSERVATIONS = 20_000
MAX_PENDING_BLUETOOTH_ALERTS = 256
MAX_RESOLVED_BLUETOOTH_ALERTS = 5_000
BLUETOOTH_ALERT_TTL_DAYS = 30
DEFAULT_CHECK_COOLDOWN_SECONDS = 10
DEFAULT_CHECK_LEASE_SECONDS = 30
BLUETOOTH_OBSERVATION_FRESH_SECONDS = 5 * 60
TRUST_STATES = frozenset({"unreviewed", "recognized", "watch", "retired"})
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
_IDENTITY_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_ALERT_RECEIPT = re.compile(r"[0-9a-f]{32}\Z")
_SEPARATED_HARDWARE_ADDRESS = re.compile(
    r"(?i)(?<![0-9a-f])(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}(?![0-9a-f])"
)
_DOTTED_HARDWARE_ADDRESS = re.compile(
    r"(?i)(?<![0-9a-f])(?:[0-9a-f]{4}\.){2}[0-9a-f]{4}(?![0-9a-f])"
)
_SPACED_HARDWARE_ADDRESS = re.compile(
    r"(?i)(?<![0-9a-f])(?:[0-9a-f]{2} ){5}[0-9a-f]{2}(?![0-9a-f])"
)
_COMPACT_HARDWARE_ADDRESS = re.compile(
    r"(?i)(?<![0-9a-f])[0-9a-f]{12}(?![0-9a-f])"
)
_IPV4_ADDRESS = re.compile(
    r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)"
)
_ADDRESS_PATTERNS = (
    _SEPARATED_HARDWARE_ADDRESS,
    _DOTTED_HARDWARE_ADDRESS,
    _SPACED_HARDWARE_ADDRESS,
    _COMPACT_HARDWARE_ADDRESS,
    _IPV4_ADDRESS,
)
_MAX_PROVIDER_BYTES = 1_000_000


class BluetoothInventoryError(RuntimeError):
    """A bounded read-only Bluetooth inventory could not be collected safely."""


class BluetoothInventoryRateLimited(BluetoothInventoryError):
    """A durable cooldown or another live Bluetooth check blocked this attempt."""

    def __init__(self, message: str, *, retry_after_seconds: int) -> None:
        super().__init__(message)
        self.retry_after_seconds = max(1, int(retry_after_seconds))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def _clean_text(value: Any, limit: int) -> str | None:
    text = _CONTROL_CHARACTERS.sub(" ", str(value or ""))
    text = " ".join(text.split()).strip()
    return text[:limit] or None


def _redact_address_like_text(value: Any, limit: int) -> tuple[str | None, bool]:
    # Redact before applying the final field bound so truncation cannot cut an
    # address into a form that evades the detector.
    clean = _clean_text(value, max(4_096, limit * 4))
    if clean is None:
        return None, False
    redacted = clean
    for pattern in _ADDRESS_PATTERNS:
        redacted = pattern.sub("[redacted address]", redacted)
    return redacted[:limit] or None, redacted != clean


def _contains_address_like(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_contains_address_like(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_address_like(item) for item in value)
    if not isinstance(value, str):
        return False
    return any(pattern.search(value) is not None for pattern in _ADDRESS_PATTERNS)


def _optional_bool(value: Any, *, available: bool) -> bool | None:
    if not available:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "1"}:
            return True
        if normalized in {"false", "0"}:
            return False
    return None


def _clean_categories(value: Any) -> tuple[list[str], bool]:
    if isinstance(value, str):
        source = [value]
    elif isinstance(value, list):
        source = value
    else:
        source = []
    result: list[str] = []
    any_redacted = False
    for item in source[:16]:
        clean, redacted = _redact_address_like_text(item, 80)
        any_redacted = any_redacted or redacted
        if clean and clean.casefold() not in {row.casefold() for row in result}:
            result.append(clean)
    return result, any_redacted


# This script has no caller-controlled interpolation. It enumerates only endpoints
# Windows already confirms are paired. It does not use AdvertisementWatcher,
# connect, pair, unpair, write a GATT characteristic, or change a radio/device.
_WINDOWS_PAIRED_BLUETOOTH_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$deviceInfoType = [Windows.Devices.Enumeration.DeviceInformation,Windows.Devices.Enumeration,ContentType=WindowsRuntime]
$collectionType = [Windows.Devices.Enumeration.DeviceInformationCollection,Windows.Devices.Enumeration,ContentType=WindowsRuntime]
$kindType = [Windows.Devices.Enumeration.DeviceInformationKind,Windows.Devices.Enumeration,ContentType=WindowsRuntime]
$classicType = [Windows.Devices.Bluetooth.BluetoothDevice,Windows.Devices.Bluetooth,ContentType=WindowsRuntime]
$lowEnergyType = [Windows.Devices.Bluetooth.BluetoothLEDevice,Windows.Devices.Bluetooth,ContentType=WindowsRuntime]
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
  $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetParameters().Count -eq 1
} | Select-Object -First 1).MakeGenericMethod($collectionType)
$properties = [System.Collections.Generic.List[string]]::new()
@(
  'System.Devices.Aep.IsPaired',
  'System.Devices.Aep.IsPresent',
  'System.Devices.Aep.IsConnected',
  'System.Devices.Aep.Manufacturer',
  'System.Devices.Aep.ModelName',
  'System.Devices.Aep.Category'
) | ForEach-Object { [void]$properties.Add($_) }
function Read-Property($device, [string]$name) {
  $entry = @($device.Properties | Where-Object Key -eq $name | Select-Object -First 1)
  if ($entry.Count -gt 0) { return $entry[0].Value }
  return $null
}
function Read-PairedEndpoints([string]$selector, [string]$transport) {
  $operation = $deviceInfoType::FindAllAsync(
    $selector, $properties, $kindType::AssociationEndpoint
  )
  $task = $asTask.Invoke($null, @($operation))
  $task.Wait()
  foreach ($device in $task.Result) {
    $present = Read-Property $device 'System.Devices.Aep.IsPresent'
    $connected = Read-Property $device 'System.Devices.Aep.IsConnected'
    [pscustomobject]@{
      raw_id = [string]$device.Id
      transport = $transport
      name = [string]$device.Name
      paired = [bool]$device.Pairing.IsPaired
      paired_evidence_available = $true
      present = $present
      present_evidence_available = ($null -ne $present)
      connected = $connected
      connected_evidence_available = ($null -ne $connected)
      manufacturer = [string](Read-Property $device 'System.Devices.Aep.Manufacturer')
      model_name = [string](Read-Property $device 'System.Devices.Aep.ModelName')
      categories = @((Read-Property $device 'System.Devices.Aep.Category'))
    }
  }
}
$rows = @()
$rows += @(Read-PairedEndpoints ($classicType::GetDeviceSelectorFromPairingState($true)) 'classic')
$rows += @(Read-PairedEndpoints ($lowEnergyType::GetDeviceSelectorFromPairingState($true)) 'low_energy')
[pscustomobject]@{
  provider = 'windows_device_information'
  observed_at = [datetime]::UtcNow.ToString('o')
  devices = @($rows)
} | ConvertTo-Json -Compress -Depth 5
"""


def _windows_paired_bluetooth(*, timeout: float = 8.0) -> dict[str, Any]:
    if os.name != "nt":
        raise BluetoothInventoryError(
            "Paired Bluetooth inventory currently requires Windows"
        )
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    executable = (
        system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    )
    if not executable.is_file():
        raise BluetoothInventoryError("Windows PowerShell is unavailable")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            [
                str(executable),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                _WINDOWS_PAIRED_BLUETOOTH_SCRIPT,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(1.0, min(float(timeout), 30.0)),
            check=False,
            creationflags=flags,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BluetoothInventoryError(
            "Windows paired-Bluetooth enumeration failed"
        ) from exc
    if completed.returncode != 0:
        raise BluetoothInventoryError(
            "Windows paired-Bluetooth enumeration returned an error"
        )
    output = completed.stdout.lstrip("\ufeff").strip()
    if not output or len(output.encode("utf-8", errors="replace")) > _MAX_PROVIDER_BYTES:
        raise BluetoothInventoryError(
            "Windows paired-Bluetooth enumeration returned invalid data"
        )
    try:
        decoded = json.loads(output)
    except json.JSONDecodeError as exc:
        raise BluetoothInventoryError(
            "Windows paired-Bluetooth enumeration returned invalid data"
        ) from exc
    if not isinstance(decoded, dict):
        raise BluetoothInventoryError(
            "Windows paired-Bluetooth enumeration returned invalid data"
        )
    return decoded


def _normalize_provider_result(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BluetoothInventoryError("Bluetooth provider returned invalid data")
    raw_rows = value.get("devices", [])
    if not isinstance(raw_rows, list):
        raise BluetoothInventoryError("Bluetooth provider returned invalid devices")
    if len(raw_rows) > MAX_BLUETOOTH_DEVICES:
        raise BluetoothInventoryError("Bluetooth provider exceeded the device limit")
    devices: list[dict[str, Any]] = []
    seen_raw: set[str] = set()
    for raw in raw_rows:
        if not isinstance(raw, dict) or raw.get("paired") is not True:
            continue
        raw_id = _clean_text(raw.get("raw_id"), 2_048)
        if raw_id is None or raw_id in seen_raw:
            continue
        transport = str(raw.get("transport") or "").strip().casefold()
        if transport not in {"classic", "low_energy"}:
            transport = "unknown"
        paired_available = raw.get("paired_evidence_available") is True
        if not paired_available:
            continue
        present_available = raw.get("present_evidence_available") is True
        connected_available = raw.get("connected_evidence_available") is True
        name, name_redacted = _redact_address_like_text(raw.get("name"), 160)
        manufacturer, manufacturer_redacted = _redact_address_like_text(
            raw.get("manufacturer"), 120
        )
        model_name, model_redacted = _redact_address_like_text(
            raw.get("model_name"), 160
        )
        categories, categories_redacted = _clean_categories(raw.get("categories"))
        seen_raw.add(raw_id)
        devices.append({
            "raw_id": raw_id,
            "transport": transport,
            "name": name,
            "manufacturer": manufacturer,
            "model_name": model_name,
            "categories": categories,
            "metadata_address_redacted": any((
                name_redacted,
                manufacturer_redacted,
                model_redacted,
                categories_redacted,
            )),
            "paired": True,
            "paired_evidence_available": True,
            "present": _optional_bool(
                raw.get("present"), available=present_available
            ),
            "present_evidence_available": present_available,
            "connected": _optional_bool(
                raw.get("connected"), available=connected_available
            ),
            "connected_evidence_available": connected_available,
        })
    return {
        "provider": "windows_device_information",
        "devices": devices,
        "observed_at": _clean_text(value.get("observed_at"), 80),
    }


def _signal(
    rule_id: str,
    severity: str,
    summary: str,
    recommended_action: str,
    *,
    benign_explanations: tuple[str, ...],
    device_id: str | None = None,
) -> dict[str, Any]:
    return {
        "rule_id": rule_id,
        "severity": severity,
        "summary": summary,
        "recommended_action": recommended_action,
        "benign_explanations": list(benign_explanations),
        "device_id": device_id,
        "compromise_established": False,
    }


def assess_bluetooth_inventory(
    *,
    last_check_at: str | None,
    freshness_state: str,
    baseline_created: bool,
    devices: list[dict[str, Any]],
    newly_observed: set[str],
) -> dict[str, Any]:
    """Return a deterministic review assessment; never a compromise verdict."""
    signals: list[dict[str, Any]] = []
    if not last_check_at:
        signals.append(_signal(
            "observation_not_established",
            "medium",
            "Jarvis has not completed a paired-Bluetooth inventory check.",
            "Run an explicit read-only check before drawing conclusions.",
            benign_explanations=(
                "Bluetooth inventory may be disabled or Windows may not expose the API.",
            ),
        ))
    elif freshness_state != "fresh":
        signals.append(_signal(
            "observation_stale",
            "medium",
            "The last paired-Bluetooth inventory is stale, so current pairing state is unknown.",
            "Run a fresh read-only check before reviewing device state or changing a trust label.",
            benign_explanations=(
                "Jarvis may have been stopped, asleep, paused, or unable to query Windows.",
            ),
        ))
    elif baseline_created:
        signals.append(_signal(
            "baseline_created",
            "informational",
            "The first paired-Bluetooth baseline was recorded without treating existing devices as new.",
            "Label devices you recognize; no containment action is warranted from a baseline alone.",
            benign_explanations=(
                "All endpoints may have been paired before Jarvis began observing them.",
            ),
        ))
    for device in devices[:MAX_BLUETOOTH_DEVICES]:
        device_id = str(device.get("device_id") or "")
        trust = str(device.get("trust_state") or "unreviewed").casefold()
        paired_now = device.get("paired_now") is True
        if freshness_state != "fresh" or not paired_now:
            continue
        observation_at: datetime | None = None
        profile_updated_at: datetime | None = None
        timing_evidence_valid = True
        try:
            if device.get("last_observed_at"):
                observation_at = _parse_time(str(device["last_observed_at"]))
            if device.get("profile_updated_at"):
                profile_updated_at = _parse_time(str(device["profile_updated_at"]))
        except (TypeError, ValueError):
            observation_at = None
            profile_updated_at = None
            timing_evidence_valid = False
        profile_has_fresh_evidence = (
            timing_evidence_valid
            and (
                profile_updated_at is None
                or (
                    observation_at is not None
                    and observation_at > profile_updated_at
                )
            )
        )
        if trust == "retired":
            if not profile_has_fresh_evidence:
                continue
            signals.append(_signal(
                "retired_endpoint_paired",
                "high",
                "An endpoint marked as retired is again reported as paired by Windows.",
                "Confirm whether it was intentionally returned to use; otherwise remove it through Windows after verifying the device.",
                benign_explanations=(
                    "The local retired label may be outdated, or Windows may retain a stale paired record.",
                ),
                device_id=device_id,
            ))
        elif trust == "watch":
            if not profile_has_fresh_evidence:
                continue
            signals.append(_signal(
                "watched_endpoint_paired",
                "medium",
                "An endpoint marked for review is reported as paired by Windows.",
                "Review the Windows-reported details and pairing history before changing the device.",
                benign_explanations=(
                    "The device may be expected and the watch label may simply need updating.",
                ),
                device_id=device_id,
            ))
        elif trust == "unreviewed" and device_id in newly_observed:
            signals.append(_signal(
                "new_unreviewed_paired_endpoint",
                "medium",
                "A Windows-paired Bluetooth endpoint was first observed by Jarvis after the baseline.",
                "Match it to a device you intentionally paired before taking action.",
                benign_explanations=(
                    "It may be a newly paired accessory, a re-paired device, or an endpoint whose Windows identity changed.",
                ),
                device_id=device_id,
            ))
    rank = {"none": 0, "informational": 1, "medium": 2, "high": 3}
    highest = max(
        (str(item["severity"]) for item in signals),
        key=lambda value: rank.get(value, 0),
        default="none",
    )
    attention = sum(
        1 for item in signals if item["severity"] in {"medium", "high"}
    )
    posture = "review" if attention else (
        "baseline" if baseline_created else "limited_observation"
    )
    return {
        "schema_version": 1,
        "posture": posture,
        "highest_severity": highest,
        "attention_signal_count": attention,
        "signals": signals,
        "conclusion": (
            "One or more paired Bluetooth endpoint records need operator review."
            if attention
            else "No endpoint-specific review signal is established by the available paired-device evidence."
        ),
        "evidence_boundary": (
            "This assessment covers only Windows-reported paired endpoints. It does not scan nearby radios, inspect traffic, prove physical proximity, or establish compromise."
        ),
        "compromise_established": False,
        "automatic_containment": {"enabled": False, "actions_taken": 0},
    }


class BluetoothInventory:
    """Durable, private history of endpoints Windows already reports as paired.

    Raw Windows Association Endpoint identifiers are used only in memory to build
    a keyed digest. They are never persisted, logged, returned, or model-visible.
    """

    def __init__(
        self,
        data_dir: Path,
        *,
        enumerator: Callable[[], dict[str, Any]] = _windows_paired_bluetooth,
        clock: Callable[[], datetime] = _utc_now,
        min_check_interval_seconds: int = DEFAULT_CHECK_COOLDOWN_SECONDS,
        lease_seconds: int = DEFAULT_CHECK_LEASE_SECONDS,
    ) -> None:
        self.path = Path(data_dir).resolve() / "bluetooth-inventory.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.enumerator = enumerator
        self.clock = clock
        self.min_check_interval_seconds = max(0, int(min_check_interval_seconds))
        self.lease_seconds = max(10, min(int(lease_seconds), 300))
        self._lock = threading.Lock()
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        # A future build may own a different schema and different privacy
        # invariants. Inspect an existing version through a read-only handle
        # before this build enables WAL, creates tables, migrates columns, or
        # scrubs metadata. A rejected future database therefore remains byte
        # for byte untouched by this process.
        self._reject_future_schema_readonly()
        with self._connect() as connection:
            schema = self._read_existing_schema(connection)
            if schema is not None and schema > BLUETOOTH_INVENTORY_SCHEMA_VERSION:
                raise BluetoothInventoryError(
                    "Bluetooth database schema is newer than this Jarvis build"
                )
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS bluetooth_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bluetooth_devices (
                    device_id TEXT PRIMARY KEY,
                    identity_sha256 TEXT UNIQUE NOT NULL,
                    os_name TEXT,
                    manufacturer TEXT,
                    model_name TEXT,
                    categories_json TEXT NOT NULL DEFAULT '[]',
                    transports_json TEXT NOT NULL DEFAULT '[]',
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    seen_count INTEGER NOT NULL,
                    label TEXT,
                    trust_state TEXT NOT NULL DEFAULT 'unreviewed',
                    device_type TEXT,
                    identity_confidence TEXT NOT NULL DEFAULT 'moderate',
                    identity_basis TEXT NOT NULL,
                    profile_updated_at TEXT,
                    metadata_address_redacted INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS bluetooth_checks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    observed_at TEXT NOT NULL,
                    completed_at TEXT,
                    status TEXT NOT NULL,
                    observed_devices INTEGER NOT NULL DEFAULT 0,
                    provider TEXT NOT NULL,
                    error_code TEXT
                );
                CREATE TABLE IF NOT EXISTS bluetooth_observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    check_id INTEGER NOT NULL,
                    device_id TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    transport TEXT NOT NULL,
                    paired INTEGER NOT NULL,
                    present INTEGER,
                    present_evidence_available INTEGER NOT NULL,
                    connected INTEGER,
                    connected_evidence_available INTEGER NOT NULL,
                    FOREIGN KEY(check_id) REFERENCES bluetooth_checks(id),
                    FOREIGN KEY(device_id) REFERENCES bluetooth_devices(device_id)
                );
                CREATE TABLE IF NOT EXISTS bluetooth_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    check_id INTEGER,
                    device_id TEXT,
                    observed_at TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    FOREIGN KEY(device_id) REFERENCES bluetooth_devices(device_id)
                );
                CREATE TABLE IF NOT EXISTS bluetooth_alert_receipts (
                    event_id INTEGER PRIMARY KEY,
                    receipt_id TEXT NOT NULL UNIQUE
                        CHECK(length(receipt_id)=32 AND
                              receipt_id NOT GLOB '*[^0-9a-f]*'),
                    created_at TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending'
                        CHECK(state IN ('pending', 'acknowledged', 'expired')),
                    resolved_at TEXT,
                    resolution_reason TEXT,
                    CHECK(
                        (state='pending' AND resolved_at IS NULL AND
                         resolution_reason IS NULL)
                        OR
                        (state!='pending' AND resolved_at IS NOT NULL AND
                         resolution_reason IS NOT NULL)
                    ),
                    FOREIGN KEY(event_id) REFERENCES bluetooth_events(id)
                        ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS bluetooth_check_leases (
                    lease_name TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    leased_until TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS bluetooth_observations_check_idx
                    ON bluetooth_observations(check_id, device_id);
                CREATE INDEX IF NOT EXISTS bluetooth_events_device_idx
                    ON bluetooth_events(device_id, id DESC);
                CREATE INDEX IF NOT EXISTS bluetooth_alert_receipts_state_idx
                    ON bluetooth_alert_receipts(state, event_id);
            """)
            device_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(bluetooth_devices)"
                ).fetchall()
            }
            if "metadata_address_redacted" not in device_columns:
                connection.execute(
                    """
                    ALTER TABLE bluetooth_devices ADD COLUMN
                    metadata_address_redacted INTEGER NOT NULL DEFAULT 0
                    """
                )
            # Migration-time defense in depth: an earlier build may have stored
            # an address embedded in a Windows display/model string. Scrub it
            # before any status/detail renderer can read the row.
            for row in connection.execute(
                """
                SELECT device_id, os_name, manufacturer, model_name,
                       categories_json, label, device_type,
                       metadata_address_redacted
                FROM bluetooth_devices
                """
            ).fetchall():
                os_name, name_redacted = _redact_address_like_text(
                    row["os_name"], 160
                )
                manufacturer, manufacturer_redacted = _redact_address_like_text(
                    row["manufacturer"], 120
                )
                model_name, model_redacted = _redact_address_like_text(
                    row["model_name"], 160
                )
                try:
                    raw_categories = json.loads(str(row["categories_json"]))
                except (json.JSONDecodeError, TypeError):
                    raw_categories = []
                categories, categories_redacted = _clean_categories(raw_categories)
                label, label_redacted = _redact_address_like_text(row["label"], 120)
                device_type, type_redacted = _redact_address_like_text(
                    row["device_type"], 80
                )
                was_redacted = bool(
                    row["metadata_address_redacted"]
                    or name_redacted
                    or manufacturer_redacted
                    or model_redacted
                    or categories_redacted
                    or label_redacted
                    or type_redacted
                )
                connection.execute(
                    """
                    UPDATE bluetooth_devices SET
                        os_name=?, manufacturer=?, model_name=?,
                        categories_json=?, label=?, device_type=?,
                        metadata_address_redacted=?
                    WHERE device_id=?
                    """,
                    (
                        os_name,
                        manufacturer,
                        model_name,
                        json.dumps(categories, separators=(",", ":")),
                        label,
                        device_type,
                        int(was_redacted),
                        str(row["device_id"]),
                    ),
                )
            connection.execute(
                """
                INSERT INTO bluetooth_meta(key, value) VALUES ('schema_version', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (str(BLUETOOTH_INVENTORY_SCHEMA_VERSION),),
            )
            secret = connection.execute(
                "SELECT value FROM bluetooth_meta WHERE key='identity_secret'"
            ).fetchone()
            if secret is None:
                connection.execute(
                    "INSERT INTO bluetooth_meta(key, value) VALUES ('identity_secret', ?)",
                    (secrets.token_hex(32),),
                )

    @staticmethod
    def _read_existing_schema(connection: sqlite3.Connection) -> int | None:
        table = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type='table' AND name='bluetooth_meta'
            """
        ).fetchone()
        if table is None:
            return None
        row = connection.execute(
            "SELECT value FROM bluetooth_meta WHERE key='schema_version'"
        ).fetchone()
        if row is None:
            return None
        try:
            return int(row["value"] if isinstance(row, sqlite3.Row) else row[0])
        except (TypeError, ValueError, OverflowError) as exc:
            raise BluetoothInventoryError(
                "Bluetooth database schema version is invalid"
            ) from exc

    def _reject_future_schema_readonly(self) -> None:
        if not self.path.exists():
            return
        uri = f"{self.path.as_uri()}?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=10)
            connection.row_factory = sqlite3.Row
            try:
                schema = self._read_existing_schema(connection)
            finally:
                connection.close()
        except BluetoothInventoryError:
            raise
        except sqlite3.DatabaseError as exc:
            raise BluetoothInventoryError(
                "Bluetooth database could not be inspected safely"
            ) from exc
        if schema is not None and schema > BLUETOOTH_INVENTORY_SCHEMA_VERSION:
            raise BluetoothInventoryError(
                "Bluetooth database schema is newer than this Jarvis build"
            )

    def _identity_secret(self, connection: sqlite3.Connection) -> bytes:
        row = connection.execute(
            "SELECT value FROM bluetooth_meta WHERE key='identity_secret'"
        ).fetchone()
        if row is None:
            raise BluetoothInventoryError("Bluetooth identity key is unavailable")
        try:
            secret = bytes.fromhex(str(row["value"]))
        except ValueError as exc:
            raise BluetoothInventoryError("Bluetooth identity key is invalid") from exc
        if len(secret) != 32:
            raise BluetoothInventoryError("Bluetooth identity key is invalid")
        return secret

    @staticmethod
    def _identity_digest(secret: bytes, raw_id: str) -> str:
        return hmac.new(
            secret,
            raw_id.encode("utf-8", errors="strict"),
            hashlib.sha256,
        ).hexdigest()

    def _acquire_check(self, now: datetime) -> tuple[str, int]:
        owner = uuid4().hex
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            lease = connection.execute(
                "SELECT leased_until FROM bluetooth_check_leases WHERE lease_name='paired'"
            ).fetchone()
            if lease is not None:
                remaining = int(
                    (_parse_time(str(lease["leased_until"])) - now).total_seconds()
                )
                if remaining > 0:
                    raise BluetoothInventoryRateLimited(
                        "A paired-Bluetooth check is already running",
                        retry_after_seconds=remaining,
                    )
            last = connection.execute(
                """
                SELECT completed_at FROM bluetooth_checks
                WHERE status='completed' ORDER BY id DESC LIMIT 1
                """
            ).fetchone()
            if self.min_check_interval_seconds and last and last["completed_at"]:
                elapsed = int(
                    (now - _parse_time(str(last["completed_at"]))).total_seconds()
                )
                if elapsed < self.min_check_interval_seconds:
                    raise BluetoothInventoryRateLimited(
                        "Please wait briefly before checking paired Bluetooth again",
                        retry_after_seconds=(
                            self.min_check_interval_seconds - max(0, elapsed)
                        ),
                    )
            connection.execute(
                """
                INSERT INTO bluetooth_check_leases(lease_name, owner, leased_until)
                VALUES ('paired', ?, ?)
                ON CONFLICT(lease_name) DO UPDATE SET
                    owner=excluded.owner, leased_until=excluded.leased_until
                """,
                (owner, _iso(now + timedelta(seconds=self.lease_seconds))),
            )
            row = connection.execute(
                """
                INSERT INTO bluetooth_checks(
                    observed_at, status, observed_devices, provider
                ) VALUES (?, 'running', 0, 'windows_device_information')
                RETURNING id
                """,
                (_iso(now),),
            ).fetchone()
        assert row is not None
        return owner, int(row["id"])

    def _release_check(self, owner: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM bluetooth_check_leases WHERE lease_name='paired' AND owner=?",
                (owner,),
            )

    def _failed_check(self, check_id: int, error_code: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE bluetooth_checks
                SET status='failed', completed_at=?, error_code=? WHERE id=?
                """,
                (_iso(self.clock().astimezone(timezone.utc)), error_code[:80], check_id),
            )

    @staticmethod
    def _event(
        connection: sqlite3.Connection,
        *,
        check_id: int | None,
        device_id: str | None,
        observed_at: str,
        event_type: str,
        summary: str,
    ) -> int:
        cursor = connection.execute(
            """
            INSERT INTO bluetooth_events(
                check_id, device_id, observed_at, event_type, summary
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (check_id, device_id, observed_at, event_type[:80], summary[:300]),
        )
        event_id = int(cursor.lastrowid)
        if event_type == "new_paired_endpoint_observed":
            if check_id is None or device_id is None:
                raise BluetoothInventoryError(
                    "Bluetooth discovery alert is missing bounded event evidence"
                )
            connection.execute(
                """
                INSERT INTO bluetooth_alert_receipts(
                    event_id, receipt_id, created_at, state
                ) VALUES (?, ?, ?, 'pending')
                """,
                (event_id, secrets.token_hex(16), observed_at),
            )
        return event_id

    def _maintain_alert_retention(
        self,
        connection: sqlite3.Connection,
        *,
        now: datetime,
    ) -> None:
        now_text = _iso(now)
        cutoff = _iso(now - timedelta(days=BLUETOOTH_ALERT_TTL_DAYS))
        connection.execute(
            """
            UPDATE bluetooth_alert_receipts SET
                state='expired', resolved_at=?, resolution_reason='ttl'
            WHERE state='pending' AND created_at < ?
            """,
            (now_text, cutoff),
        )
        overflow = connection.execute(
            """
            SELECT event_id FROM bluetooth_alert_receipts
            WHERE state='pending'
            ORDER BY event_id DESC
            LIMIT -1 OFFSET ?
            """,
            (MAX_PENDING_BLUETOOTH_ALERTS,),
        ).fetchall()
        if overflow:
            connection.executemany(
                """
                UPDATE bluetooth_alert_receipts SET
                    state='expired', resolved_at=?, resolution_reason='capacity'
                WHERE event_id=? AND state='pending'
                """,
                ((now_text, int(row["event_id"])) for row in overflow),
            )
        connection.execute(
            """
            DELETE FROM bluetooth_alert_receipts
            WHERE state!='pending' AND event_id NOT IN (
                SELECT event_id FROM bluetooth_alert_receipts
                WHERE state!='pending'
                ORDER BY event_id DESC LIMIT ?
            )
            """,
            (MAX_RESOLVED_BLUETOOTH_ALERTS,),
        )
        # Pending receipts survive ordinary event pruning. Resolved receipts
        # remain replay-safe until their own bounded retention removes them.
        connection.execute(
            """
            DELETE FROM bluetooth_events
            WHERE id NOT IN (
                SELECT id FROM bluetooth_events ORDER BY id DESC LIMIT ?
            ) AND id NOT IN (
                SELECT event_id FROM bluetooth_alert_receipts
            )
            """,
            (MAX_BLUETOOTH_EVENTS,),
        )

    def check(self, *, include_os_metadata: bool = False) -> dict[str, Any]:
        now = self.clock().astimezone(timezone.utc)
        owner, check_id = self._acquire_check(now)
        try:
            try:
                result = _normalize_provider_result(self.enumerator())
            except BluetoothInventoryError:
                self._failed_check(check_id, "provider_unavailable")
                raise
            except Exception as exc:
                self._failed_check(check_id, "provider_failed")
                raise BluetoothInventoryError(
                    "Paired-Bluetooth inventory provider failed"
                ) from exc
            now_text = _iso(now)
            with self._lock, self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                completed_before = int(connection.execute(
                    "SELECT COUNT(*) FROM bluetooth_checks WHERE status='completed'"
                ).fetchone()[0])
                baseline_created = completed_before == 0
                previous_check = connection.execute(
                    """
                    SELECT id FROM bluetooth_checks WHERE status='completed'
                    ORDER BY id DESC LIMIT 1
                    """
                ).fetchone()
                previous_ids: set[str] = set()
                if previous_check is not None:
                    previous_ids = {
                        str(row["device_id"])
                        for row in connection.execute(
                            "SELECT DISTINCT device_id FROM bluetooth_observations WHERE check_id=?",
                            (int(previous_check["id"]),),
                        ).fetchall()
                    }
                secret = self._identity_secret(connection)
                current_ids: set[str] = set()
                newly_created: set[str] = set()
                transports_by_id: dict[str, set[str]] = {}
                for source in result["devices"]:
                    digest = self._identity_digest(secret, source["raw_id"])
                    if not _IDENTITY_DIGEST.fullmatch(digest):
                        raise BluetoothInventoryError(
                            "Bluetooth endpoint identity could not be bounded"
                        )
                    prior = connection.execute(
                        "SELECT * FROM bluetooth_devices WHERE identity_sha256=?",
                        (digest,),
                    ).fetchone()
                    device_id = (
                        str(prior["device_id"]) if prior is not None else uuid4().hex
                    )
                    current_ids.add(device_id)
                    transports = transports_by_id.setdefault(device_id, set())
                    transports.add(str(source["transport"]))
                    if prior is None:
                        newly_created.add(device_id)
                        first_seen = now_text
                        seen_count = 1
                        label = device_type = profile_updated_at = None
                        trust_state = "unreviewed"
                    else:
                        first_seen = str(prior["first_seen"])
                        seen_count = int(prior["seen_count"]) + 1
                        label = prior["label"]
                        device_type = prior["device_type"]
                        profile_updated_at = prior["profile_updated_at"]
                        trust_state = str(prior["trust_state"] or "unreviewed")
                        try:
                            transports.update(json.loads(str(prior["transports_json"])))
                        except (json.JSONDecodeError, TypeError):
                            pass
                    connection.execute(
                        """
                        INSERT INTO bluetooth_devices(
                            device_id, identity_sha256, os_name, manufacturer,
                            model_name, categories_json, transports_json,
                            first_seen, last_seen, seen_count, label, trust_state,
                            device_type, identity_confidence, identity_basis,
                            profile_updated_at, metadata_address_redacted
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(identity_sha256) DO UPDATE SET
                            os_name=COALESCE(excluded.os_name, bluetooth_devices.os_name),
                            manufacturer=COALESCE(excluded.manufacturer, bluetooth_devices.manufacturer),
                            model_name=COALESCE(excluded.model_name, bluetooth_devices.model_name),
                            categories_json=excluded.categories_json,
                            transports_json=excluded.transports_json,
                            last_seen=excluded.last_seen,
                            seen_count=excluded.seen_count,
                            metadata_address_redacted=(
                                bluetooth_devices.metadata_address_redacted
                                OR excluded.metadata_address_redacted
                            )
                        """,
                        (
                            device_id,
                            digest,
                            source["name"],
                            source["manufacturer"],
                            source["model_name"],
                            json.dumps(source["categories"], separators=(",", ":")),
                            json.dumps(sorted(transports), separators=(",", ":")),
                            first_seen,
                            now_text,
                            seen_count,
                            label,
                            trust_state,
                            device_type,
                            "moderate",
                            "Windows paired Association Endpoint identifier, stored only as a keyed digest",
                            profile_updated_at,
                            int(source["metadata_address_redacted"]),
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO bluetooth_observations(
                            check_id, device_id, observed_at, transport, paired,
                            present, present_evidence_available, connected,
                            connected_evidence_available
                        ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?)
                        """,
                        (
                            check_id,
                            device_id,
                            now_text,
                            source["transport"],
                            None if source["present"] is None else int(source["present"]),
                            int(source["present_evidence_available"]),
                            None if source["connected"] is None else int(source["connected"]),
                            int(source["connected_evidence_available"]),
                        ),
                    )
                    if prior is None:
                        self._event(
                            connection,
                            check_id=check_id,
                            device_id=device_id,
                            observed_at=now_text,
                            event_type=(
                                "baseline_endpoint_observed"
                                if baseline_created
                                else "new_paired_endpoint_observed"
                            ),
                            summary=(
                                "A Windows-paired Bluetooth endpoint was included in the initial baseline"
                                if baseline_created
                                else "A Windows-paired Bluetooth endpoint was first observed by Jarvis"
                            ),
                        )
                    elif device_id not in previous_ids:
                        self._event(
                            connection,
                            check_id=check_id,
                            device_id=device_id,
                            observed_at=now_text,
                            event_type="paired_endpoint_returned",
                            summary="A previously observed Windows-paired Bluetooth endpoint appeared again",
                        )
                for missing in sorted(previous_ids - current_ids):
                    self._event(
                        connection,
                        check_id=check_id,
                        device_id=missing,
                        observed_at=now_text,
                        event_type="endpoint_no_longer_listed",
                        summary="A previously listed paired Bluetooth endpoint was absent from this Windows snapshot",
                    )
                connection.execute(
                    """
                    UPDATE bluetooth_checks SET
                        status='completed', completed_at=?, observed_devices=?, provider=?
                    WHERE id=?
                    """,
                    (
                        now_text,
                        len(current_ids),
                        str(result["provider"]),
                        check_id,
                    ),
                )
                connection.execute(
                    """
                    DELETE FROM bluetooth_observations WHERE id NOT IN (
                        SELECT id FROM bluetooth_observations
                        ORDER BY id DESC LIMIT ?
                    )
                    """,
                    (MAX_BLUETOOTH_OBSERVATIONS,),
                )
                self._maintain_alert_retention(
                    connection,
                    now=now,
                )
            newly_observed = set() if baseline_created else newly_created
            return self._snapshot(
                include_os_metadata=include_os_metadata,
                new=newly_observed,
                baseline_created=baseline_created,
                expected_check_id=check_id,
            )
        finally:
            self._release_check(owner)

    def _last_check_state(
        self, connection: sqlite3.Connection
    ) -> tuple[sqlite3.Row | None, dict[str, sqlite3.Row]]:
        check = connection.execute(
            """
            SELECT * FROM bluetooth_checks WHERE status='completed'
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
        if check is None:
            return None, {}
        rows = connection.execute(
            """
            SELECT * FROM bluetooth_observations WHERE check_id=? ORDER BY id
            """,
            (int(check["id"]),),
        ).fetchall()
        return check, {str(row["device_id"]): row for row in rows}

    def _snapshot(
        self,
        *,
        include_os_metadata: bool,
        new: set[str] | None = None,
        baseline_created: bool = False,
        expected_check_id: int | None = None,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            check, observations = self._last_check_state(connection)
            if expected_check_id is not None and (
                check is None or int(check["id"]) != int(expected_check_id)
            ):
                raise BluetoothInventoryError(
                    "Bluetooth check result was superseded before it could be read"
                )
            rows = connection.execute(
                "SELECT * FROM bluetooth_devices ORDER BY last_seen DESC, device_id"
            ).fetchall()
        now = self.clock().astimezone(timezone.utc)
        last_check_at = str(check["completed_at"]) if check else None
        last_check_age_seconds: int | None = None
        clock_anomaly = False
        if last_check_at is None:
            freshness_state = "missing"
        else:
            try:
                age = int((now - _parse_time(last_check_at)).total_seconds())
                if age < 0:
                    clock_anomaly = True
                    freshness_state = "stale"
                    last_check_age_seconds = 0
                else:
                    last_check_age_seconds = age
                    freshness_state = (
                        "fresh"
                        if age <= BLUETOOTH_OBSERVATION_FRESH_SECONDS
                        else "stale"
                    )
            except (TypeError, ValueError):
                freshness_state = "stale"
                clock_anomaly = True
        new_ids = set(new or ())
        rendered = [
            self._render_device(
                row,
                observation=observations.get(str(row["device_id"])),
                observation_fresh=freshness_state == "fresh",
                is_new=str(row["device_id"]) in new_ids,
                include_os_metadata=include_os_metadata,
            )
            for row in rows[:MAX_BLUETOOTH_DEVICES]
        ]
        assessment = assess_bluetooth_inventory(
            last_check_at=last_check_at,
            freshness_state=freshness_state,
            baseline_created=baseline_created,
            devices=rendered,
            newly_observed=new_ids,
        )
        addresses_exposed = _contains_address_like(rendered)
        if addresses_exposed:
            raise BluetoothInventoryError(
                "Bluetooth inventory output failed address-redaction verification"
            )
        return {
            "enabled": True,
            "mode": "paired-readonly",
            "provider": "windows_device_information",
            "last_check_id": int(check["id"]) if check else None,
            "last_check_at": last_check_at,
            "freshness": {
                "state": freshness_state,
                "age_seconds": last_check_age_seconds,
                "fresh_for_seconds": BLUETOOTH_OBSERVATION_FRESH_SECONDS,
                "clock_anomaly": clock_anomaly,
            },
            "baseline_created": bool(baseline_created),
            "paired_now": len(observations) if freshness_state == "fresh" else 0,
            "paired_in_last_check": len(observations),
            "known_endpoints": len(rendered),
            "new_endpoints": len(new_ids),
            "devices": rendered,
            "security_assessment": assessment,
            "nearby_rf_scan_performed": False,
            "pairing_or_control_performed": False,
            "metadata_address_redactions": sum(
                1 for row in rendered if row.get("metadata_address_redacted") is True
            ),
            "addresses_exposed": addresses_exposed,
            "limitations": [
                "Only endpoints Windows reports as paired are included; nearby unpaired Bluetooth radios are not scanned.",
                "A Windows display name may be user-authored and is not proof of an exact manufacturer or model.",
                "Connection and presence are reported only when Windows exposes those exact properties.",
                "A newly observed endpoint is new to Jarvis history, not proof it was just paired or is malicious.",
            ],
        }

    @staticmethod
    def _render_device(
        row: sqlite3.Row,
        *,
        observation: sqlite3.Row | None,
        observation_fresh: bool,
        is_new: bool,
        include_os_metadata: bool,
    ) -> dict[str, Any]:
        device_id = str(row["device_id"])
        label, label_redacted = _redact_address_like_text(row["label"], 120)
        device_type, type_redacted = _redact_address_like_text(
            row["device_type"], 80
        )
        paired_in_last_check = observation is not None
        paired_now = paired_in_last_check and observation_fresh
        present_in_last_check = (
            bool(observation["present"])
            if observation is not None and observation["present"] is not None
            else None
        )
        connected_in_last_check = (
            bool(observation["connected"])
            if observation is not None and observation["connected"] is not None
            else None
        )
        item: dict[str, Any] = {
            "device_id": device_id,
            "display_name": label or (
                f"{device_type} {device_id[:6]}"
                if device_type
                else f"Bluetooth endpoint {device_id[:6]}"
            ),
            "label": label,
            "device_type": device_type,
            "trust_state": str(row["trust_state"] or "unreviewed"),
            "identity_confidence": str(row["identity_confidence"] or "moderate"),
            "identity_basis": str(row["identity_basis"]),
            "paired_now": paired_now,
            "paired_in_last_check": paired_in_last_check,
            "paired_evidence": "Windows DeviceInformation.Pairing.IsPaired",
            "present": (
                present_in_last_check
                if observation_fresh
                else None
            ),
            "present_evidence_available": bool(
                observation_fresh
                and observation is not None
                and observation["present_evidence_available"]
            ),
            "present_in_last_check": present_in_last_check,
            "present_evidence_available_in_last_check": bool(
                observation is not None
                and observation["present_evidence_available"]
            ),
            "connected": (
                connected_in_last_check
                if observation_fresh
                else None
            ),
            "connected_evidence_available": bool(
                observation_fresh
                and observation is not None
                and observation["connected_evidence_available"]
            ),
            "connected_in_last_check": connected_in_last_check,
            "connected_evidence_available_in_last_check": bool(
                observation is not None
                and observation["connected_evidence_available"]
            ),
            "transports": json.loads(str(row["transports_json"])),
            "first_seen": str(row["first_seen"]),
            "last_seen": str(row["last_seen"]),
            "last_observed_at": (
                str(observation["observed_at"])
                if observation is not None
                else None
            ),
            "seen_count": int(row["seen_count"]),
            "profile_updated_at": row["profile_updated_at"],
            "is_new": bool(is_new),
            "metadata_address_redacted": bool(
                row["metadata_address_redacted"]
                or label_redacted
                or type_redacted
            ),
            "enrolled": False,
            "access_authorized": False,
            "control_enabled": False,
            "trust_notice": (
                "This local label does not pair, connect to, control, or grant Jarvis access to the endpoint."
            ),
        }
        if include_os_metadata:
            os_name, name_redacted = _redact_address_like_text(row["os_name"], 160)
            manufacturer, manufacturer_redacted = _redact_address_like_text(
                row["manufacturer"], 120
            )
            model_name, model_redacted = _redact_address_like_text(
                row["model_name"], 160
            )
            try:
                raw_categories = json.loads(str(row["categories_json"]))
            except (json.JSONDecodeError, TypeError):
                raw_categories = []
            categories, categories_redacted = _clean_categories(raw_categories)
            item.update({
                "os_reported_name": os_name,
                "manufacturer": manufacturer,
                "model_name": model_name,
                "categories": categories,
                "metadata_notice": (
                    "These fields are reported by Windows/device metadata and may be absent, generic, or user-authored."
                ),
            })
            item["metadata_address_redacted"] = bool(
                item["metadata_address_redacted"]
                or name_redacted
                or manufacturer_redacted
                or model_redacted
                or categories_redacted
            )
            if label is None and os_name:
                item["display_name"] = os_name
        return item

    def status(self, *, include_os_metadata: bool = False) -> dict[str, Any]:
        return self._snapshot(include_os_metadata=include_os_metadata)

    def list_devices(self, *, include_os_metadata: bool = False) -> dict[str, Any]:
        return self._snapshot(include_os_metadata=include_os_metadata)

    def device_detail(
        self,
        device_id: str,
        *,
        event_limit: int = 100,
        include_os_metadata: bool = False,
    ) -> dict[str, Any]:
        normalized = str(device_id or "").strip().casefold()
        if not normalized:
            raise ValueError("device_id is required")
        if isinstance(event_limit, bool) or not 1 <= int(event_limit) <= 500:
            raise ValueError("event_limit must be between 1 and 500")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM bluetooth_devices WHERE device_id=?", (normalized,)
            ).fetchone()
            if row is None:
                raise KeyError("Bluetooth endpoint was not found")
            latest_check, observations = self._last_check_state(connection)
        observation_fresh = False
        if latest_check is not None and latest_check["completed_at"]:
            try:
                age = int(
                    (
                        self.clock().astimezone(timezone.utc)
                        - _parse_time(str(latest_check["completed_at"]))
                    ).total_seconds()
                )
                observation_fresh = (
                    0 <= age <= BLUETOOTH_OBSERVATION_FRESH_SECONDS
                )
            except (TypeError, ValueError):
                observation_fresh = False
        device = self._render_device(
            row,
            observation=observations.get(normalized),
            observation_fresh=observation_fresh,
            is_new=False,
            include_os_metadata=include_os_metadata,
        )
        return {
            "device": device,
            "events": self.events(
                limit=event_limit, device_id=normalized
            )["events"],
        }

    def events(
        self, *, limit: int = 100, device_id: str | None = None
    ) -> dict[str, Any]:
        if isinstance(limit, bool) or not 1 <= int(limit) <= 500:
            raise ValueError("limit must be between 1 and 500")
        if device_id:
            rows_params: tuple[Any, ...] = (
                str(device_id).strip().casefold(), int(limit)
            )
            where = "WHERE e.device_id=?"
        else:
            rows_params = (int(limit),)
            where = ""
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT e.id, e.device_id, e.observed_at, e.event_type, e.summary,
                       d.label, d.device_type
                FROM bluetooth_events e
                LEFT JOIN bluetooth_devices d ON d.device_id=e.device_id
                {where} ORDER BY e.id DESC LIMIT ?
                """,
                rows_params,
            ).fetchall()
        return {
            "events": [
                {
                    "event_id": int(row["id"]),
                    "device_id": row["device_id"],
                    "observed_at": str(row["observed_at"]),
                    "event_type": str(row["event_type"]),
                    "summary": str(row["summary"]),
                    "display_name": (
                        _redact_address_like_text(row["label"], 120)[0]
                    ) or (
                        f"{_redact_address_like_text(row['device_type'], 80)[0]} {str(row['device_id'])[:6]}"
                        if _redact_address_like_text(row["device_type"], 80)[0]
                        else f"Bluetooth endpoint {str(row['device_id'])[:6]}"
                    ),
                }
                for row in rows
            ]
        }

    def pending_alerts(self, *, limit: int = 50) -> dict[str, Any]:
        """Return durable first-observed alerts without marking them delivered."""
        if isinstance(limit, bool) or not 1 <= int(limit) <= 100:
            raise ValueError("limit must be between 1 and 100")
        now = self.clock().astimezone(timezone.utc)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._maintain_alert_retention(connection, now=now)
            total = int(connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM bluetooth_alert_receipts r
                JOIN bluetooth_events e ON e.id=r.event_id
                WHERE r.state='pending'
                  AND e.event_type='new_paired_endpoint_observed'
                """
            ).fetchone()["count"])
            rows = connection.execute(
                """
                SELECT r.event_id, r.receipt_id, r.created_at,
                       e.device_id, e.observed_at, e.event_type, e.summary,
                       d.label, d.device_type, d.trust_state
                FROM bluetooth_alert_receipts r
                JOIN bluetooth_events e ON e.id=r.event_id
                JOIN bluetooth_devices d ON d.device_id=e.device_id
                WHERE r.state='pending'
                  AND e.event_type='new_paired_endpoint_observed'
                ORDER BY r.event_id ASC LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        alerts: list[dict[str, Any]] = []
        for row in rows:
            label, _ = _redact_address_like_text(row["label"], 120)
            device_type, _ = _redact_address_like_text(row["device_type"], 80)
            device_id = str(row["device_id"])
            alerts.append({
                "event_id": int(row["event_id"]),
                "receipt_id": str(row["receipt_id"]),
                "state": "pending",
                "created_at": str(row["created_at"]),
                "observed_at": str(row["observed_at"]),
                "event_type": str(row["event_type"]),
                "summary": str(row["summary"]),
                "device_id": device_id,
                "display_name": label or (
                    f"{device_type} {device_id[:6]}"
                    if device_type
                    else f"Bluetooth endpoint {device_id[:6]}"
                ),
                "trust_state": str(row["trust_state"] or "unreviewed"),
                "evidence_boundary": (
                    "First observed by Jarvis after its paired-device baseline; "
                    "this is not proof the endpoint was just paired or is malicious."
                ),
            })
        if _contains_address_like(alerts):
            raise BluetoothInventoryError(
                "Bluetooth alert output failed address-redaction verification"
            )
        return {
            "pending_count": total,
            "returned": len(alerts),
            "has_more": total > len(alerts),
            "alerts": alerts,
            "addresses_exposed": False,
        }

    def acknowledge_alert(
        self,
        *,
        event_id: int,
        receipt_id: str,
    ) -> dict[str, Any]:
        """Idempotently acknowledge one exact durable discovery receipt."""
        if (
            isinstance(event_id, bool)
            or not isinstance(event_id, int)
            or not 1 <= event_id <= 9_223_372_036_854_775_807
        ):
            raise ValueError("event_id must be a positive bounded integer")
        receipt = str(receipt_id or "")
        if _ALERT_RECEIPT.fullmatch(receipt) is None:
            raise ValueError("receipt_id must be exactly 32 lowercase hex characters")
        now = self.clock().astimezone(timezone.utc)
        now_text = _iso(now)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._maintain_alert_retention(connection, now=now)
            row = connection.execute(
                """
                SELECT r.state, r.resolved_at, r.resolution_reason
                FROM bluetooth_alert_receipts r
                JOIN bluetooth_events e ON e.id=r.event_id
                WHERE r.event_id=? AND r.receipt_id=?
                  AND e.event_type='new_paired_endpoint_observed'
                """,
                (event_id, receipt),
            ).fetchone()
            if row is None:
                raise KeyError("Bluetooth alert receipt was not found")
            state = str(row["state"])
            if state == "expired":
                raise BluetoothInventoryError(
                    "Bluetooth alert receipt is no longer pending"
                )
            if state == "acknowledged":
                return {
                    "event_id": event_id,
                    "receipt_id": receipt,
                    "state": "acknowledged",
                    "acknowledged_at": str(row["resolved_at"]),
                    "changed": False,
                }
            changed = connection.execute(
                """
                UPDATE bluetooth_alert_receipts SET
                    state='acknowledged', resolved_at=?,
                    resolution_reason='operator_acknowledged'
                WHERE event_id=? AND receipt_id=? AND state='pending'
                """,
                (now_text, event_id, receipt),
            ).rowcount
            if changed != 1:
                raise BluetoothInventoryError(
                    "Bluetooth alert acknowledgement could not be recorded"
                )
        return {
            "event_id": event_id,
            "receipt_id": receipt,
            "state": "acknowledged",
            "acknowledged_at": now_text,
            "changed": True,
        }

    def set_profile(
        self,
        device_id: str,
        *,
        label: str | None = None,
        trust_state: str | None = None,
        device_type: str | None = None,
    ) -> dict[str, Any]:
        normalized = str(device_id or "").strip().casefold()
        if not normalized:
            raise ValueError("device_id is required")
        if label is None and trust_state is None and device_type is None:
            raise ValueError("Provide at least one profile field")
        clean_label = (
            None
            if label is None
            else _redact_address_like_text(label, 120)[0]
        )
        clean_type = (
            None
            if device_type is None
            else _redact_address_like_text(device_type, 80)[0]
        )
        clean_trust = (
            None if trust_state is None else str(trust_state).strip().casefold()
        )
        if clean_trust is not None and clean_trust not in TRUST_STATES:
            raise ValueError(
                "trust_state must be unreviewed, recognized, watch, or retired"
            )
        now_text = _iso(self.clock().astimezone(timezone.utc))
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM bluetooth_devices WHERE device_id=?", (normalized,)
            ).fetchone()
            if row is None:
                raise KeyError("Bluetooth endpoint was not found")
            connection.execute(
                """
                UPDATE bluetooth_devices SET
                    label=?, device_type=?, trust_state=?, profile_updated_at=?
                WHERE device_id=?
                """,
                (
                    row["label"] if label is None else clean_label,
                    row["device_type"] if device_type is None else clean_type,
                    str(row["trust_state"]) if clean_trust is None else clean_trust,
                    now_text,
                    normalized,
                ),
            )
            self._event(
                connection,
                check_id=None,
                device_id=normalized,
                observed_at=now_text,
                event_type="profile_updated",
                summary="Operator-updated local Bluetooth labels; device authority was unchanged",
            )
        return self.device_detail(normalized)["device"]
