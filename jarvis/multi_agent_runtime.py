"""Persistent non-memory control plane for autonomous peer agents.

This module deliberately does not import or mutate the governed memory schemas.
It owns stable runtime identities, mailboxes, rooms, task ownership, delegation,
and an ordered event envelope.  Model reasoning remains outside this store: an
agent-bound caller chooses peers, messages, room participants, and delegates.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Self

RUNTIME_SCHEMA_VERSION = 1
RUNTIME_APPLICATION_ID = 0x4A4D4152  # "JMAR"
EVENT_SCHEMA_VERSION = 2

# Closed v2 content contract. Never SELECT * into durable event payloads: adding
# a projection column must not silently change the exported historical schema.
_EVENT_RECORDS = {
    "agent": (
        "runtime_agents",
        "agent_id",
        "agent_id display_name role purpose personality specialties_json model_provider model_name autonomy authority request_policy lifecycle project_id created_by created_at updated_at",
    ),
    "direct_message": (
        "runtime_direct_messages",
        "message_id",
        "message_id sender_id recipient_id body reply_to_message_id task_id created_at",
    ),
    "direct_message_receipt": (
        "runtime_direct_message_receipts",
        "message_id",
        "message_id recipient_id delivered_at read_at",
    ),
    "room": (
        "runtime_rooms",
        "room_id",
        "room_id project_id name kind access created_by state created_at updated_at",
    ),
    "room_message": (
        "runtime_room_messages",
        "message_id",
        "message_id room_id sender_id body reply_to_message_id task_id created_at",
    ),
    "room_message_cursor": (
        "runtime_room_message_cursors",
        "cursor_id",
        "cursor_id room_id agent_id last_message_id last_event_sequence updated_at",
    ),
    "task": (
        "runtime_tasks",
        "task_id",
        "task_id project_id title description owner_id created_by parent_task_id status result created_at updated_at",
    ),
    "delegation": (
        "runtime_task_delegations",
        "delegation_id",
        "delegation_id task_id from_agent_id to_agent_id reason created_at",
    ),
    "capability_request": (
        "runtime_capability_requests",
        "request_id",
        "request_id agent_id capability scope_kind scope_id reason policy status decision_reason created_at decided_at",
    ),
    "capability_grant": (
        "runtime_capability_grants",
        "grant_id",
        "grant_id request_id agent_id capability scope_kind scope_id granted_by granted_at expires_at revoked_at revoked_by",
    ),
    "artifact": (
        "runtime_artifacts",
        "artifact_id",
        "artifact_id owner_id project_id task_id room_id name media_type uri sha256 size_bytes created_at",
    ),
    "collaboration_request": (
        "runtime_collaboration_requests",
        "request_id",
        "request_id requester_id target_agent_id project_id room_id task_id artifact_id kind prompt options_json status created_at updated_at",
    ),
    "collaboration_response": (
        "runtime_collaboration_responses",
        "response_id",
        "response_id request_id responder_id body selected_option evidence_artifact_id created_at",
    ),
}

_EVENT_RESULT_KINDS = {
    **dict.fromkeys(
        (
            "agent.created",
            "agent.running",
            "agent.paused",
            "agent.stopped",
            "agent.model_changed",
            "agent.policy_changed",
        ),
        "agent",
    ),
    "message.direct_sent": "direct_message",
    "message.direct_acknowledged": "direct_message_receipt",
    **dict.fromkeys(
        ("room.created", "room.agent_invited", "room.agent_joined", "room.agent_left"),
        "room",
    ),
    "room.message_sent": "room_message",
    "room.cursor_advanced": "room_message_cursor",
    **dict.fromkeys(
        (
            "task.created",
            "task.dependency_added",
            "task.running",
            "task.blocked",
            "task.completed",
            "task.failed",
            "task.cancelled",
        ),
        "task",
    ),
    "task.delegated": "delegation",
    "capability.requested": "capability_request",
    "capability.denied": "capability_request",
    "capability.granted": "capability_grant",
    "capability.revoked": "capability_grant",
    "artifact.shared": "artifact",
    "collaboration.requested": "collaboration_request",
    "collaboration.responded": "collaboration_response",
    "collaboration.closed": "collaboration_request",
}


class RuntimeScope(str, Enum):
    GLOBAL = "GLOBAL"
    PROJECT = "PROJECT"
    AGENT = "AGENT"
    TASK = "TASK"
    ROOM = "ROOM"
    PRIVATE = "PRIVATE"


class AgentLifecycle(str, Enum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"


class AgentAutonomy(str, Enum):
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    FULL = "FULL"


class AgentAuthority(str, Enum):
    SANDBOX = "SANDBOX"
    STANDARD = "STANDARD"
    POWER = "POWER"
    OWNER = "OWNER"


class CapabilityRequestPolicy(str, Enum):
    ASK_OWNER = "ASK_OWNER"
    AUTO_TRUSTED = "AUTO_TRUSTED"
    AUTO_PROJECT = "AUTO_PROJECT"
    OWNER_AUTO = "OWNER_AUTO"
    DENY = "DENY"


class CapabilityRequestStatus(str, Enum):
    PENDING = "PENDING"
    GRANTED = "GRANTED"
    DENIED = "DENIED"


class TaskStatus(str, Enum):
    OPEN = "OPEN"
    ASSIGNED = "ASSIGNED"
    RUNNING = "RUNNING"
    BLOCKED = "BLOCKED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class RoomAccess(str, Enum):
    INVITE_ONLY = "INVITE_ONLY"
    OPEN = "OPEN"


class CollaborationKind(str, Enum):
    HELP = "HELP"
    REVIEW = "REVIEW"
    OPINION = "OPINION"
    VOTE = "VOTE"
    EVIDENCE = "EVIDENCE"


class CollaborationStatus(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class MultiAgentRuntimeError(RuntimeError):
    """Base error for deterministic runtime-control-plane failures."""


class RuntimeStoreError(MultiAgentRuntimeError):
    """The runtime store could not be opened without crossing its boundary."""


class RuntimeConflictError(MultiAgentRuntimeError):
    """A command conflicts with durable state or an earlier command receipt."""


class RuntimeNotFoundError(MultiAgentRuntimeError):
    """A requested runtime entity does not exist."""


class RuntimePermissionError(MultiAgentRuntimeError):
    """An actor is not allowed to perform the requested state transition."""


@dataclass(frozen=True)
class AgentRecord:
    agent_id: str
    display_name: str
    role: str
    purpose: str
    personality: str
    specialties: tuple[str, ...]
    model_provider: str
    model_name: str
    autonomy: AgentAutonomy
    authority: AgentAuthority
    request_policy: CapabilityRequestPolicy
    lifecycle: AgentLifecycle
    project_id: str | None
    created_by: str
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class DirectMessage:
    message_id: str
    sender_id: str
    recipient_id: str
    body: str
    reply_to_message_id: str | None
    task_id: str | None
    created_at: float


@dataclass(frozen=True)
class DirectMessageReceipt:
    message_id: str
    recipient_id: str
    delivered_at: float
    read_at: float | None


@dataclass(frozen=True)
class RoomRecord:
    room_id: str
    project_id: str | None
    name: str
    kind: str
    access: RoomAccess
    created_by: str
    state: str
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class RoomMessage:
    message_id: str
    room_id: str
    sender_id: str
    body: str
    reply_to_message_id: str | None
    task_id: str | None
    created_at: float


@dataclass(frozen=True)
class RoomMessageCursor:
    cursor_id: str
    room_id: str
    agent_id: str
    last_message_id: str
    last_event_sequence: int
    updated_at: float


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    project_id: str | None
    title: str
    description: str
    owner_id: str
    created_by: str
    parent_task_id: str | None
    status: TaskStatus
    result: str | None
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class DelegationRecord:
    delegation_id: str
    task_id: str
    from_agent_id: str
    to_agent_id: str
    reason: str
    created_at: float


@dataclass(frozen=True)
class RuntimeEvent:
    sequence: int
    event_id: str
    event_type: str
    schema_version: int
    occurred_at: float
    actor_id: str
    project_id: str | None
    scope: RuntimeScope
    scope_id: str | None
    subject_kind: str
    subject_id: str
    correlation_id: str
    causation_id: str | None
    idempotency_key: str
    command_sha256: str
    payload: Mapping[str, Any]
    result_kind: str
    result_id: str


@dataclass(frozen=True)
class RuntimeIntegrityReport:
    event_count: int
    agent_count: int
    room_count: int
    direct_message_count: int
    room_message_count: int
    direct_message_receipt_count: int
    room_message_cursor_count: int
    task_count: int
    task_dependency_count: int
    delegation_count: int
    capability_request_count: int
    capability_grant_count: int
    artifact_count: int
    collaboration_request_count: int
    collaboration_response_count: int


@dataclass(frozen=True)
class CapabilityRequestRecord:
    request_id: str
    agent_id: str
    capability: str
    scope: RuntimeScope
    scope_id: str | None
    reason: str
    policy: CapabilityRequestPolicy
    status: CapabilityRequestStatus
    decision_reason: str | None
    created_at: float
    decided_at: float | None


@dataclass(frozen=True)
class CapabilityGrantRecord:
    grant_id: str
    request_id: str | None
    agent_id: str
    capability: str
    scope: RuntimeScope
    scope_id: str | None
    granted_by: str
    granted_at: float
    expires_at: float | None
    revoked_at: float | None
    revoked_by: str | None


@dataclass(frozen=True)
class ArtifactRecord:
    artifact_id: str
    owner_id: str
    project_id: str | None
    task_id: str | None
    room_id: str | None
    name: str
    media_type: str
    uri: str
    sha256: str
    size_bytes: int | None
    created_at: float


@dataclass(frozen=True)
class CollaborationRequestRecord:
    request_id: str
    requester_id: str
    target_agent_id: str | None
    project_id: str | None
    room_id: str | None
    task_id: str | None
    artifact_id: str | None
    kind: CollaborationKind
    prompt: str
    options: tuple[str, ...]
    status: CollaborationStatus
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class CollaborationResponseRecord:
    response_id: str
    request_id: str
    responder_id: str
    body: str
    selected_option: str | None
    evidence_artifact_id: str | None
    created_at: float


_ID_PREFIXES = frozenset(
    {
        "agt",
        "msg",
        "room",
        "task",
        "dlg",
        "evt",
        "cap",
        "art",
        "req",
        "rsp",
        "cur",
    }
)
_ID_RE = re.compile(r"^(agt|msg|room|task|dlg|evt|cap|art|req|rsp|cur)_[0-9a-f]{32}$")
_KNOWN_TABLES = frozenset(
    {
        "runtime_agents",
        "runtime_rooms",
        "runtime_room_members",
        "runtime_tasks",
        "runtime_task_dependencies",
        "runtime_direct_messages",
        "runtime_direct_message_receipts",
        "runtime_room_messages",
        "runtime_room_message_cursors",
        "runtime_task_delegations",
        "runtime_capability_requests",
        "runtime_capability_grants",
        "runtime_artifacts",
        "runtime_collaboration_requests",
        "runtime_collaboration_responses",
        "runtime_events",
        "sqlite_sequence",
    }
)


def _new_id(prefix: str) -> str:
    if prefix not in _ID_PREFIXES:
        raise ValueError("unknown runtime identifier prefix")
    return f"{prefix}_{uuid.uuid4().hex}"


def _canonical_json(value: Mapping[str, Any] | Sequence[Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _command_digest(command: str, payload: Mapping[str, Any]) -> str:
    document = {"command": command, "payload": payload}
    return hashlib.sha256(_canonical_json(document).encode("utf-8")).hexdigest()


def _required_text(name: str, value: str, *, maximum: int) -> str:
    normalized = str(value).strip()
    if not normalized:
        raise ValueError(f"{name} must not be empty")
    if len(normalized) > maximum:
        raise ValueError(f"{name} exceeds {maximum} characters")
    if "\x00" in normalized:
        raise ValueError(f"{name} contains a NUL character")
    return normalized


def _optional_text(name: str, value: str | None, *, maximum: int) -> str | None:
    if value is None:
        return None
    return _required_text(name, value, maximum=maximum)


def _identifier(value: str, prefix: str) -> str:
    candidate = str(value)
    if not _ID_RE.fullmatch(candidate) or not candidate.startswith(f"{prefix}_"):
        raise ValueError(f"invalid {prefix} identifier")
    return candidate


class MultiAgentRuntimeStore:
    """SQLite-backed projections and immutable events for peer runtime state."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        in_memory = str(self.path) == ":memory:"
        if not in_memory:
            from .sqlite_preflight import validate_database_path

            try:
                path_exists = validate_database_path(self.path)
            except OSError as exc:
                raise RuntimeStoreError(
                    "multi-agent runtime database could not be inspected safely"
                ) from exc
            if path_exists:
                self._preflight_existing_store()
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), timeout=30.0)
        try:
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute("PRAGMA busy_timeout=30000")
            self._migrate()
            if not in_memory:
                self.db.execute("PRAGMA journal_mode=WAL")
        except BaseException:
            self.db.close()
            raise

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        self.db.close()

    @staticmethod
    def _store_tables(db: sqlite3.Connection) -> frozenset[str]:
        return frozenset(
            str(row[0])
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        )

    @classmethod
    def _validate_store_authority(cls, db: sqlite3.Connection) -> None:
        application_id = int(db.execute("PRAGMA application_id").fetchone()[0])
        version = int(db.execute("PRAGMA user_version").fetchone()[0])
        tables = cls._store_tables(db)
        nonempty = bool(tables - {"sqlite_sequence"})
        if application_id not in {0, RUNTIME_APPLICATION_ID}:
            raise RuntimeStoreError(
                "multi-agent runtime database belongs to a different application"
            )
        if version > RUNTIME_SCHEMA_VERSION:
            raise RuntimeStoreError(
                "multi-agent runtime database schema is newer than this runtime"
            )
        if application_id == 0:
            if version != 0 or nonempty:
                raise RuntimeStoreError(
                    "existing unmarked database is not a multi-agent runtime store"
                )
            return
        if version != RUNTIME_SCHEMA_VERSION or tables != _KNOWN_TABLES:
            raise RuntimeStoreError(
                "multi-agent runtime database schema marker is invalid"
            )

    def _preflight_existing_store(self) -> None:
        try:
            from .sqlite_preflight import inspection_connection

            with inspection_connection(self.path) as db:
                self._validate_store_authority(db)
        except RuntimeStoreError:
            raise
        except (OSError, sqlite3.DatabaseError, TypeError, ValueError) as exc:
            raise RuntimeStoreError(
                "multi-agent runtime database could not be inspected safely"
            ) from exc

    def _migrate(self) -> None:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self._validate_store_authority(self.db)
            schema = """
                CREATE TABLE IF NOT EXISTS runtime_agents (
                    agent_id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    role TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    personality TEXT NOT NULL,
                    specialties_json TEXT NOT NULL,
                    model_provider TEXT NOT NULL,
                    model_name TEXT NOT NULL,
                    autonomy TEXT NOT NULL,
                    authority TEXT NOT NULL,
                    request_policy TEXT NOT NULL,
                    lifecycle TEXT NOT NULL,
                    project_id TEXT,
                    created_by TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_runtime_agents_discovery
                    ON runtime_agents(project_id, lifecycle, agent_id);

                CREATE TABLE IF NOT EXISTS runtime_rooms (
                    room_id TEXT PRIMARY KEY,
                    project_id TEXT,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    access TEXT NOT NULL,
                    created_by TEXT NOT NULL REFERENCES runtime_agents(agent_id),
                    state TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runtime_room_members (
                    room_id TEXT NOT NULL REFERENCES runtime_rooms(room_id),
                    agent_id TEXT NOT NULL REFERENCES runtime_agents(agent_id),
                    membership TEXT NOT NULL,
                    invited_by TEXT NOT NULL REFERENCES runtime_agents(agent_id),
                    invited_at REAL NOT NULL,
                    joined_at REAL,
                    left_at REAL,
                    PRIMARY KEY(room_id, agent_id)
                );

                CREATE TABLE IF NOT EXISTS runtime_tasks (
                    task_id TEXT PRIMARY KEY,
                    project_id TEXT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    owner_id TEXT NOT NULL REFERENCES runtime_agents(agent_id),
                    created_by TEXT NOT NULL REFERENCES runtime_agents(agent_id),
                    parent_task_id TEXT REFERENCES runtime_tasks(task_id),
                    status TEXT NOT NULL,
                    result TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_runtime_tasks_owner
                    ON runtime_tasks(owner_id, status, task_id);

                CREATE TABLE IF NOT EXISTS runtime_task_dependencies (
                    task_id TEXT NOT NULL REFERENCES runtime_tasks(task_id),
                    depends_on_task_id TEXT NOT NULL REFERENCES runtime_tasks(task_id),
                    added_by TEXT NOT NULL REFERENCES runtime_agents(agent_id),
                    created_at REAL NOT NULL,
                    PRIMARY KEY(task_id, depends_on_task_id),
                    CHECK(task_id <> depends_on_task_id)
                );
                CREATE INDEX IF NOT EXISTS idx_runtime_task_dependencies_reverse
                    ON runtime_task_dependencies(depends_on_task_id, task_id);

                CREATE TABLE IF NOT EXISTS runtime_direct_messages (
                    message_id TEXT PRIMARY KEY,
                    sender_id TEXT NOT NULL REFERENCES runtime_agents(agent_id),
                    recipient_id TEXT NOT NULL REFERENCES runtime_agents(agent_id),
                    body TEXT NOT NULL,
                    reply_to_message_id TEXT REFERENCES runtime_direct_messages(message_id),
                    task_id TEXT REFERENCES runtime_tasks(task_id),
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_runtime_direct_mailbox
                    ON runtime_direct_messages(recipient_id, created_at, message_id);

                CREATE TABLE IF NOT EXISTS runtime_direct_message_receipts (
                    message_id TEXT PRIMARY KEY
                        REFERENCES runtime_direct_messages(message_id),
                    recipient_id TEXT NOT NULL REFERENCES runtime_agents(agent_id),
                    delivered_at REAL NOT NULL,
                    read_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_runtime_direct_receipts_recipient
                    ON runtime_direct_message_receipts(recipient_id, read_at);

                CREATE TABLE IF NOT EXISTS runtime_room_messages (
                    message_id TEXT PRIMARY KEY,
                    room_id TEXT NOT NULL REFERENCES runtime_rooms(room_id),
                    sender_id TEXT NOT NULL REFERENCES runtime_agents(agent_id),
                    body TEXT NOT NULL,
                    reply_to_message_id TEXT REFERENCES runtime_room_messages(message_id),
                    task_id TEXT REFERENCES runtime_tasks(task_id),
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_runtime_room_messages
                    ON runtime_room_messages(room_id, created_at, message_id);

                CREATE TABLE IF NOT EXISTS runtime_room_message_cursors (
                    cursor_id TEXT PRIMARY KEY,
                    room_id TEXT NOT NULL REFERENCES runtime_rooms(room_id),
                    agent_id TEXT NOT NULL REFERENCES runtime_agents(agent_id),
                    last_message_id TEXT NOT NULL
                        REFERENCES runtime_room_messages(message_id),
                    last_event_sequence INTEGER NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(room_id, agent_id)
                );

                CREATE TABLE IF NOT EXISTS runtime_task_delegations (
                    delegation_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES runtime_tasks(task_id),
                    from_agent_id TEXT NOT NULL REFERENCES runtime_agents(agent_id),
                    to_agent_id TEXT NOT NULL REFERENCES runtime_agents(agent_id),
                    reason TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_runtime_delegations_task
                    ON runtime_task_delegations(task_id, created_at, delegation_id);

                CREATE TABLE IF NOT EXISTS runtime_capability_requests (
                    request_id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL REFERENCES runtime_agents(agent_id),
                    capability TEXT NOT NULL,
                    scope_kind TEXT NOT NULL,
                    scope_id TEXT,
                    reason TEXT NOT NULL,
                    policy TEXT NOT NULL,
                    status TEXT NOT NULL,
                    decision_reason TEXT,
                    created_at REAL NOT NULL,
                    decided_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_runtime_capability_requests
                    ON runtime_capability_requests(agent_id, status, created_at);

                CREATE TABLE IF NOT EXISTS runtime_capability_grants (
                    grant_id TEXT PRIMARY KEY,
                    request_id TEXT UNIQUE REFERENCES runtime_capability_requests(request_id),
                    agent_id TEXT NOT NULL REFERENCES runtime_agents(agent_id),
                    capability TEXT NOT NULL,
                    scope_kind TEXT NOT NULL,
                    scope_id TEXT,
                    granted_by TEXT NOT NULL,
                    granted_at REAL NOT NULL,
                    expires_at REAL,
                    revoked_at REAL,
                    revoked_by TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_runtime_capability_grants
                    ON runtime_capability_grants(
                        agent_id, capability, scope_kind, scope_id, expires_at
                    );

                CREATE TABLE IF NOT EXISTS runtime_artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL REFERENCES runtime_agents(agent_id),
                    project_id TEXT,
                    task_id TEXT REFERENCES runtime_tasks(task_id),
                    room_id TEXT REFERENCES runtime_rooms(room_id),
                    name TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    uri TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    size_bytes INTEGER,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_runtime_artifacts_task
                    ON runtime_artifacts(task_id, created_at, artifact_id);
                CREATE INDEX IF NOT EXISTS idx_runtime_artifacts_room
                    ON runtime_artifacts(room_id, created_at, artifact_id);

                CREATE TABLE IF NOT EXISTS runtime_collaboration_requests (
                    request_id TEXT PRIMARY KEY,
                    requester_id TEXT NOT NULL REFERENCES runtime_agents(agent_id),
                    target_agent_id TEXT REFERENCES runtime_agents(agent_id),
                    project_id TEXT,
                    room_id TEXT REFERENCES runtime_rooms(room_id),
                    task_id TEXT REFERENCES runtime_tasks(task_id),
                    artifact_id TEXT REFERENCES runtime_artifacts(artifact_id),
                    kind TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    options_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_runtime_collaboration_inbox
                    ON runtime_collaboration_requests(
                        target_agent_id, project_id, status, created_at, request_id
                    );

                CREATE TABLE IF NOT EXISTS runtime_collaboration_responses (
                    response_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL
                        REFERENCES runtime_collaboration_requests(request_id),
                    responder_id TEXT NOT NULL REFERENCES runtime_agents(agent_id),
                    body TEXT NOT NULL,
                    selected_option TEXT,
                    evidence_artifact_id TEXT REFERENCES runtime_artifacts(artifact_id),
                    created_at REAL NOT NULL,
                    UNIQUE(request_id, responder_id)
                );
                CREATE INDEX IF NOT EXISTS idx_runtime_collaboration_responses
                    ON runtime_collaboration_responses(
                        request_id, created_at, response_id
                    );

                CREATE TABLE IF NOT EXISTS runtime_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    schema_version INTEGER NOT NULL,
                    occurred_at REAL NOT NULL,
                    actor_id TEXT NOT NULL,
                    project_id TEXT,
                    scope_kind TEXT NOT NULL,
                    scope_id TEXT,
                    subject_kind TEXT NOT NULL,
                    subject_id TEXT NOT NULL,
                    correlation_id TEXT NOT NULL,
                    causation_id TEXT,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    command_sha256 TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    result_kind TEXT NOT NULL,
                    result_id TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_runtime_events_subject
                    ON runtime_events(subject_kind, subject_id, sequence);
                CREATE INDEX IF NOT EXISTS idx_runtime_events_correlation
                    ON runtime_events(correlation_id, sequence);
                """
            # sqlite3.executescript() commits an open transaction first. Execute
            # these fixed statements individually so marker validation, schema
            # creation, and marker writes remain one BEGIN IMMEDIATE transaction.
            for statement in schema.split(";"):
                if statement.strip():
                    self.db.execute(statement)
            # Execute triggers separately: their bodies contain semicolons.
            for operation in ("UPDATE", "DELETE"):
                self.db.execute(
                    f"""CREATE TRIGGER IF NOT EXISTS runtime_events_no_{operation.lower()}
                        BEFORE {operation} ON runtime_events
                        BEGIN SELECT RAISE(ABORT, 'runtime events are immutable'); END"""
                )
            # REPLACE can delete a conflicting row without firing DELETE triggers
            # when recursive_triggers is off. Reject conflicts before insertion.
            self.db.execute(
                """CREATE TRIGGER IF NOT EXISTS runtime_events_no_replace
                   BEFORE INSERT ON runtime_events
                   WHEN EXISTS (SELECT 1 FROM runtime_events
                                WHERE sequence=NEW.sequence OR event_id=NEW.event_id
                                   OR idempotency_key=NEW.idempotency_key)
                   BEGIN SELECT RAISE(ABORT, 'runtime events are immutable'); END"""
            )
            self.db.execute(f"PRAGMA application_id={RUNTIME_APPLICATION_ID}")
            self.db.execute(f"PRAGMA user_version={RUNTIME_SCHEMA_VERSION}")
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    @staticmethod
    def _agent_from_row(row: sqlite3.Row) -> AgentRecord:
        return AgentRecord(
            agent_id=str(row["agent_id"]),
            display_name=str(row["display_name"]),
            role=str(row["role"]),
            purpose=str(row["purpose"]),
            personality=str(row["personality"]),
            specialties=tuple(json.loads(str(row["specialties_json"]))),
            model_provider=str(row["model_provider"]),
            model_name=str(row["model_name"]),
            autonomy=AgentAutonomy(str(row["autonomy"])),
            authority=AgentAuthority(str(row["authority"])),
            request_policy=CapabilityRequestPolicy(str(row["request_policy"])),
            lifecycle=AgentLifecycle(str(row["lifecycle"])),
            project_id=None if row["project_id"] is None else str(row["project_id"]),
            created_by=str(row["created_by"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    @staticmethod
    def _direct_message_from_row(row: sqlite3.Row) -> DirectMessage:
        return DirectMessage(
            message_id=str(row["message_id"]),
            sender_id=str(row["sender_id"]),
            recipient_id=str(row["recipient_id"]),
            body=str(row["body"]),
            reply_to_message_id=(
                None
                if row["reply_to_message_id"] is None
                else str(row["reply_to_message_id"])
            ),
            task_id=None if row["task_id"] is None else str(row["task_id"]),
            created_at=float(row["created_at"]),
        )

    @staticmethod
    def _direct_message_receipt_from_row(row: sqlite3.Row) -> DirectMessageReceipt:
        return DirectMessageReceipt(
            message_id=str(row["message_id"]),
            recipient_id=str(row["recipient_id"]),
            delivered_at=float(row["delivered_at"]),
            read_at=None if row["read_at"] is None else float(row["read_at"]),
        )

    @staticmethod
    def _room_from_row(row: sqlite3.Row) -> RoomRecord:
        return RoomRecord(
            room_id=str(row["room_id"]),
            project_id=None if row["project_id"] is None else str(row["project_id"]),
            name=str(row["name"]),
            kind=str(row["kind"]),
            access=RoomAccess(str(row["access"])),
            created_by=str(row["created_by"]),
            state=str(row["state"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    @staticmethod
    def _room_message_from_row(row: sqlite3.Row) -> RoomMessage:
        return RoomMessage(
            message_id=str(row["message_id"]),
            room_id=str(row["room_id"]),
            sender_id=str(row["sender_id"]),
            body=str(row["body"]),
            reply_to_message_id=(
                None
                if row["reply_to_message_id"] is None
                else str(row["reply_to_message_id"])
            ),
            task_id=None if row["task_id"] is None else str(row["task_id"]),
            created_at=float(row["created_at"]),
        )

    @staticmethod
    def _room_message_cursor_from_row(row: sqlite3.Row) -> RoomMessageCursor:
        return RoomMessageCursor(
            cursor_id=str(row["cursor_id"]),
            room_id=str(row["room_id"]),
            agent_id=str(row["agent_id"]),
            last_message_id=str(row["last_message_id"]),
            last_event_sequence=int(row["last_event_sequence"]),
            updated_at=float(row["updated_at"]),
        )

    @staticmethod
    def _task_from_row(row: sqlite3.Row) -> TaskRecord:
        return TaskRecord(
            task_id=str(row["task_id"]),
            project_id=None if row["project_id"] is None else str(row["project_id"]),
            title=str(row["title"]),
            description=str(row["description"]),
            owner_id=str(row["owner_id"]),
            created_by=str(row["created_by"]),
            parent_task_id=(
                None if row["parent_task_id"] is None else str(row["parent_task_id"])
            ),
            status=TaskStatus(str(row["status"])),
            result=None if row["result"] is None else str(row["result"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    @staticmethod
    def _delegation_from_row(row: sqlite3.Row) -> DelegationRecord:
        return DelegationRecord(
            delegation_id=str(row["delegation_id"]),
            task_id=str(row["task_id"]),
            from_agent_id=str(row["from_agent_id"]),
            to_agent_id=str(row["to_agent_id"]),
            reason=str(row["reason"]),
            created_at=float(row["created_at"]),
        )

    @staticmethod
    def _capability_request_from_row(row: sqlite3.Row) -> CapabilityRequestRecord:
        return CapabilityRequestRecord(
            request_id=str(row["request_id"]),
            agent_id=str(row["agent_id"]),
            capability=str(row["capability"]),
            scope=RuntimeScope(str(row["scope_kind"])),
            scope_id=None if row["scope_id"] is None else str(row["scope_id"]),
            reason=str(row["reason"]),
            policy=CapabilityRequestPolicy(str(row["policy"])),
            status=CapabilityRequestStatus(str(row["status"])),
            decision_reason=(
                None if row["decision_reason"] is None else str(row["decision_reason"])
            ),
            created_at=float(row["created_at"]),
            decided_at=(
                None if row["decided_at"] is None else float(row["decided_at"])
            ),
        )

    @staticmethod
    def _capability_grant_from_row(row: sqlite3.Row) -> CapabilityGrantRecord:
        return CapabilityGrantRecord(
            grant_id=str(row["grant_id"]),
            request_id=None if row["request_id"] is None else str(row["request_id"]),
            agent_id=str(row["agent_id"]),
            capability=str(row["capability"]),
            scope=RuntimeScope(str(row["scope_kind"])),
            scope_id=None if row["scope_id"] is None else str(row["scope_id"]),
            granted_by=str(row["granted_by"]),
            granted_at=float(row["granted_at"]),
            expires_at=None if row["expires_at"] is None else float(row["expires_at"]),
            revoked_at=None if row["revoked_at"] is None else float(row["revoked_at"]),
            revoked_by=None if row["revoked_by"] is None else str(row["revoked_by"]),
        )

    @staticmethod
    def _artifact_from_row(row: sqlite3.Row) -> ArtifactRecord:
        return ArtifactRecord(
            artifact_id=str(row["artifact_id"]),
            owner_id=str(row["owner_id"]),
            project_id=None if row["project_id"] is None else str(row["project_id"]),
            task_id=None if row["task_id"] is None else str(row["task_id"]),
            room_id=None if row["room_id"] is None else str(row["room_id"]),
            name=str(row["name"]),
            media_type=str(row["media_type"]),
            uri=str(row["uri"]),
            sha256=str(row["sha256"]),
            size_bytes=None if row["size_bytes"] is None else int(row["size_bytes"]),
            created_at=float(row["created_at"]),
        )

    @staticmethod
    def _collaboration_request_from_row(
        row: sqlite3.Row,
    ) -> CollaborationRequestRecord:
        return CollaborationRequestRecord(
            request_id=str(row["request_id"]),
            requester_id=str(row["requester_id"]),
            target_agent_id=(
                None if row["target_agent_id"] is None else str(row["target_agent_id"])
            ),
            project_id=None if row["project_id"] is None else str(row["project_id"]),
            room_id=None if row["room_id"] is None else str(row["room_id"]),
            task_id=None if row["task_id"] is None else str(row["task_id"]),
            artifact_id=(
                None if row["artifact_id"] is None else str(row["artifact_id"])
            ),
            kind=CollaborationKind(str(row["kind"])),
            prompt=str(row["prompt"]),
            options=tuple(json.loads(str(row["options_json"]))),
            status=CollaborationStatus(str(row["status"])),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    @staticmethod
    def _collaboration_response_from_row(
        row: sqlite3.Row,
    ) -> CollaborationResponseRecord:
        return CollaborationResponseRecord(
            response_id=str(row["response_id"]),
            request_id=str(row["request_id"]),
            responder_id=str(row["responder_id"]),
            body=str(row["body"]),
            selected_option=(
                None if row["selected_option"] is None else str(row["selected_option"])
            ),
            evidence_artifact_id=(
                None
                if row["evidence_artifact_id"] is None
                else str(row["evidence_artifact_id"])
            ),
            created_at=float(row["created_at"]),
        )

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> RuntimeEvent:
        return RuntimeEvent(
            sequence=int(row["sequence"]),
            event_id=str(row["event_id"]),
            event_type=str(row["event_type"]),
            schema_version=int(row["schema_version"]),
            occurred_at=float(row["occurred_at"]),
            actor_id=str(row["actor_id"]),
            project_id=None if row["project_id"] is None else str(row["project_id"]),
            scope=RuntimeScope(str(row["scope_kind"])),
            scope_id=None if row["scope_id"] is None else str(row["scope_id"]),
            subject_kind=str(row["subject_kind"]),
            subject_id=str(row["subject_id"]),
            correlation_id=str(row["correlation_id"]),
            causation_id=(
                None if row["causation_id"] is None else str(row["causation_id"])
            ),
            idempotency_key=str(row["idempotency_key"]),
            command_sha256=str(row["command_sha256"]),
            payload=json.loads(str(row["payload_json"])),
            result_kind=str(row["result_kind"]),
            result_id=str(row["result_id"]),
        )

    def _replay_result_id(
        self,
        *,
        event_type: str,
        idempotency_key: str,
        command_sha256: str,
        result_kind: str,
    ) -> str | None:
        row = self.db.execute(
            "SELECT * FROM runtime_events WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if row is None:
            return None
        event = self._event_from_row(row)
        if (
            event.event_type != event_type
            or event.command_sha256 != command_sha256
            or event.result_kind != result_kind
        ):
            raise RuntimeConflictError(
                "idempotency key was already used for a different command"
            )
        return event.result_id

    def _append_event(
        self,
        *,
        event_type: str,
        actor_id: str,
        project_id: str | None,
        scope: RuntimeScope,
        scope_id: str | None,
        subject_kind: str,
        subject_id: str,
        idempotency_key: str,
        command_sha256: str,
        payload: Mapping[str, Any],
        result_kind: str,
        result_id: str,
        occurred_at: float,
        correlation_id: str | None,
        causation_id: str | None,
    ) -> RuntimeEvent:
        event_id = _new_id("evt")
        if correlation_id is not None:
            _identifier(correlation_id, "evt")
            if (
                self.db.execute(
                    "SELECT 1 FROM runtime_events WHERE event_id=?", (correlation_id,)
                ).fetchone()
                is None
            ):
                raise RuntimeNotFoundError("correlation event does not exist")
        if causation_id is not None:
            _identifier(causation_id, "evt")
            if (
                self.db.execute(
                    "SELECT 1 FROM runtime_events WHERE event_id=?", (causation_id,)
                ).fetchone()
                is None
            ):
                raise RuntimeNotFoundError("causation event does not exist")
        effective_correlation = correlation_id or event_id
        if _EVENT_RESULT_KINDS.get(event_type) != result_kind:
            raise RuntimeStoreError("unsupported runtime event vocabulary")
        table, id_column, fields = _EVENT_RECORDS[result_kind]
        record_row = self.db.execute(
            f"SELECT {', '.join(fields.split())} FROM {table} WHERE {id_column}=?",
            (result_id,),
        ).fetchone()
        if record_row is None:
            raise RuntimeStoreError("runtime event result has no content snapshot")
        record = dict(record_row)
        payload = {
            **payload,
            "record": record,
            "record_sha256": hashlib.sha256(
                _canonical_json(record).encode("utf-8")
            ).hexdigest(),
        }
        self.db.execute(
            """INSERT INTO runtime_events(
                   event_id, event_type, schema_version, occurred_at, actor_id,
                   project_id, scope_kind, scope_id, subject_kind, subject_id,
                   correlation_id, causation_id, idempotency_key, command_sha256,
                   payload_json, result_kind, result_id
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event_id,
                event_type,
                EVENT_SCHEMA_VERSION,
                occurred_at,
                actor_id,
                project_id,
                scope.value,
                scope_id,
                subject_kind,
                subject_id,
                effective_correlation,
                causation_id,
                idempotency_key,
                command_sha256,
                _canonical_json(payload),
                result_kind,
                result_id,
            ),
        )
        row = self.db.execute(
            "SELECT * FROM runtime_events WHERE event_id=?", (event_id,)
        ).fetchone()
        assert row is not None
        return self._event_from_row(row)

    @staticmethod
    def _command_key(value: str) -> str:
        return _required_text("idempotency_key", value, maximum=200)

    def _require_agent(self, agent_id: str) -> AgentRecord:
        _identifier(agent_id, "agt")
        row = self.db.execute(
            "SELECT * FROM runtime_agents WHERE agent_id=?", (agent_id,)
        ).fetchone()
        if row is None:
            raise RuntimeNotFoundError("agent does not exist")
        return self._agent_from_row(row)

    def _require_running_agent(self, agent_id: str) -> AgentRecord:
        agent = self._require_agent(agent_id)
        if agent.lifecycle is not AgentLifecycle.RUNNING:
            raise RuntimePermissionError("agent must be RUNNING for this operation")
        return agent

    def create_agent(
        self,
        *,
        display_name: str,
        role: str,
        purpose: str = "",
        personality: str = "",
        specialties: Sequence[str] = (),
        model_provider: str = "unbound",
        model_name: str = "unbound",
        autonomy: AgentAutonomy = AgentAutonomy.NORMAL,
        authority: AgentAuthority = AgentAuthority.STANDARD,
        request_policy: CapabilityRequestPolicy = CapabilityRequestPolicy.ASK_OWNER,
        project_id: str | None = None,
        created_by: str = "owner",
        idempotency_key: str,
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> AgentRecord:
        display_name = _required_text("display_name", display_name, maximum=120)
        role = _required_text("role", role, maximum=200)
        purpose = str(purpose).strip()
        personality = str(personality).strip()
        if len(purpose) > 4000 or len(personality) > 4000:
            raise ValueError("purpose and personality must not exceed 4000 characters")
        normalized_specialties = tuple(
            sorted(
                {
                    _required_text("specialty", value, maximum=100).casefold()
                    for value in specialties
                }
            )
        )
        provider = _required_text("model_provider", model_provider, maximum=100)
        model = _required_text("model_name", model_name, maximum=200)
        project_id = _optional_text("project_id", project_id, maximum=200)
        created_by = _required_text("created_by", created_by, maximum=200)
        key = self._command_key(idempotency_key)
        payload = {
            "display_name": display_name,
            "role": role,
            "purpose": purpose,
            "personality": personality,
            "specialties": list(normalized_specialties),
            "model_provider": provider,
            "model_name": model,
            "autonomy": autonomy.value,
            "authority": authority.value,
            "request_policy": request_policy.value,
            "project_id": project_id,
            "created_by": created_by,
        }
        digest = _command_digest("agent.created", payload)
        with self._transaction():
            replay = self._replay_result_id(
                event_type="agent.created",
                idempotency_key=key,
                command_sha256=digest,
                result_kind="agent",
            )
            if replay is not None:
                return self._require_agent(replay)
            agent_id = _new_id("agt")
            now = time.time()
            self.db.execute(
                """INSERT INTO runtime_agents(
                       agent_id, display_name, role, purpose, personality,
                       specialties_json, model_provider, model_name, autonomy,
                       authority, request_policy, lifecycle, project_id, created_by,
                       created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    agent_id,
                    display_name,
                    role,
                    purpose,
                    personality,
                    _canonical_json(list(normalized_specialties)),
                    provider,
                    model,
                    autonomy.value,
                    authority.value,
                    request_policy.value,
                    AgentLifecycle.CREATED.value,
                    project_id,
                    created_by,
                    now,
                    now,
                ),
            )
            self._append_event(
                event_type="agent.created",
                actor_id=created_by,
                project_id=project_id,
                scope=RuntimeScope.PROJECT if project_id else RuntimeScope.GLOBAL,
                scope_id=project_id,
                subject_kind="agent",
                subject_id=agent_id,
                idempotency_key=key,
                command_sha256=digest,
                payload={
                    "display_name": display_name,
                    "role": role,
                    "specialties": list(normalized_specialties),
                    "autonomy": autonomy.value,
                    "authority": authority.value,
                    "request_policy": request_policy.value,
                },
                result_kind="agent",
                result_id=agent_id,
                occurred_at=now,
                correlation_id=correlation_id,
                causation_id=causation_id,
            )
            return self._require_agent(agent_id)

    def get_agent(self, agent_id: str) -> AgentRecord:
        return self._require_agent(agent_id)

    def bind_agent(self, agent_id: str) -> AgentRuntimeContext:
        self._require_agent(agent_id)
        return AgentRuntimeContext(self, agent_id)

    def set_agent_lifecycle(
        self,
        agent_id: str,
        lifecycle: AgentLifecycle,
        *,
        actor_id: str,
        idempotency_key: str,
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> AgentRecord:
        _identifier(agent_id, "agt")
        if actor_id not in {agent_id, "owner"}:
            raise RuntimePermissionError("only the agent or owner may change lifecycle")
        key = self._command_key(idempotency_key)
        payload = {
            "agent_id": agent_id,
            "lifecycle": lifecycle.value,
            "actor_id": actor_id,
        }
        event_type = f"agent.{lifecycle.value.casefold()}"
        digest = _command_digest(event_type, payload)
        allowed = {
            AgentLifecycle.CREATED: {AgentLifecycle.RUNNING, AgentLifecycle.STOPPED},
            AgentLifecycle.RUNNING: {AgentLifecycle.PAUSED, AgentLifecycle.STOPPED},
            AgentLifecycle.PAUSED: {AgentLifecycle.RUNNING, AgentLifecycle.STOPPED},
            AgentLifecycle.STOPPED: {AgentLifecycle.RUNNING},
        }
        with self._transaction():
            replay = self._replay_result_id(
                event_type=event_type,
                idempotency_key=key,
                command_sha256=digest,
                result_kind="agent",
            )
            if replay is not None:
                return self._require_agent(replay)
            agent = self._require_agent(agent_id)
            if lifecycle not in allowed[agent.lifecycle]:
                raise RuntimeConflictError(
                    f"invalid lifecycle transition {agent.lifecycle.value} -> {lifecycle.value}"
                )
            now = time.time()
            self.db.execute(
                "UPDATE runtime_agents SET lifecycle=?, updated_at=? WHERE agent_id=?",
                (lifecycle.value, now, agent_id),
            )
            self._append_event(
                event_type=event_type,
                actor_id=actor_id,
                project_id=agent.project_id,
                scope=RuntimeScope.AGENT,
                scope_id=agent_id,
                subject_kind="agent",
                subject_id=agent_id,
                idempotency_key=key,
                command_sha256=digest,
                payload={"from": agent.lifecycle.value, "to": lifecycle.value},
                result_kind="agent",
                result_id=agent_id,
                occurred_at=now,
                correlation_id=correlation_id,
                causation_id=causation_id,
            )
            return self._require_agent(agent_id)

    def find_agents(
        self,
        requester_id: str,
        *,
        specialty: str | None = None,
        lifecycle: AgentLifecycle = AgentLifecycle.RUNNING,
        include_self: bool = False,
    ) -> tuple[AgentRecord, ...]:
        requester = self._require_agent(requester_id)
        clauses = ["lifecycle=?"]
        params: list[Any] = [lifecycle.value]
        if not include_self:
            clauses.append("agent_id<>?")
            params.append(requester_id)
        if requester.project_id is None:
            clauses.append("project_id IS NULL")
        else:
            clauses.append("(project_id IS NULL OR project_id=?)")
            params.append(requester.project_id)
        rows = self.db.execute(
            f"SELECT * FROM runtime_agents WHERE {' AND '.join(clauses)} ORDER BY agent_id",
            params,
        ).fetchall()
        agents = tuple(self._agent_from_row(row) for row in rows)
        if specialty is None:
            return agents
        needle = _required_text("specialty", specialty, maximum=100).casefold()
        return tuple(agent for agent in agents if needle in agent.specialties)

    def update_agent_model(
        self,
        agent_id: str,
        *,
        actor_id: str,
        model_provider: str,
        model_name: str,
        idempotency_key: str,
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> AgentRecord:
        _identifier(agent_id, "agt")
        if actor_id not in {agent_id, "owner"}:
            raise RuntimePermissionError("only the agent or owner may change its model")
        provider = _required_text("model_provider", model_provider, maximum=100)
        model = _required_text("model_name", model_name, maximum=200)
        key = self._command_key(idempotency_key)
        payload = {
            "agent_id": agent_id,
            "actor_id": actor_id,
            "model_provider": provider,
            "model_name": model,
        }
        digest = _command_digest("agent.model_changed", payload)
        with self._transaction():
            replay = self._replay_result_id(
                event_type="agent.model_changed",
                idempotency_key=key,
                command_sha256=digest,
                result_kind="agent",
            )
            if replay is not None:
                return self._require_agent(replay)
            agent = self._require_agent(agent_id)
            now = time.time()
            self.db.execute(
                """UPDATE runtime_agents
                   SET model_provider=?, model_name=?, updated_at=?
                   WHERE agent_id=?""",
                (provider, model, now, agent_id),
            )
            self._append_event(
                event_type="agent.model_changed",
                actor_id=actor_id,
                project_id=agent.project_id,
                scope=RuntimeScope.AGENT,
                scope_id=agent_id,
                subject_kind="agent",
                subject_id=agent_id,
                idempotency_key=key,
                command_sha256=digest,
                payload={
                    "from_provider": agent.model_provider,
                    "from_model": agent.model_name,
                    "to_provider": provider,
                    "to_model": model,
                },
                result_kind="agent",
                result_id=agent_id,
                occurred_at=now,
                correlation_id=correlation_id,
                causation_id=causation_id,
            )
            return self._require_agent(agent_id)

    def update_agent_policy(
        self,
        agent_id: str,
        *,
        actor_id: str,
        autonomy: AgentAutonomy,
        authority: AgentAuthority,
        request_policy: CapabilityRequestPolicy | None = None,
        idempotency_key: str,
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> AgentRecord:
        _identifier(agent_id, "agt")
        key = self._command_key(idempotency_key)
        requested_policy = request_policy
        payload = {
            "agent_id": agent_id,
            "actor_id": actor_id,
            "autonomy": autonomy.value,
            "authority": authority.value,
            "request_policy": (
                None if requested_policy is None else requested_policy.value
            ),
        }
        digest = _command_digest("agent.policy_changed", payload)
        with self._transaction():
            replay = self._replay_result_id(
                event_type="agent.policy_changed",
                idempotency_key=key,
                command_sha256=digest,
                result_kind="agent",
            )
            if replay is not None:
                return self._require_agent(replay)
            if actor_id != "owner":
                actor = self._require_agent(actor_id)
                if actor.authority is not AgentAuthority.OWNER:
                    raise RuntimePermissionError(
                        "only the owner authority may change agent policy"
                    )
            agent = self._require_agent(agent_id)
            effective_policy = requested_policy or agent.request_policy
            now = time.time()
            self.db.execute(
                """UPDATE runtime_agents
                   SET autonomy=?, authority=?, request_policy=?, updated_at=?
                   WHERE agent_id=?""",
                (
                    autonomy.value,
                    authority.value,
                    effective_policy.value,
                    now,
                    agent_id,
                ),
            )
            self._append_event(
                event_type="agent.policy_changed",
                actor_id=actor_id,
                project_id=agent.project_id,
                scope=RuntimeScope.AGENT,
                scope_id=agent_id,
                subject_kind="agent",
                subject_id=agent_id,
                idempotency_key=key,
                command_sha256=digest,
                payload={
                    "from_autonomy": agent.autonomy.value,
                    "from_authority": agent.authority.value,
                    "from_request_policy": agent.request_policy.value,
                    "to_autonomy": autonomy.value,
                    "to_authority": authority.value,
                    "to_request_policy": effective_policy.value,
                },
                result_kind="agent",
                result_id=agent_id,
                occurred_at=now,
                correlation_id=correlation_id,
                causation_id=causation_id,
            )
            return self._require_agent(agent_id)

    def send_direct_message(
        self,
        *,
        sender_id: str,
        recipient_id: str,
        body: str,
        idempotency_key: str,
        reply_to_message_id: str | None = None,
        task_id: str | None = None,
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> DirectMessage:
        _identifier(sender_id, "agt")
        _identifier(recipient_id, "agt")
        if sender_id == recipient_id:
            raise RuntimeConflictError("direct messages require a distinct recipient")
        body = _required_text("body", body, maximum=100_000)
        if reply_to_message_id is not None:
            _identifier(reply_to_message_id, "msg")
        if task_id is not None:
            _identifier(task_id, "task")
        key = self._command_key(idempotency_key)
        payload = {
            "sender_id": sender_id,
            "recipient_id": recipient_id,
            "body": body,
            "reply_to_message_id": reply_to_message_id,
            "task_id": task_id,
        }
        digest = _command_digest("message.direct_sent", payload)
        with self._transaction():
            replay = self._replay_result_id(
                event_type="message.direct_sent",
                idempotency_key=key,
                command_sha256=digest,
                result_kind="direct_message",
            )
            if replay is not None:
                return self.get_direct_message(replay)
            sender = self._require_running_agent(sender_id)
            recipient = self._require_agent(recipient_id)
            if recipient.project_id not in {None, sender.project_id}:
                raise RuntimePermissionError("recipient belongs to another project")
            if task_id is not None:
                self.get_task(task_id)
            if reply_to_message_id is not None:
                parent = self.get_direct_message(reply_to_message_id)
                if {parent.sender_id, parent.recipient_id} != {sender_id, recipient_id}:
                    raise RuntimePermissionError("reply participants do not match")
            message_id = _new_id("msg")
            now = time.time()
            self.db.execute(
                """INSERT INTO runtime_direct_messages(
                       message_id, sender_id, recipient_id, body,
                       reply_to_message_id, task_id, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    message_id,
                    sender_id,
                    recipient_id,
                    body,
                    reply_to_message_id,
                    task_id,
                    now,
                ),
            )
            self._append_event(
                event_type="message.direct_sent",
                actor_id=sender_id,
                project_id=sender.project_id,
                scope=RuntimeScope.PRIVATE,
                scope_id=recipient_id,
                subject_kind="direct_message",
                subject_id=message_id,
                idempotency_key=key,
                command_sha256=digest,
                payload={
                    "sender_id": sender_id,
                    "recipient_id": recipient_id,
                    "reply_to_message_id": reply_to_message_id,
                    "task_id": task_id,
                    "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
                },
                result_kind="direct_message",
                result_id=message_id,
                occurred_at=now,
                correlation_id=correlation_id,
                causation_id=causation_id,
            )
            return self.get_direct_message(message_id)

    def get_direct_message(self, message_id: str) -> DirectMessage:
        _identifier(message_id, "msg")
        row = self.db.execute(
            "SELECT * FROM runtime_direct_messages WHERE message_id=?", (message_id,)
        ).fetchone()
        if row is None:
            raise RuntimeNotFoundError("direct message does not exist")
        return self._direct_message_from_row(row)

    def mailbox(
        self, requester_id: str, *, include_sent: bool = False, limit: int = 200
    ) -> tuple[DirectMessage, ...]:
        self._require_agent(requester_id)
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        # UUIDs identify messages, not their order. Creation events commit with
        # messages and provide a durable tie-breaker when timestamps coincide.
        if include_sent:
            rows = self.db.execute(
                """SELECT message.* FROM runtime_direct_messages AS message
                   JOIN runtime_events AS event
                     ON event.subject_kind='direct_message'
                    AND event.subject_id=message.message_id
                    AND event.event_type='message.direct_sent'
                   WHERE message.recipient_id=? OR message.sender_id=?
                   ORDER BY message.created_at, event.sequence LIMIT ?""",
                (requester_id, requester_id, limit),
            ).fetchall()
        else:
            rows = self.db.execute(
                """SELECT message.* FROM runtime_direct_messages AS message
                   JOIN runtime_events AS event
                     ON event.subject_kind='direct_message'
                    AND event.subject_id=message.message_id
                    AND event.event_type='message.direct_sent'
                   WHERE message.recipient_id=?
                   ORDER BY message.created_at, event.sequence LIMIT ?""",
                (requester_id, limit),
            ).fetchall()
        return tuple(self._direct_message_from_row(row) for row in rows)

    def acknowledge_direct_message(
        self,
        *,
        recipient_id: str,
        message_id: str,
        read: bool,
        idempotency_key: str,
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> DirectMessageReceipt:
        _identifier(recipient_id, "agt")
        _identifier(message_id, "msg")
        if not isinstance(read, bool):
            raise TypeError("read must be a boolean")
        key = self._command_key(idempotency_key)
        payload = {
            "recipient_id": recipient_id,
            "message_id": message_id,
            "read": read,
        }
        digest = _command_digest("message.direct_acknowledged", payload)
        with self._transaction():
            replay = self._replay_result_id(
                event_type="message.direct_acknowledged",
                idempotency_key=key,
                command_sha256=digest,
                result_kind="direct_message_receipt",
            )
            if replay is not None:
                return self.get_direct_message_receipt(replay)
            recipient = self._require_running_agent(recipient_id)
            message = self.get_direct_message(message_id)
            if message.recipient_id != recipient_id:
                raise RuntimePermissionError(
                    "only the direct-message recipient may acknowledge it"
                )
            existing = self.db.execute(
                """SELECT * FROM runtime_direct_message_receipts
                   WHERE message_id=?""",
                (message_id,),
            ).fetchone()
            now = time.time()
            if existing is None:
                self.db.execute(
                    """INSERT INTO runtime_direct_message_receipts(
                           message_id, recipient_id, delivered_at, read_at
                       ) VALUES (?, ?, ?, ?)""",
                    (message_id, recipient_id, now, now if read else None),
                )
                previous = "UNACKNOWLEDGED"
            else:
                receipt = self._direct_message_receipt_from_row(existing)
                if receipt.read_at is not None or not read:
                    raise RuntimeConflictError(
                        "direct message already has that acknowledgement"
                    )
                self.db.execute(
                    """UPDATE runtime_direct_message_receipts
                       SET read_at=? WHERE message_id=?""",
                    (now, message_id),
                )
                previous = "DELIVERED"
            self._append_event(
                event_type="message.direct_acknowledged",
                actor_id=recipient_id,
                project_id=recipient.project_id,
                scope=RuntimeScope.PRIVATE,
                scope_id=recipient_id,
                subject_kind="direct_message",
                subject_id=message_id,
                idempotency_key=key,
                command_sha256=digest,
                payload={
                    "from": previous,
                    "to": "READ" if read else "DELIVERED",
                },
                result_kind="direct_message_receipt",
                result_id=message_id,
                occurred_at=now,
                correlation_id=correlation_id,
                causation_id=causation_id,
            )
            return self.get_direct_message_receipt(message_id)

    def get_direct_message_receipt(self, message_id: str) -> DirectMessageReceipt:
        _identifier(message_id, "msg")
        row = self.db.execute(
            """SELECT * FROM runtime_direct_message_receipts
               WHERE message_id=?""",
            (message_id,),
        ).fetchone()
        if row is None:
            raise RuntimeNotFoundError("direct message has no delivery receipt")
        return self._direct_message_receipt_from_row(row)

    def unread_direct_messages(
        self, recipient_id: str, *, limit: int = 200
    ) -> tuple[DirectMessage, ...]:
        self._require_agent(recipient_id)
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        rows = self.db.execute(
            """SELECT message.* FROM runtime_direct_messages AS message
               JOIN runtime_events AS event
                 ON event.subject_kind='direct_message'
                AND event.subject_id=message.message_id
                AND event.event_type='message.direct_sent'
               LEFT JOIN runtime_direct_message_receipts AS receipt
                 ON receipt.message_id=message.message_id
               WHERE message.recipient_id=? AND receipt.read_at IS NULL
               ORDER BY message.created_at, event.sequence LIMIT ?""",
            (recipient_id, limit),
        ).fetchall()
        return tuple(self._direct_message_from_row(row) for row in rows)

    def create_room(
        self,
        *,
        creator_id: str,
        name: str,
        kind: str = "collaboration",
        access: RoomAccess = RoomAccess.INVITE_ONLY,
        project_id: str | None = None,
        idempotency_key: str,
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> RoomRecord:
        _identifier(creator_id, "agt")
        name = _required_text("name", name, maximum=200)
        kind = _required_text("kind", kind, maximum=100).casefold()
        project_id = _optional_text("project_id", project_id, maximum=200)
        key = self._command_key(idempotency_key)
        payload = {
            "creator_id": creator_id,
            "name": name,
            "kind": kind,
            "access": access.value,
            "project_id": project_id,
        }
        digest = _command_digest("room.created", payload)
        with self._transaction():
            replay = self._replay_result_id(
                event_type="room.created",
                idempotency_key=key,
                command_sha256=digest,
                result_kind="room",
            )
            if replay is not None:
                return self.get_room(replay)
            creator = self._require_running_agent(creator_id)
            effective_project = (
                project_id if project_id is not None else creator.project_id
            )
            if (
                creator.project_id is not None
                and effective_project is not None
                and effective_project != creator.project_id
            ):
                raise RuntimePermissionError(
                    "agent cannot create a room in another project"
                )
            room_id = _new_id("room")
            now = time.time()
            self.db.execute(
                """INSERT INTO runtime_rooms(
                       room_id, project_id, name, kind, access, created_by, state,
                       created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?)""",
                (
                    room_id,
                    effective_project,
                    name,
                    kind,
                    access.value,
                    creator_id,
                    now,
                    now,
                ),
            )
            self.db.execute(
                """INSERT INTO runtime_room_members(
                       room_id, agent_id, membership, invited_by, invited_at,
                       joined_at, left_at
                   ) VALUES (?, ?, 'JOINED', ?, ?, ?, NULL)""",
                (room_id, creator_id, creator_id, now, now),
            )
            self._append_event(
                event_type="room.created",
                actor_id=creator_id,
                project_id=effective_project,
                scope=RuntimeScope.ROOM,
                scope_id=room_id,
                subject_kind="room",
                subject_id=room_id,
                idempotency_key=key,
                command_sha256=digest,
                payload={"name": name, "kind": kind, "access": access.value},
                result_kind="room",
                result_id=room_id,
                occurred_at=now,
                correlation_id=correlation_id,
                causation_id=causation_id,
            )
            return self.get_room(room_id)

    def get_room(self, room_id: str) -> RoomRecord:
        _identifier(room_id, "room")
        row = self.db.execute(
            "SELECT * FROM runtime_rooms WHERE room_id=?", (room_id,)
        ).fetchone()
        if row is None:
            raise RuntimeNotFoundError("room does not exist")
        return self._room_from_row(row)

    def _room_membership(self, room_id: str, agent_id: str) -> str | None:
        row = self.db.execute(
            """SELECT membership FROM runtime_room_members
               WHERE room_id=? AND agent_id=?""",
            (room_id, agent_id),
        ).fetchone()
        return None if row is None else str(row["membership"])

    def invite_agent(
        self,
        *,
        room_id: str,
        inviter_id: str,
        invitee_id: str,
        idempotency_key: str,
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> RoomRecord:
        _identifier(room_id, "room")
        _identifier(inviter_id, "agt")
        _identifier(invitee_id, "agt")
        if inviter_id == invitee_id:
            raise RuntimeConflictError("an agent cannot invite itself")
        key = self._command_key(idempotency_key)
        payload = {
            "room_id": room_id,
            "inviter_id": inviter_id,
            "invitee_id": invitee_id,
        }
        digest = _command_digest("room.agent_invited", payload)
        with self._transaction():
            replay = self._replay_result_id(
                event_type="room.agent_invited",
                idempotency_key=key,
                command_sha256=digest,
                result_kind="room",
            )
            if replay is not None:
                return self.get_room(replay)
            self._require_running_agent(inviter_id)
            invitee = self._require_agent(invitee_id)
            room = self.get_room(room_id)
            if room.state != "ACTIVE":
                raise RuntimeConflictError("room is not active")
            if self._room_membership(room_id, inviter_id) != "JOINED":
                raise RuntimePermissionError("inviter is not a joined room member")
            if room.project_id is not None and invitee.project_id not in {
                None,
                room.project_id,
            }:
                raise RuntimePermissionError("invitee belongs to another project")
            current = self._room_membership(room_id, invitee_id)
            if current in {"INVITED", "JOINED"}:
                raise RuntimeConflictError("agent is already invited or joined")
            now = time.time()
            self.db.execute(
                """INSERT INTO runtime_room_members(
                       room_id, agent_id, membership, invited_by, invited_at,
                       joined_at, left_at
                   ) VALUES (?, ?, 'INVITED', ?, ?, NULL, NULL)
                   ON CONFLICT(room_id, agent_id) DO UPDATE SET
                       membership='INVITED', invited_by=excluded.invited_by,
                       invited_at=excluded.invited_at, joined_at=NULL, left_at=NULL""",
                (room_id, invitee_id, inviter_id, now),
            )
            self._append_event(
                event_type="room.agent_invited",
                actor_id=inviter_id,
                project_id=room.project_id,
                scope=RuntimeScope.ROOM,
                scope_id=room_id,
                subject_kind="agent",
                subject_id=invitee_id,
                idempotency_key=key,
                command_sha256=digest,
                payload={"room_id": room_id, "invitee_id": invitee_id},
                result_kind="room",
                result_id=room_id,
                occurred_at=now,
                correlation_id=correlation_id,
                causation_id=causation_id,
            )
            return room

    def join_room(
        self,
        *,
        room_id: str,
        agent_id: str,
        idempotency_key: str,
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> RoomRecord:
        _identifier(room_id, "room")
        _identifier(agent_id, "agt")
        key = self._command_key(idempotency_key)
        payload = {"room_id": room_id, "agent_id": agent_id}
        digest = _command_digest("room.agent_joined", payload)
        with self._transaction():
            replay = self._replay_result_id(
                event_type="room.agent_joined",
                idempotency_key=key,
                command_sha256=digest,
                result_kind="room",
            )
            if replay is not None:
                return self.get_room(replay)
            agent = self._require_running_agent(agent_id)
            room = self.get_room(room_id)
            if room.project_id is not None and agent.project_id not in {
                None,
                room.project_id,
            }:
                raise RuntimePermissionError("agent belongs to another project")
            current = self._room_membership(room_id, agent_id)
            if current == "JOINED":
                raise RuntimeConflictError("agent is already joined")
            if room.access is RoomAccess.INVITE_ONLY and current != "INVITED":
                raise RuntimePermissionError("invite-only room requires an invitation")
            now = time.time()
            inviter = room.created_by
            self.db.execute(
                """INSERT INTO runtime_room_members(
                       room_id, agent_id, membership, invited_by, invited_at,
                       joined_at, left_at
                   ) VALUES (?, ?, 'JOINED', ?, ?, ?, NULL)
                   ON CONFLICT(room_id, agent_id) DO UPDATE SET
                       membership='JOINED', joined_at=excluded.joined_at, left_at=NULL""",
                (room_id, agent_id, inviter, now, now),
            )
            self._append_event(
                event_type="room.agent_joined",
                actor_id=agent_id,
                project_id=room.project_id,
                scope=RuntimeScope.ROOM,
                scope_id=room_id,
                subject_kind="agent",
                subject_id=agent_id,
                idempotency_key=key,
                command_sha256=digest,
                payload={"room_id": room_id},
                result_kind="room",
                result_id=room_id,
                occurred_at=now,
                correlation_id=correlation_id,
                causation_id=causation_id,
            )
            return room

    def room_members(self, requester_id: str, room_id: str) -> tuple[AgentRecord, ...]:
        self._require_agent(requester_id)
        self.get_room(room_id)
        if self._room_membership(room_id, requester_id) != "JOINED":
            raise RuntimePermissionError("requester is not a joined room member")
        rows = self.db.execute(
            """SELECT a.* FROM runtime_agents AS a
               JOIN runtime_room_members AS m ON m.agent_id=a.agent_id
               WHERE m.room_id=? AND m.membership='JOINED'
               ORDER BY a.agent_id""",
            (room_id,),
        ).fetchall()
        return tuple(self._agent_from_row(row) for row in rows)

    def leave_room(
        self,
        *,
        room_id: str,
        agent_id: str,
        idempotency_key: str,
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> RoomRecord:
        _identifier(room_id, "room")
        _identifier(agent_id, "agt")
        key = self._command_key(idempotency_key)
        payload = {"room_id": room_id, "agent_id": agent_id}
        digest = _command_digest("room.agent_left", payload)
        with self._transaction():
            replay = self._replay_result_id(
                event_type="room.agent_left",
                idempotency_key=key,
                command_sha256=digest,
                result_kind="room",
            )
            if replay is not None:
                return self.get_room(replay)
            self._require_agent(agent_id)
            room = self.get_room(room_id)
            if self._room_membership(room_id, agent_id) != "JOINED":
                raise RuntimePermissionError("agent is not a joined room member")
            now = time.time()
            self.db.execute(
                """UPDATE runtime_room_members
                   SET membership='LEFT', left_at=?
                   WHERE room_id=? AND agent_id=?""",
                (now, room_id, agent_id),
            )
            self._append_event(
                event_type="room.agent_left",
                actor_id=agent_id,
                project_id=room.project_id,
                scope=RuntimeScope.ROOM,
                scope_id=room_id,
                subject_kind="agent",
                subject_id=agent_id,
                idempotency_key=key,
                command_sha256=digest,
                payload={"room_id": room_id},
                result_kind="room",
                result_id=room_id,
                occurred_at=now,
                correlation_id=correlation_id,
                causation_id=causation_id,
            )
            return room

    def send_room_message(
        self,
        *,
        room_id: str,
        sender_id: str,
        body: str,
        idempotency_key: str,
        reply_to_message_id: str | None = None,
        task_id: str | None = None,
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> RoomMessage:
        _identifier(room_id, "room")
        _identifier(sender_id, "agt")
        body = _required_text("body", body, maximum=100_000)
        if reply_to_message_id is not None:
            _identifier(reply_to_message_id, "msg")
        if task_id is not None:
            _identifier(task_id, "task")
        key = self._command_key(idempotency_key)
        payload = {
            "room_id": room_id,
            "sender_id": sender_id,
            "body": body,
            "reply_to_message_id": reply_to_message_id,
            "task_id": task_id,
        }
        digest = _command_digest("room.message_sent", payload)
        with self._transaction():
            replay = self._replay_result_id(
                event_type="room.message_sent",
                idempotency_key=key,
                command_sha256=digest,
                result_kind="room_message",
            )
            if replay is not None:
                return self.get_room_message(replay)
            self._require_running_agent(sender_id)
            room = self.get_room(room_id)
            if self._room_membership(room_id, sender_id) != "JOINED":
                raise RuntimePermissionError("sender is not a joined room member")
            if task_id is not None:
                self.get_task(task_id)
            if reply_to_message_id is not None:
                parent = self.get_room_message(reply_to_message_id)
                if parent.room_id != room_id:
                    raise RuntimePermissionError("reply belongs to a different room")
            message_id = _new_id("msg")
            now = time.time()
            self.db.execute(
                """INSERT INTO runtime_room_messages(
                       message_id, room_id, sender_id, body,
                       reply_to_message_id, task_id, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    message_id,
                    room_id,
                    sender_id,
                    body,
                    reply_to_message_id,
                    task_id,
                    now,
                ),
            )
            self._append_event(
                event_type="room.message_sent",
                actor_id=sender_id,
                project_id=room.project_id,
                scope=RuntimeScope.ROOM,
                scope_id=room_id,
                subject_kind="room_message",
                subject_id=message_id,
                idempotency_key=key,
                command_sha256=digest,
                payload={
                    "room_id": room_id,
                    "reply_to_message_id": reply_to_message_id,
                    "task_id": task_id,
                    "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
                },
                result_kind="room_message",
                result_id=message_id,
                occurred_at=now,
                correlation_id=correlation_id,
                causation_id=causation_id,
            )
            return self.get_room_message(message_id)

    def get_room_message(self, message_id: str) -> RoomMessage:
        _identifier(message_id, "msg")
        row = self.db.execute(
            "SELECT * FROM runtime_room_messages WHERE message_id=?", (message_id,)
        ).fetchone()
        if row is None:
            raise RuntimeNotFoundError("room message does not exist")
        return self._room_message_from_row(row)

    def room_messages(
        self, requester_id: str, room_id: str, *, limit: int = 500
    ) -> tuple[RoomMessage, ...]:
        self._require_agent(requester_id)
        self.get_room(room_id)
        if self._room_membership(room_id, requester_id) != "JOINED":
            raise RuntimePermissionError("requester is not a joined room member")
        if limit < 1 or limit > 2000:
            raise ValueError("limit must be between 1 and 2000")
        rows = self.db.execute(
            """SELECT message.* FROM runtime_room_messages AS message
               JOIN runtime_events AS event
                 ON event.subject_kind='room_message'
                AND event.subject_id=message.message_id
                AND event.event_type='room.message_sent'
               WHERE message.room_id=?
               ORDER BY message.created_at, event.sequence LIMIT ?""",
            (room_id, limit),
        ).fetchall()
        return tuple(self._room_message_from_row(row) for row in rows)

    def advance_room_message_cursor(
        self,
        *,
        agent_id: str,
        room_id: str,
        through_message_id: str,
        idempotency_key: str,
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> RoomMessageCursor:
        _identifier(agent_id, "agt")
        _identifier(room_id, "room")
        _identifier(through_message_id, "msg")
        key = self._command_key(idempotency_key)
        payload = {
            "agent_id": agent_id,
            "room_id": room_id,
            "through_message_id": through_message_id,
        }
        digest = _command_digest("room.cursor_advanced", payload)
        with self._transaction():
            replay = self._replay_result_id(
                event_type="room.cursor_advanced",
                idempotency_key=key,
                command_sha256=digest,
                result_kind="room_message_cursor",
            )
            if replay is not None:
                return self.get_room_message_cursor(replay)
            self._require_running_agent(agent_id)
            room = self.get_room(room_id)
            if self._room_membership(room_id, agent_id) != "JOINED":
                raise RuntimePermissionError("agent is not a joined room member")
            message = self.get_room_message(through_message_id)
            if message.room_id != room_id:
                raise RuntimePermissionError("cursor message belongs to another room")
            event_row = self.db.execute(
                """SELECT sequence FROM runtime_events
                   WHERE event_type='room.message_sent'
                     AND result_kind='room_message' AND result_id=?""",
                (through_message_id,),
            ).fetchone()
            if event_row is None:
                raise RuntimeStoreError("room message has no creation event")
            event_sequence = int(event_row["sequence"])
            existing_row = self.db.execute(
                """SELECT * FROM runtime_room_message_cursors
                   WHERE room_id=? AND agent_id=?""",
                (room_id, agent_id),
            ).fetchone()
            now = time.time()
            if existing_row is None:
                cursor_id = _new_id("cur")
                previous_sequence = 0
                self.db.execute(
                    """INSERT INTO runtime_room_message_cursors(
                           cursor_id, room_id, agent_id, last_message_id,
                           last_event_sequence, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        cursor_id,
                        room_id,
                        agent_id,
                        through_message_id,
                        event_sequence,
                        now,
                    ),
                )
            else:
                existing = self._room_message_cursor_from_row(existing_row)
                cursor_id = existing.cursor_id
                previous_sequence = existing.last_event_sequence
                if event_sequence <= previous_sequence:
                    raise RuntimeConflictError(
                        "room message cursor cannot move backward"
                    )
                self.db.execute(
                    """UPDATE runtime_room_message_cursors
                       SET last_message_id=?, last_event_sequence=?, updated_at=?
                       WHERE cursor_id=?""",
                    (through_message_id, event_sequence, now, cursor_id),
                )
            self._append_event(
                event_type="room.cursor_advanced",
                actor_id=agent_id,
                project_id=room.project_id,
                scope=RuntimeScope.ROOM,
                scope_id=room_id,
                subject_kind="room_message_cursor",
                subject_id=cursor_id,
                idempotency_key=key,
                command_sha256=digest,
                payload={
                    "from_event_sequence": previous_sequence,
                    "to_event_sequence": event_sequence,
                    "through_message_id": through_message_id,
                },
                result_kind="room_message_cursor",
                result_id=cursor_id,
                occurred_at=now,
                correlation_id=correlation_id,
                causation_id=causation_id,
            )
            return self.get_room_message_cursor(cursor_id)

    def get_room_message_cursor(self, cursor_id: str) -> RoomMessageCursor:
        _identifier(cursor_id, "cur")
        row = self.db.execute(
            "SELECT * FROM runtime_room_message_cursors WHERE cursor_id=?",
            (cursor_id,),
        ).fetchone()
        if row is None:
            raise RuntimeNotFoundError("room message cursor does not exist")
        return self._room_message_cursor_from_row(row)

    def unread_room_messages(
        self, agent_id: str, room_id: str, *, limit: int = 500
    ) -> tuple[RoomMessage, ...]:
        self._require_agent(agent_id)
        self.get_room(room_id)
        if self._room_membership(room_id, agent_id) != "JOINED":
            raise RuntimePermissionError("agent is not a joined room member")
        if limit < 1 or limit > 2000:
            raise ValueError("limit must be between 1 and 2000")
        cursor_row = self.db.execute(
            """SELECT last_event_sequence FROM runtime_room_message_cursors
               WHERE room_id=? AND agent_id=?""",
            (room_id, agent_id),
        ).fetchone()
        last_sequence = 0 if cursor_row is None else int(cursor_row[0])
        rows = self.db.execute(
            """SELECT message.*
               FROM runtime_room_messages AS message
               JOIN runtime_events AS event
                 ON event.result_kind='room_message'
                AND event.result_id=message.message_id
                AND event.event_type='room.message_sent'
               WHERE message.room_id=? AND event.sequence>?
               ORDER BY event.sequence LIMIT ?""",
            (room_id, last_sequence, limit),
        ).fetchall()
        return tuple(self._room_message_from_row(row) for row in rows)

    def create_task(
        self,
        *,
        creator_id: str,
        title: str,
        description: str,
        idempotency_key: str,
        owner_id: str | None = None,
        project_id: str | None = None,
        parent_task_id: str | None = None,
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> TaskRecord:
        _identifier(creator_id, "agt")
        owner_id = creator_id if owner_id is None else _identifier(owner_id, "agt")
        title = _required_text("title", title, maximum=300)
        description = _required_text("description", description, maximum=20_000)
        project_id = _optional_text("project_id", project_id, maximum=200)
        if parent_task_id is not None:
            _identifier(parent_task_id, "task")
        key = self._command_key(idempotency_key)
        payload = {
            "creator_id": creator_id,
            "owner_id": owner_id,
            "title": title,
            "description": description,
            "project_id": project_id,
            "parent_task_id": parent_task_id,
        }
        digest = _command_digest("task.created", payload)
        with self._transaction():
            replay = self._replay_result_id(
                event_type="task.created",
                idempotency_key=key,
                command_sha256=digest,
                result_kind="task",
            )
            if replay is not None:
                return self.get_task(replay)
            creator = self._require_running_agent(creator_id)
            owner = self._require_agent(owner_id)
            effective_project = (
                project_id if project_id is not None else creator.project_id
            )
            if owner.project_id not in {None, effective_project}:
                raise RuntimePermissionError("task owner belongs to another project")
            if parent_task_id is not None:
                parent = self.get_task(parent_task_id)
                if parent.owner_id != creator_id:
                    raise RuntimePermissionError(
                        "only the parent owner may create a child task"
                    )
                if effective_project is None:
                    effective_project = parent.project_id
                elif parent.project_id != effective_project:
                    raise RuntimeConflictError("parent task belongs to another project")
            task_id = _new_id("task")
            now = time.time()
            initial_status = (
                TaskStatus.OPEN if owner_id == creator_id else TaskStatus.ASSIGNED
            )
            self.db.execute(
                """INSERT INTO runtime_tasks(
                       task_id, project_id, title, description, owner_id, created_by,
                       parent_task_id, status, result, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)""",
                (
                    task_id,
                    effective_project,
                    title,
                    description,
                    owner_id,
                    creator_id,
                    parent_task_id,
                    initial_status.value,
                    now,
                    now,
                ),
            )
            self._append_event(
                event_type="task.created",
                actor_id=creator_id,
                project_id=effective_project,
                scope=RuntimeScope.TASK,
                scope_id=task_id,
                subject_kind="task",
                subject_id=task_id,
                idempotency_key=key,
                command_sha256=digest,
                payload={
                    "title": title,
                    "owner_id": owner_id,
                    "parent_task_id": parent_task_id,
                    "status": initial_status.value,
                },
                result_kind="task",
                result_id=task_id,
                occurred_at=now,
                correlation_id=correlation_id,
                causation_id=causation_id,
            )
            return self.get_task(task_id)

    def get_task(self, task_id: str) -> TaskRecord:
        _identifier(task_id, "task")
        row = self.db.execute(
            "SELECT * FROM runtime_tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        if row is None:
            raise RuntimeNotFoundError("task does not exist")
        return self._task_from_row(row)

    def _task_reaches(self, start_task_id: str, target_task_id: str) -> bool:
        row = self.db.execute(
            """WITH RECURSIVE dependency_chain(task_id) AS (
                   SELECT depends_on_task_id
                   FROM runtime_task_dependencies
                   WHERE task_id=?
                   UNION
                   SELECT d.depends_on_task_id
                   FROM runtime_task_dependencies AS d
                   JOIN dependency_chain AS c ON d.task_id=c.task_id
               )
               SELECT 1 FROM dependency_chain WHERE task_id=? LIMIT 1""",
            (start_task_id, target_task_id),
        ).fetchone()
        return row is not None

    def add_task_dependency(
        self,
        *,
        task_id: str,
        depends_on_task_id: str,
        actor_id: str,
        idempotency_key: str,
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> TaskRecord:
        _identifier(task_id, "task")
        _identifier(depends_on_task_id, "task")
        _identifier(actor_id, "agt")
        if task_id == depends_on_task_id:
            raise RuntimeConflictError("task cannot depend on itself")
        key = self._command_key(idempotency_key)
        payload = {
            "task_id": task_id,
            "depends_on_task_id": depends_on_task_id,
            "actor_id": actor_id,
        }
        digest = _command_digest("task.dependency_added", payload)
        with self._transaction():
            replay = self._replay_result_id(
                event_type="task.dependency_added",
                idempotency_key=key,
                command_sha256=digest,
                result_kind="task",
            )
            if replay is not None:
                return self.get_task(replay)
            self._require_running_agent(actor_id)
            task = self.get_task(task_id)
            dependency = self.get_task(depends_on_task_id)
            if task.owner_id != actor_id:
                raise RuntimePermissionError(
                    "only the current task owner may add a dependency"
                )
            if task.status in {
                TaskStatus.COMPLETED,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            }:
                raise RuntimeConflictError("terminal task cannot gain a dependency")
            if task.status is TaskStatus.RUNNING:
                raise RuntimeConflictError(
                    "running task cannot gain a dependency; block it first"
                )
            if task.project_id != dependency.project_id:
                raise RuntimeConflictError("task dependency belongs to another project")
            if (
                self.db.execute(
                    """SELECT 1 FROM runtime_task_dependencies
                   WHERE task_id=? AND depends_on_task_id=?""",
                    (task_id, depends_on_task_id),
                ).fetchone()
                is not None
            ):
                raise RuntimeConflictError("task dependency already exists")
            if self._task_reaches(depends_on_task_id, task_id):
                raise RuntimeConflictError("task dependency would create a cycle")
            now = time.time()
            self.db.execute(
                """INSERT INTO runtime_task_dependencies(
                       task_id, depends_on_task_id, added_by, created_at
                   ) VALUES (?, ?, ?, ?)""",
                (task_id, depends_on_task_id, actor_id, now),
            )
            self._append_event(
                event_type="task.dependency_added",
                actor_id=actor_id,
                project_id=task.project_id,
                scope=RuntimeScope.TASK,
                scope_id=task_id,
                subject_kind="task",
                subject_id=task_id,
                idempotency_key=key,
                command_sha256=digest,
                payload={"depends_on_task_id": depends_on_task_id},
                result_kind="task",
                result_id=task_id,
                occurred_at=now,
                correlation_id=correlation_id,
                causation_id=causation_id,
            )
            return task

    def task_dependencies(
        self, requester_id: str, task_id: str
    ) -> tuple[TaskRecord, ...]:
        requester = self._require_agent(requester_id)
        task = self.get_task(task_id)
        if requester.project_id not in {None, task.project_id}:
            raise RuntimePermissionError("task belongs to another project")
        rows = self.db.execute(
            """SELECT t.* FROM runtime_tasks AS t
               JOIN runtime_task_dependencies AS d
                 ON d.depends_on_task_id=t.task_id
               WHERE d.task_id=? ORDER BY t.created_at, t.task_id""",
            (task_id,),
        ).fetchall()
        return tuple(self._task_from_row(row) for row in rows)

    def delegate_task(
        self,
        *,
        task_id: str,
        from_agent_id: str,
        to_agent_id: str,
        reason: str,
        idempotency_key: str,
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> DelegationRecord:
        _identifier(task_id, "task")
        _identifier(from_agent_id, "agt")
        _identifier(to_agent_id, "agt")
        if from_agent_id == to_agent_id:
            raise RuntimeConflictError("task delegation requires a distinct recipient")
        reason = _required_text("reason", reason, maximum=4000)
        key = self._command_key(idempotency_key)
        payload = {
            "task_id": task_id,
            "from_agent_id": from_agent_id,
            "to_agent_id": to_agent_id,
            "reason": reason,
        }
        digest = _command_digest("task.delegated", payload)
        with self._transaction():
            replay = self._replay_result_id(
                event_type="task.delegated",
                idempotency_key=key,
                command_sha256=digest,
                result_kind="delegation",
            )
            if replay is not None:
                return self.get_delegation(replay)
            self._require_running_agent(from_agent_id)
            recipient = self._require_agent(to_agent_id)
            task = self.get_task(task_id)
            if task.owner_id != from_agent_id:
                raise RuntimePermissionError(
                    "only the current task owner may delegate it"
                )
            if task.status in {
                TaskStatus.COMPLETED,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            }:
                raise RuntimeConflictError("terminal task cannot be delegated")
            if recipient.project_id not in {None, task.project_id}:
                raise RuntimePermissionError("recipient belongs to another project")
            delegation_id = _new_id("dlg")
            now = time.time()
            self.db.execute(
                """UPDATE runtime_tasks
                   SET owner_id=?, status=?, updated_at=? WHERE task_id=?""",
                (to_agent_id, TaskStatus.ASSIGNED.value, now, task_id),
            )
            self.db.execute(
                """INSERT INTO runtime_task_delegations(
                       delegation_id, task_id, from_agent_id, to_agent_id, reason,
                       created_at
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (delegation_id, task_id, from_agent_id, to_agent_id, reason, now),
            )
            self._append_event(
                event_type="task.delegated",
                actor_id=from_agent_id,
                project_id=task.project_id,
                scope=RuntimeScope.TASK,
                scope_id=task_id,
                subject_kind="task",
                subject_id=task_id,
                idempotency_key=key,
                command_sha256=digest,
                payload={
                    "from_agent_id": from_agent_id,
                    "to_agent_id": to_agent_id,
                    "delegation_id": delegation_id,
                    "reason": reason,
                },
                result_kind="delegation",
                result_id=delegation_id,
                occurred_at=now,
                correlation_id=correlation_id,
                causation_id=causation_id,
            )
            return self.get_delegation(delegation_id)

    def get_delegation(self, delegation_id: str) -> DelegationRecord:
        _identifier(delegation_id, "dlg")
        row = self.db.execute(
            "SELECT * FROM runtime_task_delegations WHERE delegation_id=?",
            (delegation_id,),
        ).fetchone()
        if row is None:
            raise RuntimeNotFoundError("task delegation does not exist")
        return self._delegation_from_row(row)

    def update_task_status(
        self,
        *,
        task_id: str,
        actor_id: str,
        status: TaskStatus,
        idempotency_key: str,
        result: str | None = None,
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> TaskRecord:
        _identifier(task_id, "task")
        _identifier(actor_id, "agt")
        result = _optional_text("result", result, maximum=100_000)
        key = self._command_key(idempotency_key)
        payload = {
            "task_id": task_id,
            "actor_id": actor_id,
            "status": status.value,
            "result": result,
        }
        event_type = f"task.{status.value.casefold()}"
        digest = _command_digest(event_type, payload)
        allowed = {
            TaskStatus.OPEN: {
                TaskStatus.RUNNING,
                TaskStatus.BLOCKED,
                TaskStatus.CANCELLED,
            },
            TaskStatus.ASSIGNED: {
                TaskStatus.RUNNING,
                TaskStatus.BLOCKED,
                TaskStatus.CANCELLED,
            },
            TaskStatus.RUNNING: {
                TaskStatus.BLOCKED,
                TaskStatus.COMPLETED,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            },
            TaskStatus.BLOCKED: {
                TaskStatus.RUNNING,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            },
            TaskStatus.COMPLETED: set(),
            TaskStatus.FAILED: set(),
            TaskStatus.CANCELLED: set(),
        }
        with self._transaction():
            replay = self._replay_result_id(
                event_type=event_type,
                idempotency_key=key,
                command_sha256=digest,
                result_kind="task",
            )
            if replay is not None:
                return self.get_task(replay)
            self._require_running_agent(actor_id)
            task = self.get_task(task_id)
            if task.owner_id != actor_id:
                raise RuntimePermissionError(
                    "only the current task owner may update it"
                )
            if status not in allowed[task.status]:
                raise RuntimeConflictError(
                    f"invalid task transition {task.status.value} -> {status.value}"
                )
            if status in {TaskStatus.RUNNING, TaskStatus.COMPLETED}:
                unfinished = self.db.execute(
                    """SELECT d.depends_on_task_id
                       FROM runtime_task_dependencies AS d
                       JOIN runtime_tasks AS dependency
                         ON dependency.task_id=d.depends_on_task_id
                       WHERE d.task_id=? AND dependency.status<>'COMPLETED'
                       LIMIT 1""",
                    (task_id,),
                ).fetchone()
                if unfinished is not None:
                    raise RuntimeConflictError("task has an unfinished dependency")
            if status is TaskStatus.COMPLETED and result is None:
                raise RuntimeConflictError("completed task requires a result")
            if (
                status not in {TaskStatus.COMPLETED, TaskStatus.FAILED}
                and result is not None
            ):
                raise RuntimeConflictError(
                    "result is accepted only for completed or failed tasks"
                )
            now = time.time()
            self.db.execute(
                """UPDATE runtime_tasks SET status=?, result=?, updated_at=?
                   WHERE task_id=?""",
                (status.value, result, now, task_id),
            )
            self._append_event(
                event_type=event_type,
                actor_id=actor_id,
                project_id=task.project_id,
                scope=RuntimeScope.TASK,
                scope_id=task_id,
                subject_kind="task",
                subject_id=task_id,
                idempotency_key=key,
                command_sha256=digest,
                payload={
                    "from": task.status.value,
                    "to": status.value,
                    "result_sha256": (
                        None
                        if result is None
                        else hashlib.sha256(result.encode("utf-8")).hexdigest()
                    ),
                },
                result_kind="task",
                result_id=task_id,
                occurred_at=now,
                correlation_id=correlation_id,
                causation_id=causation_id,
            )
            return self.get_task(task_id)

    def list_agent_tasks(
        self, requester_id: str, *, statuses: Sequence[TaskStatus] = ()
    ) -> tuple[TaskRecord, ...]:
        self._require_agent(requester_id)
        params: list[Any] = [requester_id]
        where = "owner_id=?"
        if statuses:
            where += f" AND status IN ({','.join('?' for _ in statuses)})"
            params.extend(status.value for status in statuses)
        rows = self.db.execute(
            f"SELECT * FROM runtime_tasks WHERE {where} ORDER BY created_at, task_id",
            params,
        ).fetchall()
        return tuple(self._task_from_row(row) for row in rows)

    @staticmethod
    def _scope_target(scope: RuntimeScope, scope_id: str | None) -> str | None:
        if scope is RuntimeScope.GLOBAL:
            if scope_id is not None:
                raise ValueError("GLOBAL capability scope must not have a scope_id")
            return None
        return _required_text("scope_id", str(scope_id or ""), maximum=200)

    def _require_owner_authority(self, actor_id: str) -> None:
        if actor_id == "owner":
            return
        actor = self._require_agent(actor_id)
        if actor.authority is not AgentAuthority.OWNER:
            raise RuntimePermissionError(
                "only the owner authority may decide capability grants"
            )

    def request_capability(
        self,
        *,
        agent_id: str,
        capability: str,
        scope: RuntimeScope,
        scope_id: str | None,
        reason: str,
        idempotency_key: str,
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> CapabilityRequestRecord:
        _identifier(agent_id, "agt")
        capability = _required_text("capability", capability, maximum=200).casefold()
        effective_scope_id = self._scope_target(scope, scope_id)
        reason = _required_text("reason", reason, maximum=4000)
        key = self._command_key(idempotency_key)
        payload = {
            "agent_id": agent_id,
            "capability": capability,
            "scope": scope.value,
            "scope_id": effective_scope_id,
            "reason": reason,
        }
        digest = _command_digest("capability.requested", payload)
        with self._transaction():
            replay = self._replay_result_id(
                event_type="capability.requested",
                idempotency_key=key,
                command_sha256=digest,
                result_kind="capability_request",
            )
            if replay is not None:
                return self.get_capability_request(replay)
            agent = self._require_running_agent(agent_id)
            if scope is RuntimeScope.AGENT:
                self._require_agent(_identifier(str(effective_scope_id), "agt"))
            elif scope is RuntimeScope.TASK:
                self.get_task(_identifier(str(effective_scope_id), "task"))
            elif scope is RuntimeScope.ROOM:
                self.get_room(_identifier(str(effective_scope_id), "room"))
            elif (
                scope is RuntimeScope.PROJECT and agent.project_id != effective_scope_id
            ):
                raise RuntimePermissionError(
                    "agent may request only its own project scope"
                )
            auto_grant = (
                agent.request_policy is CapabilityRequestPolicy.AUTO_PROJECT
                and scope is RuntimeScope.PROJECT
                and effective_scope_id == agent.project_id
            ) or (
                agent.request_policy is CapabilityRequestPolicy.OWNER_AUTO
                and agent.authority is AgentAuthority.OWNER
            )
            if agent.request_policy is CapabilityRequestPolicy.DENY:
                status = CapabilityRequestStatus.DENIED
                decision_reason = "denied by the agent request policy"
            elif auto_grant:
                status = CapabilityRequestStatus.GRANTED
                decision_reason = "granted by the agent request policy"
            else:
                # AUTO_TRUSTED remains pending until a deterministic trust input
                # is supplied by a later relationship/reputation slice.
                status = CapabilityRequestStatus.PENDING
                decision_reason = None
            request_id = _new_id("cap")
            now = time.time()
            self.db.execute(
                """INSERT INTO runtime_capability_requests(
                       request_id, agent_id, capability, scope_kind, scope_id,
                       reason, policy, status, decision_reason, created_at,
                       decided_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    request_id,
                    agent_id,
                    capability,
                    scope.value,
                    effective_scope_id,
                    reason,
                    agent.request_policy.value,
                    status.value,
                    decision_reason,
                    now,
                    now if status is not CapabilityRequestStatus.PENDING else None,
                ),
            )
            request_event = self._append_event(
                event_type="capability.requested",
                actor_id=agent_id,
                project_id=agent.project_id,
                scope=scope,
                scope_id=effective_scope_id,
                subject_kind="capability_request",
                subject_id=request_id,
                idempotency_key=key,
                command_sha256=digest,
                payload={
                    "capability": capability,
                    "policy": agent.request_policy.value,
                    "status": status.value,
                },
                result_kind="capability_request",
                result_id=request_id,
                occurred_at=now,
                correlation_id=correlation_id,
                causation_id=causation_id,
            )
            if auto_grant:
                grant_id = _new_id("cap")
                self.db.execute(
                    """INSERT INTO runtime_capability_grants(
                           grant_id, request_id, agent_id, capability, scope_kind,
                           scope_id, granted_by, granted_at, expires_at, revoked_at,
                           revoked_by
                       ) VALUES (?, ?, ?, ?, ?, ?, 'system', ?, NULL, NULL, NULL)""",
                    (
                        grant_id,
                        request_id,
                        agent_id,
                        capability,
                        scope.value,
                        effective_scope_id,
                        now,
                    ),
                )
                auto_payload = {
                    "request_id": request_id,
                    "agent_id": agent_id,
                    "capability": capability,
                    "policy": agent.request_policy.value,
                    "grant_id": grant_id,
                }
                self._append_event(
                    event_type="capability.granted",
                    actor_id="system",
                    project_id=agent.project_id,
                    scope=scope,
                    scope_id=effective_scope_id,
                    subject_kind="capability_request",
                    subject_id=request_id,
                    idempotency_key=f"auto:{request_id}",
                    command_sha256=_command_digest(
                        "capability.auto_granted", auto_payload
                    ),
                    payload=auto_payload,
                    result_kind="capability_grant",
                    result_id=grant_id,
                    occurred_at=now,
                    correlation_id=request_event.correlation_id,
                    causation_id=request_event.event_id,
                )
            return self.get_capability_request(request_id)

    def get_capability_request(self, request_id: str) -> CapabilityRequestRecord:
        _identifier(request_id, "cap")
        row = self.db.execute(
            "SELECT * FROM runtime_capability_requests WHERE request_id=?",
            (request_id,),
        ).fetchone()
        if row is None:
            raise RuntimeNotFoundError("capability request does not exist")
        return self._capability_request_from_row(row)

    def decide_capability_request(
        self,
        request_id: str,
        *,
        actor_id: str,
        grant: bool,
        decision_reason: str,
        idempotency_key: str,
        expires_at: float | None = None,
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> CapabilityRequestRecord | CapabilityGrantRecord:
        _identifier(request_id, "cap")
        decision_reason = _required_text(
            "decision_reason", decision_reason, maximum=4000
        )
        if expires_at is not None:
            expires_at = float(expires_at)
        key = self._command_key(idempotency_key)
        event_type = "capability.granted" if grant else "capability.denied"
        result_kind = "capability_grant" if grant else "capability_request"
        payload = {
            "request_id": request_id,
            "actor_id": actor_id,
            "grant": grant,
            "decision_reason": decision_reason,
            "expires_at": expires_at,
        }
        digest = _command_digest(event_type, payload)
        with self._transaction():
            replay = self._replay_result_id(
                event_type=event_type,
                idempotency_key=key,
                command_sha256=digest,
                result_kind=result_kind,
            )
            if replay is not None:
                if grant:
                    return self.get_capability_grant(replay)
                return self.get_capability_request(replay)
            self._require_owner_authority(actor_id)
            request = self.get_capability_request(request_id)
            if request.status is not CapabilityRequestStatus.PENDING:
                raise RuntimeConflictError("capability request is already decided")
            now = time.time()
            if grant and expires_at is not None and expires_at <= now:
                raise RuntimeConflictError("capability grant expiration must be future")
            next_status = (
                CapabilityRequestStatus.GRANTED
                if grant
                else CapabilityRequestStatus.DENIED
            )
            self.db.execute(
                """UPDATE runtime_capability_requests
                   SET status=?, decision_reason=?, decided_at=?
                   WHERE request_id=?""",
                (next_status.value, decision_reason, now, request_id),
            )
            result_id = request_id
            grant_id: str | None = None
            if grant:
                grant_id = _new_id("cap")
                result_id = grant_id
                self.db.execute(
                    """INSERT INTO runtime_capability_grants(
                           grant_id, request_id, agent_id, capability, scope_kind,
                           scope_id, granted_by, granted_at, expires_at, revoked_at,
                           revoked_by
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)""",
                    (
                        grant_id,
                        request_id,
                        request.agent_id,
                        request.capability,
                        request.scope.value,
                        request.scope_id,
                        actor_id,
                        now,
                        expires_at,
                    ),
                )
            agent = self._require_agent(request.agent_id)
            self._append_event(
                event_type=event_type,
                actor_id=actor_id,
                project_id=agent.project_id,
                scope=request.scope,
                scope_id=request.scope_id,
                subject_kind="capability_request",
                subject_id=request_id,
                idempotency_key=key,
                command_sha256=digest,
                payload={
                    "agent_id": request.agent_id,
                    "capability": request.capability,
                    "decision_reason": decision_reason,
                    "expires_at": expires_at,
                    "grant_id": grant_id,
                },
                result_kind=result_kind,
                result_id=result_id,
                occurred_at=now,
                correlation_id=correlation_id,
                causation_id=causation_id,
            )
            if grant:
                assert grant_id is not None
                return self.get_capability_grant(grant_id)
            return self.get_capability_request(request_id)

    def get_capability_grant(self, grant_id: str) -> CapabilityGrantRecord:
        _identifier(grant_id, "cap")
        row = self.db.execute(
            "SELECT * FROM runtime_capability_grants WHERE grant_id=?", (grant_id,)
        ).fetchone()
        if row is None:
            raise RuntimeNotFoundError("capability grant does not exist")
        return self._capability_grant_from_row(row)

    def revoke_capability(
        self,
        grant_id: str,
        *,
        actor_id: str,
        idempotency_key: str,
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> CapabilityGrantRecord:
        _identifier(grant_id, "cap")
        key = self._command_key(idempotency_key)
        payload = {"grant_id": grant_id, "actor_id": actor_id}
        digest = _command_digest("capability.revoked", payload)
        with self._transaction():
            replay = self._replay_result_id(
                event_type="capability.revoked",
                idempotency_key=key,
                command_sha256=digest,
                result_kind="capability_grant",
            )
            if replay is not None:
                return self.get_capability_grant(replay)
            self._require_owner_authority(actor_id)
            grant = self.get_capability_grant(grant_id)
            if grant.revoked_at is not None:
                raise RuntimeConflictError("capability grant is already revoked")
            now = time.time()
            self.db.execute(
                """UPDATE runtime_capability_grants
                   SET revoked_at=?, revoked_by=? WHERE grant_id=?""",
                (now, actor_id, grant_id),
            )
            agent = self._require_agent(grant.agent_id)
            self._append_event(
                event_type="capability.revoked",
                actor_id=actor_id,
                project_id=agent.project_id,
                scope=grant.scope,
                scope_id=grant.scope_id,
                subject_kind="capability_grant",
                subject_id=grant_id,
                idempotency_key=key,
                command_sha256=digest,
                payload={
                    "agent_id": grant.agent_id,
                    "capability": grant.capability,
                },
                result_kind="capability_grant",
                result_id=grant_id,
                occurred_at=now,
                correlation_id=correlation_id,
                causation_id=causation_id,
            )
            return self.get_capability_grant(grant_id)

    def effective_capabilities(
        self,
        agent_id: str,
        *,
        scope: RuntimeScope,
        scope_id: str | None,
        at: float | None = None,
    ) -> tuple[CapabilityGrantRecord, ...]:
        self._require_agent(agent_id)
        effective_scope_id = self._scope_target(scope, scope_id)
        now = time.time() if at is None else float(at)
        rows = self.db.execute(
            """SELECT * FROM runtime_capability_grants
               WHERE agent_id=? AND revoked_at IS NULL
                 AND (expires_at IS NULL OR expires_at>?)
                 AND (
                     scope_kind='GLOBAL'
                     OR (scope_kind=? AND scope_id IS ?)
                 )
               ORDER BY capability, grant_id""",
            (agent_id, now, scope.value, effective_scope_id),
        ).fetchall()
        return tuple(self._capability_grant_from_row(row) for row in rows)

    def has_capability(
        self,
        agent_id: str,
        capability: str,
        *,
        scope: RuntimeScope,
        scope_id: str | None,
        at: float | None = None,
    ) -> bool:
        needle = _required_text("capability", capability, maximum=200).casefold()
        return any(
            grant.capability == needle
            for grant in self.effective_capabilities(
                agent_id, scope=scope, scope_id=scope_id, at=at
            )
        )

    def share_artifact(
        self,
        *,
        owner_id: str,
        name: str,
        media_type: str,
        uri: str,
        sha256: str,
        idempotency_key: str,
        size_bytes: int | None = None,
        task_id: str | None = None,
        room_id: str | None = None,
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> ArtifactRecord:
        _identifier(owner_id, "agt")
        name = _required_text("name", name, maximum=300)
        media_type = _required_text("media_type", media_type, maximum=200).casefold()
        uri = _required_text("uri", uri, maximum=4000)
        sha256 = _required_text("sha256", sha256, maximum=64).casefold()
        if not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ValueError("sha256 must contain 64 lowercase hexadecimal characters")
        if size_bytes is not None:
            size_bytes = int(size_bytes)
            if size_bytes < 0:
                raise ValueError("size_bytes must not be negative")
        if task_id is not None:
            _identifier(task_id, "task")
        if room_id is not None:
            _identifier(room_id, "room")
        key = self._command_key(idempotency_key)
        payload = {
            "owner_id": owner_id,
            "name": name,
            "media_type": media_type,
            "uri": uri,
            "sha256": sha256,
            "size_bytes": size_bytes,
            "task_id": task_id,
            "room_id": room_id,
        }
        digest = _command_digest("artifact.shared", payload)
        with self._transaction():
            replay = self._replay_result_id(
                event_type="artifact.shared",
                idempotency_key=key,
                command_sha256=digest,
                result_kind="artifact",
            )
            if replay is not None:
                return self.get_artifact(replay)
            owner = self._require_running_agent(owner_id)
            project_id = owner.project_id
            task: TaskRecord | None = None
            room: RoomRecord | None = None
            if task_id is not None:
                task = self.get_task(task_id)
                if task.owner_id != owner_id:
                    raise RuntimePermissionError(
                        "only the current task owner may share its artifact"
                    )
                project_id = task.project_id
            if room_id is not None:
                room = self.get_room(room_id)
                if self._room_membership(room_id, owner_id) != "JOINED":
                    raise RuntimePermissionError(
                        "artifact owner is not a joined room member"
                    )
                if task is not None and room.project_id != task.project_id:
                    raise RuntimeConflictError(
                        "artifact scopes belong to different projects"
                    )
                if (
                    task is None
                    and project_id is not None
                    and room.project_id != project_id
                ):
                    raise RuntimeConflictError(
                        "artifact scopes belong to different projects"
                    )
                project_id = room.project_id
            artifact_id = _new_id("art")
            now = time.time()
            self.db.execute(
                """INSERT INTO runtime_artifacts(
                       artifact_id, owner_id, project_id, task_id, room_id, name,
                       media_type, uri, sha256, size_bytes, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    artifact_id,
                    owner_id,
                    project_id,
                    task_id,
                    room_id,
                    name,
                    media_type,
                    uri,
                    sha256,
                    size_bytes,
                    now,
                ),
            )
            if room is not None:
                scope = RuntimeScope.ROOM
                scope_id = room_id
            elif task is not None:
                scope = RuntimeScope.TASK
                scope_id = task_id
            elif project_id is not None:
                scope = RuntimeScope.PROJECT
                scope_id = project_id
            else:
                scope = RuntimeScope.AGENT
                scope_id = owner_id
            self._append_event(
                event_type="artifact.shared",
                actor_id=owner_id,
                project_id=project_id,
                scope=scope,
                scope_id=scope_id,
                subject_kind="artifact",
                subject_id=artifact_id,
                idempotency_key=key,
                command_sha256=digest,
                payload={
                    "name": name,
                    "media_type": media_type,
                    "sha256": sha256,
                    "size_bytes": size_bytes,
                    "task_id": task_id,
                    "room_id": room_id,
                },
                result_kind="artifact",
                result_id=artifact_id,
                occurred_at=now,
                correlation_id=correlation_id,
                causation_id=causation_id,
            )
            return self.get_artifact(artifact_id)

    def get_artifact(self, artifact_id: str) -> ArtifactRecord:
        _identifier(artifact_id, "art")
        row = self.db.execute(
            "SELECT * FROM runtime_artifacts WHERE artifact_id=?", (artifact_id,)
        ).fetchone()
        if row is None:
            raise RuntimeNotFoundError("artifact does not exist")
        return self._artifact_from_row(row)

    def room_artifacts(
        self, requester_id: str, room_id: str
    ) -> tuple[ArtifactRecord, ...]:
        self._require_agent(requester_id)
        self.get_room(room_id)
        if self._room_membership(room_id, requester_id) != "JOINED":
            raise RuntimePermissionError("requester is not a joined room member")
        rows = self.db.execute(
            """SELECT * FROM runtime_artifacts WHERE room_id=?
               ORDER BY created_at, artifact_id""",
            (room_id,),
        ).fetchall()
        return tuple(self._artifact_from_row(row) for row in rows)

    def task_artifacts(
        self, requester_id: str, task_id: str
    ) -> tuple[ArtifactRecord, ...]:
        requester = self._require_agent(requester_id)
        task = self.get_task(task_id)
        if requester.project_id not in {None, task.project_id}:
            raise RuntimePermissionError("task belongs to another project")
        rows = self.db.execute(
            """SELECT * FROM runtime_artifacts WHERE task_id=?
               ORDER BY created_at, artifact_id""",
            (task_id,),
        ).fetchall()
        return tuple(self._artifact_from_row(row) for row in rows)

    @staticmethod
    def _collaboration_scope(
        request: CollaborationRequestRecord,
    ) -> tuple[RuntimeScope, str | None]:
        if request.room_id is not None:
            return RuntimeScope.ROOM, request.room_id
        if request.task_id is not None:
            return RuntimeScope.TASK, request.task_id
        if request.target_agent_id is not None:
            return RuntimeScope.AGENT, request.target_agent_id
        if request.project_id is not None:
            return RuntimeScope.PROJECT, request.project_id
        return RuntimeScope.GLOBAL, None

    def create_collaboration_request(
        self,
        *,
        requester_id: str,
        kind: CollaborationKind,
        prompt: str,
        idempotency_key: str,
        target_agent_id: str | None = None,
        options: Sequence[str] = (),
        room_id: str | None = None,
        task_id: str | None = None,
        artifact_id: str | None = None,
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> CollaborationRequestRecord:
        _identifier(requester_id, "agt")
        kind = CollaborationKind(kind)
        prompt = _required_text("prompt", prompt, maximum=20_000)
        if target_agent_id is not None:
            _identifier(target_agent_id, "agt")
        if room_id is not None:
            _identifier(room_id, "room")
        if task_id is not None:
            _identifier(task_id, "task")
        if artifact_id is not None:
            _identifier(artifact_id, "art")
        if kind is CollaborationKind.HELP:
            if target_agent_id is not None:
                raise ValueError("HELP is a broadcast and must not name a target agent")
        elif target_agent_id is None:
            raise ValueError(f"{kind.value} requires a target agent")
        if target_agent_id == requester_id:
            raise RuntimePermissionError(
                "an agent cannot request collaboration from itself"
            )
        normalized_options: list[str] = []
        seen_options: set[str] = set()
        for value in options:
            option = _required_text("option", value, maximum=500)
            if option in seen_options:
                raise ValueError("vote options must be unique")
            seen_options.add(option)
            normalized_options.append(option)
        if kind is CollaborationKind.VOTE:
            if not 2 <= len(normalized_options) <= 20:
                raise ValueError("VOTE requires between 2 and 20 options")
        elif normalized_options:
            raise ValueError("options are accepted only for VOTE requests")
        key = self._command_key(idempotency_key)
        payload = {
            "requester_id": requester_id,
            "target_agent_id": target_agent_id,
            "kind": kind.value,
            "prompt": prompt,
            "options": normalized_options,
            "room_id": room_id,
            "task_id": task_id,
            "artifact_id": artifact_id,
        }
        digest = _command_digest("collaboration.requested", payload)
        with self._transaction():
            replay = self._replay_result_id(
                event_type="collaboration.requested",
                idempotency_key=key,
                command_sha256=digest,
                result_kind="collaboration_request",
            )
            if replay is not None:
                return self.get_collaboration_request(replay)
            requester = self._require_running_agent(requester_id)
            if target_agent_id is not None:
                target = self._require_agent(target_agent_id)
                if target.project_id != requester.project_id:
                    raise RuntimePermissionError(
                        "collaboration target belongs to another project"
                    )
            else:
                target = None
            if room_id is not None:
                room = self.get_room(room_id)
                if room.project_id != requester.project_id:
                    raise RuntimePermissionError("room belongs to another project")
                if self._room_membership(room_id, requester_id) != "JOINED":
                    raise RuntimePermissionError(
                        "collaboration requester is not a joined room member"
                    )
                if (
                    target is not None
                    and self._room_membership(room_id, target.agent_id) != "JOINED"
                ):
                    raise RuntimePermissionError(
                        "collaboration target is not a joined room member"
                    )
            if task_id is not None:
                task = self.get_task(task_id)
                if task.project_id != requester.project_id:
                    raise RuntimePermissionError("task belongs to another project")
            if artifact_id is not None:
                artifact = self.get_artifact(artifact_id)
                if artifact.project_id != requester.project_id:
                    raise RuntimePermissionError("artifact belongs to another project")
            request_id = _new_id("req")
            now = time.time()
            self.db.execute(
                """INSERT INTO runtime_collaboration_requests(
                       request_id, requester_id, target_agent_id, project_id,
                       room_id, task_id, artifact_id, kind, prompt, options_json,
                       status, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    request_id,
                    requester_id,
                    target_agent_id,
                    requester.project_id,
                    room_id,
                    task_id,
                    artifact_id,
                    kind.value,
                    prompt,
                    _canonical_json(normalized_options),
                    CollaborationStatus.OPEN.value,
                    now,
                    now,
                ),
            )
            request = self.get_collaboration_request(request_id)
            scope, scope_id = self._collaboration_scope(request)
            self._append_event(
                event_type="collaboration.requested",
                actor_id=requester_id,
                project_id=requester.project_id,
                scope=scope,
                scope_id=scope_id,
                subject_kind="collaboration_request",
                subject_id=request_id,
                idempotency_key=key,
                command_sha256=digest,
                payload={
                    "kind": kind.value,
                    "target_agent_id": target_agent_id,
                    "room_id": room_id,
                    "task_id": task_id,
                    "artifact_id": artifact_id,
                    "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                    "options_sha256": hashlib.sha256(
                        _canonical_json(normalized_options).encode("utf-8")
                    ).hexdigest(),
                },
                result_kind="collaboration_request",
                result_id=request_id,
                occurred_at=now,
                correlation_id=correlation_id,
                causation_id=causation_id,
            )
            return request

    def get_collaboration_request(self, request_id: str) -> CollaborationRequestRecord:
        _identifier(request_id, "req")
        row = self.db.execute(
            "SELECT * FROM runtime_collaboration_requests WHERE request_id=?",
            (request_id,),
        ).fetchone()
        if row is None:
            raise RuntimeNotFoundError("collaboration request does not exist")
        return self._collaboration_request_from_row(row)

    def _can_observe_collaboration(
        self, agent_id: str, request: CollaborationRequestRecord
    ) -> bool:
        agent = self._require_agent(agent_id)
        if agent_id == request.requester_id:
            return True
        if request.target_agent_id is not None:
            return agent_id == request.target_agent_id
        if agent.project_id != request.project_id:
            return False
        return (
            request.room_id is None
            or self._room_membership(request.room_id, agent_id) == "JOINED"
        )

    def collaboration_inbox(
        self,
        agent_id: str,
        *,
        include_closed: bool = False,
        include_sent: bool = False,
    ) -> tuple[CollaborationRequestRecord, ...]:
        agent = self._require_agent(agent_id)
        clauses = [
            "(target_agent_id=? OR (target_agent_id IS NULL AND project_id IS ?))"
        ]
        params: list[Any] = [agent_id, agent.project_id]
        if include_sent:
            clauses[0] = f"({clauses[0]} OR requester_id=?)"
            params.append(agent_id)
        else:
            clauses.append("requester_id<>?")
            params.append(agent_id)
        if not include_closed:
            clauses.append("status=?")
            params.append(CollaborationStatus.OPEN.value)
        rows = self.db.execute(
            f"""SELECT * FROM runtime_collaboration_requests
                WHERE {" AND ".join(clauses)}
                ORDER BY created_at, request_id""",
            params,
        ).fetchall()
        requests = tuple(self._collaboration_request_from_row(row) for row in rows)
        return tuple(
            request
            for request in requests
            if self._can_observe_collaboration(agent_id, request)
        )

    def respond_to_collaboration(
        self,
        *,
        request_id: str,
        responder_id: str,
        body: str,
        idempotency_key: str,
        selected_option: str | None = None,
        evidence_artifact_id: str | None = None,
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> CollaborationResponseRecord:
        _identifier(request_id, "req")
        _identifier(responder_id, "agt")
        body = _required_text("body", body, maximum=100_000)
        selected_option = _optional_text(
            "selected_option", selected_option, maximum=500
        )
        if evidence_artifact_id is not None:
            _identifier(evidence_artifact_id, "art")
        key = self._command_key(idempotency_key)
        payload = {
            "request_id": request_id,
            "responder_id": responder_id,
            "body": body,
            "selected_option": selected_option,
            "evidence_artifact_id": evidence_artifact_id,
        }
        digest = _command_digest("collaboration.responded", payload)
        with self._transaction():
            replay = self._replay_result_id(
                event_type="collaboration.responded",
                idempotency_key=key,
                command_sha256=digest,
                result_kind="collaboration_response",
            )
            if replay is not None:
                return self.get_collaboration_response(replay)
            self._require_running_agent(responder_id)
            request = self.get_collaboration_request(request_id)
            if request.status is not CollaborationStatus.OPEN:
                raise RuntimeConflictError("collaboration request is not open")
            if responder_id == request.requester_id:
                raise RuntimePermissionError(
                    "collaboration requester cannot answer its own request"
                )
            if not self._can_observe_collaboration(responder_id, request):
                raise RuntimePermissionError(
                    "agent is not eligible to answer this collaboration request"
                )
            if request.kind is CollaborationKind.VOTE:
                if selected_option not in request.options:
                    raise RuntimeConflictError(
                        "vote response must select one of the request options"
                    )
            elif selected_option is not None:
                raise RuntimeConflictError(
                    "selected_option is accepted only for VOTE responses"
                )
            if evidence_artifact_id is not None:
                evidence = self.get_artifact(evidence_artifact_id)
                if evidence.project_id != request.project_id:
                    raise RuntimePermissionError(
                        "response evidence belongs to another project"
                    )
            if (
                self.db.execute(
                    """SELECT 1 FROM runtime_collaboration_responses
                   WHERE request_id=? AND responder_id=?""",
                    (request_id, responder_id),
                ).fetchone()
                is not None
            ):
                raise RuntimeConflictError(
                    "agent already responded to this collaboration request"
                )
            response_id = _new_id("rsp")
            now = time.time()
            self.db.execute(
                """INSERT INTO runtime_collaboration_responses(
                       response_id, request_id, responder_id, body,
                       selected_option, evidence_artifact_id, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    response_id,
                    request_id,
                    responder_id,
                    body,
                    selected_option,
                    evidence_artifact_id,
                    now,
                ),
            )
            closes_request = request.target_agent_id is not None
            if closes_request:
                self.db.execute(
                    """UPDATE runtime_collaboration_requests
                       SET status=?, updated_at=? WHERE request_id=?""",
                    (CollaborationStatus.CLOSED.value, now, request_id),
                )
            scope, scope_id = self._collaboration_scope(request)
            self._append_event(
                event_type="collaboration.responded",
                actor_id=responder_id,
                project_id=request.project_id,
                scope=scope,
                scope_id=scope_id,
                subject_kind="collaboration_request",
                subject_id=request_id,
                idempotency_key=key,
                command_sha256=digest,
                payload={
                    "response_id": response_id,
                    "selected_option": selected_option,
                    "evidence_artifact_id": evidence_artifact_id,
                    "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
                    "closed_request": closes_request,
                },
                result_kind="collaboration_response",
                result_id=response_id,
                occurred_at=now,
                correlation_id=correlation_id,
                causation_id=causation_id,
            )
            return self.get_collaboration_response(response_id)

    def get_collaboration_response(
        self, response_id: str
    ) -> CollaborationResponseRecord:
        _identifier(response_id, "rsp")
        row = self.db.execute(
            "SELECT * FROM runtime_collaboration_responses WHERE response_id=?",
            (response_id,),
        ).fetchone()
        if row is None:
            raise RuntimeNotFoundError("collaboration response does not exist")
        return self._collaboration_response_from_row(row)

    def collaboration_responses(
        self, agent_id: str, request_id: str
    ) -> tuple[CollaborationResponseRecord, ...]:
        request = self.get_collaboration_request(request_id)
        if not self._can_observe_collaboration(agent_id, request):
            raise RuntimePermissionError(
                "agent cannot observe this collaboration request"
            )
        rows = self.db.execute(
            """SELECT * FROM runtime_collaboration_responses WHERE request_id=?
               ORDER BY created_at, response_id""",
            (request_id,),
        ).fetchall()
        return tuple(self._collaboration_response_from_row(row) for row in rows)

    def close_collaboration_request(
        self,
        *,
        request_id: str,
        requester_id: str,
        idempotency_key: str,
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> CollaborationRequestRecord:
        _identifier(request_id, "req")
        _identifier(requester_id, "agt")
        key = self._command_key(idempotency_key)
        payload = {"request_id": request_id, "requester_id": requester_id}
        digest = _command_digest("collaboration.closed", payload)
        with self._transaction():
            replay = self._replay_result_id(
                event_type="collaboration.closed",
                idempotency_key=key,
                command_sha256=digest,
                result_kind="collaboration_request",
            )
            if replay is not None:
                return self.get_collaboration_request(replay)
            self._require_running_agent(requester_id)
            request = self.get_collaboration_request(request_id)
            if request.requester_id != requester_id:
                raise RuntimePermissionError(
                    "only the requester may close a collaboration request"
                )
            if request.status is not CollaborationStatus.OPEN:
                raise RuntimeConflictError("collaboration request is not open")
            now = time.time()
            self.db.execute(
                """UPDATE runtime_collaboration_requests
                   SET status=?, updated_at=? WHERE request_id=?""",
                (CollaborationStatus.CLOSED.value, now, request_id),
            )
            scope, scope_id = self._collaboration_scope(request)
            self._append_event(
                event_type="collaboration.closed",
                actor_id=requester_id,
                project_id=request.project_id,
                scope=scope,
                scope_id=scope_id,
                subject_kind="collaboration_request",
                subject_id=request_id,
                idempotency_key=key,
                command_sha256=digest,
                payload={
                    "from": request.status.value,
                    "to": CollaborationStatus.CLOSED.value,
                },
                result_kind="collaboration_request",
                result_id=request_id,
                occurred_at=now,
                correlation_id=correlation_id,
                causation_id=causation_id,
            )
            return self.get_collaboration_request(request_id)

    def events(
        self,
        *,
        after_sequence: int = 0,
        subject_kind: str | None = None,
        subject_id: str | None = None,
        limit: int = 1000,
    ) -> tuple[RuntimeEvent, ...]:
        if after_sequence < 0:
            raise ValueError("after_sequence must not be negative")
        if limit < 1 or limit > 5000:
            raise ValueError("limit must be between 1 and 5000")
        clauses = ["sequence>?"]
        params: list[Any] = [after_sequence]
        if subject_kind is not None:
            clauses.append("subject_kind=?")
            params.append(_required_text("subject_kind", subject_kind, maximum=100))
        if subject_id is not None:
            clauses.append("subject_id=?")
            params.append(_required_text("subject_id", subject_id, maximum=200))
        params.append(limit)
        rows = self.db.execute(
            f"""SELECT * FROM runtime_events WHERE {" AND ".join(clauses)}
                ORDER BY sequence LIMIT ?""",
            params,
        ).fetchall()
        return tuple(self._event_from_row(row) for row in rows)

    @staticmethod
    def _verify_event_content(event: RuntimeEvent) -> None:
        """Validate v2 content using only the event, never current projections."""
        if event.schema_version != EVENT_SCHEMA_VERSION:
            raise RuntimeStoreError(
                "content replay requires event schema 2; legacy history cannot be backfilled"
            )
        if _EVENT_RESULT_KINDS.get(event.event_type) != event.result_kind:
            raise RuntimeStoreError("unsupported runtime event vocabulary")
        _, id_column, fields = _EVENT_RECORDS[event.result_kind]
        if not isinstance(event.payload, dict):
            raise RuntimeStoreError("runtime event payload must be an object")
        record = event.payload.get("record")
        if not isinstance(record, dict) or set(record) != set(fields.split()):
            raise RuntimeStoreError("runtime event content snapshot has invalid fields")
        if record[id_column] != event.result_id:
            raise RuntimeStoreError("runtime event content snapshot has wrong identity")
        digest = hashlib.sha256(_canonical_json(record).encode("utf-8")).hexdigest()
        if digest != event.payload.get("record_sha256"):
            raise RuntimeStoreError("runtime event content snapshot digest mismatch")

    def replay_events(
        self,
        *,
        after_sequence: int = 0,
        after_event_id: str | None = None,
        limit: int = 1000,
    ) -> tuple[RuntimeEvent, ...]:
        """Read a contiguous, content-complete batch for a trusted follower.

        Persist both cursor values atomically with the follower's projections.
        A nonzero cursor must identify its predecessor in this particular store.
        Legacy v1 events remain available through events(), but are not claimed
        to contain reconstructible historical content. No projection reads occur.
        """
        if type(after_sequence) is not int or after_sequence < 0:
            raise ValueError("after_sequence must be a nonnegative integer")
        if type(limit) is not int or not 1 <= limit <= 5000:
            raise ValueError("limit must be between 1 and 5000")
        if after_sequence == 0:
            if after_event_id is not None:
                raise RuntimeConflictError(
                    "initial replay cursor cannot have an event ID"
                )
        else:
            predecessor = self.db.execute(
                "SELECT event_id FROM runtime_events WHERE sequence=?",
                (after_sequence,),
            ).fetchone()
            if predecessor is None or predecessor["event_id"] != after_event_id:
                raise RuntimeConflictError(
                    "replay cursor does not match this runtime history"
                )
        batch = self.events(after_sequence=after_sequence, limit=limit)
        for expected, event in enumerate(batch, start=after_sequence + 1):
            if event.sequence != expected:
                raise RuntimeStoreError("runtime replay event sequence has a gap")
            self._verify_event_content(event)
        return batch

    def verify_integrity(self) -> RuntimeIntegrityReport:
        """Verify SQLite, foreign keys, event envelopes, and projection provenance."""
        integrity_rows = self.db.execute("PRAGMA integrity_check").fetchall()
        if [str(row[0]) for row in integrity_rows] != ["ok"]:
            raise RuntimeStoreError("SQLite integrity check failed")
        if self.db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RuntimeStoreError("runtime projection has a foreign-key violation")

        event_rows = self.db.execute(
            "SELECT * FROM runtime_events ORDER BY sequence"
        ).fetchall()
        events = tuple(self._event_from_row(row) for row in event_rows)
        event_count = len(events)
        if [event.sequence for event in events] != list(range(1, event_count + 1)):
            raise RuntimeStoreError("runtime event sequence is not contiguous")
        known_event_ids = {event.event_id for event in events}
        for event, row in zip(events, event_rows, strict=True):
            _identifier(event.event_id, "evt")
            _identifier(event.correlation_id, "evt")
            if event.correlation_id not in known_event_ids:
                raise RuntimeStoreError("runtime event has a missing correlation root")
            if (
                event.causation_id is not None
                and event.causation_id not in known_event_ids
            ):
                raise RuntimeStoreError("runtime event has a missing causation link")
            if event.schema_version not in {1, EVENT_SCHEMA_VERSION}:
                raise RuntimeStoreError(
                    "runtime event uses an unsupported schema version"
                )
            if event.schema_version == EVENT_SCHEMA_VERSION:
                self._verify_event_content(event)
            if str(row["payload_json"]) != _canonical_json(event.payload):
                raise RuntimeStoreError("runtime event payload is not canonical JSON")
            if not re.fullmatch(r"[0-9a-f]{64}", event.command_sha256):
                raise RuntimeStoreError("runtime event command digest is invalid")

        if (
            self.db.execute(
                """SELECT 1 FROM runtime_task_dependencies AS d
               JOIN runtime_tasks AS task ON task.task_id=d.task_id
               JOIN runtime_tasks AS prerequisite ON prerequisite.task_id=d.depends_on_task_id
               WHERE task.status IN ('RUNNING', 'COMPLETED')
                 AND prerequisite.status<>'COMPLETED' LIMIT 1"""
            ).fetchone()
            is not None
        ):
            raise RuntimeStoreError(
                "running or completed task has an unfinished dependency"
            )

        projection_specs = (
            ("runtime_agents", "agent_id", "agent", "agent.created"),
            ("runtime_rooms", "room_id", "room", "room.created"),
            (
                "runtime_direct_messages",
                "message_id",
                "direct_message",
                "message.direct_sent",
            ),
            (
                "runtime_direct_message_receipts",
                "message_id",
                "direct_message_receipt",
                "message.direct_acknowledged",
            ),
            (
                "runtime_room_messages",
                "message_id",
                "room_message",
                "room.message_sent",
            ),
            (
                "runtime_room_message_cursors",
                "cursor_id",
                "room_message_cursor",
                "room.cursor_advanced",
            ),
            ("runtime_tasks", "task_id", "task", "task.created"),
            (
                "runtime_task_delegations",
                "delegation_id",
                "delegation",
                "task.delegated",
            ),
            (
                "runtime_capability_requests",
                "request_id",
                "capability_request",
                "capability.requested",
            ),
            (
                "runtime_capability_grants",
                "grant_id",
                "capability_grant",
                "capability.granted",
            ),
            (
                "runtime_artifacts",
                "artifact_id",
                "artifact",
                "artifact.shared",
            ),
            (
                "runtime_collaboration_requests",
                "request_id",
                "collaboration_request",
                "collaboration.requested",
            ),
            (
                "runtime_collaboration_responses",
                "response_id",
                "collaboration_response",
                "collaboration.responded",
            ),
        )
        counts: dict[str, int] = {}
        for table, id_column, result_kind, creation_event in projection_specs:
            count = int(self.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            counts[table] = count
            missing = self.db.execute(
                f"""SELECT p.{id_column} FROM {table} AS p
                    LEFT JOIN runtime_events AS e
                      ON e.result_kind=? AND e.result_id=p.{id_column}
                     AND e.event_type=?
                    WHERE e.event_id IS NULL LIMIT 1""",
                (result_kind, creation_event),
            ).fetchone()
            if missing is not None:
                raise RuntimeStoreError(
                    f"{table} contains a projection without its creation event"
                )

        dependency_rows = self.db.execute(
            """SELECT task_id, depends_on_task_id
               FROM runtime_task_dependencies"""
        ).fetchall()
        dependency_events = {
            (event.subject_id, str(event.payload.get("depends_on_task_id", "")))
            for event in events
            if event.event_type == "task.dependency_added"
        }
        for row in dependency_rows:
            edge = (str(row["task_id"]), str(row["depends_on_task_id"]))
            if edge not in dependency_events:
                raise RuntimeStoreError(
                    "runtime_task_dependencies contains an edge without its event"
                )

        invalid_receipt = self.db.execute(
            """SELECT receipt.message_id
               FROM runtime_direct_message_receipts AS receipt
               JOIN runtime_direct_messages AS message
                 ON message.message_id=receipt.message_id
               WHERE receipt.recipient_id<>message.recipient_id
                  OR (receipt.read_at IS NOT NULL
                      AND receipt.read_at<receipt.delivered_at)
               LIMIT 1"""
        ).fetchone()
        if invalid_receipt is not None:
            raise RuntimeStoreError("direct-message receipt is inconsistent")

        invalid_cursor = self.db.execute(
            """SELECT cursor.cursor_id
               FROM runtime_room_message_cursors AS cursor
               JOIN runtime_room_messages AS message
                 ON message.message_id=cursor.last_message_id
               LEFT JOIN runtime_events AS event
                 ON event.event_type='room.message_sent'
                AND event.result_kind='room_message'
                AND event.result_id=message.message_id
               WHERE message.room_id<>cursor.room_id
                  OR event.sequence IS NULL
                  OR event.sequence<>cursor.last_event_sequence
               LIMIT 1"""
        ).fetchone()
        if invalid_cursor is not None:
            raise RuntimeStoreError("room-message cursor is inconsistent")

        return RuntimeIntegrityReport(
            event_count=event_count,
            agent_count=counts["runtime_agents"],
            room_count=counts["runtime_rooms"],
            direct_message_count=counts["runtime_direct_messages"],
            room_message_count=counts["runtime_room_messages"],
            direct_message_receipt_count=counts["runtime_direct_message_receipts"],
            room_message_cursor_count=counts["runtime_room_message_cursors"],
            task_count=counts["runtime_tasks"],
            task_dependency_count=len(dependency_rows),
            delegation_count=counts["runtime_task_delegations"],
            capability_request_count=counts["runtime_capability_requests"],
            capability_grant_count=counts["runtime_capability_grants"],
            artifact_count=counts["runtime_artifacts"],
            collaboration_request_count=counts["runtime_collaboration_requests"],
            collaboration_response_count=counts["runtime_collaboration_responses"],
        )


class AgentRuntimeContext:
    """Agent-bound façade: the caller, not JARVIS, selects peers and actions."""

    def __init__(self, store: MultiAgentRuntimeStore, agent_id: str) -> None:
        self._store = store
        self.agent_id = agent_id

    def profile(self) -> AgentRecord:
        return self._store.get_agent(self.agent_id)

    def start(self, *, idempotency_key: str) -> AgentRecord:
        return self._store.set_agent_lifecycle(
            self.agent_id,
            AgentLifecycle.RUNNING,
            actor_id=self.agent_id,
            idempotency_key=idempotency_key,
        )

    def add_task_dependency(
        self,
        task_id: str,
        depends_on_task_id: str,
        *,
        idempotency_key: str,
    ) -> TaskRecord:
        return self._store.add_task_dependency(
            task_id=task_id,
            depends_on_task_id=depends_on_task_id,
            actor_id=self.agent_id,
            idempotency_key=idempotency_key,
        )

    def task_dependencies(self, task_id: str) -> tuple[TaskRecord, ...]:
        return self._store.task_dependencies(self.agent_id, task_id)

    def pause(self, *, idempotency_key: str) -> AgentRecord:
        return self._store.set_agent_lifecycle(
            self.agent_id,
            AgentLifecycle.PAUSED,
            actor_id=self.agent_id,
            idempotency_key=idempotency_key,
        )

    def stop(self, *, idempotency_key: str) -> AgentRecord:
        return self._store.set_agent_lifecycle(
            self.agent_id,
            AgentLifecycle.STOPPED,
            actor_id=self.agent_id,
            idempotency_key=idempotency_key,
        )

    def find_agents(self, *, specialty: str | None = None) -> tuple[AgentRecord, ...]:
        return self._store.find_agents(self.agent_id, specialty=specialty)

    def change_model(
        self,
        model_provider: str,
        model_name: str,
        *,
        idempotency_key: str,
    ) -> AgentRecord:
        return self._store.update_agent_model(
            self.agent_id,
            actor_id=self.agent_id,
            model_provider=model_provider,
            model_name=model_name,
            idempotency_key=idempotency_key,
        )

    def send_message(
        self,
        recipient_id: str,
        body: str,
        *,
        idempotency_key: str,
        task_id: str | None = None,
    ) -> DirectMessage:
        return self._store.send_direct_message(
            sender_id=self.agent_id,
            recipient_id=recipient_id,
            body=body,
            task_id=task_id,
            idempotency_key=idempotency_key,
        )

    def reply(
        self, message_id: str, body: str, *, idempotency_key: str
    ) -> DirectMessage:
        original = self._store.get_direct_message(message_id)
        if self.agent_id not in {original.sender_id, original.recipient_id}:
            raise RuntimePermissionError("agent is not a participant in the message")
        recipient = (
            original.sender_id
            if self.agent_id == original.recipient_id
            else original.recipient_id
        )
        return self._store.send_direct_message(
            sender_id=self.agent_id,
            recipient_id=recipient,
            body=body,
            reply_to_message_id=message_id,
            task_id=original.task_id,
            idempotency_key=idempotency_key,
        )

    def mailbox(self, *, include_sent: bool = False) -> tuple[DirectMessage, ...]:
        return self._store.mailbox(self.agent_id, include_sent=include_sent)

    def acknowledge_message(
        self,
        message_id: str,
        *,
        read: bool,
        idempotency_key: str,
    ) -> DirectMessageReceipt:
        return self._store.acknowledge_direct_message(
            recipient_id=self.agent_id,
            message_id=message_id,
            read=read,
            idempotency_key=idempotency_key,
        )

    def unread_mailbox(self) -> tuple[DirectMessage, ...]:
        return self._store.unread_direct_messages(self.agent_id)

    def create_room(
        self,
        name: str,
        *,
        idempotency_key: str,
        kind: str = "collaboration",
        access: RoomAccess = RoomAccess.INVITE_ONLY,
    ) -> RoomRecord:
        return self._store.create_room(
            creator_id=self.agent_id,
            name=name,
            kind=kind,
            access=access,
            idempotency_key=idempotency_key,
        )

    def invite(
        self, room_id: str, invitee_id: str, *, idempotency_key: str
    ) -> RoomRecord:
        return self._store.invite_agent(
            room_id=room_id,
            inviter_id=self.agent_id,
            invitee_id=invitee_id,
            idempotency_key=idempotency_key,
        )

    def join_room(self, room_id: str, *, idempotency_key: str) -> RoomRecord:
        return self._store.join_room(
            room_id=room_id,
            agent_id=self.agent_id,
            idempotency_key=idempotency_key,
        )

    def leave_room(self, room_id: str, *, idempotency_key: str) -> RoomRecord:
        return self._store.leave_room(
            room_id=room_id,
            agent_id=self.agent_id,
            idempotency_key=idempotency_key,
        )

    def room_members(self, room_id: str) -> tuple[AgentRecord, ...]:
        return self._store.room_members(self.agent_id, room_id)

    def send_room_message(
        self,
        room_id: str,
        body: str,
        *,
        idempotency_key: str,
        task_id: str | None = None,
    ) -> RoomMessage:
        return self._store.send_room_message(
            room_id=room_id,
            sender_id=self.agent_id,
            body=body,
            task_id=task_id,
            idempotency_key=idempotency_key,
        )

    def room_messages(self, room_id: str) -> tuple[RoomMessage, ...]:
        return self._store.room_messages(self.agent_id, room_id)

    def advance_room_cursor(
        self,
        room_id: str,
        through_message_id: str,
        *,
        idempotency_key: str,
    ) -> RoomMessageCursor:
        return self._store.advance_room_message_cursor(
            agent_id=self.agent_id,
            room_id=room_id,
            through_message_id=through_message_id,
            idempotency_key=idempotency_key,
        )

    def unread_room_messages(self, room_id: str) -> tuple[RoomMessage, ...]:
        return self._store.unread_room_messages(self.agent_id, room_id)

    def create_task(
        self,
        title: str,
        description: str,
        *,
        idempotency_key: str,
        parent_task_id: str | None = None,
    ) -> TaskRecord:
        return self._store.create_task(
            creator_id=self.agent_id,
            title=title,
            description=description,
            parent_task_id=parent_task_id,
            idempotency_key=idempotency_key,
        )

    def delegate_task(
        self,
        task_id: str,
        to_agent_id: str,
        reason: str,
        *,
        idempotency_key: str,
    ) -> DelegationRecord:
        return self._store.delegate_task(
            task_id=task_id,
            from_agent_id=self.agent_id,
            to_agent_id=to_agent_id,
            reason=reason,
            idempotency_key=idempotency_key,
        )

    def update_task(
        self,
        task_id: str,
        status: TaskStatus,
        *,
        idempotency_key: str,
        result: str | None = None,
    ) -> TaskRecord:
        return self._store.update_task_status(
            task_id=task_id,
            actor_id=self.agent_id,
            status=status,
            result=result,
            idempotency_key=idempotency_key,
        )

    def tasks(self, *, statuses: Sequence[TaskStatus] = ()) -> tuple[TaskRecord, ...]:
        return self._store.list_agent_tasks(self.agent_id, statuses=statuses)

    def request_capability(
        self,
        capability: str,
        *,
        scope: RuntimeScope,
        scope_id: str | None,
        reason: str,
        idempotency_key: str,
    ) -> CapabilityRequestRecord:
        return self._store.request_capability(
            agent_id=self.agent_id,
            capability=capability,
            scope=scope,
            scope_id=scope_id,
            reason=reason,
            idempotency_key=idempotency_key,
        )

    def effective_capabilities(
        self, *, scope: RuntimeScope, scope_id: str | None
    ) -> tuple[CapabilityGrantRecord, ...]:
        return self._store.effective_capabilities(
            self.agent_id, scope=scope, scope_id=scope_id
        )

    def has_capability(
        self,
        capability: str,
        *,
        scope: RuntimeScope,
        scope_id: str | None,
    ) -> bool:
        return self._store.has_capability(
            self.agent_id,
            capability,
            scope=scope,
            scope_id=scope_id,
        )

    def share_artifact(
        self,
        name: str,
        *,
        media_type: str,
        uri: str,
        sha256: str,
        idempotency_key: str,
        size_bytes: int | None = None,
        task_id: str | None = None,
        room_id: str | None = None,
    ) -> ArtifactRecord:
        return self._store.share_artifact(
            owner_id=self.agent_id,
            name=name,
            media_type=media_type,
            uri=uri,
            sha256=sha256,
            size_bytes=size_bytes,
            task_id=task_id,
            room_id=room_id,
            idempotency_key=idempotency_key,
        )

    def room_artifacts(self, room_id: str) -> tuple[ArtifactRecord, ...]:
        return self._store.room_artifacts(self.agent_id, room_id)

    def task_artifacts(self, task_id: str) -> tuple[ArtifactRecord, ...]:
        return self._store.task_artifacts(self.agent_id, task_id)

    def request_collaboration(
        self,
        target_agent_id: str,
        kind: CollaborationKind,
        prompt: str,
        *,
        idempotency_key: str,
        options: Sequence[str] = (),
        room_id: str | None = None,
        task_id: str | None = None,
        artifact_id: str | None = None,
    ) -> CollaborationRequestRecord:
        return self._store.create_collaboration_request(
            requester_id=self.agent_id,
            target_agent_id=target_agent_id,
            kind=kind,
            prompt=prompt,
            options=options,
            room_id=room_id,
            task_id=task_id,
            artifact_id=artifact_id,
            idempotency_key=idempotency_key,
        )

    def request_review(
        self,
        target_agent_id: str,
        prompt: str,
        *,
        idempotency_key: str,
        room_id: str | None = None,
        task_id: str | None = None,
        artifact_id: str | None = None,
    ) -> CollaborationRequestRecord:
        return self.request_collaboration(
            target_agent_id,
            CollaborationKind.REVIEW,
            prompt,
            room_id=room_id,
            task_id=task_id,
            artifact_id=artifact_id,
            idempotency_key=idempotency_key,
        )

    def request_opinion(
        self,
        target_agent_id: str,
        prompt: str,
        *,
        idempotency_key: str,
        room_id: str | None = None,
        task_id: str | None = None,
    ) -> CollaborationRequestRecord:
        return self.request_collaboration(
            target_agent_id,
            CollaborationKind.OPINION,
            prompt,
            room_id=room_id,
            task_id=task_id,
            idempotency_key=idempotency_key,
        )

    def request_vote(
        self,
        target_agent_id: str,
        prompt: str,
        options: Sequence[str],
        *,
        idempotency_key: str,
        room_id: str | None = None,
        task_id: str | None = None,
    ) -> CollaborationRequestRecord:
        return self.request_collaboration(
            target_agent_id,
            CollaborationKind.VOTE,
            prompt,
            options=options,
            room_id=room_id,
            task_id=task_id,
            idempotency_key=idempotency_key,
        )

    def request_evidence(
        self,
        target_agent_id: str,
        prompt: str,
        *,
        idempotency_key: str,
        room_id: str | None = None,
        task_id: str | None = None,
        artifact_id: str | None = None,
    ) -> CollaborationRequestRecord:
        return self.request_collaboration(
            target_agent_id,
            CollaborationKind.EVIDENCE,
            prompt,
            room_id=room_id,
            task_id=task_id,
            artifact_id=artifact_id,
            idempotency_key=idempotency_key,
        )

    def broadcast_help(
        self,
        prompt: str,
        *,
        idempotency_key: str,
        room_id: str | None = None,
        task_id: str | None = None,
        artifact_id: str | None = None,
    ) -> CollaborationRequestRecord:
        return self._store.create_collaboration_request(
            requester_id=self.agent_id,
            kind=CollaborationKind.HELP,
            prompt=prompt,
            room_id=room_id,
            task_id=task_id,
            artifact_id=artifact_id,
            idempotency_key=idempotency_key,
        )

    def collaboration_inbox(
        self, *, include_closed: bool = False, include_sent: bool = False
    ) -> tuple[CollaborationRequestRecord, ...]:
        return self._store.collaboration_inbox(
            self.agent_id,
            include_closed=include_closed,
            include_sent=include_sent,
        )

    def respond_to_collaboration(
        self,
        request_id: str,
        body: str,
        *,
        idempotency_key: str,
        selected_option: str | None = None,
        evidence_artifact_id: str | None = None,
    ) -> CollaborationResponseRecord:
        return self._store.respond_to_collaboration(
            request_id=request_id,
            responder_id=self.agent_id,
            body=body,
            selected_option=selected_option,
            evidence_artifact_id=evidence_artifact_id,
            idempotency_key=idempotency_key,
        )

    def collaboration_responses(
        self, request_id: str
    ) -> tuple[CollaborationResponseRecord, ...]:
        return self._store.collaboration_responses(self.agent_id, request_id)

    def close_collaboration(
        self, request_id: str, *, idempotency_key: str
    ) -> CollaborationRequestRecord:
        return self._store.close_collaboration_request(
            request_id=request_id,
            requester_id=self.agent_id,
            idempotency_key=idempotency_key,
        )
