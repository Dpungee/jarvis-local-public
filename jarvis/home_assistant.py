from __future__ import annotations

import ipaddress
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_ENTITY = re.compile(r"remote\.[a-z0-9_]{1,200}\Z")
_TRACKER_ENTITY = re.compile(r"device_tracker\.[a-z0-9_]{1,200}\Z")
_MAC = re.compile(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\Z", re.I)
_PACKAGE = re.compile(
    r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*){1,9}\Z",
    re.I,
)
_APP_ALIASES = {
    "spotify": "com.spotify.tv.android",
    "youtube": "com.google.android.youtube.tv",
}
_REMOTE_COMMANDS = {
    "home": "HOME",
    "back": "BACK",
    "select": "DPAD_CENTER",
    "up": "DPAD_UP",
    "down": "DPAD_DOWN",
    "left": "DPAD_LEFT",
    "right": "DPAD_RIGHT",
    "play_pause": "MEDIA_PLAY_PAUSE",
    "play": "MEDIA_PLAY",
    "pause": "MEDIA_PAUSE",
    "next": "MEDIA_NEXT",
    "previous": "MEDIA_PREVIOUS",
    "volume_up": "VOLUME_UP",
    "volume_down": "VOLUME_DOWN",
    "mute": "VOLUME_MUTE",
    "power": "POWER",
}

_DEVICE_SENSOR_SUFFIXES = {
    "link_rate": "link_rate_mbps",
    "signal_strength": "signal_percent",
    "signal": "signal_percent",
    "link_type": "link_type",
    "type": "link_type",
    "ssid": "ssid",
    "access_point_mac": "access_point_mac",
    "conn_ap_mac": "access_point_mac",
}
_ROUTER_METRIC_SUFFIXES = frozenset({
    "upload_today", "download_today",
    "upload_yesterday", "download_yesterday",
    "upload_week", "upload_week_average",
    "download_week", "download_week_average",
    "upload_month", "upload_month_average",
    "download_month", "download_month_average",
    "upload_last_month", "upload_last_month_average",
    "download_last_month", "download_last_month_average",
    "uplink_bandwidth", "downlink_bandwidth", "average_ping",
    "cpu_utilization", "memory_utilization", "ethernet_link_status",
})


class HomeAssistantError(RuntimeError):
    """A paired Home Assistant action failed or could not be read back."""


def normalize_home_assistant_url(value: str) -> str:
    text = str(value or "").strip()
    parsed = urllib.parse.urlsplit(text)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError(
            "Home Assistant URL must be an exact HTTP(S) origin without credentials or a path"
        )
    host = parsed.hostname.casefold()
    loopback = host == "localhost"
    if not loopback:
        try:
            address = ipaddress.ip_address(host)
        except ValueError as exc:
            raise ValueError(
                "Home Assistant URL must use localhost or a private IP literal"
            ) from exc
        if not (address.is_private or address.is_loopback or address.is_link_local):
            raise ValueError("Home Assistant URL must stay on the private local network")
        loopback = address.is_loopback
    if parsed.scheme == "http" and not loopback:
        raise ValueError(
            "Home Assistant URL must use HTTPS for non-loopback private addresses"
        )
    port = parsed.port
    rendered_host = f"[{host}]" if ":" in host else host
    return f"{parsed.scheme}://{rendered_host}{f':{port}' if port else ''}"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        del req, fp, code, msg, headers, newurl
        return None


@dataclass(frozen=True)
class HomeDeviceState:
    entity_id: str
    friendly_name: str
    state: str
    current_activity: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "friendly_name": self.friendly_name,
            "state": self.state,
            "current_activity": self.current_activity,
        }


class HomeAssistantProvider:
    """Bounded local device control through an operator-paired HA instance."""

    def __init__(
        self,
        base_url: str,
        token: str,
        allowed_entities: tuple[str, ...],
        *,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.base_url = normalize_home_assistant_url(base_url)
        self.token = str(token)
        if not 20 <= len(self.token) <= 4096 or any(
            ord(character) < 32 for character in self.token
        ):
            raise ValueError("Home Assistant token is invalid")
        normalized = tuple(dict.fromkeys(
            str(entity).strip().casefold() for entity in allowed_entities
        ))
        if len(normalized) > 64 or any(
            _ENTITY.fullmatch(entity) is None for entity in normalized
        ):
            raise ValueError("Home Assistant entities must be at most 64 remote.* entity IDs")
        self.allowed_entities = normalized
        self.timeout_seconds = min(30.0, max(1.0, float(timeout_seconds)))
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirect(),
        )

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        if not path.startswith("/api/") or ".." in path:
            raise ValueError("Home Assistant API path is invalid")
        body = None
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }
        if payload is not None:
            body = json.dumps(
                payload, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with self._opener.open(request, timeout=self.timeout_seconds) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise HomeAssistantError("Home Assistant response exceeded the safety limit")
        except urllib.error.HTTPError as exc:
            if 300 <= exc.code < 400:
                raise HomeAssistantError("Home Assistant redirects are refused") from exc
            raise HomeAssistantError(
                f"Home Assistant returned HTTP {exc.code}"
            ) from exc
        except (OSError, urllib.error.URLError) as exc:
            raise HomeAssistantError("Home Assistant is unavailable") from exc
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HomeAssistantError("Home Assistant returned invalid JSON") from exc

    def _state(self, entity_id: str) -> HomeDeviceState:
        entity = str(entity_id).strip().casefold()
        if entity not in self.allowed_entities:
            raise PermissionError("Home Assistant entity is not in the operator allowlist")
        value = self._request("GET", f"/api/states/{entity}")
        if not isinstance(value, dict):
            raise HomeAssistantError("Home Assistant returned an invalid device state")
        attributes = value.get("attributes")
        if not isinstance(attributes, dict):
            attributes = {}
        return HomeDeviceState(
            entity_id=entity,
            friendly_name=str(attributes.get("friendly_name") or entity)[:200],
            state=str(value.get("state") or "unknown")[:80],
            current_activity=(
                str(attributes.get("current_activity"))[:300]
                if attributes.get("current_activity") is not None
                else None
            ),
        )

    def status(self) -> dict[str, Any]:
        devices: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for entity in self.allowed_entities:
            try:
                devices.append(self._state(entity).as_dict())
            except (HomeAssistantError, PermissionError) as exc:
                errors.append({"entity_id": entity, "error": str(exc)[:300]})
        return {
            "configured": True,
            "provider": "home_assistant",
            "base_url": self.base_url,
            "devices": devices,
            "errors": errors,
            "credentials_exposed": False,
        }

    @staticmethod
    def _network_device_type(*values: str | None) -> tuple[str, float]:
        text = " ".join(str(value or "") for value in values).casefold()
        rules = (
            (r"\b(?:iphone|ipad|ios)\b", "Apple mobile device", 0.90),
            (r"\b(?:android|pixel|galaxy|oneplus|moto(?:rola)?)\b", "Android mobile device", 0.85),
            (r"\b(?:google\s*tv|android\s*tv|chromecast|roku|fire\s*tv|shield|smart\s*tv|television)\b", "TV or streaming device", 0.90),
            (r"\b(?:windows|desktop|laptop|macbook|imac|computer|pc)\b", "Computer", 0.80),
            (r"\b(?:printer|laserjet|officejet|epson|brother)\b", "Printer", 0.85),
            (r"\b(?:camera|doorbell|thermostat|speaker|echo|homepod|sonos)\b", "Smart-home device", 0.75),
        )
        for pattern, label, confidence in rules:
            if re.search(pattern, text):
                return label, confidence
        return "Unknown network device", 0.0

    @staticmethod
    def _private_ipv4(value: Any) -> str | None:
        try:
            address = ipaddress.ip_address(str(value or ""))
        except ValueError:
            return None
        if isinstance(address, ipaddress.IPv4Address) and address.is_private:
            return str(address)
        return None

    @staticmethod
    def _normalized_mac(value: Any) -> str | None:
        rendered = str(value or "").strip().replace("-", ":").casefold()
        return rendered if _MAC.fullmatch(rendered) else None

    @staticmethod
    def _metric_value(state: Any, unit: Any) -> dict[str, Any]:
        text = str(state or "")[:120]
        try:
            value: Any = float(text)
        except ValueError:
            value = text
        return {
            "value": value,
            "unit": str(unit or "")[:40] or None,
        }

    def network_telemetry(self) -> dict[str, Any]:
        """Return only bounded router-derived NETGEAR state from Home Assistant."""
        states = self._request("GET", "/api/states")
        if not isinstance(states, list) or len(states) > 4096:
            raise HomeAssistantError("Home Assistant returned an invalid state inventory")

        devices_by_slug: dict[str, dict[str, Any]] = {}
        device_metrics: list[tuple[str, str, Any, Any]] = []
        router_metrics: dict[str, dict[str, Any]] = {}
        for item in states:
            if not isinstance(item, dict):
                continue
            entity_id = str(item.get("entity_id") or "").strip().casefold()
            attributes = item.get("attributes")
            if not isinstance(attributes, dict):
                attributes = {}
            if _TRACKER_ENTITY.fullmatch(entity_id):
                if str(attributes.get("source_type") or "").casefold() != "router":
                    continue
                ipv4 = self._private_ipv4(attributes.get("ip"))
                mac = self._normalized_mac(attributes.get("mac"))
                if ipv4 is None and mac is None:
                    continue
                slug = entity_id.split(".", 1)[1]
                friendly_name = str(
                    attributes.get("friendly_name")
                    or attributes.get("hostname")
                    or slug.replace("_", " ")
                )[:200]
                hostname = str(attributes.get("hostname") or "")[:255] or None
                device_type, confidence = self._network_device_type(
                    friendly_name,
                    hostname,
                    attributes.get("icon"),
                )
                devices_by_slug[slug] = {
                    "entity_id": entity_id,
                    "friendly_name": friendly_name,
                    "hostname": hostname,
                    "ipv4": ipv4,
                    "mac": mac,
                    "connected": str(item.get("state") or "").casefold() == "home",
                    "router_state_since": str(item.get("last_changed") or "")[:80] or None,
                    "device_type": device_type,
                    "device_type_confidence": confidence,
                    "identification_basis": "router name and advertised metadata",
                }
                continue
            if not entity_id.startswith("sensor."):
                continue
            slug = entity_id.split(".", 1)[1]
            matched_device_metric = False
            for suffix, output_key in _DEVICE_SENSOR_SUFFIXES.items():
                marker = f"_{suffix}"
                if slug.endswith(marker) and len(slug) > len(marker):
                    device_metrics.append((
                        slug[:-len(marker)], output_key,
                        item.get("state"), attributes.get("unit_of_measurement"),
                    ))
                    matched_device_metric = True
                    break
            if matched_device_metric:
                continue
            for suffix in _ROUTER_METRIC_SUFFIXES:
                if slug == suffix or slug.endswith(f"_{suffix}"):
                    router_metrics[entity_id] = {
                        "metric": suffix,
                        **self._metric_value(
                            item.get("state"), attributes.get("unit_of_measurement")
                        ),
                    }
                    break

        for device_slug, key, value, unit in device_metrics:
            device = devices_by_slug.get(device_slug)
            if device is None:
                continue
            if key in {"link_rate_mbps", "signal_percent"}:
                device[key] = self._metric_value(value, unit)["value"]
            else:
                device[key] = str(value or "")[:200] or None

        devices = sorted(
            devices_by_slug.values(),
            key=lambda item: (
                not bool(item["connected"]),
                str(item["friendly_name"]).casefold(),
            ),
        )
        return {
            "provider": "home_assistant_netgear",
            "base_url": self.base_url,
            "devices": devices,
            "connected_devices": sum(1 for item in devices if item["connected"]),
            "known_devices": len(devices),
            "router_metrics": router_metrics,
            "bandwidth_scope": (
                "Router-wide totals and speed-test capacity only. Per-device link_rate_mbps "
                "is negotiated link capacity, not actual data usage."
            ),
            "connection_time_basis": (
                "Home Assistant router-state transition time, not guaranteed association uptime."
            ),
            "credentials_exposed": False,
            "limitations": [
                "The configured NETGEAR router does not expose authoritative per-device byte totals through this integration.",
                "Device type is a confidence-scored inference and may require an operator alias.",
                "NETGEAR diagnostic sensors must be enabled in Home Assistant before they appear.",
            ],
        }

    def resolve_entity(self, device: str) -> HomeDeviceState:
        target = str(device or "").strip().casefold()
        if target in self.allowed_entities:
            return self._state(target)
        states = [self._state(entity) for entity in self.allowed_entities]
        matches = [
            state for state in states if state.friendly_name.strip().casefold() == target
        ]
        if len(matches) != 1:
            raise ValueError(
                "Device must match one exact allowlisted entity ID or unique friendly name"
            )
        return matches[0]

    @staticmethod
    def normalize_app(app: str | None) -> str:
        value = str(app or "").strip().casefold()
        value = _APP_ALIASES.get(value, value)
        if _PACKAGE.fullmatch(value) is None:
            raise ValueError("App must be a known alias or exact Android package name")
        return value

    def approval_snapshot(
        self,
        device: str,
        action: str,
        app: str | None = None,
    ) -> dict[str, Any]:
        normalized_action = str(action).strip().casefold()
        if normalized_action not in {*_REMOTE_COMMANDS, "open_app"}:
            raise ValueError("Unsupported home-device action")
        state = self.resolve_entity(device)
        package = self.normalize_app(app) if normalized_action == "open_app" else None
        return {
            "resolved_entity": state.entity_id,
            "resolved_friendly_name": state.friendly_name,
            "resolved_action": normalized_action,
            "resolved_app": package,
            "provider_origin": self.base_url,
        }

    def control(
        self,
        *,
        entity_id: str,
        action: str,
        app: str | None = None,
    ) -> dict[str, Any]:
        entity = str(entity_id).strip().casefold()
        if entity not in self.allowed_entities:
            raise PermissionError("Home Assistant entity is not in the operator allowlist")
        normalized_action = str(action).strip().casefold()
        expected_activity = None
        if normalized_action == "open_app":
            expected_activity = self.normalize_app(app)
            service = "/api/services/remote/turn_on"
            payload = {"entity_id": entity, "activity": expected_activity}
        else:
            command = _REMOTE_COMMANDS.get(normalized_action)
            if command is None:
                raise ValueError("Unsupported home-device action")
            service = "/api/services/remote/send_command"
            payload = {"entity_id": entity, "command": command}
        response = self._request("POST", service, payload)

        command_accepted = isinstance(response, (list, dict)) or response is None
        readback = self._state(entity)
        if expected_activity is not None:
            for _attempt in range(3):
                if (readback.current_activity or "").casefold() == expected_activity:
                    break
                time.sleep(0.25)
                readback = self._state(entity)
            effect_verified: bool | None = (
                command_accepted
                and (readback.current_activity or "").casefold() == expected_activity
            )
            verification_basis = (
                "current_activity_exact_match"
                if effect_verified
                else "current_activity_did_not_match"
            )
        else:
            # A generic entity read proves only that Home Assistant remained
            # reachable. It cannot observe a remote-navigation button press on
            # the television, so the physical effect must remain unknown.
            effect_verified = None
            verification_basis = "remote_effect_not_observable_from_entity_state"
        return {
            "provider": "home_assistant",
            "entity_id": entity,
            "friendly_name": readback.friendly_name,
            "action": normalized_action,
            "app": expected_activity,
            "command_accepted": command_accepted,
            "readback_completed": True,
            "effect_verified": effect_verified,
            "effect_verification_basis": verification_basis,
            "state": readback.state,
            "current_activity": readback.current_activity,
            "credentials_exposed": False,
        }
