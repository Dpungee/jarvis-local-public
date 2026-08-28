from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from .config import MAX_DOTENV_BYTES, ROOT, Config


SCHEMA_VERSION = 2
DECISIONS = frozenset({"setup", "skip", "disable"})
_KEY_PATTERN = re.compile(r"^\s*([A-Z][A-Z0-9_]*)\s*=")
_WINDOWS_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_INITIALIZE_LOCK = threading.RLock()
_CONFIGURATION_THREAD_LOCK = threading.RLock()
_CONFIGURATION_LOCK_TIMEOUT_SECONDS = 15.0
_JOURNAL_RECOVERABLE_STATES = frozenset({"prepared", "applied", "conflict"})
_JOURNAL_TERMINAL_STATES = frozenset({"finalized", "compensated", "aborted"})


class FeatureOnboardingError(RuntimeError):
    """A bounded optional-feature configuration could not be completed safely."""


class FeatureOnboardingConflict(FeatureOnboardingError):
    """The reviewed feature configuration changed before the decision was applied."""


@dataclass(frozen=True)
class FeatureSpec:
    capability_id: str
    title: str
    description: str
    safety_boundary: str
    setup_values: tuple[tuple[str, str], ...]
    disable_values: tuple[tuple[str, str], ...]
    depends_on: tuple[str, ...] = ()


FEATURE_SPECS: tuple[FeatureSpec, ...] = (
    FeatureSpec(
        "private-lan-inventory",
        "Home-network inventory",
        "Remember devices observed on one private network that you explicitly pair.",
        "Never scans public addresses, credentials, packets, routed networks, or device files.",
        (("JARVIS_NETWORK_ACCESS", "private-lan"),),
        (("JARVIS_NETWORK_ACCESS", "disabled"),),
    ),
    FeatureSpec(
        "private-lan-monitoring",
        "Automatic home-network checks",
        "Recheck the paired private network on a bounded schedule and report changes.",
        "Read-only observation only; pause and stop suppress every background check.",
        (
            ("JARVIS_NETWORK_ACCESS", "private-lan"),
            ("JARVIS_NETWORK_MONITOR_ENABLED", "1"),
        ),
        (("JARVIS_NETWORK_MONITOR_ENABLED", "0"),),
        ("private-lan-inventory",),
    ),
    FeatureSpec(
        "network-defense-alerts",
        "Network security assessments",
        "Create durable, evidence-scored alerts for new or materially changed devices.",
        "An alert is an assessment, never proof of compromise or authority to contain a device.",
        (
            ("JARVIS_NETWORK_ACCESS", "private-lan"),
            ("JARVIS_NETWORK_DEFENSE_MODE", "alert-only"),
        ),
        (("JARVIS_NETWORK_DEFENSE_MODE", "disabled"),),
        ("private-lan-inventory",),
    ),
    FeatureSpec(
        "network-defense-safe-readonly",
        "Automatic read-only diagnostics",
        "Let Jarvis select reviewed, installed passive diagnostics when an alert needs more evidence.",
        "No active probing, downloads, quarantine, blocking, firewall changes, or containment are automatic.",
        (
            ("JARVIS_NETWORK_ACCESS", "private-lan"),
            ("JARVIS_NETWORK_DEFENSE_MODE", "safe-readonly"),
        ),
        (("JARVIS_NETWORK_DEFENSE_MODE", "alert-only"),),
        ("private-lan-inventory", "network-defense-alerts"),
    ),
    FeatureSpec(
        "bluetooth-inventory",
        "Paired Bluetooth inventory",
        "Remember devices Windows already reports as paired over Bluetooth.",
        "Does not discover nearby unpaired radios, pair devices, connect, or control them.",
        (("JARVIS_BLUETOOTH_ACCESS", "paired-readonly"),),
        (("JARVIS_BLUETOOTH_ACCESS", "disabled"),),
    ),
    FeatureSpec(
        "bluetooth-monitoring",
        "Automatic paired-Bluetooth checks",
        "Recheck Windows' paired-device inventory and report first observations or changes.",
        "Read-only local enumeration; pause and stop suppress every background check.",
        (
            ("JARVIS_BLUETOOTH_ACCESS", "paired-readonly"),
            ("JARVIS_BLUETOOTH_MONITOR_ENABLED", "1"),
        ),
        (("JARVIS_BLUETOOTH_MONITOR_ENABLED", "0"),),
        ("bluetooth-inventory",),
    ),
    FeatureSpec(
        "network-security-alerts-ui",
        "On-screen security explanations",
        "Show durable popups explaining the signal, evidence, confidence, actions, and limitations.",
        "Popup receipts acknowledge information only and can never authorize a probe or containment action.",
        (("JARVIS_NETWORK_INCIDENT_POPUPS_ENABLED", "1"),),
        (("JARVIS_NETWORK_INCIDENT_POPUPS_ENABLED", "0"),),
    ),
)

_FEATURE_BY_ID = {spec.capability_id: spec for spec in FEATURE_SPECS}
MANAGED_ENV_KEYS = frozenset(
    key
    for spec in FEATURE_SPECS
    for key, _value in (*spec.setup_values, *spec.disable_values)
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ordinary_env(path: Path) -> bytes:
    if not os.path.lexists(path):
        return b""
    try:
        details = os.lstat(path)
    except OSError as exc:
        raise FeatureOnboardingError("Jarvis could not inspect its feature configuration.") from exc
    attributes = getattr(details, "st_file_attributes", 0)
    if (
        stat.S_ISLNK(details.st_mode)
        or attributes & _WINDOWS_REPARSE_POINT
        or not stat.S_ISREG(details.st_mode)
    ):
        raise FeatureOnboardingError("Jarvis .env must be an ordinary non-link file.")
    if details.st_size > MAX_DOTENV_BYTES:
        raise FeatureOnboardingError(f"Jarvis .env exceeds {MAX_DOTENV_BYTES} bytes.")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise FeatureOnboardingError("Jarvis could not read its feature configuration.") from exc


def _configuration_sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _render_env(existing: bytes, updates: Mapping[str, str]) -> bytes:
    try:
        text = existing.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FeatureOnboardingError("Jarvis .env must be UTF-8 text.") from exc
    newline = "\r\n" if "\r\n" in text else "\n"
    had_final_newline = text.endswith(("\n", "\r"))
    lines = text.splitlines()
    rendered: list[str] = []
    written: set[str] = set()
    for line in lines:
        match = _KEY_PATTERN.match(line)
        key = match.group(1) if match else None
        if key not in updates:
            rendered.append(line)
            continue
        if key not in written:
            rendered.append(f"{key}={updates[key]}")
            written.add(key)
    missing = [key for key in sorted(updates) if key not in written]
    if missing:
        if rendered and rendered[-1].strip():
            rendered.append("")
        rendered.append("# Optional features selected through Jarvis setup.")
        rendered.extend(f"{key}={updates[key]}" for key in missing)
    output = newline.join(rendered)
    if output and (had_final_newline or missing):
        output += newline
    encoded = output.encode("utf-8")
    if len(encoded) > MAX_DOTENV_BYTES:
        raise FeatureOnboardingError(
            f"Updated Jarvis .env would exceed {MAX_DOTENV_BYTES} bytes."
        )
    return encoded


def _managed_values(raw: bytes) -> dict[str, str]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FeatureOnboardingError("Jarvis .env must be UTF-8 text.") from exc
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if key in MANAGED_ENV_KEYS and key not in values:
            values[key] = value.strip().strip("'\"")
    return values


def _lock_file(handle: Any, *, exclusive: bool) -> None:
    """Acquire or release the first byte of a process-shared lock file."""
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        mode = msvcrt.LK_NBLCK if exclusive else msvcrt.LK_UNLCK
        msvcrt.locking(handle.fileno(), mode, 1)
        return
    import fcntl

    mode = fcntl.LOCK_EX | fcntl.LOCK_NB if exclusive else fcntl.LOCK_UN
    fcntl.flock(handle.fileno(), mode)


@contextmanager
def _cross_process_lock(path: Path, *, timeout: float) -> Any:
    """Serialize feature configuration across Jarvis processes.

    SQLite serializes database writes, but it cannot serialize the adjacent
    replacement of ``.env``.  This small advisory lock covers both resources.
    The in-process lock is also required because POSIX flock locks are scoped
    to a process rather than a Python thread.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with _CONFIGURATION_THREAD_LOCK:
        handle = path.open("a+b")
        acquired = False
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
                os.fsync(handle.fileno())
            deadline = time.monotonic() + max(0.1, float(timeout))
            while True:
                try:
                    _lock_file(handle, exclusive=True)
                    acquired = True
                    break
                except (BlockingIOError, OSError):
                    if time.monotonic() >= deadline:
                        raise FeatureOnboardingError(
                            "Another Jarvis process is changing optional-feature settings."
                        )
                    time.sleep(0.025)
            yield
        finally:
            if acquired:
                try:
                    _lock_file(handle, exclusive=False)
                except OSError:
                    pass
            handle.close()


def _fsync_directory(path: Path) -> None:
    """Best-effort directory durability after replacing or removing .env."""
    if os.name == "nt":
        return
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY)
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        if descriptor is not None:
            os.close(descriptor)


class FeatureOnboardingStore:
    """Durable, reversible choices for optional features with a strict env allowlist."""

    def __init__(self, root: Path | str = ROOT, data_dir: Path | str | None = None) -> None:
        self.root = Path(root).resolve()
        self.data_dir = Path(data_dir if data_dir is not None else self.root / "data").resolve()
        self.env_path = self.root / ".env"
        self.db_path = self.data_dir / "feature_onboarding.db"
        self.lock_path = self.data_dir / ".feature_onboarding.configuration.lock"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with self._configuration_lock():
            self._initialize()
            self._recover_pending_changes_locked()

    @contextmanager
    def _configuration_lock(self) -> Any:
        with _cross_process_lock(
            self.lock_path, timeout=_CONFIGURATION_LOCK_TIMEOUT_SECONDS
        ):
            yield

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def _initialize(self) -> None:
        with _INITIALIZE_LOCK:
            connection = self._connect()
            try:
                current = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if current > SCHEMA_VERSION:
                    raise FeatureOnboardingError(
                        "Feature-onboarding data is newer than this Jarvis runtime."
                    )
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS feature_decisions (
                        capability_id TEXT PRIMARY KEY,
                        decision TEXT NOT NULL CHECK(decision IN ('setup','skip','disable')),
                        updated_at TEXT NOT NULL,
                        configuration_sha256 TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS feature_decision_receipts (
                        receipt_id TEXT PRIMARY KEY,
                        capability_id TEXT NOT NULL,
                        decision TEXT NOT NULL,
                        configuration_sha256 TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS feature_change_journal (
                        operation_id TEXT PRIMARY KEY,
                        receipt_id TEXT NOT NULL,
                        capability_id TEXT NOT NULL,
                        decision TEXT NOT NULL CHECK(decision IN ('setup','skip','disable')),
                        affected_ids_json TEXT NOT NULL,
                        before_configuration_sha256 TEXT NOT NULL,
                        after_configuration_sha256 TEXT NOT NULL,
                        before_existed INTEGER NOT NULL CHECK(before_existed IN (0,1)),
                        changed INTEGER NOT NULL CHECK(changed IN (0,1)),
                        status TEXT NOT NULL CHECK(status IN (
                            'prepared','applied','finalized','compensated','aborted','conflict'
                        )),
                        error_code TEXT,
                        observed_configuration_sha256 TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_feature_change_journal_status
                    ON feature_change_journal(status, created_at);
                    """
                )
                if current < SCHEMA_VERSION:
                    connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                connection.commit()
            finally:
                connection.close()

    @staticmethod
    def _spec(capability_id: str) -> FeatureSpec:
        normalized = str(capability_id).strip().casefold()
        try:
            return _FEATURE_BY_ID[normalized]
        except KeyError as exc:
            raise ValueError("Unknown optional capability ID") from exc

    @staticmethod
    def _dependency_ids(capability_id: str) -> tuple[str, ...]:
        ordered: list[str] = []

        def visit(current: str) -> None:
            for dependency in _FEATURE_BY_ID[current].depends_on:
                visit(dependency)
                if dependency not in ordered:
                    ordered.append(dependency)

        visit(capability_id)
        return tuple(ordered)

    @staticmethod
    def _dependent_ids(capability_id: str) -> tuple[str, ...]:
        selected: set[str] = set()
        changed = True
        while changed:
            changed = False
            for candidate in FEATURE_SPECS:
                if candidate.capability_id in selected:
                    continue
                if capability_id in candidate.depends_on or any(
                    dependency in selected for dependency in candidate.depends_on
                ):
                    selected.add(candidate.capability_id)
                    changed = True
        return tuple(
            spec.capability_id
            for spec in FEATURE_SPECS
            if spec.capability_id in selected
        )

    def setup_plan(self, capability_id: str) -> dict[str, Any]:
        spec = self._spec(capability_id)
        return {
            "capability_id": spec.capability_id,
            "title": spec.title,
            "description": spec.description,
            "safety_boundary": spec.safety_boundary,
            "depends_on": list(spec.depends_on),
            "prepares_dependencies": list(self._dependency_ids(spec.capability_id)),
            "disables_dependents": list(self._dependent_ids(spec.capability_id)),
            "managed_settings": dict(spec.setup_values),
            "commands": [],
            "downloads": [],
            "network_calls": [],
            "active_probes": [],
            "containment_actions": [],
            "requires_restart": bool(spec.setup_values),
        }

    def _decisions(self) -> dict[str, dict[str, str]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT capability_id, decision, updated_at, configuration_sha256 "
                "FROM feature_decisions"
            ).fetchall()
        finally:
            connection.close()
        return {str(row["capability_id"]): dict(row) for row in rows}

    def list_status(self) -> dict[str, Any]:
        with self._configuration_lock():
            self._recover_pending_changes_locked()
            raw = _ordinary_env(self.env_path)
            values = _managed_values(raw)
            decisions = self._decisions()
        features: list[dict[str, Any]] = []
        pending = 0
        for spec in FEATURE_SPECS:
            stored = decisions.get(spec.capability_id)
            configured = self._configured(spec.capability_id, values)
            decision = str(stored["decision"]) if stored is not None else (
                "setup" if configured else "pending"
            )
            if decision == "pending":
                pending += 1
            features.append({
                "capability_id": spec.capability_id,
                "title": spec.title,
                "description": spec.description,
                "safety_boundary": spec.safety_boundary,
                "depends_on": list(spec.depends_on),
                "disables_dependents": list(self._dependent_ids(spec.capability_id)),
                "decision": decision,
                "configured": configured,
                "setup_available": True,
                "can_change_later": True,
                "requires_restart": bool(spec.setup_values),
                "updated_at": None if stored is None else stored["updated_at"],
            })
        return {
            "schema_version": SCHEMA_VERSION,
            "complete": pending == 0,
            "pending_count": pending,
            "configuration_sha256": _configuration_sha256(raw),
            "features": features,
            "downloads_performed": False,
            "active_probes_performed": False,
            "containment_authorized": False,
        }

    @staticmethod
    def _configured(capability_id: str, values: Mapping[str, str]) -> bool:
        normalized = {
            key: str(value).strip().casefold() for key, value in values.items()
        }
        if capability_id == "network-defense-alerts":
            return (
                normalized.get("JARVIS_NETWORK_ACCESS") == "private-lan"
                and normalized.get("JARVIS_NETWORK_DEFENSE_MODE")
                in {"alert-only", "safe-readonly"}
            )
        spec = _FEATURE_BY_ID[capability_id]
        return bool(spec.setup_values) and all(
            normalized.get(key) == value.casefold()
            for key, value in spec.setup_values
        )

    def decide(
        self,
        capability_id: str,
        decision: str,
        *,
        expected_configuration_sha256: str | None = None,
    ) -> dict[str, Any]:
        spec = self._spec(capability_id)
        normalized_decision = str(decision).strip().casefold()
        if normalized_decision not in DECISIONS:
            raise ValueError("decision must be setup, skip, or disable")
        if expected_configuration_sha256 is not None and re.fullmatch(
            r"[0-9a-f]{64}", str(expected_configuration_sha256)
        ) is None:
            raise ValueError("expected_configuration_sha256 must be lowercase SHA-256")

        with self._configuration_lock():
            self._recover_pending_changes_locked()
            before_existed = os.path.lexists(self.env_path)
            raw = _ordinary_env(self.env_path)
            before_sha = _configuration_sha256(raw)
            if (
                expected_configuration_sha256 is not None
                and expected_configuration_sha256 != before_sha
            ):
                raise FeatureOnboardingConflict(
                    "Jarvis feature configuration changed; refresh before deciding."
                )

            affected_ids, updates = self._change_effects(
                spec.capability_id, normalized_decision
            )
            rendered = _render_env(raw, updates) if updates else raw
            after_sha = _configuration_sha256(rendered)
            changed = rendered != raw
            now = _utc_now()
            operation_id = uuid.uuid4().hex
            receipt_id = hashlib.sha256(
                (
                    "jarvis-feature-decision-v2\0"
                    + operation_id
                    + "\0"
                    + spec.capability_id
                    + "\0"
                    + normalized_decision
                    + "\0"
                    + after_sha
                ).encode("utf-8")
            ).hexdigest()[:32]
            self._prepare_change(
                operation_id=operation_id,
                receipt_id=receipt_id,
                capability_id=spec.capability_id,
                decision=normalized_decision,
                affected_ids=affected_ids,
                before_sha=before_sha,
                after_sha=after_sha,
                before_existed=before_existed,
                changed=changed,
                now=now,
            )

            applied_seen = False
            try:
                if changed:
                    self._atomic_write_env(
                        rendered,
                        expected_current_sha=before_sha,
                        expected_current_existed=before_existed,
                    )
                    applied_seen = True
                current_existed = os.path.lexists(self.env_path)
                current_sha = _configuration_sha256(_ordinary_env(self.env_path))
                if current_sha != after_sha or (changed and not current_existed):
                    raise FeatureOnboardingConflict(
                        "Jarvis feature configuration changed during final verification."
                    )
                self._mark_journal(
                    operation_id,
                    status="applied",
                    observed_sha=current_sha,
                    error_code=None,
                )
                self._finalize_change(operation_id)
            except BaseException as exc:
                # A commit can fail ambiguously. If the journal is already
                # finalized, the authority change has its durable receipt and
                # must not be compensated behind that receipt.
                if self._journal_is_finalized(operation_id):
                    return self._decision_result(
                        spec.capability_id,
                        normalized_decision,
                        affected_ids,
                        changed,
                        after_sha,
                        receipt_id,
                    )
                current_existed = os.path.lexists(self.env_path)
                try:
                    current_sha = _configuration_sha256(_ordinary_env(self.env_path))
                except FeatureOnboardingError:
                    current_sha = ""
                if changed and current_sha == after_sha and current_existed:
                    applied_seen = True
                    try:
                        self._restore_env(
                            raw,
                            before_existed=before_existed,
                            expected_current_sha=after_sha,
                            expected_current_existed=True,
                        )
                    except BaseException:
                        pass
                restored_existed = os.path.lexists(self.env_path)
                try:
                    restored_sha = _configuration_sha256(_ordinary_env(self.env_path))
                except FeatureOnboardingError:
                    restored_sha = ""
                restored = restored_sha == before_sha and (
                    restored_existed == before_existed
                    or (not before_existed and restored_sha == _configuration_sha256(b""))
                )
                if restored:
                    try:
                        self._mark_journal(
                            operation_id,
                            status="compensated" if applied_seen else "aborted",
                            observed_sha=restored_sha,
                            error_code=(
                                "database_finalization_failed"
                                if applied_seen
                                else "configuration_apply_failed"
                            ),
                        )
                    except BaseException:
                        # The prepared journal is already durable. A later
                        # startup will observe the before-hash and close it.
                        pass
                else:
                    try:
                        self._mark_journal(
                            operation_id,
                            status="conflict",
                            observed_sha=restored_sha or None,
                            error_code="configuration_changed_during_decision",
                        )
                    except BaseException:
                        pass
                if isinstance(exc, FeatureOnboardingConflict):
                    raise
                if restored:
                    raise FeatureOnboardingError(
                        "Jarvis could not finalize the feature decision; the configuration was restored."
                    ) from exc
                raise FeatureOnboardingError(
                    "Jarvis could not finalize the feature decision; restart recovery is required."
                ) from exc

            return self._decision_result(
                spec.capability_id,
                normalized_decision,
                affected_ids,
                changed,
                after_sha,
                receipt_id,
            )

    @staticmethod
    def _change_effects(
        capability_id: str, decision: str
    ) -> tuple[tuple[str, ...], dict[str, str]]:
        if decision == "setup":
            affected_ids = (
                *FeatureOnboardingStore._dependency_ids(capability_id),
                capability_id,
            )
            updates: dict[str, str] = {}
            for affected_id in affected_ids:
                updates.update(dict(_FEATURE_BY_ID[affected_id].setup_values))
            return affected_ids, updates
        if decision == "disable":
            affected_ids = (
                capability_id,
                *FeatureOnboardingStore._dependent_ids(capability_id),
            )
            updates = {}
            for affected_id in reversed(affected_ids):
                updates.update(dict(_FEATURE_BY_ID[affected_id].disable_values))
            return affected_ids, updates
        return (capability_id,), {}

    @staticmethod
    def _decision_result(
        capability_id: str,
        decision: str,
        affected_ids: tuple[str, ...],
        changed: bool,
        after_sha: str,
        receipt_id: str,
    ) -> dict[str, Any]:
        return {
            "capability_id": capability_id,
            "decision": decision,
            "also_changed": [item for item in affected_ids if item != capability_id],
            "changed": changed,
            "restart_required": changed,
            "configuration_sha256": after_sha,
            "receipt_id": receipt_id,
            "downloads_performed": False,
            "active_probes_performed": False,
            "containment_authorized": False,
        }

    def _prepare_change(
        self,
        *,
        operation_id: str,
        receipt_id: str,
        capability_id: str,
        decision: str,
        affected_ids: tuple[str, ...],
        before_sha: str,
        after_sha: str,
        before_existed: bool,
        changed: bool,
        now: str,
    ) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO feature_change_journal("
                "operation_id, receipt_id, capability_id, decision, affected_ids_json, "
                "before_configuration_sha256, after_configuration_sha256, before_existed, "
                "changed, status, error_code, observed_configuration_sha256, created_at, updated_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,'prepared',NULL,NULL,?,?)",
                (
                    operation_id,
                    receipt_id,
                    capability_id,
                    decision,
                    json.dumps(list(affected_ids), separators=(",", ":")),
                    before_sha,
                    after_sha,
                    int(before_existed),
                    int(changed),
                    now,
                    now,
                ),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _validated_journal(row: sqlite3.Row) -> dict[str, Any]:
        capability_id = str(row["capability_id"])
        decision = str(row["decision"])
        if capability_id not in _FEATURE_BY_ID or decision not in DECISIONS:
            raise FeatureOnboardingError("Feature change journal contains an invalid decision.")
        try:
            affected_ids = tuple(json.loads(str(row["affected_ids_json"])))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise FeatureOnboardingError("Feature change journal is not valid JSON.") from exc
        expected_ids, _updates = FeatureOnboardingStore._change_effects(
            capability_id, decision
        )
        if affected_ids != expected_ids:
            raise FeatureOnboardingError("Feature change journal has an invalid dependency scope.")
        before_sha = str(row["before_configuration_sha256"])
        after_sha = str(row["after_configuration_sha256"])
        if re.fullmatch(r"[0-9a-f]{64}", before_sha) is None or re.fullmatch(
            r"[0-9a-f]{64}", after_sha
        ) is None:
            raise FeatureOnboardingError("Feature change journal has an invalid digest.")
        status = str(row["status"])
        if status not in _JOURNAL_RECOVERABLE_STATES | _JOURNAL_TERMINAL_STATES:
            raise FeatureOnboardingError("Feature change journal has an invalid state.")
        return {
            "operation_id": str(row["operation_id"]),
            "receipt_id": str(row["receipt_id"]),
            "capability_id": capability_id,
            "decision": decision,
            "affected_ids": affected_ids,
            "before_sha": before_sha,
            "after_sha": after_sha,
            "before_existed": bool(row["before_existed"]),
            "changed": bool(row["changed"]),
            "status": status,
            "created_at": str(row["created_at"]),
        }

    def _journal_row(self, operation_id: str) -> sqlite3.Row | None:
        connection = self._connect()
        try:
            return connection.execute(
                "SELECT * FROM feature_change_journal WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
        finally:
            connection.close()

    def _mark_journal(
        self,
        operation_id: str,
        *,
        status: str,
        observed_sha: str | None,
        error_code: str | None,
    ) -> None:
        if status not in _JOURNAL_RECOVERABLE_STATES | _JOURNAL_TERMINAL_STATES:
            raise ValueError("invalid journal state")
        if observed_sha is not None and re.fullmatch(r"[0-9a-f]{64}", observed_sha) is None:
            raise ValueError("invalid observed configuration digest")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE feature_change_journal SET status=?, error_code=?, "
                "observed_configuration_sha256=?, updated_at=? WHERE operation_id=?",
                (status, error_code, observed_sha, _utc_now(), operation_id),
            )
            if cursor.rowcount != 1:
                raise FeatureOnboardingError("Feature change journal entry disappeared.")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _journal_is_finalized(self, operation_id: str) -> bool:
        try:
            row = self._journal_row(operation_id)
        except BaseException:
            return False
        return row is not None and str(row["status"]) == "finalized"

    def _finalize_change(self, operation_id: str) -> None:
        row = self._journal_row(operation_id)
        if row is None:
            raise FeatureOnboardingError("Feature change journal entry disappeared.")
        journal = self._validated_journal(row)
        if journal["status"] == "finalized":
            return
        if journal["status"] not in _JOURNAL_RECOVERABLE_STATES:
            raise FeatureOnboardingError("Feature change journal is already closed.")
        current_sha = _configuration_sha256(_ordinary_env(self.env_path))
        if current_sha != journal["after_sha"]:
            raise FeatureOnboardingConflict(
                "Jarvis feature configuration changed before receipt finalization."
            )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            latest = connection.execute(
                "SELECT * FROM feature_change_journal WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if latest is None:
                raise FeatureOnboardingError("Feature change journal entry disappeared.")
            journal = self._validated_journal(latest)
            if journal["status"] == "finalized":
                connection.rollback()
                return
            if journal["status"] not in _JOURNAL_RECOVERABLE_STATES:
                raise FeatureOnboardingError("Feature change journal is already closed.")
            now = _utc_now()
            for affected_id in journal["affected_ids"]:
                connection.execute(
                    "INSERT INTO feature_decisions(capability_id, decision, updated_at, configuration_sha256) "
                    "VALUES(?,?,?,?) ON CONFLICT(capability_id) DO UPDATE SET "
                    "decision=excluded.decision, updated_at=excluded.updated_at, "
                    "configuration_sha256=excluded.configuration_sha256",
                    (affected_id, journal["decision"], now, journal["after_sha"]),
                )
            connection.execute(
                "INSERT INTO feature_decision_receipts("
                "receipt_id, capability_id, decision, configuration_sha256, created_at"
                ") VALUES(?,?,?,?,?)",
                (
                    journal["receipt_id"],
                    journal["capability_id"],
                    journal["decision"],
                    journal["after_sha"],
                    journal["created_at"],
                ),
            )
            connection.execute(
                "UPDATE feature_change_journal SET status='finalized', error_code=NULL, "
                "observed_configuration_sha256=?, updated_at=? WHERE operation_id=?",
                (journal["after_sha"], now, operation_id),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _recover_pending_changes_locked(self) -> None:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM feature_change_journal "
                "WHERE status IN ('prepared','applied','conflict') ORDER BY created_at, operation_id"
            ).fetchall()
        finally:
            connection.close()
        for row in rows:
            journal = self._validated_journal(row)
            current_existed = os.path.lexists(self.env_path)
            current_raw = _ordinary_env(self.env_path)
            current_sha = _configuration_sha256(current_raw)
            if current_sha == journal["after_sha"] and (
                not journal["changed"] or current_existed
            ):
                self._finalize_change(journal["operation_id"])
                continue
            if current_sha == journal["before_sha"]:
                if not journal["before_existed"] and current_existed and not current_raw:
                    self._restore_env(
                        b"",
                        before_existed=False,
                        expected_current_sha=current_sha,
                        expected_current_existed=True,
                    )
                self._mark_journal(
                    journal["operation_id"],
                    status="compensated" if journal["changed"] else "aborted",
                    observed_sha=current_sha,
                    error_code="restart_recovery_before_apply",
                )
                continue
            self._mark_journal(
                journal["operation_id"],
                status="conflict",
                observed_sha=current_sha,
                error_code="restart_recovery_digest_conflict",
            )
            raise FeatureOnboardingConflict(
                "Jarvis found an unfinished feature change and a conflicting .env; review it before continuing."
            )

    def _restore_env(
        self,
        raw: bytes,
        *,
        before_existed: bool,
        expected_current_sha: str,
        expected_current_existed: bool,
    ) -> None:
        if before_existed:
            self._atomic_write_env(
                raw,
                expected_current_sha=expected_current_sha,
                expected_current_existed=expected_current_existed,
            )
            return
        current_existed = os.path.lexists(self.env_path)
        current_sha = _configuration_sha256(_ordinary_env(self.env_path))
        if current_sha != expected_current_sha or current_existed != expected_current_existed:
            raise FeatureOnboardingConflict(
                "Jarvis feature configuration changed before compensation."
            )
        if current_existed:
            self.env_path.unlink()
            _fsync_directory(self.root)

    def _atomic_write_env(
        self,
        encoded: bytes,
        *,
        expected_current_sha: str,
        expected_current_existed: bool,
    ) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        existing_mode: int | None = None
        if os.path.lexists(self.env_path):
            _ordinary_env(self.env_path)
            existing_mode = stat.S_IMODE(self.env_path.stat().st_mode)
        descriptor, name = tempfile.mkstemp(
            prefix=".jarvis-feature-", suffix=".tmp", dir=self.root
        )
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as destination:
                destination.write(encoded)
                destination.flush()
                os.fsync(destination.fileno())
            if existing_mode is not None:
                os.chmod(temporary, existing_mode)
            # The final expected-hash check is intentionally after the temp
            # file is durable and immediately before the atomic replacement.
            current_existed = os.path.lexists(self.env_path)
            current_sha = _configuration_sha256(_ordinary_env(self.env_path))
            if (
                current_sha != expected_current_sha
                or current_existed != expected_current_existed
            ):
                raise FeatureOnboardingConflict(
                    "Jarvis feature configuration changed during the decision."
                )
            os.replace(temporary, self.env_path)
            _fsync_directory(self.root)
        except BaseException:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise


def run_interactive(
    store: FeatureOnboardingStore,
    *,
    input_fn: Callable[[str], str] = input,
    output: TextIO = sys.stdout,
) -> dict[str, Any]:
    status = store.list_status()
    pending = [row for row in status["features"] if row["decision"] == "pending"]
    if not pending:
        print("Optional Jarvis features are already reviewed. You can change them later in Settings.", file=output)
        return status
    print("\nOptional Jarvis features", file=output)
    print("Choose Set up, Not now, or Keep disabled for each item.", file=output)
    print("Every choice can be changed later in Jarvis Settings or by asking Jarvis.\n", file=output)
    current_sha = str(status["configuration_sha256"])
    for row in pending:
        print(str(row["title"]), file=output)
        print(f"  {row['description']}", file=output)
        print(f"  Safety: {row['safety_boundary']}", file=output)
        try:
            answer = input_fn("  [s]et up / [n]ot now / keep [d]isabled (default n): ")
        except (EOFError, KeyboardInterrupt):
            answer = "n"
        selected = {"s": "setup", "setup": "setup", "d": "disable", "disable": "disable"}.get(
            str(answer).strip().casefold(), "skip"
        )
        result = store.decide(
            str(row["capability_id"]),
            selected,
            expected_configuration_sha256=current_sha,
        )
        current_sha = str(result["configuration_sha256"])
        print(f"  Saved: {selected.replace('_', ' ')}\n", file=output)
    print("Optional-feature review complete. Configuration changes apply after Jarvis restarts.", file=output)
    return store.list_status()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Review optional Jarvis features")
    parser.add_argument("--interactive", action="store_true")
    parser.add_argument("--status", action="store_true")
    choice = parser.add_mutually_exclusive_group()
    choice.add_argument("--setup", metavar="CAPABILITY_ID")
    choice.add_argument("--skip", metavar="CAPABILITY_ID")
    choice.add_argument("--disable", metavar="CAPABILITY_ID")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = Config.load()
        store = FeatureOnboardingStore(config.root, config.data_dir)
        if args.interactive:
            if not bool(getattr(sys.stdin, "isatty", lambda: False)()):
                print("Interactive optional-feature setup requires a terminal.", file=sys.stderr)
                return 2
            run_interactive(store)
            return 0
        selected = args.setup or args.skip or args.disable
        if selected:
            decision = "setup" if args.setup else "skip" if args.skip else "disable"
            print(json.dumps(store.decide(selected, decision), ensure_ascii=True))
            return 0
        print(json.dumps(store.list_status(), ensure_ascii=True, indent=2))
        return 0
    except (FeatureOnboardingError, ValueError) as exc:
        print(f"Feature setup failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
