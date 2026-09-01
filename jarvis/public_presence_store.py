"""Isolated persistence and authorization primitives for Public Presence.

The store accepts only a database named ``public_presence.db`` and has no
dependency on JARVIS's private memory implementation.  It deliberately exposes
no platform client and no publish/send method.  Its action reservations are a
durable boundary primitive that a future, separately reviewed adapter may use.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Mapping, Sequence
from uuid import uuid4

from .public_bridge import PublicBridgeObject, PublicBridgeError, sanitize_public_text


PUBLIC_PRESENCE_SCHEMA_VERSION = 3
PUBLIC_PRESENCE_APPLICATION_ID = 0x4A505542  # "JPUB"
MAX_APPROVAL_LIFETIME_SECONDS = 7 * 24 * 60 * 60

PUBLIC_PLATFORMS = frozenset(
    {"simulation", "moltbook", "discord", "x", "public_website"}
)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_PUBLIC_TARGET = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@#-]{0,255}\Z")
_ACTOR = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}\Z")
_OUTCOMES = frozenset({"simulated_success", "simulated_failure", "cancelled"})


class PublicPresenceStoreError(RuntimeError):
    """Base error for fail-closed public-presence persistence."""


class PublicPresenceStopped(PublicPresenceStoreError):
    """An action was attempted while Public Presence was not operational."""


class ApprovalError(PublicPresenceStoreError):
    """An approval was absent or not usable."""


class ApprovalExpired(ApprovalError):
    pass


class ApprovalMismatch(ApprovalError):
    pass


class ApprovalReplay(ApprovalError):
    pass


class IdempotencyConflict(ApprovalError):
    pass


class _ClosingConnection(sqlite3.Connection):
    """Make ``with connection`` close the Windows file handle after commit/rollback."""

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        try:
            return bool(super().__exit__(exc_type, exc_value, traceback))
        finally:
            self.close()


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _execute_script_in_current_transaction(db: sqlite3.Connection, script: str) -> None:
    """Execute DDL without sqlite3.executescript's implicit transaction boundary."""
    pending = ""
    for line in script.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            statement = pending.strip()
            pending = ""
            if statement:
                db.execute(statement)
    if pending.strip():
        raise PublicPresenceStoreError("incomplete public schema migration statement")


def _now(value: float | None = None) -> float:
    if isinstance(value, bool):
        raise ValueError("timestamp must be numeric")
    moment = time.time() if value is None else float(value)
    if not math.isfinite(moment) or moment < 0:
        raise ValueError("timestamp must be finite and non-negative")
    return moment


def _safe_actor(value: Any) -> str:
    if type(value) is not str or _ACTOR.fullmatch(value.strip()) is None:
        raise ValueError("actor must be a bounded non-secret identifier")
    # Run the shared public-data scanner as defense in depth.
    return sanitize_public_text(value.strip(), "actor", 128)


def _target(value: Any, label: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if type(value) is not str or _PUBLIC_TARGET.fullmatch(value.strip()) is None:
        raise ValueError(f"{label} must be a bounded public target identifier")
    return sanitize_public_text(value.strip(), label, 256)


def _digest(value: Any, label: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value.strip().casefold()) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value.strip().casefold()


def _required_row(value: Any, label: str) -> Any:
    if value is None:
        raise PublicPresenceStoreError(f"{label} was not persisted")
    return value


class PublicPresenceStore:
    """Restart-safe storage for the isolated, non-live public control plane."""

    def __init__(self, path: Path) -> None:
        candidate = Path(path)
        if candidate.name.casefold() != "public_presence.db":
            raise ValueError("Public Presence may only open a database named public_presence.db")
        from .sqlite_preflight import validate_database_path

        try:
            path_exists = validate_database_path(candidate)
        except OSError as exc:
            raise PublicPresenceStoreError(
                "public database could not be inspected safely"
            ) from exc
        self.path = candidate.resolve(strict=False)
        if path_exists:
            self._preflight_existing_store()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

    def _preflight_existing_store(self) -> None:
        from .sqlite_preflight import inspection_connection
        try:
            with inspection_connection(self.path) as db:
                db.row_factory = sqlite3.Row
                existing_id = int(db.execute("PRAGMA application_id").fetchone()[0])
                version = int(db.execute("PRAGMA user_version").fetchone()[0])
                tables = {str(row[0]) for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()}
                public_version = None
                if "public_schema" in tables:
                    row = db.execute(
                        "SELECT version FROM public_schema WHERE singleton=1"
                    ).fetchone()
                    public_version = None if row is None else int(row["version"])
        except (OSError, sqlite3.DatabaseError, TypeError, ValueError) as exc:
            raise PublicPresenceStoreError(
                "public database could not be inspected safely"
            ) from exc
        if existing_id not in {0, PUBLIC_PRESENCE_APPLICATION_ID}:
            raise PublicPresenceStoreError("database belongs to a different application")
        if existing_id == 0 and tables - {"sqlite_sequence"}:
            raise PublicPresenceStoreError("existing unmarked database is not a public store")
        if version > PUBLIC_PRESENCE_SCHEMA_VERSION or (
            public_version is not None and public_version > PUBLIC_PRESENCE_SCHEMA_VERSION
        ):
            raise PublicPresenceStoreError("public database schema is newer than this runtime")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(
            str(self.path), timeout=5.0, factory=_ClosingConnection
        )
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=5000")
        return db

    def _migrate(self) -> None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing_id = int(db.execute("PRAGMA application_id").fetchone()[0])
            existing_user_version = int(
                db.execute("PRAGMA user_version").fetchone()[0]
            )
            if existing_id not in {0, PUBLIC_PRESENCE_APPLICATION_ID}:
                raise PublicPresenceStoreError("database belongs to a different application")
            tables = {
                str(row[0])
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if existing_id == 0 and tables - {"sqlite_sequence"}:
                raise PublicPresenceStoreError("existing unmarked database is not a public store")
            if existing_user_version > PUBLIC_PRESENCE_SCHEMA_VERSION:
                raise PublicPresenceStoreError(
                    "public database schema is newer than this runtime"
                )
            # Read and validate the application-owned version marker before WAL,
            # DDL, ALTERs, expiry changes, or audit writes. Unknown future state
            # must remain byte-for-byte outside this runtime's authority.
            if "public_schema" in tables:
                try:
                    preflight_schema = db.execute(
                        "SELECT version FROM public_schema WHERE singleton=1"
                    ).fetchone()
                except sqlite3.DatabaseError as exc:
                    raise PublicPresenceStoreError(
                        "public database schema marker is invalid"
                    ) from exc
                if (
                    preflight_schema is not None
                    and int(preflight_schema["version"])
                    > PUBLIC_PRESENCE_SCHEMA_VERSION
                ):
                    raise PublicPresenceStoreError(
                        "public database schema is newer than this runtime"
                    )
            db.execute(f"PRAGMA application_id={PUBLIC_PRESENCE_APPLICATION_ID}")
            _execute_script_in_current_transaction(
                db,
                """
                CREATE TABLE IF NOT EXISTS public_schema (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    version INTEGER NOT NULL,
                    migrated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS public_control_state (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
                    paused INTEGER NOT NULL CHECK(paused IN (0,1)),
                    emergency_stopped INTEGER NOT NULL CHECK(emergency_stopped IN (0,1)),
                    updated_at REAL NOT NULL,
                    updated_by TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS public_bridge_inbox (
                    bridge_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    record_sha256 TEXT NOT NULL UNIQUE,
                    expires_at REAL NOT NULL,
                    accepted_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS public_bridge_authorizations (
                    authorization_id TEXT PRIMARY KEY,
                    bridge_id TEXT NOT NULL UNIQUE,
                    record_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN
                        ('pending','approved','rejected','expired','consumed')),
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    decided_at REAL,
                    decided_by TEXT,
                    consumed_at REAL
                );
                CREATE TABLE IF NOT EXISTS public_approvals (
                    approval_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL CHECK(status IN
                        ('pending','approved','rejected','expired','consumed')),
                    exact_text TEXT NOT NULL,
                    text_sha256 TEXT NOT NULL,
                    media_hashes_json TEXT NOT NULL,
                    source_hashes_json TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    destination TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    reply_target TEXT,
                    action_fingerprint TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    decided_at REAL,
                    decided_by TEXT,
                    consumed_at REAL
                );
                CREATE TABLE IF NOT EXISTS public_action_reservations (
                    reservation_id TEXT PRIMARY KEY,
                    approval_id TEXT NOT NULL UNIQUE,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    action_fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN
                        ('reserved','simulated_success','simulated_failure','cancelled')),
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    external_receipt_sha256 TEXT,
                    FOREIGN KEY(approval_id) REFERENCES public_approvals(approval_id)
                );
                CREATE TABLE IF NOT EXISTS public_audit_receipts (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    receipt_id TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    subject_id TEXT,
                    details_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    previous_hash TEXT NOT NULL,
                    receipt_hash TEXT NOT NULL UNIQUE
                );
                CREATE INDEX IF NOT EXISTS idx_public_bridge_expiry
                    ON public_bridge_inbox(expires_at);
                CREATE INDEX IF NOT EXISTS idx_public_bridge_authorization_status
                    ON public_bridge_authorizations(status, expires_at);
                CREATE INDEX IF NOT EXISTS idx_public_approval_status_expiry
                    ON public_approvals(status, expires_at);
                CREATE TRIGGER IF NOT EXISTS public_audit_receipts_no_update
                BEFORE UPDATE ON public_audit_receipts
                BEGIN
                    SELECT RAISE(ABORT, 'public audit receipts are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS public_audit_receipts_no_delete
                BEFORE DELETE ON public_audit_receipts
                BEGIN
                    SELECT RAISE(ABORT, 'public audit receipts are append-only');
                END;
                """
            )
            approval_columns = {
                str(row[1])
                for row in db.execute("PRAGMA table_info(public_approvals)").fetchall()
            }
            if "source_hashes_json" not in approval_columns:
                db.execute(
                    "ALTER TABLE public_approvals ADD COLUMN source_hashes_json TEXT NOT NULL DEFAULT '[]'"
                )
            if "idempotency_key" not in approval_columns:
                db.execute(
                    "ALTER TABLE public_approvals ADD COLUMN idempotency_key TEXT"
                )
                # Legacy approvals were not bound to an idempotency key and
                # cannot safely survive this migration as usable authority.
                db.execute(
                    """UPDATE public_approvals SET status='expired'
                       WHERE status IN ('pending','approved')"""
                )
            db.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS idx_public_approval_idempotency
                   ON public_approvals(idempotency_key)
                   WHERE idempotency_key IS NOT NULL"""
            )
            schema_row = db.execute(
                "SELECT version FROM public_schema WHERE singleton=1"
            ).fetchone()
            if schema_row is not None and int(schema_row["version"]) > PUBLIC_PRESENCE_SCHEMA_VERSION:
                raise PublicPresenceStoreError("public database schema is newer than this runtime")
            previous_version = (
                None if schema_row is None else int(schema_row["version"])
            )
            if previous_version is None or previous_version < 3:
                receipt_count = int(
                    db.execute(
                        "SELECT COUNT(*) FROM public_audit_receipts"
                    ).fetchone()[0]
                )
                if receipt_count:
                    raise PublicPresenceStoreError(
                        "legacy public audit ledger cannot be upgraded without review"
                    )
                self._append_audit(
                    db,
                    event_type="audit.genesis",
                    outcome="initialized",
                    subject_id=None,
                    details={"schema_version": PUBLIC_PRESENCE_SCHEMA_VERSION},
                    created_at=time.time(),
                )
            db.execute(
                """INSERT INTO public_schema(singleton, version, migrated_at)
                   VALUES(1,?,?)
                   ON CONFLICT(singleton) DO UPDATE SET
                       version=excluded.version, migrated_at=excluded.migrated_at""",
                (PUBLIC_PRESENCE_SCHEMA_VERSION, time.time()),
            )
            db.execute(
                """INSERT OR IGNORE INTO public_control_state(
                       singleton, enabled, paused, emergency_stopped, updated_at, updated_by
                   ) VALUES(1,0,1,0,?,'bootstrap')""",
                (time.time(),),
            )
            db.execute(f"PRAGMA user_version={PUBLIC_PRESENCE_SCHEMA_VERSION}")
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL").fetchone()

    @staticmethod
    def _control_row(db: sqlite3.Connection) -> dict[str, Any]:
        row = db.execute(
            """SELECT enabled, paused, emergency_stopped, updated_at, updated_by
               FROM public_control_state WHERE singleton=1"""
        ).fetchone()
        if row is None:
            raise PublicPresenceStoreError("public control state is unavailable")
        enabled = bool(row["enabled"])
        paused = bool(row["paused"])
        stopped = bool(row["emergency_stopped"])
        can_act = enabled and not paused and not stopped
        return {
            "enabled": enabled,
            "paused": paused,
            "emergency_stopped": stopped,
            "effective_state": (
                "emergency_stopped"
                if stopped
                else "disabled"
                if not enabled
                else "paused"
                if paused
                else "ready"
            ),
            "can_external_action": can_act,
            "updated_at": float(row["updated_at"]),
            "updated_by": str(row["updated_by"]),
        }

    @staticmethod
    def _append_audit(
        db: sqlite3.Connection,
        *,
        event_type: str,
        outcome: str,
        subject_id: str | None,
        details: Mapping[str, Any],
        created_at: float,
    ) -> dict[str, Any]:
        safe_event = _target(event_type, "event_type")
        safe_outcome = _target(outcome, "outcome")
        safe_subject = _target(subject_id, "subject_id", optional=True)
        details_json = _canonical_json(dict(details))
        if len(details_json) > 20_000:
            raise ValueError("audit details exceed 20000 characters")
        # Audit details are generated by this module, but reject accidental
        # credential material if a future caller expands them.
        sanitize_public_text(details_json, "audit details", 20_000, allow_empty=True)
        previous = db.execute(
            "SELECT receipt_hash FROM public_audit_receipts ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else str(previous["receipt_hash"])
        receipt_id = uuid4().hex
        unsigned = {
            "receipt_id": receipt_id,
            "event_type": safe_event,
            "outcome": safe_outcome,
            "subject_id": safe_subject,
            "details": json.loads(details_json),
            "created_at": created_at,
            "previous_hash": previous_hash,
        }
        receipt_hash = _sha256(_canonical_json(unsigned))
        cursor = db.execute(
            """INSERT INTO public_audit_receipts(
                   receipt_id, event_type, outcome, subject_id, details_json,
                   created_at, previous_hash, receipt_hash
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                receipt_id,
                safe_event,
                safe_outcome,
                safe_subject,
                details_json,
                created_at,
                previous_hash,
                receipt_hash,
            ),
        )
        return {**unsigned, "sequence": int(cursor.lastrowid), "receipt_hash": receipt_hash}

    def get_control_state(self) -> dict[str, Any]:
        with self._connect() as db:
            return self._control_row(db)

    def status(self) -> dict[str, Any]:
        return self.get_control_state()

    def set_enabled(self, enabled: bool, *, actor: str = "operator") -> dict[str, Any]:
        if type(enabled) is not bool:
            raise TypeError("enabled must be a boolean")
        safe_actor = _safe_actor(actor)
        now = time.time()
        blocked = False
        result: dict[str, Any] | None = None
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self._control_row(db)
            if enabled and state["emergency_stopped"]:
                self._append_audit(
                    db,
                    event_type="control.enabled",
                    outcome="blocked",
                    subject_id=None,
                    details={"actor": safe_actor, "requested": True, "reason": "emergency_stop"},
                    created_at=now,
                )
                blocked = True
            else:
                db.execute(
                    """UPDATE public_control_state
                       SET enabled=?, paused=CASE WHEN ?=0 THEN 1 ELSE paused END,
                           updated_at=?, updated_by=? WHERE singleton=1""",
                    (int(enabled), int(enabled), now, safe_actor),
                )
                result = self._control_row(db)
                self._append_audit(
                    db,
                    event_type="control.enabled",
                    outcome="accepted",
                    subject_id=None,
                    details={"actor": safe_actor, "enabled": enabled},
                    created_at=now,
                )
        if blocked:
            raise PublicPresenceStopped("clear the emergency stop before enabling Public Presence")
        if result is None:
            raise PublicPresenceStoreError("public control state was not persisted")
        return result

    def set_paused(self, paused: bool, *, actor: str = "operator") -> dict[str, Any]:
        if type(paused) is not bool:
            raise TypeError("paused must be a boolean")
        safe_actor = _safe_actor(actor)
        now = time.time()
        blocked = False
        result: dict[str, Any] | None = None
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self._control_row(db)
            if not paused and (not state["enabled"] or state["emergency_stopped"]):
                self._append_audit(
                    db,
                    event_type="control.paused",
                    outcome="blocked",
                    subject_id=None,
                    details={
                        "actor": safe_actor,
                        "requested": False,
                        "reason": "disabled_or_emergency_stopped",
                    },
                    created_at=now,
                )
                blocked = True
            else:
                db.execute(
                    """UPDATE public_control_state SET paused=?, updated_at=?, updated_by=?
                       WHERE singleton=1""",
                    (int(paused), now, safe_actor),
                )
                result = self._control_row(db)
                self._append_audit(
                    db,
                    event_type="control.paused",
                    outcome="accepted",
                    subject_id=None,
                    details={"actor": safe_actor, "paused": paused},
                    created_at=now,
                )
        if blocked:
            raise PublicPresenceStopped("Public Presence must be enabled and not stopped before resume")
        if result is None:
            raise PublicPresenceStoreError("public pause state was not persisted")
        return result

    def emergency_stop(self, *, actor: str = "operator") -> dict[str, Any]:
        safe_actor = _safe_actor(actor)
        now = time.time()
        with self._connect() as db:
            db.execute(
                """UPDATE public_control_state
                   SET enabled=0, paused=1, emergency_stopped=1,
                       updated_at=?, updated_by=? WHERE singleton=1""",
                (now, safe_actor),
            )
            result = self._control_row(db)
            self._append_audit(
                db,
                event_type="control.emergency_stop",
                outcome="accepted",
                subject_id=None,
                details={"actor": safe_actor},
                created_at=now,
            )
            return result

    def clear_emergency_stop(self, *, actor: str = "operator") -> dict[str, Any]:
        safe_actor = _safe_actor(actor)
        now = time.time()
        with self._connect() as db:
            db.execute(
                """UPDATE public_control_state
                   SET enabled=0, paused=1, emergency_stopped=0,
                       updated_at=?, updated_by=? WHERE singleton=1""",
                (now, safe_actor),
            )
            result = self._control_row(db)
            self._append_audit(
                db,
                event_type="control.emergency_clear",
                outcome="accepted",
                subject_id=None,
                details={"actor": safe_actor, "requires_explicit_reenable": True},
                created_at=now,
            )
            return result

    @staticmethod
    def _bridge_authorization_id(value: PublicBridgeObject) -> str:
        approvals = tuple(
            item
            for item in value.provenance
            if item.source_kind == "operator_approval"
        )
        if len(approvals) != 1:
            raise PublicBridgeError(
                "bridge object must contain exactly one operator approval identity"
            )
        return _target(approvals[0].source_id, "authorization_id") or ""

    def request_bridge_authorization(
        self,
        value: PublicBridgeObject,
        *,
        actor: str = "operator",
        now: float | None = None,
    ) -> dict[str, Any]:
        """Create a local review row bound to one exact sanitized bridge object."""

        if type(value) is not PublicBridgeObject:
            raise PublicBridgeError("only a typed PublicBridgeObject may be authorized")
        safe_actor = _safe_actor(actor)
        moment = _now(now)
        value.assert_current(now=moment)
        authorization_id = self._bridge_authorization_id(value)
        record_sha256 = _sha256(_canonical_json(value.to_record()))
        expires_at = min(
            value.expires_at,
            moment + MAX_APPROVAL_LIFETIME_SECONDS,
        )
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                """SELECT authorization_id, bridge_id, record_sha256, status,
                          created_at, expires_at, decided_at, decided_by, consumed_at
                   FROM public_bridge_authorizations WHERE authorization_id=?""",
                (authorization_id,),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["bridge_id"]) == value.bridge_id
                    and hmac.compare_digest(
                        str(existing["record_sha256"]), record_sha256
                    )
                ):
                    return dict(existing)
                raise IdempotencyConflict(
                    "operator bridge authorization identity was reused for different content"
                )
            db.execute(
                """INSERT INTO public_bridge_authorizations(
                       authorization_id, bridge_id, record_sha256, status,
                       created_at, expires_at
                   ) VALUES(?,?,?,'pending',?,?)""",
                (
                    authorization_id,
                    value.bridge_id,
                    record_sha256,
                    moment,
                    expires_at,
                ),
            )
            self._append_audit(
                db,
                event_type="bridge.authorization_requested",
                outcome="pending",
                subject_id=authorization_id,
                details={
                    "actor": safe_actor,
                    "bridge_id": value.bridge_id,
                    "record_sha256": record_sha256,
                    "expires_at": expires_at,
                },
                created_at=moment,
            )
            row = db.execute(
                "SELECT * FROM public_bridge_authorizations WHERE authorization_id=?",
                (authorization_id,),
            ).fetchone()
            return dict(_required_row(row, "bridge authorization"))

    def decide_bridge_authorization(
        self,
        authorization_id: str,
        approve: bool,
        *,
        actor: str = "operator",
        now: float | None = None,
    ) -> dict[str, Any]:
        safe_id = _target(authorization_id, "authorization_id")
        if type(approve) is not bool:
            raise TypeError("approve must be a boolean")
        safe_actor = _safe_actor(actor)
        moment = _now(now)
        terminal_error: PublicPresenceStoreError | None = None
        result: dict[str, Any] | None = None
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT status, expires_at FROM public_bridge_authorizations
                   WHERE authorization_id=?""",
                (safe_id,),
            ).fetchone()
            if row is None:
                terminal_error = ApprovalError(
                    "bridge authorization does not exist"
                )
            elif str(row["status"]) != "pending":
                terminal_error = ApprovalReplay(
                    "bridge authorization has already been decided or consumed"
                )
            else:
                expired = float(row["expires_at"]) <= moment
                status = "expired" if expired else "approved" if approve else "rejected"
                db.execute(
                    """UPDATE public_bridge_authorizations
                       SET status=?, decided_at=?, decided_by=?
                       WHERE authorization_id=? AND status='pending'""",
                    (status, moment, safe_actor, safe_id),
                )
                self._append_audit(
                    db,
                    event_type="bridge.authorization_decided",
                    outcome=status,
                    subject_id=safe_id,
                    details={"actor": safe_actor},
                    created_at=moment,
                )
                decided = db.execute(
                    "SELECT * FROM public_bridge_authorizations WHERE authorization_id=?",
                    (safe_id,),
                ).fetchone()
                result = dict(_required_row(decided, "bridge authorization decision"))
                if expired:
                    terminal_error = ApprovalExpired(
                        "bridge authorization expired before decision"
                    )
        if terminal_error is not None:
            raise terminal_error
        if result is None:
            raise PublicPresenceStoreError("bridge authorization decision was not persisted")
        return result

    def accept_bridge_object(
        self,
        value: PublicBridgeObject,
        *,
        now: float | None = None,
    ) -> dict[str, Any]:
        if type(value) is not PublicBridgeObject:
            raise PublicBridgeError("only a typed PublicBridgeObject may enter the public store")
        moment = _now(now)
        value.assert_current(now=moment)
        record_json = _canonical_json(value.to_record())
        record_sha256 = _sha256(record_json)
        authorization_id = self._bridge_authorization_id(value)
        terminal_error: ApprovalError | None = None
        result: dict[str, Any] | None = None
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                """SELECT bridge_id, kind, record_sha256, expires_at, accepted_at
                   FROM public_bridge_inbox WHERE bridge_id=?""",
                (value.bridge_id,),
            ).fetchone()
            if existing is not None:
                if not hmac.compare_digest(
                    str(existing["record_sha256"]), record_sha256
                ):
                    raise IdempotencyConflict(
                        "bridge_id was already used for different content"
                    )

            stopped = bool(self._control_row(db)["emergency_stopped"])

            authorization = db.execute(
                """SELECT authorization_id, bridge_id, record_sha256, status,
                          expires_at, consumed_at
                   FROM public_bridge_authorizations WHERE authorization_id=?""",
                (authorization_id,),
            ).fetchone()
            if existing is None and stopped:
                terminal_error = PublicPresenceStopped(
                    "Public Presence emergency stop blocks new bridge intake"
                )
            elif authorization is None:
                terminal_error = ApprovalError(
                    "bridge object has no trusted operator authorization"
                )
            elif (
                str(authorization["bridge_id"]) != value.bridge_id
                or not hmac.compare_digest(
                    str(authorization["record_sha256"]), record_sha256
                )
            ):
                terminal_error = ApprovalMismatch(
                    "bridge object does not match its operator authorization"
                )
            else:
                status = str(authorization["status"])
                if existing is not None:
                    if status != "consumed":
                        terminal_error = ApprovalReplay(
                            "accepted bridge object has no consumed authorization"
                        )
                    else:
                        result = dict(existing)
                elif status == "approved" and float(authorization["expires_at"]) <= moment:
                    db.execute(
                        """UPDATE public_bridge_authorizations
                           SET status='expired' WHERE authorization_id=?
                           AND status='approved'""",
                        (authorization_id,),
                    )
                    self._append_audit(
                        db,
                        event_type="bridge.authorization_expired",
                        outcome="expired",
                        subject_id=authorization_id,
                        details={"bridge_id": value.bridge_id},
                        created_at=moment,
                    )
                    terminal_error = ApprovalExpired(
                        "bridge authorization expired before use"
                    )
                elif status == "approved":
                    db.execute(
                        """INSERT INTO public_bridge_inbox(
                               bridge_id, kind, record_json, record_sha256,
                               expires_at, accepted_at
                           ) VALUES(?,?,?,?,?,?)""",
                        (
                            value.bridge_id,
                            value.kind,
                            record_json,
                            record_sha256,
                            value.expires_at,
                            moment,
                        ),
                    )
                    updated = db.execute(
                        """UPDATE public_bridge_authorizations
                           SET status='consumed', consumed_at=?
                           WHERE authorization_id=? AND status='approved'
                           AND bridge_id=? AND record_sha256=?""",
                        (
                            moment,
                            authorization_id,
                            value.bridge_id,
                            record_sha256,
                        ),
                    )
                    if updated.rowcount != 1:
                        raise ApprovalReplay(
                            "bridge authorization was consumed concurrently"
                        )
                    self._append_audit(
                        db,
                        event_type="bridge.accepted",
                        outcome="accepted",
                        subject_id=value.bridge_id,
                        details={
                            "authorization_id": authorization_id,
                            "kind": value.kind,
                            "record_sha256": record_sha256,
                        },
                        created_at=moment,
                    )
                    result = {
                        "bridge_id": value.bridge_id,
                        "kind": value.kind,
                        "record_sha256": record_sha256,
                        "expires_at": value.expires_at,
                        "accepted_at": moment,
                    }
                elif status == "expired":
                    terminal_error = ApprovalExpired(
                        "bridge authorization is expired"
                    )
                elif status in {"rejected", "consumed"}:
                    terminal_error = ApprovalReplay(
                        "bridge authorization is rejected or already consumed"
                    )
                else:
                    terminal_error = ApprovalError(
                        "bridge authorization is still pending"
                    )
            if terminal_error is not None:
                self._append_audit(
                    db,
                    event_type="bridge.rejected",
                    outcome="blocked",
                    subject_id=authorization_id,
                    details={
                        "bridge_id": value.bridge_id,
                        "record_sha256": record_sha256,
                        "reason": type(terminal_error).__name__,
                    },
                    created_at=moment,
                )
        if terminal_error is not None:
            raise terminal_error
        if result is None:
            raise PublicPresenceStoreError("accepted bridge object was not persisted")
        return result

    def get_bridge_object(
        self,
        bridge_id: str,
        *,
        now: float | None = None,
    ) -> PublicBridgeObject | None:
        safe_id = _target(bridge_id, "bridge_id")
        moment = _now(now)
        with self._connect() as db:
            row = db.execute(
                """SELECT bridge_id, kind, record_json, record_sha256
                   FROM public_bridge_inbox WHERE bridge_id=? AND expires_at>?""",
                (safe_id, moment),
            ).fetchone()
        if row is None:
            return None
        try:
            record_json = str(row["record_json"])
            if not hmac.compare_digest(
                _sha256(record_json), str(row["record_sha256"])
            ):
                raise PublicPresenceStoreError(
                    "stored bridge object row digest does not match"
                )
            parsed = json.loads(record_json)
            value = PublicBridgeObject.from_record(parsed, now=moment)
            if value.bridge_id != safe_id or value.kind != str(row["kind"]):
                raise PublicPresenceStoreError(
                    "stored bridge object identity does not match its row"
                )
            return value
        except (json.JSONDecodeError, PublicBridgeError) as exc:
            raise PublicPresenceStoreError("stored bridge object failed integrity validation") from exc

    @staticmethod
    def _action_components(
        *,
        exact_text: str,
        media_hashes: Sequence[str],
        source_hashes: Sequence[str],
        idempotency_key: str,
        platform: str,
        destination: str,
        account_id: str,
        reply_target: str | None,
    ) -> tuple[dict[str, Any], str]:
        safe_text = sanitize_public_text(exact_text, "exact_text", 20_000)
        safe_media = tuple(_digest(item, "media hash") for item in media_hashes)
        if len(safe_media) > 8 or len(set(safe_media)) != len(safe_media):
            raise ValueError("media_hashes must contain at most 8 unique SHA-256 digests")
        safe_sources = tuple(sorted(_digest(item, "source hash") for item in source_hashes))
        if len(safe_sources) > 32 or len(set(safe_sources)) != len(safe_sources):
            raise ValueError("source_hashes must contain at most 32 unique SHA-256 digests")
        safe_key = _target(idempotency_key, "idempotency_key")
        safe_platform = str(platform).strip().casefold()
        if safe_platform not in PUBLIC_PLATFORMS:
            raise ValueError("unsupported public platform")
        components = {
            "text_sha256": _sha256(safe_text),
            "media_hashes": list(safe_media),
            "source_hashes": list(safe_sources),
            "idempotency_key": safe_key,
            "platform": safe_platform,
            "destination": _target(destination, "destination"),
            "account_id": _target(account_id, "account_id"),
            "reply_target": _target(reply_target, "reply_target", optional=True),
        }
        return components, safe_text

    def create_approval(
        self,
        *,
        exact_text: str,
        media_hashes: Sequence[str] = (),
        source_hashes: Sequence[str] = (),
        idempotency_key: str,
        platform: str,
        destination: str,
        account_id: str,
        reply_target: str | None = None,
        expires_at: float,
        now: float | None = None,
    ) -> dict[str, Any]:
        moment = _now(now)
        expiry = _now(expires_at)
        if expiry <= moment or expiry - moment > MAX_APPROVAL_LIFETIME_SECONDS:
            raise ValueError("approval expiry must be in the future and within seven days")
        components, safe_text = self._action_components(
            exact_text=exact_text,
            media_hashes=media_hashes,
            source_hashes=source_hashes,
            idempotency_key=idempotency_key,
            platform=platform,
            destination=destination,
            account_id=account_id,
            reply_target=reply_target,
        )
        fingerprint = _sha256(_canonical_json(components))
        approval_id = uuid4().hex
        with self._connect() as db:
            db.execute(
                """INSERT INTO public_approvals(
                       approval_id, status, exact_text, text_sha256, media_hashes_json,
                       source_hashes_json, idempotency_key,
                       platform, destination, account_id, reply_target,
                       action_fingerprint, created_at, expires_at
                   ) VALUES(?,'pending',?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    approval_id,
                    safe_text,
                    components["text_sha256"],
                    _canonical_json(components["media_hashes"]),
                    _canonical_json(components["source_hashes"]),
                    components["idempotency_key"],
                    components["platform"],
                    components["destination"],
                    components["account_id"],
                    components["reply_target"],
                    fingerprint,
                    moment,
                    expiry,
                ),
            )
            self._append_audit(
                db,
                event_type="approval.created",
                outcome="pending",
                subject_id=approval_id,
                details={**components, "action_fingerprint": fingerprint, "expires_at": expiry},
                created_at=moment,
            )
        return self.get_approval(approval_id) or {}

    def get_approval(self, approval_id: str) -> dict[str, Any] | None:
        safe_id = _target(approval_id, "approval_id")
        with self._connect() as db:
            row = db.execute(
                """SELECT approval_id, status, text_sha256, media_hashes_json,
                          source_hashes_json, idempotency_key,
                          platform, destination, account_id, reply_target,
                          action_fingerprint, created_at, expires_at, decided_at,
                          decided_by, consumed_at
                   FROM public_approvals WHERE approval_id=?""",
                (safe_id,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["media_hashes"] = json.loads(result.pop("media_hashes_json"))
        result["source_hashes"] = json.loads(result.pop("source_hashes_json"))
        return result

    def decide_approval(
        self,
        approval_id: str,
        approve: bool,
        *,
        actor: str = "operator",
        now: float | None = None,
    ) -> dict[str, Any]:
        safe_id = _target(approval_id, "approval_id")
        if type(approve) is not bool:
            raise TypeError("approve must be a boolean")
        safe_actor = _safe_actor(actor)
        moment = _now(now)
        expired = False
        with self._connect() as db:
            # Serialize the read/decision transition. Without an immediate
            # transaction, two connections can both observe ``pending`` before
            # either writes and report conflicting successful decisions.
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT status, expires_at FROM public_approvals WHERE approval_id=?",
                (safe_id,),
            ).fetchone()
            if row is None:
                raise ApprovalError("approval does not exist")
            if str(row["status"]) != "pending":
                raise ApprovalReplay("approval has already been decided or consumed")
            if float(row["expires_at"]) <= moment:
                db.execute(
                    "UPDATE public_approvals SET status='expired', decided_at=?, decided_by=? WHERE approval_id=?",
                    (moment, safe_actor, safe_id),
                )
                self._append_audit(
                    db,
                    event_type="approval.decided",
                    outcome="expired",
                    subject_id=safe_id,
                    details={"actor": safe_actor},
                    created_at=moment,
                )
                expired = True
            else:
                status = "approved" if approve else "rejected"
                db.execute(
                    """UPDATE public_approvals
                       SET status=?, decided_at=?, decided_by=? WHERE approval_id=?""",
                    (status, moment, safe_actor, safe_id),
                )
                self._append_audit(
                    db,
                    event_type="approval.decided",
                    outcome=status,
                    subject_id=safe_id,
                    details={"actor": safe_actor},
                    created_at=moment,
                )
        if expired:
            raise ApprovalExpired("approval expired before it was decided")
        return self.get_approval(safe_id) or {}

    def reserve_approved_action(
        self,
        *,
        approval_id: str,
        idempotency_key: str,
        exact_text: str,
        media_hashes: Sequence[str] = (),
        source_hashes: Sequence[str] = (),
        platform: str,
        destination: str,
        account_id: str,
        reply_target: str | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Atomically consume one exact approval or return its prior reservation.

        This method performs no network or platform operation.  Returning the
        same reservation for the same idempotency key is what allows a future
        adapter to recover after a crash without issuing a duplicate action.
        """

        safe_approval_id = _target(approval_id, "approval_id")
        safe_key = _target(idempotency_key, "idempotency_key")
        moment = _now(now)
        components, _safe_text = self._action_components(
            exact_text=exact_text,
            media_hashes=media_hashes,
            source_hashes=source_hashes,
            idempotency_key=safe_key,
            platform=platform,
            destination=destination,
            account_id=account_id,
            reply_target=reply_target,
        )
        fingerprint = _sha256(_canonical_json(components))
        terminal_error: PublicPresenceStoreError | None = None
        result: dict[str, Any] | None = None
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            control = self._control_row(db)
            if not control["can_external_action"]:
                self._append_audit(
                    db,
                    event_type="action.reserved",
                    outcome="blocked",
                    subject_id=safe_approval_id,
                    details={"reason": control["effective_state"]},
                    created_at=moment,
                )
                terminal_error = PublicPresenceStopped(
                    f"Public Presence cannot act while {control['effective_state']}"
                )
            prior = db.execute(
                """SELECT reservation_id, approval_id, idempotency_key,
                          action_fingerprint, status, created_at, updated_at,
                          external_receipt_sha256
                   FROM public_action_reservations WHERE idempotency_key=?""",
                (safe_key,),
            ).fetchone()
            if terminal_error is not None:
                pass
            elif prior is not None:
                if (
                    str(prior["approval_id"]) != safe_approval_id
                    or not hmac.compare_digest(str(prior["action_fingerprint"]), fingerprint)
                ):
                    terminal_error = IdempotencyConflict(
                        "idempotency key was already bound to a different approved action"
                    )
                    self._append_audit(
                        db,
                        event_type="action.reserved",
                        outcome="idempotency_conflict",
                        subject_id=safe_approval_id,
                        details={"idempotency_key": safe_key},
                        created_at=moment,
                    )
                else:
                    self._append_audit(
                        db,
                        event_type="action.reserved",
                        outcome="idempotent_replay",
                        subject_id=str(prior["reservation_id"]),
                        details={"approval_id": safe_approval_id, "idempotency_key": safe_key},
                        created_at=moment,
                    )
                    result = dict(prior)
            else:
                control = self._control_row(db)
                if not control["can_external_action"]:
                    self._append_audit(
                        db,
                        event_type="action.reserved",
                        outcome="blocked",
                        subject_id=safe_approval_id,
                        details={"reason": control["effective_state"]},
                        created_at=moment,
                    )
                    terminal_error = PublicPresenceStopped(
                        f"Public Presence cannot act while {control['effective_state']}"
                    )
                else:
                    approval = db.execute(
                        """SELECT status, action_fingerprint, expires_at, consumed_at
                           FROM public_approvals WHERE approval_id=?""",
                        (safe_approval_id,),
                    ).fetchone()
                    outcome = "blocked"
                    if approval is None:
                        terminal_error = ApprovalError("approval does not exist")
                        reason = "missing_approval"
                    elif (
                        float(approval["expires_at"]) <= moment
                        and str(approval["status"]) != "consumed"
                    ):
                        db.execute(
                            "UPDATE public_approvals SET status='expired' WHERE approval_id=?",
                            (safe_approval_id,),
                        )
                        terminal_error = ApprovalExpired(
                            "approval expired before action reservation"
                        )
                        outcome = "expired"
                        reason = "expired"
                    elif (
                        str(approval["status"]) == "consumed"
                        or approval["consumed_at"] is not None
                    ):
                        terminal_error = ApprovalReplay(
                            "one-shot approval has already been consumed"
                        )
                        reason = "approval_replay"
                    elif str(approval["status"]) != "approved":
                        terminal_error = ApprovalError("approval is not approved")
                        reason = f"status_{approval['status']}"
                    elif not hmac.compare_digest(
                        str(approval["action_fingerprint"]), fingerprint
                    ):
                        terminal_error = ApprovalMismatch(
                            "text, media, destination, account, or reply target changed after approval"
                        )
                        reason = "approval_substitution"
                    else:
                        terminal_error = None
                        reason = ""
                    if terminal_error is not None:
                        self._append_audit(
                            db,
                            event_type="action.reserved",
                            outcome=outcome,
                            subject_id=safe_approval_id,
                            details={"reason": reason, "action_fingerprint": fingerprint},
                            created_at=moment,
                        )
                    else:
                        reservation_id = uuid4().hex
                        db.execute(
                            """INSERT INTO public_action_reservations(
                                   reservation_id, approval_id, idempotency_key,
                                   action_fingerprint, status, created_at, updated_at
                               ) VALUES(?,?,?,?, 'reserved', ?, ?)""",
                            (
                                reservation_id,
                                safe_approval_id,
                                safe_key,
                                fingerprint,
                                moment,
                                moment,
                            ),
                        )
                        db.execute(
                            """UPDATE public_approvals SET status='consumed', consumed_at=?
                               WHERE approval_id=? AND status='approved'""",
                            (moment, safe_approval_id),
                        )
                        self._append_audit(
                            db,
                            event_type="action.reserved",
                            outcome="reserved",
                            subject_id=reservation_id,
                            details={
                                "approval_id": safe_approval_id,
                                "idempotency_key": safe_key,
                                "action_fingerprint": fingerprint,
                            },
                            created_at=moment,
                        )
                        row = db.execute(
                            "SELECT * FROM public_action_reservations WHERE reservation_id=?",
                            (reservation_id,),
                        ).fetchone()
                        result = dict(_required_row(row, "public action reservation"))
        if terminal_error is not None:
            raise terminal_error
        if result is None:
            raise PublicPresenceStoreError("public action reservation was not persisted")
        return result

    def record_simulation_outcome(
        self,
        reservation_id: str,
        outcome: str,
        *,
        external_receipt_sha256: str | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Record a fixture/simulation result; this does not contact a platform."""

        safe_id = _target(reservation_id, "reservation_id")
        normalized_outcome = str(outcome).strip().casefold()
        if normalized_outcome not in _OUTCOMES:
            raise ValueError("outcome must be simulated_success, simulated_failure, or cancelled")
        receipt = (
            None
            if external_receipt_sha256 is None
            else _digest(external_receipt_sha256, "external_receipt_sha256")
        )
        moment = _now(now)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM public_action_reservations WHERE reservation_id=?",
                (safe_id,),
            ).fetchone()
            if row is None:
                raise PublicPresenceStoreError("action reservation does not exist")
            current = str(row["status"])
            if current != "reserved":
                if current == normalized_outcome and (
                    row["external_receipt_sha256"] == receipt
                ):
                    return dict(row)
                raise IdempotencyConflict("simulation outcome is already final")
            db.execute(
                """UPDATE public_action_reservations
                   SET status=?, updated_at=?, external_receipt_sha256=?
                   WHERE reservation_id=?""",
                (normalized_outcome, moment, receipt, safe_id),
            )
            self._append_audit(
                db,
                event_type="action.simulation",
                outcome=normalized_outcome,
                subject_id=safe_id,
                details={"external_receipt_sha256": receipt},
                created_at=moment,
            )
            result = db.execute(
                "SELECT * FROM public_action_reservations WHERE reservation_id=?",
                (safe_id,),
            ).fetchone()
            return dict(_required_row(result, "public simulation outcome"))

    def list_audit_receipts(self, *, limit: int = 100) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), 1_000))
        with self._connect() as db:
            rows = db.execute(
                """SELECT sequence, receipt_id, event_type, outcome, subject_id,
                          details_json, created_at, previous_hash, receipt_hash
                   FROM public_audit_receipts ORDER BY sequence DESC LIMIT ?""",
                (bounded,),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item.pop("details_json"))
            result.append(item)
        return result

    def verify_audit_chain(self) -> bool:
        with self._connect() as db:
            rows = db.execute(
                """SELECT sequence, receipt_id, event_type, outcome, subject_id,
                          details_json, created_at, previous_hash, receipt_hash
                   FROM public_audit_receipts ORDER BY sequence ASC"""
            ).fetchall()
            sequence_row = db.execute(
                "SELECT seq FROM sqlite_sequence WHERE name='public_audit_receipts'"
            ).fetchone()
        if not rows:
            return False
        if sequence_row is None or int(sequence_row["seq"]) != int(rows[-1]["sequence"]):
            return False
        first = rows[0]
        if (
            str(first["event_type"]) != "audit.genesis"
            or str(first["outcome"]) != "initialized"
            or first["subject_id"] is not None
        ):
            return False
        previous_hash = "0" * 64
        for row in rows:
            if str(row["previous_hash"]) != previous_hash:
                return False
            try:
                details = json.loads(str(row["details_json"]))
            except json.JSONDecodeError:
                return False
            unsigned = {
                "receipt_id": str(row["receipt_id"]),
                "event_type": str(row["event_type"]),
                "outcome": str(row["outcome"]),
                "subject_id": row["subject_id"],
                "details": details,
                "created_at": float(row["created_at"]),
                "previous_hash": previous_hash,
            }
            calculated = _sha256(_canonical_json(unsigned))
            if not hmac.compare_digest(calculated, str(row["receipt_hash"])):
                return False
            previous_hash = calculated
        return True
