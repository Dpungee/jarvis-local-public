from __future__ import annotations

import argparse
import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .public_presence_store import PublicPresenceStore


_ALLOWED_MODES = frozenset({"offline", "observe", "suggest"})
_SAFE_ADVERTISED_TOOLS = frozenset({
    "public_presence_status",
    "public_presence_health",
    "moltbook_status",
    "moltbook_read_feed",
    "moltbook_read_thread",
    "moltbook_search",
    "moltbook_get_profile",
    "moltbook_draft_post",
    "moltbook_draft_reply",
})
_SOURCE_ROOT = Path(__file__).resolve().parents[1]
_MAX_DATA_PATH_CHARS = 4_096


class PublicPresenceUnavailable(RuntimeError):
    """Raised when the isolated public process is not explicitly permitted to run."""


@dataclass(frozen=True)
class PublicControlState:
    social_paused: bool = True
    emergency_stopped: bool = False
    mode: str = "offline"
    revision: int = 0

    def __post_init__(self) -> None:
        if type(self.social_paused) is not bool or type(self.emergency_stopped) is not bool:
            raise ValueError("public control flags must be booleans")
        if self.mode not in _ALLOWED_MODES:
            raise ValueError("public presence control mode is invalid")
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 0:
            raise ValueError("public control revision must be a non-negative integer")

    @property
    def allows_start(self) -> bool:
        return (
            not self.social_paused
            and not self.emergency_stopped
            and self.mode in {"observe", "suggest"}
        )


def fail_closed_control_state() -> PublicControlState:
    return PublicControlState()


def control_state_from_mapping(
    value: Mapping[str, Any],
    *,
    active_mode: str = "observe",
) -> PublicControlState:
    """Narrow adapter for the independent public control store's safe fields."""

    if not isinstance(value, Mapping):
        raise ValueError("public control record must be an object")
    enabled = value.get("enabled")
    paused = value.get("paused")
    stopped = value.get("emergency_stopped")
    if type(enabled) is not bool or type(paused) is not bool or type(stopped) is not bool:
        raise ValueError("public control record must contain boolean control fields")
    if active_mode not in {"observe", "suggest"}:
        raise ValueError("active public mode is invalid")
    revision = value.get("revision", 0)
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        revision = 0
    return PublicControlState(
        social_paused=paused or not enabled,
        emergency_stopped=stopped,
        mode=active_mode if enabled else "offline",
        revision=revision,
    )


class PublicPresenceService:
    """Disconnected process lifecycle for the Public Presence security domain.

    This foundation exposes status and health only. It opens no listener, loads no
    credentials, and owns no private-JARVIS objects.
    """

    def __init__(
        self,
        *,
        enabled: bool = False,
        control_reader: Callable[[], PublicControlState] = fail_closed_control_state,
        advertised_tools: Sequence[str] = (),
        clock: Callable[[], float] = time.time,
    ) -> None:
        if type(enabled) is not bool:
            raise ValueError("public presence enabled flag must be a boolean")
        supplied_tools = set(advertised_tools)
        if supplied_tools - _SAFE_ADVERTISED_TOOLS:
            raise ValueError("public presence cannot advertise non-public tools")
        self._enabled = enabled
        self._control_reader = control_reader
        self._advertised_tools = tuple(sorted(supplied_tools))
        self._clock = clock
        self._running = False
        self._started_at: float | None = None
        self._lock = threading.RLock()

    def _controls(self) -> PublicControlState:
        try:
            controls = self._control_reader()
        except Exception as exc:
            raise PublicPresenceUnavailable("public control state is unavailable") from exc
        if not isinstance(controls, PublicControlState):
            raise PublicPresenceUnavailable("public control state is invalid")
        return controls

    def _refresh(self) -> PublicControlState:
        controls = self._controls()
        if self._running and (not self._enabled or not controls.allows_start):
            self._running = False
            self._started_at = None
        return controls

    def start(self) -> dict[str, object]:
        with self._lock:
            if self._enabled is not True:
                raise PublicPresenceUnavailable("public presence is disabled")
            controls = self._controls()
            if controls.emergency_stopped:
                raise PublicPresenceUnavailable("public presence emergency stop is active")
            if controls.social_paused:
                raise PublicPresenceUnavailable("public presence is socially paused")
            if not controls.allows_start:
                raise PublicPresenceUnavailable("public presence mode does not allow startup")
            self._running = True
            self._started_at = self._clock()
            return self._status_for(controls)

    def stop(self) -> dict[str, object]:
        with self._lock:
            self._running = False
            self._started_at = None
            return self._status_for(self._controls())

    def require_operational(self, *, required_mode: str = "observe") -> None:
        if required_mode not in {"observe", "suggest"}:
            raise ValueError("required public mode is invalid")
        with self._lock:
            controls = self._refresh()
            if not self._running or not controls.allows_start:
                raise PublicPresenceUnavailable("public presence is not operational")
            if required_mode == "suggest" and controls.mode != "suggest":
                raise PublicPresenceUnavailable("public drafting requires suggest mode")

    def _status_for(self, controls: PublicControlState) -> dict[str, object]:
        if not self._enabled:
            state = "disabled"
        elif controls.emergency_stopped:
            state = "emergency_stopped"
        elif controls.social_paused:
            state = "paused"
        elif self._running:
            state = "running"
        else:
            state = "ready"
        return {
            "enabled": self._enabled,
            "running": self._running,
            "state": state,
            "mode": controls.mode,
            "social_paused": controls.social_paused,
            "emergency_stopped": controls.emergency_stopped,
            "control_revision": controls.revision,
            "external_communication": False,
            "connected_platforms": [],
            "advertised_tools": list(self._advertised_tools),
            "started_at": self._started_at,
        }

    def status(self) -> dict[str, object]:
        with self._lock:
            return self._status_for(self._refresh())

    def health(self) -> dict[str, object]:
        status = self.status()
        return {
            "healthy": True,
            "ready": status["state"] in {"ready", "running"},
            "state": status["state"],
            "mode": status["mode"],
            "control_revision": status["control_revision"],
        }


def environment_enabled(value: str | None = None) -> bool:
    raw = os.environ.get("JARVIS_PUBLIC_PRESENCE_ENABLED", "false") if value is None else value
    return raw.strip().casefold() in {"1", "true", "yes", "on"}


def public_presence_database_path(value: str | os.PathLike[str] | None = None) -> Path:
    """Resolve only the dedicated public database under the bounded data root.

    This intentionally does not import the private Config object or read a dotenv
    file, because either would make private configuration available to the public
    process. A launcher may pass the conventional JARVIS_DATA value through the
    environment; otherwise the source-tree data directory is used.
    """

    if value is None:
        raw = os.environ.get("JARVIS_DATA")
        candidate = _SOURCE_ROOT / "data" if raw is None else Path(raw)
    else:
        raw = os.fspath(value)
        candidate = Path(raw)
    if raw is not None and (
        not raw.strip() or "\x00" in raw or len(raw) > _MAX_DATA_PATH_CHARS
    ):
        raise ValueError("JARVIS_DATA must be a bounded non-empty path")
    rendered = os.fspath(candidate)
    if "\x00" in rendered or len(rendered) > _MAX_DATA_PATH_CHARS:
        raise ValueError("JARVIS_DATA must be a bounded non-empty path")
    resolved = candidate.resolve(strict=False)
    anchor = Path(resolved.anchor).resolve(strict=False)
    if resolved == anchor:
        raise ValueError("JARVIS_DATA must not be a filesystem root")
    return resolved / "public_presence.db"


def durable_control_reader(
    database_path: Path,
    *,
    active_mode: str = "observe",
) -> Callable[[], PublicControlState]:
    """Return a reader that opens only the isolated public control database."""

    path = Path(database_path)
    if path.name.casefold() != "public_presence.db":
        raise ValueError("public control reader requires public_presence.db")
    store = PublicPresenceStore(path)

    def read() -> PublicControlState:
        return control_state_from_mapping(store.status(), active_mode=active_mode)

    return read


def _unavailable_payload(command: str, *, configured_enabled: bool) -> dict[str, object]:
    status: dict[str, object] = {
        "enabled": configured_enabled,
        "running": False,
        "state": "unavailable",
        "mode": "offline",
        "social_paused": True,
        "emergency_stopped": True,
        "control_revision": 0,
        "external_communication": False,
        "connected_platforms": [],
        "advertised_tools": [],
        "started_at": None,
    }
    if command == "health":
        return {
            "healthy": False,
            "ready": False,
            "state": "unavailable",
            "mode": "offline",
            "control_revision": 0,
        }
    return status


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Disconnected JARVIS Public Presence status")
    parser.add_argument("command", choices=("status", "health"), nargs="?", default="status")
    args = parser.parse_args(argv)
    # This command is deliberately one-shot. It reads only the separate public
    # control database and cannot start a listener, contact a platform, or publish.
    # The environment flag is necessary but never sufficient: the durable control
    # row must also be enabled, unpaused, and not emergency-stopped.
    configured_enabled = environment_enabled()
    try:
        reader = durable_control_reader(public_presence_database_path())
        service = PublicPresenceService(
            enabled=configured_enabled,
            control_reader=reader,
        )
        payload = service.health() if args.command == "health" else service.status()
    except Exception:
        # Status must remain useful without exposing a local path, schema detail,
        # or exception string. Any storage/configuration fault is an unavailable,
        # paused, stopped public boundary—not permission to fall back elsewhere.
        payload = _unavailable_payload(
            args.command,
            configured_enabled=configured_enabled,
        )
        print(json.dumps(payload, sort_keys=True))
        return 1
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
