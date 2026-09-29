from __future__ import annotations

import csv
import io
import json
import os
import re
import secrets
import stat
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable


IEEE_OUI_URL = "https://standards-oui.ieee.org/oui/oui.csv"
MAX_IEEE_OUI_BYTES = 8 * 1024 * 1024
MIN_IEEE_OUI_ROWS = 10_000
DEFAULT_REFRESH_SECONDS = 30 * 24 * 60 * 60
_WINDOWS_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class NetworkMetadataError(RuntimeError):
    """A bounded device-metadata source could not be loaded safely."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        raise NetworkMetadataError("IEEE registry redirects are not accepted")


def _clean_text(value: Any, limit: int) -> str | None:
    text = re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", str(value or ""))
    text = " ".join(text.split())[:limit]
    return text or None


def _ordinary_file(path: Path) -> bool:
    if not os.path.lexists(path):
        return False
    details = os.lstat(path)
    return bool(
        stat.S_ISREG(details.st_mode)
        and not stat.S_ISLNK(details.st_mode)
        and not (getattr(details, "st_file_attributes", 0) & _WINDOWS_REPARSE_POINT)
    )


def _ensure_ordinary_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    details = os.lstat(path)
    if (
        not stat.S_ISDIR(details.st_mode)
        or stat.S_ISLNK(details.st_mode)
        or getattr(details, "st_file_attributes", 0) & _WINDOWS_REPARSE_POINT
    ):
        raise NetworkMetadataError("Network metadata cache must be an ordinary directory")


def _atomic_bytes(path: Path, payload: bytes) -> None:
    _ensure_ordinary_directory(path.parent)
    # Keep the temporary leaf short so ordinary installations under a long
    # Windows user/project path do not cross the legacy MAX_PATH boundary.
    temporary = path.parent / f".tmp-{secrets.token_hex(6)}"
    try:
        with temporary.open("xb") as destination:
            destination.write(payload)
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _parse_registry(payload: bytes, *, minimum_rows: int) -> dict[str, str]:
    if not payload or len(payload) > MAX_IEEE_OUI_BYTES:
        raise NetworkMetadataError("IEEE registry response has an invalid size")
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise NetworkMetadataError("IEEE registry is not valid UTF-8 CSV") from exc
    reader = csv.DictReader(io.StringIO(text))
    required = {"Registry", "Assignment", "Organization Name"}
    if reader.fieldnames is None or not required.issubset(set(reader.fieldnames)):
        raise NetworkMetadataError("IEEE registry CSV headers are invalid")
    vendors: dict[str, str] = {}
    for row in reader:
        assignment = re.sub(r"[^0-9a-f]", "", str(row.get("Assignment") or "").casefold())
        organization = _clean_text(row.get("Organization Name"), 200)
        if len(assignment) != 6 or organization is None:
            continue
        vendors.setdefault(assignment, organization)
    if len(vendors) < minimum_rows:
        raise NetworkMetadataError("IEEE registry CSV is unexpectedly incomplete")
    return vendors


class IeeeOuiRegistry:
    """Locally cache the complete public IEEE OUI registry.

    The fixed registry URL receives no observed MAC address, hostname, IP address,
    or other endpoint identifier. Lookups happen against the local cache only.
    """

    def __init__(
        self,
        data_dir: Path,
        *,
        fetcher: Callable[[], bytes] | None = None,
        clock: Callable[[], float] = time.time,
        refresh_seconds: int = DEFAULT_REFRESH_SECONDS,
        minimum_rows: int = MIN_IEEE_OUI_ROWS,
    ) -> None:
        self.cache_dir = Path(data_dir).resolve() / "network-metadata"
        self.csv_path = self.cache_dir / "ieee-oui.csv"
        self.receipt_path = self.cache_dir / "ieee-oui-receipt.json"
        self.fetcher = fetcher or self._download
        self.clock = clock
        self.refresh_seconds = max(3600, int(refresh_seconds))
        self.minimum_rows = max(1, int(minimum_rows))
        self._vendors: dict[str, str] | None = None
        self._lock = threading.Lock()
        self.last_error: str | None = None
        self.last_source: str | None = None

    @staticmethod
    def _download() -> bytes:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirect(),
        )
        request = urllib.request.Request(
            IEEE_OUI_URL,
            headers={"Accept": "text/csv", "User-Agent": "JarvisLocal/0.6 network-metadata"},
            method="GET",
        )
        with opener.open(request, timeout=15.0) as response:
            if int(getattr(response, "status", 0) or response.getcode()) != 200:
                raise NetworkMetadataError("IEEE registry returned a non-success status")
            if str(response.geturl()) != IEEE_OUI_URL:
                raise NetworkMetadataError("IEEE registry origin changed unexpectedly")
            payload = response.read(MAX_IEEE_OUI_BYTES + 1)
        if len(payload) > MAX_IEEE_OUI_BYTES:
            raise NetworkMetadataError("IEEE registry exceeded the download limit")
        return payload

    def _load(self, payload: bytes) -> dict[str, str]:
        return _parse_registry(payload, minimum_rows=self.minimum_rows)

    def _cache_is_fresh(self) -> bool:
        if not _ordinary_file(self.csv_path):
            return False
        age = max(0.0, self.clock() - self.csv_path.stat().st_mtime)
        return age <= self.refresh_seconds

    def ensure_ready(self) -> bool:
        with self._lock:
            if self._vendors is not None:
                return True
            cached_payload: bytes | None = None
            if _ordinary_file(self.csv_path):
                if self.csv_path.stat().st_size > MAX_IEEE_OUI_BYTES:
                    self.last_error = "cached IEEE registry exceeds the size limit"
                else:
                    cached_payload = self.csv_path.read_bytes()
            if cached_payload is not None and self._cache_is_fresh():
                try:
                    self._vendors = self._load(cached_payload)
                    self.last_source = "fresh-cache"
                    self.last_error = None
                    return True
                except NetworkMetadataError as exc:
                    self.last_error = str(exc)

            try:
                payload = self.fetcher()
                vendors = self._load(payload)
                _atomic_bytes(self.csv_path, payload)
                receipt = {
                    "source_url": IEEE_OUI_URL,
                    "fetched_at_unix": int(self.clock()),
                    "vendor_prefixes": len(vendors),
                    "privacy": "The complete public registry was fetched; no observed endpoint identifier was sent.",
                }
                _atomic_bytes(
                    self.receipt_path,
                    (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode("utf-8"),
                )
                self._vendors = vendors
                self.last_source = "official-ieee-download"
                self.last_error = None
                return True
            except (NetworkMetadataError, OSError, ValueError) as exc:
                self.last_error = _clean_text(exc, 300) or type(exc).__name__

            if cached_payload is not None:
                try:
                    self._vendors = self._load(cached_payload)
                    self.last_source = "stale-cache"
                    return True
                except NetworkMetadataError as exc:
                    self.last_error = str(exc)
            return False

    def lookup(self, mac: Any) -> str | None:
        normalized = re.sub(r"[^0-9a-f]", "", str(mac or "").casefold())
        if len(normalized) != 12:
            return None
        # A locally administered address has no authoritative IEEE vendor prefix.
        if int(normalized[:2], 16) & 0x02:
            return None
        if self._vendors is None and not self.ensure_ready():
            return None
        return (self._vendors or {}).get(normalized[:6])

    def status(self) -> dict[str, Any]:
        return {
            "provider": "official-ieee-oui",
            "available": self._vendors is not None,
            "source": self.last_source,
            "error": self.last_error,
            "privacy": "Full registry cache only; observed endpoint identifiers are never uploaded.",
        }


_HOSTNAME_RULES: tuple[tuple[re.Pattern[str], str, float], ...] = (
    (re.compile(r"\b(?:iphone|pixel(?:[-_ ]?\d+)?|galaxy[-_ ]?(?:s|z|a)\d+|oneplus|moto[-_ ]?g|phone)\b", re.I), "Phone", 0.95),
    (re.compile(r"\b(?:ipad|tablet|galaxy[-_ ]?tab)\b", re.I), "Tablet", 0.95),
    (re.compile(r"\b(?:roku|chromecast|google[-_ ]?tv|android[-_ ]?tv|fire[-_ ]?tv|apple[-_ ]?tv|smart[-_ ]?tv|television)\b", re.I), "TV or streaming device", 0.94),
    (re.compile(r"\b(?:desktop|laptop|notebook|macbook|imac|workstation|windows[-_ ]?pc)\b", re.I), "Computer", 0.90),
    (re.compile(r"\b(?:printer|laserjet|officejet|deskjet)\b", re.I), "Printer", 0.92),
    (re.compile(r"\b(?:hue|lifx|light|bulb|lamp)\b", re.I), "Smart light", 0.88),
    (re.compile(r"\b(?:thermostat|hvac|air[-_ ]?conditioner)\b", re.I), "Smart HVAC appliance", 0.88),
    (re.compile(r"\b(?:camera|doorbell)\b", re.I), "Camera or doorbell", 0.88),
    (re.compile(r"\b(?:echo|homepod|sonos|smart[-_ ]?speaker)\b", re.I), "Smart speaker", 0.88),
    (re.compile(r"\b(?:xbox|playstation|nintendo[-_ ]?switch)\b", re.I), "Game console", 0.92),
    (re.compile(r"\b(?:nas|synology|qnap)\b", re.I), "Network storage", 0.92),
)

_VENDOR_RULES: tuple[tuple[re.Pattern[str], str, float], ...] = (
    (re.compile(r"\broku\b", re.I), "TV or streaming device", 0.92),
    (re.compile(r"\b(?:netgear|ubiquiti|aruba|tp[- ]?link|cisco)\b", re.I), "Network infrastructure", 0.86),
    (re.compile(r"\b(?:intel|dell|lenovo|hewlett|hp inc|framework computer)\b", re.I), "Computer", 0.72),
    (re.compile(r"\bgree\b", re.I), "Smart HVAC appliance", 0.88),
    (re.compile(r"\b(?:signify|philips lighting|lifx)\b", re.I), "Smart light", 0.86),
    (re.compile(r"\b(?:ring|arlo)\b", re.I), "Camera or doorbell", 0.82),
    (re.compile(r"\b(?:espressif|tuya)\b", re.I), "Smart-home or IoT device", 0.70),
    (re.compile(r"\bamazon\b", re.I), "Amazon smart-home or streaming device", 0.62),
)


class NetworkMetadataResolver:
    """Fuse operator labels, hostnames, and a local OUI cache conservatively."""

    def __init__(
        self,
        data_dir: Path,
        *,
        mode: str = "disabled",
        registry: IeeeOuiRegistry | None = None,
    ) -> None:
        normalized = str(mode or "disabled").strip().casefold()
        if normalized not in {"disabled", "hostname-oui"}:
            raise ValueError("network metadata mode is invalid")
        self.mode = normalized
        self.registry = registry or IeeeOuiRegistry(data_dir)
        self._prepared = False

    @staticmethod
    def _classify(text: str, rules: tuple[tuple[re.Pattern[str], str, float], ...]) -> tuple[str | None, float]:
        for pattern, label, confidence in rules:
            if pattern.search(text):
                return label, confidence
        return None, 0.0

    def prepare(self) -> bool:
        if self.mode == "disabled":
            return False
        if not self._prepared:
            self.registry.ensure_ready()
            self._prepared = True
        return self.registry.status()["available"] is True

    def resolve(
        self,
        *,
        mac: Any,
        hostname: Any,
        operator_type: Any = None,
        operator_label: Any = None,
    ) -> dict[str, Any]:
        if self.mode == "disabled":
            return {}
        self.prepare()
        manufacturer = self.registry.lookup(mac)
        clean_type = _clean_text(operator_type, 80)
        if clean_type:
            return {
                "manufacturer": manufacturer,
                "device_type": clean_type,
                "device_type_confidence": 1.0,
                "device_type_source": "operator-profile",
                "device_type_basis": "Operator-authored device profile",
            }

        label = _clean_text(operator_label, 120)
        if label:
            device_type, confidence = self._classify(label, _HOSTNAME_RULES)
            if device_type:
                return {
                    "manufacturer": manufacturer,
                    "device_type": device_type,
                    "device_type_confidence": max(0.98, confidence),
                    "device_type_source": "operator-label",
                    "device_type_basis": "Device type inferred from an operator-authored label",
                }

        clean_hostname = _clean_text(hostname, 255)
        if clean_hostname:
            device_type, confidence = self._classify(clean_hostname, _HOSTNAME_RULES)
            if device_type:
                return {
                    "manufacturer": manufacturer,
                    "device_type": device_type,
                    "device_type_confidence": confidence,
                    "device_type_source": "local-hostname",
                    "device_type_basis": "Device type inferred from a locally observed hostname",
                }

        if manufacturer:
            device_type, confidence = self._classify(manufacturer, _VENDOR_RULES)
            return {
                "manufacturer": manufacturer,
                "device_type": device_type,
                "device_type_confidence": confidence,
                "device_type_source": "ieee-oui" if device_type else None,
                "device_type_basis": (
                    "Broad device class inferred from the locally cached IEEE vendor assignment; exact model is not established"
                    if device_type
                    else "IEEE vendor assignment is available, but it does not establish a device class or exact model"
                ),
            }
        return {
            "manufacturer": None,
            "device_type": None,
            "device_type_confidence": 0.0,
            "device_type_source": None,
            "device_type_basis": "No reliable type metadata was available",
        }

    def status(self) -> dict[str, Any]:
        status = self.registry.status()
        status["mode"] = self.mode
        status["enabled"] = self.mode != "disabled"
        return status
