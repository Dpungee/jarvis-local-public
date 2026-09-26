"""The runtime-to-memory replay bridge (memory side, schema 51).

The multi-agent runtime is a separate SQLite authority.  It owns agents, rooms,
messages, tasks, delegation and capabilities, and it owns its own append-only
``runtime_events`` stream.  Memory owns M0-M5 and is a *follower*: every row this
module writes is derived from an accepted runtime event and is reconstructible by
replaying that stream.

Three properties hold everything else together.

**Memory never becomes a second control plane.**  This module writes only into
``memory_bridge_*`` tables.  It never touches claims, lessons, promotions,
milestones, the spine, or any other memory-authored row, so a rebuild can delete
and regenerate every row it owns without endangering data it did not create.
Ownership is expressed by table, not by a nullable column smeared across the
memory schema.

**Nothing is copied that memory cannot erase.**  The runtime is append-only by
design and has no erasure engine.  If a message body were copied here, an
operator erase would leave the runtime copy behind and the next rebuild would put
the memory copy back -- resurrection *by conformance*, because rebuild fidelity is
exactly what the contract asks for.  So the bridge retains structure, opaque
identifiers, closed enums, parsed timestamps, digests and lengths.  It retains no
message body, personality, purpose, description, result string, prompt, option,
display label or URI.  Hashing is not anonymization: a digest of a short value can
be guessed, and participants and references are themselves sensitive.

**Generations make "we added a family later" honest.**  A generation pins the
mapping, reducer and screening versions *and* the exact set of event types that
were deliberately ignored.  Adding a projection family does not quietly start
projecting new events while older ones stay behind a no-op; it opens a new
generation and replays the eligible history into it.  Older generations stay
readable, so a mapping change is a validated cutover rather than a silent
reinterpretation of existing rows.

Intervals (room membership, task ownership) are stored as **append-only
transitions**, not as mutable interval rows.  Every projection write is therefore
an INSERT, which makes retry idempotent by construction rather than by careful
UPDATE guards.  Half-open ``[start, end)`` intervals are *computed* from those
transitions by :func:`room_membership_intervals` and
:func:`task_ownership_intervals`.

This module deliberately does not import :mod:`jarvis.multi_agent_runtime`.  It
consumes plain mappings shaped like runtime events, so the memory side carries no
dependency on a Codex-owned module and can be tested without one.

Nothing here is agent-facing.  No projection is exposed to retrieval in this
slice; see ``CLAUDE_MEMORY_IMPLEMENTATION_HANDOFF_2026-09-16.md``.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping, Sequence

from .redaction import contains_private_identifier, contains_secret

BRIDGE_SCHEMA_VERSION = 54

#: Format versions.  These describe the *shape* the code understands, not the
#: operator's data: a generation pins the exact mapping rows separately.
REDUCER_VERSION = 1
SCREEN_VERSION = 1
MAPPING_FORMAT_VERSION = 1

#: The runtime's opaque identifier grammar, mirrored rather than imported.
_ID_RE = re.compile(r"\A(agt|msg|room|task|dlg|evt|cap|art|req|rsp|cur)_[0-9a-f]{32}\Z")
#: Trusted non-agent actors the runtime may name (documented in its handoff).
_RESERVED_ACTORS = frozenset({"owner", "system"})
_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")

#: Runtime project identifiers are free-form TEXT, exact and case-sensitive.
MAX_RUNTIME_PROJECT_CHARS = 200

#: The complete runtime vocabulary (30 types), mirrored as a memory-owned closed
#: set.  A type absent from both mappings halts: there is no default branch.
PROJECTED_EVENTS: frozenset[str] = frozenset({
    "agent.created", "agent.running", "agent.paused", "agent.stopped",
    "agent.model_changed", "agent.policy_changed",
    "message.direct_sent",
    "room.created", "room.agent_invited", "room.agent_joined", "room.agent_left",
    "room.message_sent",
    "task.created", "task.running", "task.blocked", "task.completed",
    "task.failed", "task.cancelled",
    "task.delegated",
    "artifact.shared",
    "collaboration.requested", "collaboration.responded", "collaboration.closed",
})

#: Deliberate no-ops, each with the reason recorded in the generation.  Seven,
#: per the contract reconciliation's correction 1.
IGNORED_EVENTS: dict[str, str] = {
    "message.direct_acknowledged": "delivery and read position are control-plane state",
    "room.cursor_advanced": "per-agent read position carries no memory meaning",
    "task.dependency_added": "deferred to the M4 dependency slice",
    "capability.requested": "authority is runtime-owned and memory does not enforce it",
    "capability.denied": "authority is runtime-owned and memory does not enforce it",
    "capability.granted": "authority is runtime-owned and memory does not enforce it",
    "capability.revoked": "authority is runtime-owned and memory does not enforce it",
}

KNOWN_EVENTS: frozenset[str] = frozenset(PROJECTED_EVENTS | set(IGNORED_EVENTS))

#: Runtime enums mirrored so an unexpected value is caught rather than stored.
_AGENT_LIFECYCLE = frozenset({"CREATED", "RUNNING", "PAUSED", "STOPPED"})
_TASK_STATUS = frozenset({
    "OPEN", "ASSIGNED", "RUNNING", "BLOCKED", "COMPLETED", "FAILED", "CANCELLED",
})
_SCOPE_KINDS = frozenset({"GLOBAL", "PROJECT", "AGENT", "TASK", "ROOM", "PRIVATE"})

#: Structural failures reported by a *trusted replay adapter* rather than by an
#: event we already hold.  The adapter classifies its own transport's exceptions
#: into this closed set; the bridge never parses an adapter's message text, and
#: an adapter that cannot classify a failure gets no durable halt at all.
READER_HALT_CODES: frozenset[str] = frozenset({
    # The adapter's history cannot serve this consumer's persisted cursor: a
    # different store, a truncated store, or a cursor that names no predecessor.
    "reader_cursor_mismatch",
    # The adapter holds the right history but it is not replayable: a sequence
    # gap, a content digest mismatch, or legacy events with no content.
    "reader_history_invalid",
    # The adapter was called outside its own contract.  A bug, not bad data, but
    # structural all the same: retrying unchanged cannot help.
    "reader_contract_violation",
})

#: Halt codes.  Structural problems only; content problems quarantine instead.
HALT_CODES: frozenset[str] = frozenset({
    "unknown_event_type",
    "unsupported_event_schema",
    "sequence_gap",
    "cursor_mismatch",
    "generation_mismatch",
    "version_mismatch",
    "legacy_schema_1",
    "malformed_event",
    "unmapped_project",
    "reducer_error",
}) | READER_HALT_CODES

#: Screening outcomes recorded on a quarantined optional field.
_SCREENS = ("secret", "private_identifier", "instruction_like", "invalid_enum")

#: Every outcome one ingest pass can have, whether it reached the bridge or
#: stopped at the worker.  Closed, because these are written to the database
#: and printed to operators: an open vocabulary is an open channel.
BRIDGE_PASS_STATUSES: frozenset[str] = frozenset({
    # The bridge ran.
    "ok", "idle", "bounded",
    # The bridge declined to run, for reasons that are not failures.
    "lease_held_elsewhere", "disabled", "unconfigured",
    "runtime_paused", "runtime_stopped", "runtime_running",
    "foreground_yield",
    # Failures.  Each of these means ingestion did not happen and something
    # is wrong; none of them may ever be rendered as healthy.
    "halted", "halt_not_recorded", "lease_lost",
    "reader_unavailable", "reader_error",
    "runtime_unavailable", "schema_missing", "error",
})

#: Outcomes that are not evidence of a problem.  ``bounded`` is here on
#: purpose: more work remaining is normal for a bounded pass, and it is
#: tracked as *backlog* rather than as a failure.  A sustained backlog is
#: still a problem, which is why backlog has its own reporting.
HEALTHY_PASS_STATUSES: frozenset[str] = frozenset({
    "ok", "idle", "bounded",
    "lease_held_elsewhere", "disabled", "unconfigured",
    "runtime_paused", "runtime_stopped", "runtime_running",
    "foreground_yield",
})

#: Failure outcomes.  Used as the primary key of the incident ledger, which
#: is what bounds that table: a flood of one failure is one row with a
#: counter, and the table can never hold more rows than there are codes.
INCIDENT_CODES: frozenset[str] = BRIDGE_PASS_STATUSES - HEALTHY_PASS_STATUSES

#: Do not rewrite the health row on every quiet pass.  A worker polling every
#: five seconds would otherwise commit a write, and grow the WAL, forever
#: while doing nothing at all.
HEALTH_HEARTBEAT_SECONDS = 60

_INSTRUCTION_LIKE = re.compile(
    r"(?is)\b(?:ignore|disregard|override|bypass|disable)\b.{0,60}"
    r"\b(?:instruction|policy|approval|safety|system|rule)\b"
    r"|\byou\s+are\s+now\b"
    r"|(?:^|\s)(?:system|assistant|developer|tool)\s*:"
)


#: Keys a halt detail may carry.  Closed, because a halt row is operator-facing
#: diagnostic state and an open key space is an open channel for runtime text.
_DETAIL_KEY_RE = re.compile(r"\A[a-z][a-z0-9_]{0,23}\Z")

#: A module-owned field path such as ``record.agent_id``.  Only the reserved
#: ``field`` key is rendered through this grammar, and every ``field=`` argument
#: in this module is a literal -- asserted by an AST test, not by convention.
_FIELD_TOKEN_RE = re.compile(r"\A[a-z][a-z0-9_]{0,24}(?:\.[a-z][a-z0-9_]{0,24})?\Z")

#: Every string a halt detail may render verbatim, beyond opaque runtime
#: identifiers.  All of it is authored here: event types we chose to mirror,
#: halt codes we defined, screens we implemented, enum members we pinned.  A
#: value the runtime supplied is *never* in this set unless it exactly equals a
#: token we already decided to name, which is the point.
_SAFE_TOKENS: frozenset[str] = frozenset(
    set(KNOWN_EVENTS)
    | set(HALT_CODES)
    | set(_SCREENS)
    | set(_AGENT_LIFECYCLE)
    | set(_TASK_STATUS)
    | set(_SCOPE_KINDS)
    | set(_RESERVED_ACTORS)
    | {"agent", "null", "true", "false"}
    # Locator words this module uses to say *where* something failed.
    | {"envelope", "record", "adapter", "cursor", "batch", "reducer", "lease"}
)


def _safe_detail(detail: Mapping[str, Any] | None) -> str:
    """Render a halt detail using closed codes and validated identifiers only.

    Runtime projects, room names, message bodies, event types we do not know and
    exception text are all *free-form runtime input*.  None of it may reach a
    persisted halt row, because that row is read by operators, printed by
    ``jarvis bridge status`` and preserved across rebuilds -- so it is exactly
    the kind of place private text goes to be forgotten about.

    Anything not provably safe renders as ``redacted``.  The locator an operator
    actually needs -- the runtime event id and sequence -- is opaque by the
    runtime's own grammar and survives, so the offending event stays findable in
    the runtime store without its content being copied here.
    """
    if not detail:
        return ""
    parts: list[str] = []
    for key in sorted(detail):
        if not _DETAIL_KEY_RE.match(str(key)):
            continue
        value = detail[key]
        if value is None:
            rendered = "null"
        elif isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, int):
            rendered = str(value)
        elif isinstance(value, str) and _ID_RE.match(value):
            rendered = value
        elif isinstance(value, str) and value in _SAFE_TOKENS:
            rendered = value
        elif key == "field" and isinstance(value, str) and _FIELD_TOKEN_RE.match(value):
            rendered = value
        else:
            rendered = "redacted"
        parts.append(f"{key}={rendered}")
    return " ".join(parts)


class BridgeError(RuntimeError):
    """A bridge operation failed closed.

    ``detail`` carries the *structured* facts a halt row may keep.  The message
    is for a developer reading a traceback and is never persisted: only
    :func:`_safe_detail` output reaches the database.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.detail: dict[str, Any] = dict(detail or {})


class BridgeHalted(BridgeError):
    """The consumer is halted and will not read until an operator resumes it."""


class LeaseLost(BridgeError):
    """This worker no longer holds the ingest lease, so it may not commit.

    Raised *inside* the batch transaction, which is what makes it useful: the
    caller's context manager rolls the transaction back, so a worker whose lease
    was taken over commits nothing at all rather than one last batch.
    """

    def __init__(self, message: str, *, detail: Mapping[str, Any] | None = None) -> None:
        super().__init__(message, code="lease_lost", detail=detail)


class ReaderFailure(BridgeError):
    """A trusted replay adapter could not serve a batch.

    ``code`` is ``None`` for an **operational** failure -- the transport could
    not be read *right now* (I/O error, a locked file, a store that has gone
    away).  Those are retried on the next pass and must never become a durable
    halt, because a halt an operator has to clear is the wrong answer to a
    transient error.

    A non-``None`` ``code`` must be a member of :data:`READER_HALT_CODES` and
    asserts a **structural** failure: the history the adapter holds cannot serve
    this consumer, and no amount of retrying will change that.  Those do become
    durable halts.

    The adapter classifies, because only the adapter knows its transport's
    exception types.  The bridge never inspects an adapter's message text, and
    an unclassified exception is treated as operational -- claiming a durable
    halt on an exception nobody classified would be an assertion the bridge
    cannot support.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        if code is not None and code not in READER_HALT_CODES:
            raise BridgeError(f"unknown reader halt code {code!r}")
        super().__init__(message, code=code, detail=detail)

    @property
    def structural(self) -> bool:
        return self.code is not None


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

BRIDGE_DDL: tuple[str, ...] = (
    # One generation pins everything that could change a projection's meaning.
    """CREATE TABLE IF NOT EXISTS memory_bridge_generations (
        id INTEGER PRIMARY KEY,
        created_at TEXT NOT NULL,
        reducer_version INTEGER NOT NULL CHECK(reducer_version > 0),
        screen_version INTEGER NOT NULL CHECK(screen_version > 0),
        mapping_format_version INTEGER NOT NULL CHECK(mapping_format_version > 0),
        projected_json TEXT NOT NULL,
        ignored_json TEXT NOT NULL,
        reason TEXT NOT NULL,
        superseded_at TEXT,
        superseded_by INTEGER,
        FOREIGN KEY(superseded_by) REFERENCES memory_bridge_generations(id)
    )""",
    # Project mappings are immutable per generation, so an old generation stays
    # reproducible after the operator changes the map.
    """CREATE TABLE IF NOT EXISTS memory_bridge_project_map (
        generation_id INTEGER NOT NULL,
        runtime_project_id TEXT NOT NULL,
        memory_project_id INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(generation_id, runtime_project_id),
        FOREIGN KEY(generation_id) REFERENCES memory_bridge_generations(id),
        FOREIGN KEY(memory_project_id) REFERENCES agent_projects(id)
    )""",
    # The paired cursor plus durable halt state.  One row per consumer.
    """CREATE TABLE IF NOT EXISTS memory_bridge_cursor (
        consumer TEXT PRIMARY KEY,
        generation_id INTEGER NOT NULL,
        last_sequence INTEGER NOT NULL DEFAULT 0 CHECK(last_sequence >= 0),
        last_event_id TEXT,
        status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','halted')),
        halt_code TEXT,
        halt_reason TEXT,
        halt_sequence INTEGER,
        halted_at TEXT,
        updated_at TEXT NOT NULL,
        CHECK((last_sequence = 0) = (last_event_id IS NULL)),
        CHECK((status = 'halted') = (halt_code IS NOT NULL)),
        CHECK((status = 'halted') = (halted_at IS NOT NULL)),
        CHECK(last_event_id IS NULL OR length(last_event_id) = 36),
        FOREIGN KEY(generation_id) REFERENCES memory_bridge_generations(id)
    )""",
    # One row per committed batch: what was projected, quarantined and ignored.
    """CREATE TABLE IF NOT EXISTS memory_bridge_batches (
        id INTEGER PRIMARY KEY,
        generation_id INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        first_sequence INTEGER NOT NULL CHECK(first_sequence > 0),
        last_sequence INTEGER NOT NULL CHECK(last_sequence > 0),
        event_count INTEGER NOT NULL CHECK(event_count > 0),
        projected_items INTEGER NOT NULL CHECK(projected_items >= 0),
        quarantined_items INTEGER NOT NULL CHECK(quarantined_items >= 0),
        ignored_events INTEGER NOT NULL CHECK(ignored_events >= 0),
        CHECK(last_sequence >= first_sequence),
        UNIQUE(generation_id, first_sequence, last_sequence),
        FOREIGN KEY(generation_id) REFERENCES memory_bridge_generations(id)
    )""",
    # A rejected optional field.  The value never lands: only its digest, the
    # screen that refused it, and enough structure to explain the decision.
    """CREATE TABLE IF NOT EXISTS memory_bridge_quarantine (
        id INTEGER PRIMARY KEY,
        generation_id INTEGER NOT NULL,
        runtime_event_id TEXT NOT NULL,
        runtime_sequence INTEGER NOT NULL CHECK(runtime_sequence > 0),
        item_key TEXT NOT NULL,
        field TEXT NOT NULL,
        screen TEXT NOT NULL,
        screen_version INTEGER NOT NULL CHECK(screen_version > 0),
        value_sha256 TEXT NOT NULL CHECK(length(value_sha256) = 64),
        value_length INTEGER NOT NULL CHECK(value_length >= 0),
        quarantined_at TEXT NOT NULL,
        UNIQUE(generation_id, runtime_event_id, item_key, field),
        FOREIGN KEY(generation_id) REFERENCES memory_bridge_generations(id)
    )""",
    # Operator suppression survives rebuilds and generation changes: it is keyed
    # on the runtime event, not on a generation.
    """CREATE TABLE IF NOT EXISTS memory_bridge_suppressions (
        runtime_event_id TEXT NOT NULL,
        item_key TEXT NOT NULL,
        reason_code TEXT NOT NULL,
        decided_by TEXT NOT NULL,
        decided_at TEXT NOT NULL,
        released_at TEXT,
        released_by TEXT,
        PRIMARY KEY(runtime_event_id, item_key)
    )""",
    # ---- projections; every one is INSERT-only ----
    """CREATE TABLE IF NOT EXISTS memory_bridge_agents (
        generation_id INTEGER NOT NULL,
        agent_id TEXT NOT NULL,
        first_sequence INTEGER NOT NULL CHECK(first_sequence > 0),
        runtime_event_id TEXT NOT NULL,
        memory_project_id INTEGER,
        created_by_kind TEXT NOT NULL CHECK(created_by_kind IN ('agent','owner','system')),
        PRIMARY KEY(generation_id, agent_id),
        FOREIGN KEY(generation_id) REFERENCES memory_bridge_generations(id)
    )""",
    """CREATE TABLE IF NOT EXISTS memory_bridge_agent_events (
        generation_id INTEGER NOT NULL,
        runtime_event_id TEXT NOT NULL,
        item_key TEXT NOT NULL,
        runtime_sequence INTEGER NOT NULL CHECK(runtime_sequence > 0),
        agent_id TEXT NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN
            ('created','running','paused','stopped','model_changed','policy_changed')),
        lifecycle TEXT CHECK(lifecycle IS NULL OR lifecycle IN
            ('CREATED','RUNNING','PAUSED','STOPPED')),
        -- A digest of the model binding or the policy tuple, depending on kind.
        -- Memory records *that* a binding or policy changed, never the authority
        -- value itself: capability events are no-ops for the same reason, and an
        -- authority value sitting in memory invites being mistaken for a grant.
        detail_sha256 TEXT CHECK(detail_sha256 IS NULL OR length(detail_sha256) = 64),
        occurred_at REAL NOT NULL,
        PRIMARY KEY(generation_id, runtime_event_id, item_key),
        FOREIGN KEY(generation_id) REFERENCES memory_bridge_generations(id)
    )""",
    """CREATE TABLE IF NOT EXISTS memory_bridge_rooms (
        generation_id INTEGER NOT NULL,
        room_id TEXT NOT NULL,
        first_sequence INTEGER NOT NULL CHECK(first_sequence > 0),
        runtime_event_id TEXT NOT NULL,
        memory_project_id INTEGER,
        created_by TEXT NOT NULL,
        -- Closed enum, so it is retained.  NULL means the runtime sent a value
        -- outside the mirrored set: the room row still lands and the field is
        -- quarantined, because losing the room would lose membership evidence.
        access TEXT CHECK(access IS NULL OR access IN ('INVITE_ONLY','OPEN')),
        -- ``name`` and ``kind`` are free-form runtime text and are never retained.
        name_sha256 TEXT NOT NULL CHECK(length(name_sha256) = 64),
        kind_sha256 TEXT NOT NULL CHECK(length(kind_sha256) = 64),
        PRIMARY KEY(generation_id, room_id),
        FOREIGN KEY(generation_id) REFERENCES memory_bridge_generations(id)
    )""",
    """CREATE TABLE IF NOT EXISTS memory_bridge_tasks (
        generation_id INTEGER NOT NULL,
        task_id TEXT NOT NULL,
        first_sequence INTEGER NOT NULL CHECK(first_sequence > 0),
        runtime_event_id TEXT NOT NULL,
        memory_project_id INTEGER,
        created_by TEXT NOT NULL,
        parent_task_id TEXT,
        PRIMARY KEY(generation_id, task_id),
        FOREIGN KEY(generation_id) REFERENCES memory_bridge_generations(id)
    )""",
    # Append-only membership transitions.  Half-open intervals are computed.
    """CREATE TABLE IF NOT EXISTS memory_bridge_room_members (
        generation_id INTEGER NOT NULL,
        runtime_event_id TEXT NOT NULL,
        item_key TEXT NOT NULL,
        runtime_sequence INTEGER NOT NULL CHECK(runtime_sequence > 0),
        room_id TEXT NOT NULL,
        agent_id TEXT NOT NULL,
        transition TEXT NOT NULL CHECK(transition IN ('invited','joined','left')),
        occurred_at REAL NOT NULL,
        PRIMARY KEY(generation_id, runtime_event_id, item_key),
        FOREIGN KEY(generation_id) REFERENCES memory_bridge_generations(id)
    )""",
    """CREATE TABLE IF NOT EXISTS memory_bridge_task_owner_events (
        generation_id INTEGER NOT NULL,
        runtime_event_id TEXT NOT NULL,
        item_key TEXT NOT NULL,
        runtime_sequence INTEGER NOT NULL CHECK(runtime_sequence > 0),
        task_id TEXT NOT NULL,
        owner_agent_id TEXT,
        transition TEXT NOT NULL CHECK(transition IN ('created','delegated')),
        from_agent_id TEXT,
        occurred_at REAL NOT NULL,
        PRIMARY KEY(generation_id, runtime_event_id, item_key),
        FOREIGN KEY(generation_id) REFERENCES memory_bridge_generations(id)
    )""",
    """CREATE TABLE IF NOT EXISTS memory_bridge_task_status_events (
        generation_id INTEGER NOT NULL,
        runtime_event_id TEXT NOT NULL,
        item_key TEXT NOT NULL,
        runtime_sequence INTEGER NOT NULL CHECK(runtime_sequence > 0),
        task_id TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN
            ('OPEN','ASSIGNED','RUNNING','BLOCKED','COMPLETED','FAILED','CANCELLED')),
        memory_project_id INTEGER,
        occurred_at REAL NOT NULL,
        PRIMARY KEY(generation_id, runtime_event_id, item_key),
        FOREIGN KEY(generation_id) REFERENCES memory_bridge_generations(id)
    )""",
    """CREATE TABLE IF NOT EXISTS memory_bridge_messages (
        generation_id INTEGER NOT NULL,
        runtime_event_id TEXT NOT NULL,
        item_key TEXT NOT NULL,
        runtime_sequence INTEGER NOT NULL CHECK(runtime_sequence > 0),
        message_id TEXT NOT NULL,
        channel TEXT NOT NULL CHECK(channel IN ('direct','room')),
        room_id TEXT,
        task_id TEXT,
        reply_to_message_id TEXT,
        body_sha256 TEXT NOT NULL CHECK(length(body_sha256) = 64),
        body_length INTEGER NOT NULL CHECK(body_length >= 0),
        occurred_at REAL NOT NULL,
        CHECK((channel = 'room') = (room_id IS NOT NULL)),
        PRIMARY KEY(generation_id, runtime_event_id, item_key),
        FOREIGN KEY(generation_id) REFERENCES memory_bridge_generations(id)
    )""",
    """CREATE TABLE IF NOT EXISTS memory_bridge_participants (
        generation_id INTEGER NOT NULL,
        runtime_event_id TEXT NOT NULL,
        item_key TEXT NOT NULL,
        runtime_sequence INTEGER NOT NULL CHECK(runtime_sequence > 0),
        agent_id TEXT NOT NULL,
        -- A room message has exactly one recorded participant, its sender.  The
        -- runtime does not name recipients for a room message and the bridge does
        -- not invent them; who could read it is decided from membership evidence.
        role TEXT NOT NULL CHECK(role IN ('sender','recipient')),
        PRIMARY KEY(generation_id, runtime_event_id, item_key),
        FOREIGN KEY(generation_id) REFERENCES memory_bridge_generations(id)
    )""",
    """CREATE TABLE IF NOT EXISTS memory_bridge_artifacts (
        generation_id INTEGER NOT NULL,
        runtime_event_id TEXT NOT NULL,
        item_key TEXT NOT NULL,
        runtime_sequence INTEGER NOT NULL CHECK(runtime_sequence > 0),
        artifact_id TEXT NOT NULL,
        shared_by TEXT NOT NULL,
        task_id TEXT,
        room_id TEXT,
        uri_sha256 TEXT NOT NULL CHECK(length(uri_sha256) = 64),
        media_type_sha256 TEXT NOT NULL CHECK(length(media_type_sha256) = 64),
        content_sha256 TEXT CHECK(content_sha256 IS NULL OR length(content_sha256) = 64),
        byte_count INTEGER CHECK(byte_count IS NULL OR byte_count >= 0),
        occurred_at REAL NOT NULL,
        PRIMARY KEY(generation_id, runtime_event_id, item_key),
        FOREIGN KEY(generation_id) REFERENCES memory_bridge_generations(id)
    )""",
    """CREATE TABLE IF NOT EXISTS memory_bridge_collaboration (
        generation_id INTEGER NOT NULL,
        runtime_event_id TEXT NOT NULL,
        item_key TEXT NOT NULL,
        runtime_sequence INTEGER NOT NULL CHECK(runtime_sequence > 0),
        request_id TEXT NOT NULL,
        response_id TEXT,
        kind TEXT NOT NULL CHECK(kind IN ('requested','responded','closed')),
        requester_id TEXT,
        responder_id TEXT,
        room_id TEXT,
        task_id TEXT,
        prompt_sha256 TEXT CHECK(prompt_sha256 IS NULL OR length(prompt_sha256) = 64),
        body_sha256 TEXT CHECK(body_sha256 IS NULL OR length(body_sha256) = 64),
        occurred_at REAL NOT NULL,
        PRIMARY KEY(generation_id, runtime_event_id, item_key),
        FOREIGN KEY(generation_id) REFERENCES memory_bridge_generations(id)
    )""",
)

BRIDGE_INDEXES: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_bridge_agent_events_agent"
    " ON memory_bridge_agent_events(generation_id, agent_id, runtime_sequence)",
    "CREATE INDEX IF NOT EXISTS idx_bridge_room_members_lookup"
    " ON memory_bridge_room_members(generation_id, room_id, agent_id, runtime_sequence)",
    "CREATE INDEX IF NOT EXISTS idx_bridge_task_owner_lookup"
    " ON memory_bridge_task_owner_events(generation_id, task_id, runtime_sequence)",
    "CREATE INDEX IF NOT EXISTS idx_bridge_task_status_lookup"
    " ON memory_bridge_task_status_events(generation_id, task_id, runtime_sequence)",
    "CREATE INDEX IF NOT EXISTS idx_bridge_participants_agent"
    " ON memory_bridge_participants(generation_id, agent_id)",
    "CREATE INDEX IF NOT EXISTS idx_bridge_messages_lookup"
    " ON memory_bridge_messages(generation_id, message_id)",
    "CREATE INDEX IF NOT EXISTS idx_bridge_quarantine_event"
    " ON memory_bridge_quarantine(generation_id, runtime_event_id)",
)

#: Every table this module owns.  A rebuild may delete from these and nothing
#: else; the list is asserted by a test against ``sqlite_master``.
BRIDGE_PROJECTION_TABLES: tuple[str, ...] = (
    "memory_bridge_agents",
    "memory_bridge_agent_events",
    "memory_bridge_rooms",
    "memory_bridge_tasks",
    "memory_bridge_room_members",
    "memory_bridge_task_owner_events",
    "memory_bridge_task_status_events",
    "memory_bridge_messages",
    "memory_bridge_participants",
    "memory_bridge_artifacts",
    "memory_bridge_collaboration",
)

BRIDGE_CONTROL_TABLES: tuple[str, ...] = (
    "memory_bridge_generations",
    "memory_bridge_project_map",
    "memory_bridge_cursor",
    "memory_bridge_batches",
    "memory_bridge_quarantine",
    "memory_bridge_suppressions",
    # Schema 52.  Listed here so "every table the bridge owns" stays one list.
    "memory_bridge_lease",
    "memory_bridge_resumes",
    # Schema 53.
    "memory_bridge_rebuilds",
    # Schema 54.
    "memory_bridge_health",
    "memory_bridge_incidents",
)

DEFAULT_CONSUMER = "runtime_bridge"


def bridge_ready(db: sqlite3.Connection) -> bool:
    """True when the schema-51 bridge tables exist."""
    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_bridge_cursor'"
    ).fetchone()
    return row is not None


def migrate_bridge_v51(db: sqlite3.Connection, *, now: str) -> dict[str, Any]:
    """Create the bridge tables inside the caller's write transaction.

    Purely additive: no existing table is read, altered, renamed or dropped, and
    no memory-authored row is touched.  A store that already carries the tables
    is left alone, so re-running the migration is safe.
    """
    if not db.in_transaction:
        raise BridgeError("the bridge migration runs inside a write transaction")
    existed = bridge_ready(db)
    for statement in BRIDGE_DDL:
        db.execute(statement)
    for statement in BRIDGE_INDEXES:
        db.execute(statement)
    created = 0 if existed else 1
    if not existed:
        generation_id = create_generation(
            db, now=now, reason="schema 51 initial generation"
        )
        db.execute(
            """INSERT INTO memory_bridge_cursor(
                   consumer, generation_id, last_sequence, last_event_id,
                   status, updated_at)
               VALUES (?, ?, 0, NULL, 'active', ?)""",
            (DEFAULT_CONSUMER, generation_id, now),
        )
    return {"created": created, "tables": len(BRIDGE_DDL)}


# ---------------------------------------------------------------------------
# Generations and project mapping
# ---------------------------------------------------------------------------

def create_generation(
    db: sqlite3.Connection,
    *,
    now: str,
    reason: str,
    projected: Iterable[str] | None = None,
    ignored: Mapping[str, str] | None = None,
) -> int:
    """Open a generation pinning the versions and the exact ignored set."""
    projected_set = sorted(PROJECTED_EVENTS if projected is None else set(projected))
    ignored_map = dict(IGNORED_EVENTS if ignored is None else ignored)
    overlap = set(projected_set) & set(ignored_map)
    if overlap:
        raise BridgeError(f"event types both projected and ignored: {sorted(overlap)}")
    classified = set(projected_set) | set(ignored_map)
    if classified != set(KNOWN_EVENTS):
        missing = sorted(set(KNOWN_EVENTS) - classified)
        extra = sorted(classified - set(KNOWN_EVENTS))
        raise BridgeError(
            "a generation must classify every known runtime event type "
            f"(unclassified={missing}, unknown={extra})"
        )
    without_reducer = sorted(set(projected_set) - set(REDUCERS))
    if without_reducer:
        # Pinning a type as projected when no reducer serves it would turn every
        # later batch carrying it into a reducer_error halt.  Refuse at the point
        # the generation is opened, where the mistake is still cheap.
        raise BridgeError(
            f"no reducer implements these projected types: {without_reducer}"
        )
    cursor = db.execute(
        """INSERT INTO memory_bridge_generations(
               created_at, reducer_version, screen_version, mapping_format_version,
               projected_json, ignored_json, reason)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            now,
            REDUCER_VERSION,
            SCREEN_VERSION,
            MAPPING_FORMAT_VERSION,
            json.dumps(projected_set, sort_keys=True, separators=(",", ":")),
            json.dumps(ignored_map, sort_keys=True, separators=(",", ":")),
            str(reason)[:500],
        ),
    )
    return int(cursor.lastrowid)


def active_generation(db: sqlite3.Connection, *, consumer: str = DEFAULT_CONSUMER) -> int:
    row = db.execute(
        "SELECT generation_id FROM memory_bridge_cursor WHERE consumer=?", (consumer,)
    ).fetchone()
    if row is None:
        raise BridgeError("no bridge consumer row; run the schema 51 migration")
    return int(row[0])


def generation_contract(db: sqlite3.Connection, generation_id: int) -> dict[str, Any]:
    row = db.execute(
        """SELECT id, reducer_version, screen_version, mapping_format_version,
                  projected_json, ignored_json, reason, superseded_at
           FROM memory_bridge_generations WHERE id=?""",
        (int(generation_id),),
    ).fetchone()
    if row is None:
        raise BridgeError(f"unknown generation {generation_id}")
    return {
        "id": int(row[0]),
        "reducer_version": int(row[1]),
        "screen_version": int(row[2]),
        "mapping_format_version": int(row[3]),
        "projected": frozenset(json.loads(row[4])),
        "ignored": dict(json.loads(row[5])),
        "reason": str(row[6]),
        "superseded_at": row[7],
    }


def set_project_mapping(
    db: sqlite3.Connection,
    generation_id: int,
    mapping: Mapping[str, int],
    *,
    now: str,
) -> int:
    """Pin exact runtime-project strings to existing memory project ids.

    Exact and case-sensitive.  No coercion, no invented projects, and -- in this
    contract -- no many-to-one aliases: two runtime strings may not share one
    memory project, because that would silently merge two histories.
    """
    generation_contract(db, generation_id)
    seen: dict[int, str] = {}
    written = 0
    for runtime_project_id, memory_project_id in mapping.items():
        if not isinstance(runtime_project_id, str) or not runtime_project_id:
            raise BridgeError("runtime project id must be a non-empty string")
        if len(runtime_project_id) > MAX_RUNTIME_PROJECT_CHARS:
            raise BridgeError("runtime project id exceeds the runtime's own bound")
        if isinstance(memory_project_id, bool) or not isinstance(memory_project_id, int):
            raise BridgeError("memory project id must be an integer")
        if memory_project_id <= 0:
            raise BridgeError("memory project id must be positive")
        if db.execute(
            "SELECT 1 FROM agent_projects WHERE id=?", (memory_project_id,)
        ).fetchone() is None:
            raise BridgeError(
                f"memory project {memory_project_id} does not exist; "
                "the bridge never creates projects"
            )
        if memory_project_id in seen:
            raise BridgeError(
                "many-to-one project aliases are not permitted in this contract: "
                f"{seen[memory_project_id]!r} and {runtime_project_id!r}"
            )
        seen[memory_project_id] = runtime_project_id
        db.execute(
            """INSERT INTO memory_bridge_project_map(
                   generation_id, runtime_project_id, memory_project_id, created_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(generation_id, runtime_project_id) DO NOTHING""",
            (int(generation_id), runtime_project_id, int(memory_project_id), now),
        )
        written += 1
    return written


def resolve_project(
    db: sqlite3.Connection,
    generation_id: int,
    runtime_project_id: Any,
    *,
    source: str = "envelope",
) -> int | None:
    """Exact lookup.  Null means absence of a project, never a wildcard.

    ``source`` says which half of the event carried the unmapped string --
    ``envelope`` or ``record`` -- so a halt names where to look without naming
    what was there.
    """
    if runtime_project_id is None:
        return None
    if not isinstance(runtime_project_id, str):
        raise BridgeError(
            "runtime project id must be a string or null",
            code="malformed_event",
            detail={"field": "project_id", "source": source},
        )
    row = db.execute(
        """SELECT memory_project_id FROM memory_bridge_project_map
           WHERE generation_id=? AND runtime_project_id=?""",
        (int(generation_id), runtime_project_id),
    ).fetchone()
    if row is None:
        # The project string itself is free-form runtime text and is deliberately
        # absent from ``detail``: the halt row records *which generation* lacks a
        # mapping and, once ``import_batch`` enriches it, which event to look up
        # in the runtime store.  The operator reads the string from the runtime,
        # which owns it, rather than from a memory diagnostic that outlives it.
        raise BridgeError(
            f"runtime project {runtime_project_id!r} is not mapped in generation "
            f"{generation_id}",
            code="unmapped_project",
            detail={"generation": int(generation_id), "source": source},
        )
    return int(row[0])


# ---------------------------------------------------------------------------
# Small validators.  A free-form string never becomes safe by being called an id.
# ---------------------------------------------------------------------------

def _identifier(value: Any, prefix: str, *, field: str) -> str:
    text = str(value)
    if not _ID_RE.match(text) or not text.startswith(f"{prefix}_"):
        raise BridgeError(
            f"{field} is not a {prefix}_ runtime identifier",
            code="malformed_event",
            detail={"field": field},
        )
    return text


def _actor(value: Any, *, field: str = "actor_id") -> tuple[str, str]:
    """Return ``(actor_id, kind)``; ``owner``/``system`` are not agents."""
    text = str(value)
    if text in _RESERVED_ACTORS:
        return text, text
    return _identifier(text, "agt", field=field), "agent"


def _enum(value: Any, allowed: frozenset[str], *, field: str) -> str:
    text = str(value)
    if text not in allowed:
        raise BridgeError(
            f"{field} has an unexpected value",
            code="malformed_event",
            detail={"field": field},
        )
    return text


def _timestamp(value: Any, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BridgeError(
            f"{field} must be an epoch number",
            code="malformed_event",
            detail={"field": field},
        )
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")) or number < 0:
        raise BridgeError(
            f"{field} is not a usable timestamp",
            code="malformed_event",
            detail={"field": field},
        )
    return number


def _optional_identifier(value: Any, prefix: str, *, field: str) -> str | None:
    if value is None:
        return None
    return _identifier(value, prefix, field=field)


def sha256_text(value: Any) -> str:
    """Digest of a text field.  Never anonymization -- short values are guessable."""
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _digest_and_length(value: Any) -> tuple[str, int]:
    text = "" if value is None else str(value)
    return sha256_text(text), len(text)


def screen_text(value: str) -> str | None:
    """Return the screen that refuses this retained text, or None.

    Implemented and unit-tested.  **No projection field currently routes text
    through it**, because this slice retains no free text at all; it exists so a
    later slice that does retain a field has one deterministic, versioned place
    to put it.  That limitation is stated rather than papered over.
    """
    text = str(value)
    if contains_secret(text):
        return "secret"
    if contains_private_identifier(text):
        return "private_identifier"
    if _INSTRUCTION_LIKE.search(text):
        return "instruction_like"
    return None


# ---------------------------------------------------------------------------
# Event normalization
# ---------------------------------------------------------------------------

_ENVELOPE_FIELDS = (
    "sequence", "event_id", "event_type", "schema_version", "occurred_at",
    "actor_id", "project_id", "scope_kind", "scope_id", "subject_kind",
    "subject_id", "idempotency_key", "command_sha256", "payload", "result_kind",
    "result_id",
)

#: Event schema versions this reducer generation understands.  Schema 1 carries
#: no historical content and is refused rather than guessed at.
SUPPORTED_EVENT_SCHEMA = 2


def _plain(value: Any) -> Any:
    """Unwrap an Enum to its value without importing the runtime's enums."""
    return getattr(value, "value", value)


def _as_mapping(event: Any) -> Mapping[str, Any]:
    """Accept a mapping or the runtime's ``RuntimeEvent`` dataclass.

    The dataclass names the scope field ``scope`` and carries a ``RuntimeScope``
    enum; the stored column is ``scope_kind`` and holds its text.  Both spellings
    are accepted so the bridge can consume ``replay_events()`` directly, and both
    are reduced to plain scalars before anything downstream inspects them.
    """
    if isinstance(event, Mapping):
        present = set(event)
        raw_get = event.get
    else:
        present = {name for name in dir(event) if not name.startswith("_")}

        def raw_get(name: str, default: Any = None) -> Any:
            return getattr(event, name, default)

    scope_kind = raw_get("scope_kind") if "scope_kind" in present else raw_get("scope")
    missing = [
        name
        for name in _ENVELOPE_FIELDS
        if name != "scope_kind" and name not in present
    ]
    if missing or scope_kind is None:
        raise BridgeError(
            f"runtime event is missing envelope fields {missing or ['scope_kind']}",
            code="malformed_event",
            # The *names* are ours, but a list of them is not a closed token, so
            # only the count is kept.  An operator with the event id can see the
            # envelope itself in the runtime store.
            detail={"field": "envelope", "missing": len(missing) or 1},
        )
    normalized = {
        name: _plain(raw_get(name))
        for name in _ENVELOPE_FIELDS
        if name != "scope_kind"
    }
    normalized["scope_kind"] = _plain(scope_kind)
    return normalized


def _normalize_event(event: Any) -> dict[str, Any]:
    """Validate one runtime event envelope without trusting any of its text."""
    raw = _as_mapping(event)
    schema_version = raw.get("schema_version")
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise BridgeError(
            "event schema_version must be an integer",
            code="malformed_event",
            detail={"field": "schema_version"},
        )
    if schema_version == 1:
        raise BridgeError(
            "runtime event schema 1 carries no reconstructible historical content",
            code="legacy_schema_1",
            detail={"schema": 1, "supported": SUPPORTED_EVENT_SCHEMA},
        )
    if schema_version != SUPPORTED_EVENT_SCHEMA:
        raise BridgeError(
            f"unsupported runtime event schema {schema_version}",
            code="unsupported_event_schema",
            detail={"schema": int(schema_version), "supported": SUPPORTED_EVENT_SCHEMA},
        )
    sequence = raw.get("sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
        raise BridgeError(
            "event sequence must be a positive integer",
            code="malformed_event",
            detail={"field": "sequence"},
        )
    event_type = str(raw.get("event_type") or "")
    if event_type not in KNOWN_EVENTS:
        # The type string is runtime-supplied and, by definition, outside every
        # vocabulary this module owns, so it is not rendered into the halt row.
        raise BridgeError(
            f"unknown runtime event type {event_type!r}",
            code="unknown_event_type",
            detail={"field": "event_type", "known": False},
        )
    payload = raw.get("payload")
    if not isinstance(payload, Mapping):
        raise BridgeError(
            "event payload must be a mapping",
            code="malformed_event",
            detail={"field": "payload"},
        )
    record = payload.get("record")
    record_sha256 = payload.get("record_sha256")
    if event_type in PROJECTED_EVENTS:
        if not isinstance(record, Mapping):
            raise BridgeError(
                "projected events must carry payload.record",
                code="malformed_event",
                detail={"field": "payload.record"},
            )
        if not isinstance(record_sha256, str) or not _SHA256_RE.match(record_sha256):
            raise BridgeError(
                "projected events must carry a payload.record_sha256 digest",
                code="malformed_event",
                detail={"field": "payload.record_sha256"},
            )
    actor_id, actor_kind = _actor(raw.get("actor_id"))
    scope_kind = _enum(raw.get("scope_kind"), _SCOPE_KINDS, field="scope_kind")
    return {
        "sequence": sequence,
        "event_id": _identifier(raw.get("event_id"), "evt", field="event_id"),
        "event_type": event_type,
        "schema_version": schema_version,
        "occurred_at": _timestamp(raw.get("occurred_at"), field="occurred_at"),
        "actor_id": actor_id,
        "actor_kind": actor_kind,
        "project_id": raw.get("project_id"),
        "scope_kind": scope_kind,
        "scope_id": raw.get("scope_id"),
        "subject_kind": raw.get("subject_kind"),
        "subject_id": raw.get("subject_id"),
        "record": dict(record) if isinstance(record, Mapping) else {},
        "record_sha256": record_sha256,
    }


# ---------------------------------------------------------------------------
# Reducers.  Closed dispatch: a type not in the table halts, there is no default.
# ---------------------------------------------------------------------------

class _Item:
    """One projected row plus any fields the screens refused."""

    __slots__ = ("table", "item_key", "columns", "quarantine")

    def __init__(
        self,
        table: str,
        item_key: str,
        columns: dict[str, Any],
        quarantine: list[dict[str, Any]] | None = None,
    ) -> None:
        if table not in BRIDGE_PROJECTION_TABLES:
            raise BridgeError(f"reducer produced an unknown table {table!r}")
        self.table = table
        self.item_key = item_key
        self.columns = columns
        self.quarantine = quarantine or []


def _closed_or_quarantine(
    value: Any, allowed: frozenset[str], *, field: str, item_key: str
) -> tuple[str | None, list[dict[str, Any]]]:
    """Retain a closed-enum value, or quarantine the field and keep the row.

    Losing the whole row would lose structural evidence (a room carries its
    membership); losing one optional label loses only the label.
    """
    text = "" if value is None else str(value)
    if text in allowed:
        return text, []
    digest, length = _digest_and_length(text)
    return None, [{
        "item_key": item_key,
        "field": field,
        "screen": "invalid_enum",
        "value_sha256": digest,
        "value_length": length,
    }]


def _reduce_agent(event: dict[str, Any], project_id: int | None) -> list[_Item]:
    record = event["record"]
    agent_id = _identifier(record.get("agent_id"), "agt", field="record.agent_id")
    kind = event["event_type"].split(".", 1)[1]
    items: list[_Item] = []
    if kind == "created":
        _actor(record.get("created_by"), field="record.created_by")
        created_by_kind = (
            str(record.get("created_by"))
            if str(record.get("created_by")) in _RESERVED_ACTORS
            else "agent"
        )
        items.append(_Item("memory_bridge_agents", "agent", {
            "agent_id": agent_id,
            "first_sequence": event["sequence"],
            "runtime_event_id": event["event_id"],
            "memory_project_id": project_id,
            "created_by_kind": created_by_kind,
        }))
    lifecycle: str | None = None
    detail: str | None = None
    if kind in {"created", "running", "paused", "stopped"}:
        lifecycle = _enum(record.get("lifecycle"), _AGENT_LIFECYCLE, field="record.lifecycle")
    if kind in {"created", "model_changed"}:
        detail = sha256_text(
            f"{record.get('model_provider')} {record.get('model_name')}"
        )
    if kind == "policy_changed":
        detail = sha256_text(
            f"{record.get('autonomy')} {record.get('authority')}"
            f" {record.get('request_policy')}"
        )
    items.append(_Item("memory_bridge_agent_events", "agent_event", {
        "agent_id": agent_id,
        "kind": kind,
        "lifecycle": lifecycle,
        "detail_sha256": detail,
        "occurred_at": event["occurred_at"],
    }))
    return items


def _reduce_room(event: dict[str, Any], project_id: int | None) -> list[_Item]:
    record = event["record"]
    room_id = _identifier(record.get("room_id"), "room", field="record.room_id")
    event_type = event["event_type"]
    items: list[_Item] = []
    if event_type == "room.created":
        created_by = _identifier(record.get("created_by"), "agt", field="record.created_by")
        access, quarantine = _closed_or_quarantine(
            record.get("access"),
            frozenset({"INVITE_ONLY", "OPEN"}),
            field="access",
            item_key="room",
        )
        items.append(_Item("memory_bridge_rooms", "room", {
            "room_id": room_id,
            "first_sequence": event["sequence"],
            "runtime_event_id": event["event_id"],
            "memory_project_id": project_id,
            "created_by": created_by,
            "access": access,
            "name_sha256": sha256_text(record.get("name")),
            "kind_sha256": sha256_text(record.get("kind")),
        }, quarantine))
        # Verified against the runtime: create_room inserts the creator's
        # membership row before appending room.created, so the creator is a
        # member from this sequence onward.
        items.append(_Item("memory_bridge_room_members", "creator_joined", {
            "room_id": room_id,
            "agent_id": created_by,
            "transition": "joined",
            "occurred_at": event["occurred_at"],
        }))
        return items
    transition = {
        "room.agent_invited": "invited",
        "room.agent_joined": "joined",
        "room.agent_left": "left",
    }[event_type]
    # The membership subject is the event's ``subject_id``, not its actor and not
    # the room record.  Verified against the runtime: ``room.agent_invited``
    # carries scope ROOM/<room_id> with the *invitee* as the subject while the
    # actor is the inviter, so reading the actor would attribute an invitation to
    # the wrong agent.  ``joined``/``left`` happen to have subject == actor, which
    # is exactly why this needed checking rather than assuming.
    agent_id = (
        event.get("subject_id")
        if event.get("subject_kind") == "agent"
        else event["actor_id"]
    )
    items.append(_Item("memory_bridge_room_members", f"member_{transition}", {
        "room_id": room_id,
        "agent_id": _identifier(agent_id, "agt", field="membership.agent_id"),
        "transition": transition,
        "occurred_at": event["occurred_at"],
    }))
    return items


def _reduce_task(event: dict[str, Any], project_id: int | None) -> list[_Item]:
    record = event["record"]
    task_id = _identifier(record.get("task_id"), "task", field="record.task_id")
    status = _enum(record.get("status"), _TASK_STATUS, field="record.status")
    items: list[_Item] = []
    if event["event_type"] == "task.created":
        _actor(record.get("created_by"), field="record.created_by")
        items.append(_Item("memory_bridge_tasks", "task", {
            "task_id": task_id,
            "first_sequence": event["sequence"],
            "runtime_event_id": event["event_id"],
            "memory_project_id": project_id,
            "created_by": str(record.get("created_by")),
            "parent_task_id": _optional_identifier(
                record.get("parent_task_id"), "task", field="record.parent_task_id"
            ),
        }))
        items.append(_Item("memory_bridge_task_owner_events", "owner_created", {
            "task_id": task_id,
            "owner_agent_id": _optional_identifier(
                record.get("owner_id"), "agt", field="record.owner_id"
            ),
            "transition": "created",
            "from_agent_id": None,
            "occurred_at": event["occurred_at"],
        }))
    items.append(_Item("memory_bridge_task_status_events", "status", {
        "task_id": task_id,
        "status": status,
        "memory_project_id": project_id,
        "occurred_at": event["occurred_at"],
    }))
    return items


def _reduce_task_delegated(event: dict[str, Any], project_id: int | None) -> list[_Item]:
    record = event["record"]
    return [_Item("memory_bridge_task_owner_events", "owner_delegated", {
        "task_id": _identifier(record.get("task_id"), "task", field="record.task_id"),
        "owner_agent_id": _identifier(
            record.get("to_agent_id"), "agt", field="record.to_agent_id"
        ),
        "transition": "delegated",
        "from_agent_id": _identifier(
            record.get("from_agent_id"), "agt", field="record.from_agent_id"
        ),
        "occurred_at": event["occurred_at"],
    })]


def _reduce_direct_message(event: dict[str, Any], project_id: int | None) -> list[_Item]:
    record = event["record"]
    message_id = _identifier(record.get("message_id"), "msg", field="record.message_id")
    sender = _identifier(record.get("sender_id"), "agt", field="record.sender_id")
    recipient = _identifier(record.get("recipient_id"), "agt", field="record.recipient_id")
    body_sha256, body_length = _digest_and_length(record.get("body"))
    return [
        _Item("memory_bridge_messages", "message", {
            "message_id": message_id,
            "channel": "direct",
            "room_id": None,
            "task_id": _optional_identifier(
                record.get("task_id"), "task", field="record.task_id"
            ),
            "reply_to_message_id": _optional_identifier(
                record.get("reply_to_message_id"), "msg", field="record.reply_to_message_id"
            ),
            "body_sha256": body_sha256,
            "body_length": body_length,
            "occurred_at": event["occurred_at"],
        }),
        _Item("memory_bridge_participants", "sender", {
            "agent_id": sender, "role": "sender",
        }),
        _Item("memory_bridge_participants", "recipient", {
            "agent_id": recipient, "role": "recipient",
        }),
    ]


def _reduce_room_message(event: dict[str, Any], project_id: int | None) -> list[_Item]:
    record = event["record"]
    body_sha256, body_length = _digest_and_length(record.get("body"))
    return [
        _Item("memory_bridge_messages", "message", {
            "message_id": _identifier(
                record.get("message_id"), "msg", field="record.message_id"
            ),
            "channel": "room",
            "room_id": _identifier(record.get("room_id"), "room", field="record.room_id"),
            "task_id": _optional_identifier(
                record.get("task_id"), "task", field="record.task_id"
            ),
            "reply_to_message_id": _optional_identifier(
                record.get("reply_to_message_id"), "msg", field="record.reply_to_message_id"
            ),
            "body_sha256": body_sha256,
            "body_length": body_length,
            "occurred_at": event["occurred_at"],
        }),
        _Item("memory_bridge_participants", "sender", {
            "agent_id": _identifier(record.get("sender_id"), "agt", field="record.sender_id"),
            "role": "sender",
        }),
    ]


def _reduce_artifact(event: dict[str, Any], project_id: int | None) -> list[_Item]:
    record = event["record"]
    size = record.get("size_bytes")
    if size is not None and (isinstance(size, bool) or not isinstance(size, int) or size < 0):
        raise BridgeError("record.size_bytes must be a nonnegative integer", code="malformed_event")
    content = record.get("sha256")
    if content is not None and not (
        isinstance(content, str) and _SHA256_RE.match(content)
    ):
        raise BridgeError("record.sha256 is not a digest", code="malformed_event")
    return [_Item("memory_bridge_artifacts", "artifact", {
        "artifact_id": _identifier(record.get("artifact_id"), "art", field="record.artifact_id"),
        "shared_by": _identifier(record.get("owner_id"), "agt", field="record.owner_id"),
        "task_id": _optional_identifier(record.get("task_id"), "task", field="record.task_id"),
        "room_id": _optional_identifier(record.get("room_id"), "room", field="record.room_id"),
        # The URI, name and media type are runtime-owned text and are not retained.
        "uri_sha256": sha256_text(record.get("uri")),
        "media_type_sha256": sha256_text(record.get("media_type")),
        "content_sha256": content,
        "byte_count": size,
        "occurred_at": event["occurred_at"],
    })]


def _reduce_collaboration(event: dict[str, Any], project_id: int | None) -> list[_Item]:
    record = event["record"]
    event_type = event["event_type"]
    if event_type == "collaboration.responded":
        return [_Item("memory_bridge_collaboration", "response", {
            "request_id": _identifier(
                record.get("request_id"), "req", field="record.request_id"
            ),
            "response_id": _identifier(
                record.get("response_id"), "rsp", field="record.response_id"
            ),
            "kind": "responded",
            "requester_id": None,
            "responder_id": _identifier(
                record.get("responder_id"), "agt", field="record.responder_id"
            ),
            "room_id": None,
            "task_id": None,
            "prompt_sha256": None,
            # The response body and the selected option are runtime text.
            "body_sha256": sha256_text(record.get("body")),
            "occurred_at": event["occurred_at"],
        })]
    kind = "requested" if event_type == "collaboration.requested" else "closed"
    return [_Item("memory_bridge_collaboration", kind, {
        "request_id": _identifier(record.get("request_id"), "req", field="record.request_id"),
        "response_id": None,
        "kind": kind,
        "requester_id": _identifier(
            record.get("requester_id"), "agt", field="record.requester_id"
        ),
        "responder_id": _optional_identifier(
            record.get("target_agent_id"), "agt", field="record.target_agent_id"
        ),
        "room_id": _optional_identifier(record.get("room_id"), "room", field="record.room_id"),
        "task_id": _optional_identifier(record.get("task_id"), "task", field="record.task_id"),
        "prompt_sha256": sha256_text(record.get("prompt")),
        "body_sha256": None,
        "occurred_at": event["occurred_at"],
    })]


REDUCERS: dict[str, Any] = {
    "agent.created": _reduce_agent,
    "agent.running": _reduce_agent,
    "agent.paused": _reduce_agent,
    "agent.stopped": _reduce_agent,
    "agent.model_changed": _reduce_agent,
    "agent.policy_changed": _reduce_agent,
    "room.created": _reduce_room,
    "room.agent_invited": _reduce_room,
    "room.agent_joined": _reduce_room,
    "room.agent_left": _reduce_room,
    "room.message_sent": _reduce_room_message,
    "message.direct_sent": _reduce_direct_message,
    "task.created": _reduce_task,
    "task.running": _reduce_task,
    "task.blocked": _reduce_task,
    "task.completed": _reduce_task,
    "task.failed": _reduce_task,
    "task.cancelled": _reduce_task,
    "task.delegated": _reduce_task_delegated,
    "artifact.shared": _reduce_artifact,
    "collaboration.requested": _reduce_collaboration,
    "collaboration.responded": _reduce_collaboration,
    "collaboration.closed": _reduce_collaboration,
}

assert set(REDUCERS) == set(PROJECTED_EVENTS), "reducer table and projected set disagree"


# ---------------------------------------------------------------------------
# Cursor, halt and resume
# ---------------------------------------------------------------------------

def read_cursor(db: sqlite3.Connection, *, consumer: str = DEFAULT_CONSUMER) -> dict[str, Any]:
    row = db.execute(
        """SELECT consumer, generation_id, last_sequence, last_event_id, status,
                  halt_code, halt_reason, halt_sequence, halted_at, updated_at
           FROM memory_bridge_cursor WHERE consumer=?""",
        (consumer,),
    ).fetchone()
    if row is None:
        raise BridgeError(f"no bridge consumer {consumer!r}")
    keys = (
        "consumer", "generation_id", "last_sequence", "last_event_id", "status",
        "halt_code", "halt_reason", "halt_sequence", "halted_at", "updated_at",
    )
    return {key: row[index] for index, key in enumerate(keys)}


def _write_halt(
    db: sqlite3.Connection,
    *,
    consumer: str,
    generation_id: int,
    expected_sequence: int,
    code: str,
    detail: Mapping[str, Any] | None,
    sequence: int | None,
    now: str,
    lease: "LeaseFence | None" = None,
) -> None:
    """Persist a halt under the same guards the batch ran under.

    Two guards, for two different attackers of the same row.

    The *cursor* guard stops a stale worker halting a consumer that has already
    moved on.  The *lease* guard stops a worker whose lease was taken over from
    writing anything at all: a halt is durable operator-facing state, so losing
    the right to ingest is losing the right to halt.

    ``halt_reason`` is built only from the closed code and :func:`_safe_detail`.
    An exception message never reaches it -- see that function for why.
    """
    if code not in HALT_CODES:
        raise BridgeError(f"unknown halt code {code!r}")
    if lease is not None:
        _assert_lease(db, lease, consumer=consumer)
    reason = " ".join(part for part in (code, _safe_detail(detail)) if part)
    updated = db.execute(
        """UPDATE memory_bridge_cursor
           SET status='halted', halt_code=?, halt_reason=?, halt_sequence=?,
               halted_at=?, updated_at=?
           WHERE consumer=? AND generation_id=? AND last_sequence=? AND status='active'""",
        (
            code, reason[:500], sequence, now, now,
            consumer, int(generation_id), int(expected_sequence),
        ),
    )
    if updated.rowcount != 1:
        # Either another worker already halted or advanced this consumer.  Do not
        # claim a durable halt that was not written.
        raise BridgeError(
            "halt was not recorded: the consumer state changed underneath this worker",
            code="cursor_mismatch",
            detail={"source": "cursor"},
        )


def halt_for_reader_failure(
    db: sqlite3.Connection,
    failure: "ReaderFailure",
    *,
    now: str,
    expected_sequence: int,
    consumer: str = DEFAULT_CONSUMER,
    lease: "LeaseFence | None" = None,
) -> dict[str, Any]:
    """Persist a durable halt for a *structural* replay-adapter failure.

    Runs inside the caller's write transaction, under the same generation and
    cursor guards an event-driven halt uses, so a stale or evicted worker cannot
    halt a consumer that has moved on.

    This exists because a rejection that lives only in an exception is not a
    state: the process that saw it exits, the next pass reads again, and the
    bridge quietly behaves as though the failure never happened.  A structural
    reader failure means the adapter's history cannot serve this consumer, which
    is precisely the condition an operator has to resolve.

    An *operational* failure must never be routed here -- callers check
    ``failure.structural`` first -- because a durable halt is the wrong response
    to a transient I/O error.
    """
    if not db.in_transaction:
        raise BridgeError("recording a reader halt runs inside a write transaction")
    if not failure.structural:
        raise BridgeError(
            "an operational reader failure is not a durable halt; retry instead"
        )
    state = read_cursor(db, consumer=consumer)
    if str(state["status"]) == "halted":
        # Already halted, by this failure or another.  Report the state that is
        # actually persisted rather than overwriting it with a fresher story.
        return {
            "status": "halted",
            "halt_code": str(state["halt_code"]),
            "recorded": False,
            "last_sequence": int(state["last_sequence"]),
        }
    generation_id = int(state["generation_id"])
    _write_halt(
        db, consumer=consumer, generation_id=generation_id,
        expected_sequence=int(expected_sequence), code=str(failure.code),
        detail={**failure.detail, "source": "adapter"},
        sequence=int(state["last_sequence"]) + 1, now=now, lease=lease,
    )
    return {
        "status": "halted",
        "halt_code": str(failure.code),
        "recorded": True,
        "last_sequence": int(state["last_sequence"]),
    }


def resume(
    db: sqlite3.Connection,
    *,
    operator: str,
    acknowledge: str,
    now: str,
    consumer: str = DEFAULT_CONSUMER,
    note: str | None = None,
) -> dict[str, Any]:
    """Clear a halt.  Explicit operator action, and deliberately not a bypass.

    Three properties matter more than the convenience of a one-word command:

    * **It cannot be done blind.** ``acknowledge`` must equal the exact halt code,
      so an operator states which failure they are clearing rather than clearing
      whatever happened to be there.
    * **It never advances the cursor.** The offending event is still the next one
      to be read.  If the underlying problem is unfixed the very next pass halts
      again on the same sequence -- resuming clears the flag, it does not skip the
      event, invent content, or widen the accepted vocabulary.
    * **It is audited.** Every resume writes ``memory_bridge_resumes`` with the
      operator, the code cleared and the cursor position at the time.

    A process restart does not clear a halt; only this call does.
    """
    if not str(operator).strip():
        raise BridgeError("resuming a halted bridge requires an operator identity")
    state = read_cursor(db, consumer=consumer)
    if str(state["status"]) != "halted":
        raise BridgeError("bridge consumer is not halted")
    halt_code = str(state["halt_code"])
    if str(acknowledge).strip() != halt_code:
        raise BridgeError(
            "resume must acknowledge the exact halt code "
            f"({halt_code!r}); refusing a blind resume"
        )
    db.execute(
        """INSERT INTO memory_bridge_resumes(
               consumer, resumed_at, operator, halt_code, halt_sequence,
               cursor_sequence, note)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            consumer, now, str(operator)[:100], halt_code, state["halt_sequence"],
            int(state["last_sequence"]), (str(note)[:500] if note else None),
        ),
    )
    db.execute(
        """UPDATE memory_bridge_cursor
           SET status='active', halt_code=NULL, halt_reason=NULL,
               halt_sequence=NULL, halted_at=NULL, updated_at=?
           WHERE consumer=? AND status='halted'""",
        (now, consumer),
    )
    return {
        "resumed_from": halt_code,
        "at_sequence": state["last_sequence"],
        "cursor_advanced": False,
        "operator": str(operator)[:100],
    }


def _suppressed_items(db: sqlite3.Connection, event_id: str) -> set[str]:
    rows = db.execute(
        """SELECT item_key FROM memory_bridge_suppressions
           WHERE runtime_event_id=? AND released_at IS NULL""",
        (event_id,),
    ).fetchall()
    return {str(row[0]) for row in rows}


def suppress_item(
    db: sqlite3.Connection,
    *,
    runtime_event_id: str,
    item_key: str,
    reason_code: str,
    decided_by: str,
    now: str,
) -> None:
    """Record a durable suppression that survives rebuilds and generations."""
    _identifier(runtime_event_id, "evt", field="runtime_event_id")
    db.execute(
        """INSERT INTO memory_bridge_suppressions(
               runtime_event_id, item_key, reason_code, decided_by, decided_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(runtime_event_id, item_key) DO UPDATE SET
               reason_code=excluded.reason_code,
               decided_by=excluded.decided_by,
               decided_at=excluded.decided_at,
               released_at=NULL, released_by=NULL""",
        (runtime_event_id, str(item_key), str(reason_code)[:100],
         str(decided_by)[:100], now),
    )


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def _insert_item(
    db: sqlite3.Connection, generation_id: int, event: dict[str, Any], item: _Item
) -> int:
    columns = {
        "generation_id": generation_id,
        "runtime_sequence": event["sequence"],
        **item.columns,
    }
    if item.table not in {"memory_bridge_agents", "memory_bridge_rooms", "memory_bridge_tasks"}:
        columns["runtime_event_id"] = event["event_id"]
        columns["item_key"] = item.item_key
    else:
        columns.pop("runtime_sequence", None)
    names = sorted(columns)
    placeholders = ",".join("?" for _ in names)
    cursor = db.execute(
        f"INSERT INTO {item.table}({','.join(names)}) VALUES ({placeholders}) "
        f"ON CONFLICT DO NOTHING",
        tuple(columns[name] for name in names),
    )
    return int(cursor.rowcount or 0)


def import_batch(
    db: sqlite3.Connection,
    events: Sequence[Any],
    *,
    now: str,
    consumer: str = DEFAULT_CONSUMER,
    expected_sequence: int | None = None,
    expected_event_id: str | None = None,
    lease: "LeaseFence | None" = None,
) -> dict[str, Any]:
    """Project one contiguous batch and advance the cursor in this transaction.

    The caller owns the transaction (``Memory`` uses ``BEGIN IMMEDIATE``), so the
    projections, quarantine rows, batch audit and cursor advance commit together
    or not at all.  A structural failure rolls the projections back to a savepoint
    and still records the halt, so "stopped" is durable state rather than an
    exception that dies with the process.

    ``lease`` fences the commit.  When supplied, the cursor advance carries an
    ``EXISTS`` clause over the lease row, so the advance and the ownership test
    are one statement: a worker whose lease was taken over cannot land one last
    batch after the takeover.  The cursor compare-and-set alone does not close
    that window -- it only serialises two workers that both still believe they
    hold the lease.
    """
    if not db.in_transaction:
        raise BridgeError("import_batch runs inside the caller's write transaction")
    if lease is not None:
        # Fail before doing any work, with a precise reason.  The authoritative
        # check is the fenced UPDATE below; this one exists so the common case
        # reports *why* rather than as an opaque cursor mismatch.
        _assert_lease(db, lease, consumer=consumer)
    state = read_cursor(db, consumer=consumer)
    if str(state["status"]) == "halted":
        raise BridgeHalted(
            f"bridge consumer is halted ({state['halt_code']}); an operator must resume it",
            code=str(state["halt_code"]),
        )
    generation_id = int(state["generation_id"])
    start_sequence = int(state["last_sequence"])
    # Compare-and-set: a stale worker that read an older cursor cannot commit.
    if expected_sequence is not None and int(expected_sequence) != start_sequence:
        raise BridgeError(
            "bridge cursor changed since this batch was read", code="cursor_mismatch"
        )
    if expected_event_id is not None and expected_event_id != state["last_event_id"]:
        raise BridgeError(
            "bridge cursor event id changed since this batch was read",
            code="cursor_mismatch",
        )
    contract = generation_contract(db, generation_id)
    if (
        contract["reducer_version"] != REDUCER_VERSION
        or contract["screen_version"] != SCREEN_VERSION
        or contract["mapping_format_version"] != MAPPING_FORMAT_VERSION
    ):
        _write_halt(
            db, consumer=consumer, generation_id=generation_id,
            expected_sequence=start_sequence, code="version_mismatch",
            detail={
                "generation": generation_id,
                "reducer": int(contract["reducer_version"]),
                "screen": int(contract["screen_version"]),
                "mapping": int(contract["mapping_format_version"]),
            },
            sequence=start_sequence, now=now, lease=lease,
        )
        return _halted_result(generation_id, start_sequence, "version_mismatch")
    if not events:
        return {
            "status": "active", "generation_id": generation_id,
            "events": 0, "projected_items": 0, "quarantined_items": 0,
            "ignored_events": 0, "suppressed_items": 0,
            "last_sequence": start_sequence,
        }

    db.execute("SAVEPOINT memory_bridge_batch")
    projected = quarantined = ignored = suppressed = 0
    previous = start_sequence
    first_sequence = None
    last_event: dict[str, Any] | None = None
    # The event a failure is *about*, which is not the last event accepted: an
    # envelope that fails normalization has no validated identity at all, and
    # naming its predecessor in the halt row would send an operator to read a
    # perfectly good event.
    offending: dict[str, Any] | None = None
    try:
        for raw in events:
            offending = None
            event = _normalize_event(raw)
            offending = event
            if event["sequence"] != previous + 1:
                raise BridgeError(
                    f"expected runtime sequence {previous + 1}, saw {event['sequence']}",
                    code="sequence_gap",
                    detail={"expected": previous + 1,
                            "saw": int(event["sequence"])},
                )
            previous = event["sequence"]
            if first_sequence is None:
                first_sequence = event["sequence"]
            last_event = event
            if event["event_type"] not in contract["projected"]:
                if event["event_type"] not in contract["ignored"]:
                    # A type this generation pinned as neither projected nor
                    # ignored must not be silently consumed.
                    raise BridgeError(
                        f"event type {event['event_type']!r} is outside generation "
                        f"{generation_id}",
                        code="unknown_event_type",
                        detail={"generation": generation_id,
                                "known": True},
                    )
                ignored += 1
                continue
            project_id = resolve_project(db, generation_id, event["project_id"])
            # The envelope scopes the event; the record scopes the entity.  An
            # unmapped value in either is an unmapped project, or a project
            # string could reach a projection through the record alone.
            record_project = event["record"].get("project_id")
            if record_project is not None and record_project != event["project_id"]:
                resolve_project(db, generation_id, record_project, source="record")
            if project_id is None and record_project is not None:
                project_id = resolve_project(
                    db, generation_id, record_project, source="record"
                )
            reducer = REDUCERS[event["event_type"]]
            items = reducer(event, project_id)
            blocked = _suppressed_items(db, event["event_id"])
            for item in items:
                if item.item_key in blocked:
                    suppressed += 1
                    continue
                projected += _insert_item(db, generation_id, event, item)
                for entry in item.quarantine:
                    db.execute(
                        """INSERT INTO memory_bridge_quarantine(
                               generation_id, runtime_event_id, runtime_sequence,
                               item_key, field, screen, screen_version,
                               value_sha256, value_length, quarantined_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT DO NOTHING""",
                        (
                            generation_id, event["event_id"], event["sequence"],
                            entry["item_key"], entry["field"], entry["screen"],
                            SCREEN_VERSION, entry["value_sha256"],
                            entry["value_length"], now,
                        ),
                    )
                    quarantined += 1
    except LeaseLost:
        # Never a halt.  This worker lost the right to write at all; whoever
        # holds the lease will redo the batch.  Re-raising leaves the halt
        # decision to the holder and rolls this transaction back untouched.
        db.execute("ROLLBACK TO SAVEPOINT memory_bridge_batch")
        db.execute("RELEASE SAVEPOINT memory_bridge_batch")
        raise
    except BridgeError as error:
        db.execute("ROLLBACK TO SAVEPOINT memory_bridge_batch")
        db.execute("RELEASE SAVEPOINT memory_bridge_batch")
        code = error.code or "reducer_error"
        if code not in HALT_CODES:
            code = "reducer_error"
        # Enrich with a locator, never with content: the offending event's own
        # opaque id and sequence, so an operator can read the event from the
        # runtime store, which owns it, instead of from a memory diagnostic.
        detail = dict(error.detail)
        if offending is not None:
            detail.setdefault("event", offending["event_id"])
            detail.setdefault("at", int(offending["sequence"]))
        _write_halt(
            db, consumer=consumer, generation_id=generation_id,
            expected_sequence=start_sequence, code=code, detail=detail,
            sequence=(previous + 1 if previous >= start_sequence else None), now=now,
            lease=lease,
        )
        return _halted_result(generation_id, start_sequence, code)
    except Exception:  # a reducer bug is structural, never quarantine
        db.execute("ROLLBACK TO SAVEPOINT memory_bridge_batch")
        db.execute("RELEASE SAVEPOINT memory_bridge_batch")
        # The exception type is a Python implementation detail and its message
        # may quote the event, so neither is persisted.
        _write_halt(
            db, consumer=consumer, generation_id=generation_id,
            expected_sequence=start_sequence, code="reducer_error",
            detail={"source": "reducer", "at": previous},
            sequence=previous, now=now, lease=lease,
        )
        return _halted_result(generation_id, start_sequence, "reducer_error")

    assert last_event is not None and first_sequence is not None
    db.execute(
        """INSERT INTO memory_bridge_batches(
               generation_id, created_at, first_sequence, last_sequence,
               event_count, projected_items, quarantined_items, ignored_events)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT DO NOTHING""",
        (
            generation_id, now, first_sequence, last_event["sequence"],
            len(events), projected, quarantined, ignored,
        ),
    )
    fence_sql, fence_params = _lease_fence_clause(lease, consumer=consumer)
    updated = db.execute(
        f"""UPDATE memory_bridge_cursor
           SET last_sequence=?, last_event_id=?, updated_at=?
           WHERE consumer=? AND generation_id=? AND last_sequence=? AND status='active'
                 {fence_sql}""",
        (
            last_event["sequence"], last_event["event_id"], now,
            consumer, generation_id, start_sequence, *fence_params,
        ),
    )
    if updated.rowcount != 1:
        db.execute("ROLLBACK TO SAVEPOINT memory_bridge_batch")
        db.execute("RELEASE SAVEPOINT memory_bridge_batch")
        if lease is not None:
            # Two different failures wear the same rowcount.  Saying'cursor
            # mismatch' when the real answer is 'you were evicted' would send an
            # operator hunting a competing cursor write that never happened.
            _assert_lease(db, lease, consumer=consumer)
        raise BridgeError(
            "bridge cursor changed during import", code="cursor_mismatch",
            detail={"source": "cursor", "at": start_sequence},
        )
    db.execute("RELEASE SAVEPOINT memory_bridge_batch")
    return {
        "status": "active",
        "generation_id": generation_id,
        "events": len(events),
        "projected_items": projected,
        "quarantined_items": quarantined,
        "ignored_events": ignored,
        "suppressed_items": suppressed,
        "last_sequence": last_event["sequence"],
        "last_event_id": last_event["event_id"],
    }


def _halted_result(generation_id: int, last_sequence: int, code: str) -> dict[str, Any]:
    return {
        "status": "halted",
        "generation_id": generation_id,
        "halt_code": code,
        "last_sequence": last_sequence,
        "events": 0,
        "projected_items": 0,
        "quarantined_items": 0,
        "ignored_events": 0,
        "suppressed_items": 0,
    }


# ---------------------------------------------------------------------------
# Rebuild
# ---------------------------------------------------------------------------

def rebuild_generation(
    db: sqlite3.Connection,
    events: Sequence[Any],
    *,
    now: str,
    consumer: str = DEFAULT_CONSUMER,
    generation_id: int | None = None,
    kind: str = "rebuild",
) -> dict[str, Any]:
    """Delete and re-derive one generation's projections from the event stream.

    Only this module's projection tables and that generation's quarantine rows are
    removed.  Memory-authored data -- claims, lessons, promotions, milestones,
    spine events -- is never touched, which is why bridge output lives in
    dedicated tables rather than as a nullable column on shared ones.

    Durable suppressions are keyed on the runtime event rather than the
    generation, so a rebuild cannot revive suppressed content.

    **A rebuild is a proposal, not a commitment.**  The destructive part runs
    inside a savepoint and the replay has to succeed before any of it stands.
    If the replay halts -- a gap, an unmapped project, an event the generation
    does not classify -- everything is rolled back: the previous projections,
    the previous quarantine evidence, the previous batch audit and the
    previous cursor are all exactly as they were.  Then, and only then, the
    attempt is recorded in ``memory_bridge_rebuilds``, so a rejected rebuild
    leaves evidence that it was tried and refused rather than no trace at all.

    The alternative -- letting the cutover stand and reporting a halt -- is
    what the first implementation did, and it trades a working projection for
    an empty halted one on the strength of history that was never validated.
    """
    if not db.in_transaction:
        raise BridgeError("rebuild runs inside the caller's write transaction")
    state = read_cursor(db, consumer=consumer)
    target = int(state["generation_id"]) if generation_id is None else int(generation_id)
    generation_contract(db, target)
    previous_generation = int(state["generation_id"])
    previous_sequence = int(state["last_sequence"])
    db.execute("SAVEPOINT memory_bridge_rebuild")
    try:
        for table in BRIDGE_PROJECTION_TABLES:
            db.execute(f"DELETE FROM {table} WHERE generation_id=?", (target,))
        db.execute(
            "DELETE FROM memory_bridge_quarantine WHERE generation_id=?", (target,)
        )
        # ``memory_bridge_batches`` is deliberately *not* cleared.  It is the
        # audit of which passes ran, not derived projection state, and the
        # contract asks for durable audit evidence.  Re-importing the same
        # span is idempotent against its UNIQUE key.
        db.execute(
            """UPDATE memory_bridge_cursor
               SET generation_id=?, last_sequence=0, last_event_id=NULL,
                   status='active', halt_code=NULL, halt_reason=NULL,
                   halt_sequence=NULL, halted_at=NULL, updated_at=?
               WHERE consumer=?""",
            (target, now, consumer),
        )
        result = import_batch(db, events, now=now, consumer=consumer)
    except BridgeError as error:
        db.execute("ROLLBACK TO SAVEPOINT memory_bridge_rebuild")
        db.execute("RELEASE SAVEPOINT memory_bridge_rebuild")
        _record_rebuild(
            db, now=now, consumer=consumer, kind=kind, outcome="rejected",
            from_generation=previous_generation, to_generation=None,
            from_sequence=previous_sequence, result_sequence=None,
            event_count=len(events),
            halt_code=(error.code if error.code in HALT_CODES else "reducer_error"),
        )
        return _rejected_rebuild(
            db, consumer=consumer,
            halt_code=(error.code if error.code in HALT_CODES else "reducer_error"),
            candidate=target,
        )
    if str(result["status"]) != "active":
        db.execute("ROLLBACK TO SAVEPOINT memory_bridge_rebuild")
        db.execute("RELEASE SAVEPOINT memory_bridge_rebuild")
        _record_rebuild(
            db, now=now, consumer=consumer, kind=kind, outcome="rejected",
            from_generation=previous_generation, to_generation=None,
            from_sequence=previous_sequence, result_sequence=None,
            event_count=len(events), halt_code=str(result["halt_code"]),
        )
        return _rejected_rebuild(
            db, consumer=consumer, halt_code=str(result["halt_code"]),
            candidate=target,
        )
    db.execute("RELEASE SAVEPOINT memory_bridge_rebuild")
    _record_rebuild(
        db, now=now, consumer=consumer, kind=kind, outcome="applied",
        from_generation=previous_generation, to_generation=target,
        from_sequence=previous_sequence,
        result_sequence=int(result["last_sequence"]),
        event_count=len(events), halt_code=None,
    )
    result["outcome"] = "applied"
    return result


def _rejected_rebuild(
    db: sqlite3.Connection,
    *,
    consumer: str,
    halt_code: str,
    candidate: int | None,
) -> dict[str, Any]:
    """Report a refused cutover, reading the state that actually survived.

    The status is ``rejected``, never ``halted``: nothing is halted, because
    the live consumer was never moved.  ``halt_code`` is the reason the
    *candidate* was refused, which is a different fact from the consumer
    being stopped, and conflating the two is how an operator ends up resuming
    a bridge that was running the whole time.
    """
    state = read_cursor(db, consumer=consumer)
    return {
        "status": "rejected",
        "outcome": "rejected",
        "halt_code": halt_code,
        "candidate_generation_id": candidate,
        "generation_id": int(state["generation_id"]),
        "last_sequence": int(state["last_sequence"]),
        "consumer_status": str(state["status"]),
        "events": 0,
        "projected_items": 0,
        "quarantined_items": 0,
        "ignored_events": 0,
        "suppressed_items": 0,
    }


def _record_rebuild(
    db: sqlite3.Connection,
    *,
    now: str,
    consumer: str,
    kind: str,
    outcome: str,
    from_generation: int,
    to_generation: int | None,
    from_sequence: int,
    result_sequence: int | None,
    event_count: int,
    halt_code: str | None,
) -> None:
    """Append the durable record of a rebuild or cutover attempt.

    Written *after* any savepoint rollback, so a rejected attempt leaves a
    row even though everything it tried to do was undone.  A rejected
    candidate generation is recorded as NULL rather than by id: the row that
    carried that id was rolled back and SQLite will hand the number to the
    next generation, so keeping it would point at the wrong thing later.
    """
    if not rebuild_audit_ready(db):
        return
    db.execute(
        """INSERT INTO memory_bridge_rebuilds(
               consumer, attempted_at, kind, outcome, from_generation_id,
               to_generation_id, from_sequence, result_sequence, event_count,
               halt_code)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            consumer, now, kind, outcome, int(from_generation),
            (None if to_generation is None else int(to_generation)),
            int(from_sequence),
            (None if result_sequence is None else int(result_sequence)),
            int(event_count), halt_code,
        ),
    )


def open_generation(
    db: sqlite3.Connection,
    events: Sequence[Any],
    *,
    now: str,
    reason: str,
    consumer: str = DEFAULT_CONSUMER,
    projected: Iterable[str] | None = None,
    ignored: Mapping[str, str] | None = None,
    carry_project_map: bool = True,
) -> dict[str, Any]:
    """Open a new generation and replay eligible history into it.

    This is how a projection family is added.  The previous generation is marked
    superseded and its rows remain readable; the new generation replays the whole
    eligible history, so events that an earlier generation deliberately ignored
    are projected now instead of being lost behind a permanent no-op.

    **The cutover happens only if the candidate validates.**  Creating the
    generation, copying the project map, marking the previous one superseded
    and moving the consumer all run inside a savepoint, and the replay has to
    finish active before any of it stands.  A candidate that halts is undone
    in full: the previous generation is still active, still un-superseded, and
    still serving its own cursor and projections.  Only the audit row in
    ``memory_bridge_rebuilds`` survives the rejection, which is the point --
    the operator needs to know the attempt happened and why it was refused.
    """
    if not db.in_transaction:
        raise BridgeError("opening a generation runs inside a write transaction")
    previous = active_generation(db, consumer=consumer)
    previous_state = read_cursor(db, consumer=consumer)
    db.execute("SAVEPOINT memory_bridge_cutover")
    try:
        generation_id = create_generation(
            db, now=now, reason=reason, projected=projected, ignored=ignored
        )
        if carry_project_map:
            db.execute(
                """INSERT INTO memory_bridge_project_map(
                       generation_id, runtime_project_id, memory_project_id,
                       created_at)
                   SELECT ?, runtime_project_id, memory_project_id, ?
                   FROM memory_bridge_project_map WHERE generation_id=?""",
                (generation_id, now, previous),
            )
        db.execute(
            """UPDATE memory_bridge_generations
               SET superseded_at=?, superseded_by=? WHERE id=?""",
            (now, generation_id, previous),
        )
        result = rebuild_generation(
            db, events, now=now, consumer=consumer, generation_id=generation_id,
            kind="generation_cutover",
        )
    except BridgeError as error:
        db.execute("ROLLBACK TO SAVEPOINT memory_bridge_cutover")
        db.execute("RELEASE SAVEPOINT memory_bridge_cutover")
        _record_rebuild(
            db, now=now, consumer=consumer, kind="generation_cutover",
            outcome="rejected", from_generation=previous, to_generation=None,
            from_sequence=int(previous_state["last_sequence"]),
            result_sequence=None, event_count=len(events),
            halt_code=(error.code if error.code in HALT_CODES else "reducer_error"),
        )
        rejected = _rejected_rebuild(
            db, consumer=consumer,
            halt_code=(error.code if error.code in HALT_CODES else "reducer_error"),
            candidate=None,
        )
        rejected["previous_generation_id"] = previous
        return rejected
    if str(result["status"]) != "active":
        # The candidate did not validate.  Undo the whole cutover -- the new
        # generation row, the copied project map, the supersede marker and the
        # consumer move -- so the previous generation keeps serving.  Only the
        # audit row, written after the rollback, survives.
        db.execute("ROLLBACK TO SAVEPOINT memory_bridge_cutover")
        db.execute("RELEASE SAVEPOINT memory_bridge_cutover")
        _record_rebuild(
            db, now=now, consumer=consumer, kind="generation_cutover",
            outcome="rejected", from_generation=previous, to_generation=None,
            from_sequence=int(previous_state["last_sequence"]),
            result_sequence=None, event_count=len(events),
            halt_code=str(result["halt_code"]),
        )
        rejected = _rejected_rebuild(
            db, consumer=consumer, halt_code=str(result["halt_code"]),
            candidate=None,
        )
        rejected["previous_generation_id"] = previous
        return rejected
    db.execute("RELEASE SAVEPOINT memory_bridge_cutover")
    result["previous_generation_id"] = previous
    return result


# ---------------------------------------------------------------------------
# Derived half-open intervals.  Computed, never stored.
# ---------------------------------------------------------------------------

def room_membership_intervals(
    db: sqlite3.Connection, generation_id: int, room_id: str
) -> list[dict[str, Any]]:
    """Half-open ``[joined, left)`` membership spans in runtime sequence order.

    An invitation is not membership: only ``joined`` opens a span.  A span with
    ``end=None`` is still open at the end of the projected history.
    """
    rows = db.execute(
        """SELECT agent_id, transition, runtime_sequence
           FROM memory_bridge_room_members
           WHERE generation_id=? AND room_id=?
           ORDER BY runtime_sequence, item_key""",
        (int(generation_id), str(room_id)),
    ).fetchall()
    spans: list[dict[str, Any]] = []
    open_span: dict[str, dict[str, Any]] = {}
    for agent_id, transition, sequence in rows:
        agent = str(agent_id)
        if transition == "joined":
            if agent not in open_span:
                span = {"agent_id": agent, "start": int(sequence), "end": None}
                open_span[agent] = span
                spans.append(span)
        elif transition == "left":
            span = open_span.pop(agent, None)
            if span is not None:
                span["end"] = int(sequence)
    return spans


def is_room_member_at(
    db: sqlite3.Connection, generation_id: int, room_id: str, agent_id: str, sequence: int
) -> bool:
    """Membership evidence as of a sequence.  Evidence, never authorization.

    A true result means this agent was a member when the event happened.  It does
    not mean the agent may read anything now: current authorization and
    revocation are a separate, operator-approved policy that this slice does not
    implement and does not expose.
    """
    for span in room_membership_intervals(db, generation_id, room_id):
        if span["agent_id"] != str(agent_id):
            continue
        if span["start"] <= int(sequence) and (span["end"] is None or int(sequence) < span["end"]):
            return True
    return False


def task_ownership_intervals(
    db: sqlite3.Connection, generation_id: int, task_id: str
) -> list[dict[str, Any]]:
    """Half-open ``[from, to)`` ownership spans; an unowned task yields none."""
    rows = db.execute(
        """SELECT owner_agent_id, runtime_sequence
           FROM memory_bridge_task_owner_events
           WHERE generation_id=? AND task_id=?
           ORDER BY runtime_sequence, item_key""",
        (int(generation_id), str(task_id)),
    ).fetchall()
    spans: list[dict[str, Any]] = []
    for owner, sequence in rows:
        if spans:
            spans[-1]["end"] = int(sequence)
        if owner is None:
            continue
        spans.append({"agent_id": str(owner), "start": int(sequence), "end": None})
    return [span for span in spans if span["agent_id"]]


def _last_rejected_rebuild(
    db: sqlite3.Connection, *, consumer: str = DEFAULT_CONSUMER
) -> dict[str, Any] | None:
    """The most recent refused rebuild or cutover, as closed codes and counts."""
    row = db.execute(
        """SELECT attempted_at, kind, from_generation_id, from_sequence,
                  event_count, halt_code
           FROM memory_bridge_rebuilds
           WHERE consumer=? AND outcome='rejected'
           ORDER BY id DESC LIMIT 1""",
        (consumer,),
    ).fetchone()
    if row is None:
        return None
    return {
        "attempted_at": row[0],
        "kind": str(row[1]),
        "from_generation_id": int(row[2]),
        "from_sequence": int(row[3]),
        "event_count": int(row[4]),
        "halt_code": str(row[5]),
    }


def bridge_status(
    db: sqlite3.Connection,
    *,
    consumer: str = DEFAULT_CONSUMER,
    now: str | None = None,
) -> dict[str, Any]:
    """Operational status.  Diagnostic only; no projection content is returned.

    ``now`` lets the caller age the health row -- how long the bridge has been
    stalled is the number an operator actually acts on, and this module has
    no clock of its own.
    """
    state = read_cursor(db, consumer=consumer)
    generation = generation_contract(db, int(state["generation_id"]))
    counts = {
        table: int(
            db.execute(
                f"SELECT COUNT(*) FROM {table} WHERE generation_id=?",
                (int(state["generation_id"]),),
            ).fetchone()[0]
        )
        for table in BRIDGE_PROJECTION_TABLES
    }
    quarantined = int(
        db.execute(
            "SELECT COUNT(*) FROM memory_bridge_quarantine WHERE generation_id=?",
            (int(state["generation_id"]),),
        ).fetchone()[0]
    )
    suppressed = int(
        db.execute(
            "SELECT COUNT(*) FROM memory_bridge_suppressions WHERE released_at IS NULL"
        ).fetchone()[0]
    )
    return {
        "consumer": state["consumer"],
        "status": state["status"],
        "halt_code": state["halt_code"],
        "halt_sequence": state["halt_sequence"],
        "last_sequence": state["last_sequence"],
        "last_event_id": state["last_event_id"],
        "generation_id": generation["id"],
        "reducer_version": generation["reducer_version"],
        "screen_version": generation["screen_version"],
        "ignored_types": sorted(generation["ignored"]),
        "projection_counts": counts,
        "quarantined_items": quarantined,
        "active_suppressions": suppressed,
        # Rejected cutovers are reported because they are otherwise invisible:
        # a refused rebuild rolls back everything it touched, so the store looks
        # exactly as it did before somebody tried and failed to change it.
        "rejected_rebuilds": (
            int(db.execute(
                """SELECT COUNT(*) FROM memory_bridge_rebuilds
                   WHERE consumer=? AND outcome='rejected'""",
                (consumer,),
            ).fetchone()[0])
            if rebuild_audit_ready(db) else 0
        ),
        "last_rejected_rebuild": (
            _last_rejected_rebuild(db, consumer=consumer)
            if rebuild_audit_ready(db) else None
        ),
        "lease": lease_state(db, consumer=consumer) if lease_ready(db) else None,
        "health": bridge_health(db, consumer=consumer, now=now),
        "resumes": (
            int(db.execute(
                "SELECT COUNT(*) FROM memory_bridge_resumes WHERE consumer=?",
                (consumer,),
            ).fetchone()[0])
            if lease_ready(db) else 0
        ),
        "agent_facing": False,
    }


# ---------------------------------------------------------------------------
# Schema 52: worker lease and resume audit
# ---------------------------------------------------------------------------

BRIDGE_LEASE_DDL: tuple[str, ...] = (
    # One ingest lease per consumer.  Two workers may run; only the lease holder
    # ingests, and a crashed holder's lease expires rather than wedging the
    # bridge forever.
    """CREATE TABLE IF NOT EXISTS memory_bridge_lease (
        consumer TEXT PRIMARY KEY,
        owner TEXT,
        token_sha256 TEXT,
        acquired_at TEXT,
        expires_at TEXT,
        CHECK((owner IS NULL) = (token_sha256 IS NULL)),
        CHECK((owner IS NULL) = (expires_at IS NULL)),
        CHECK((owner IS NULL) = (acquired_at IS NULL)),
        CHECK(token_sha256 IS NULL OR length(token_sha256) = 64)
    )""",
    # Every resume is recorded.  A halt that is cleared invisibly is a halt that
    # never happened as far as a later reader is concerned.
    """CREATE TABLE IF NOT EXISTS memory_bridge_resumes (
        id INTEGER PRIMARY KEY,
        consumer TEXT NOT NULL,
        resumed_at TEXT NOT NULL,
        operator TEXT NOT NULL,
        halt_code TEXT NOT NULL,
        halt_sequence INTEGER,
        cursor_sequence INTEGER NOT NULL CHECK(cursor_sequence >= 0),
        note TEXT
    )""",
)

#: Bounded ingestion defaults.  A worker pass is deliberately small: the bridge
#: is a follower, and a long pass would hold the memory write lock against the
#: turn path it shares a database with.
MAX_INGEST_EVENTS_PER_PASS = 500
MAX_INGEST_BATCH = 100
DEFAULT_LEASE_SECONDS = 120


def lease_ready(db: sqlite3.Connection) -> bool:
    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_bridge_lease'"
    ).fetchone()
    return row is not None


def migrate_bridge_v52(db: sqlite3.Connection, *, now: str) -> dict[str, Any]:
    """Add the ingest lease and the resume audit.  Additive; nothing is read."""
    if not db.in_transaction:
        raise BridgeError("the bridge migration runs inside a write transaction")
    existed = lease_ready(db)
    for statement in BRIDGE_LEASE_DDL:
        db.execute(statement)
    if not existed:
        db.execute(
            """INSERT INTO memory_bridge_lease(consumer, owner, token_sha256,
                                               acquired_at, expires_at)
               VALUES (?, NULL, NULL, NULL, NULL)
               ON CONFLICT(consumer) DO NOTHING""",
            (DEFAULT_CONSUMER,),
        )
    return {"created": 0 if existed else 1}


def _parse_stamp(value: Any) -> datetime:
    return datetime.fromisoformat(str(value))


def acquire_ingest_lease(
    db: sqlite3.Connection,
    *,
    owner: str,
    now: str,
    consumer: str = DEFAULT_CONSUMER,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> str | None:
    """Take the ingest lease, or return None when another worker holds it.

    Runs inside the caller's write transaction, so the read-then-write is
    serialized by SQLite rather than racing.  An expired lease is reclaimed: a
    worker that died mid-pass must not block ingestion permanently.
    """
    if not db.in_transaction:
        raise BridgeError("acquiring the ingest lease needs a write transaction")
    if not str(owner).strip():
        raise BridgeError("an ingest lease needs an owner")
    lease_seconds = max(1, min(int(lease_seconds), 3600))
    current = _parse_stamp(now)
    row = db.execute(
        "SELECT owner, expires_at FROM memory_bridge_lease WHERE consumer=?",
        (consumer,),
    ).fetchone()
    if row is not None and row[0] is not None:
        held_by = str(row[0])
        expires = _parse_stamp(row[1])
        if held_by != str(owner) and expires > current:
            return None
    token = secrets.token_hex(16)
    expires_at = (current + timedelta(seconds=lease_seconds)).isoformat()
    db.execute(
        """INSERT INTO memory_bridge_lease(consumer, owner, token_sha256,
                                           acquired_at, expires_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(consumer) DO UPDATE SET
               owner=excluded.owner, token_sha256=excluded.token_sha256,
               acquired_at=excluded.acquired_at, expires_at=excluded.expires_at""",
        (consumer, str(owner), sha256_text(token), now, expires_at),
    )
    return token


def release_ingest_lease(
    db: sqlite3.Connection,
    *,
    owner: str,
    token: str,
    consumer: str = DEFAULT_CONSUMER,
) -> bool:
    """Release only a lease this worker still holds."""
    updated = db.execute(
        """UPDATE memory_bridge_lease
           SET owner=NULL, token_sha256=NULL, acquired_at=NULL, expires_at=NULL
           WHERE consumer=? AND owner=? AND token_sha256=?""",
        (consumer, str(owner), sha256_text(token)),
    )
    return int(updated.rowcount or 0) == 1


@dataclass(frozen=True)
class LeaseFence:
    """The proof a worker offers with every durable write it makes.

    ``owner`` and ``token`` say who is writing; ``now`` is the instant the
    write is being judged against, so expiry is evaluated at commit time
    rather than at the time the pass happened to start.  A long pass renews
    the lease and carries a fresh fence; it never re-acquires, because a new
    token would invalidate exactly the fencing this exists to provide.
    """

    owner: str
    token: str
    now: str


def _lease_fence_clause(
    lease: LeaseFence | None, *, consumer: str = DEFAULT_CONSUMER
) -> tuple[str, tuple[Any, ...]]:
    """SQL that makes a write conditional on still holding the lease.

    Returned as a fragment rather than checked separately on purpose: the
    ownership test and the write have to be one statement, or there is a
    window between them.  Expiry is compared in SQL against the ISO string
    the fence carries; :func:`_assert_lease` re-checks it with real datetime
    parsing, so a store whose timestamps are not lexicographically ordered
    fails closed (the fence refuses) rather than open.
    """
    if lease is None:
        return "", ()
    return (
        """ AND EXISTS (SELECT 1 FROM memory_bridge_lease
                          WHERE consumer=? AND owner=? AND token_sha256=?
                            AND expires_at > ?)""",
        (consumer, str(lease.owner), sha256_text(lease.token), str(lease.now)),
    )


def _assert_lease(
    db: sqlite3.Connection,
    lease: LeaseFence,
    *,
    consumer: str = DEFAULT_CONSUMER,
) -> None:
    """Raise :class:`LeaseLost` unless this worker still holds the lease.

    Ownership, token and expiry, all three.  Owner alone is not enough: a
    worker that restarts under the same id and re-acquires gets a new token,
    and the old process must not be able to commit on the strength of a name
    it shares.  Expiry alone is not enough either: an expired lease nobody
    has taken over is still ours in the only sense that matters, but an
    expired lease somebody *has* taken over is not, and only the token tells
    those apart.

    Reads inside the caller's write transaction, where SQLite guarantees no
    other writer can change the row before the caller commits.
    """
    row = db.execute(
        """SELECT owner, token_sha256, expires_at FROM memory_bridge_lease
           WHERE consumer=?""",
        (consumer,),
    ).fetchone()
    if row is None or row[0] is None:
        raise LeaseLost(
            "the ingest lease is no longer held by this worker",
            detail={"source": "lease", "held": False},
        )
    if str(row[0]) != str(lease.owner) or str(row[1]) != sha256_text(lease.token):
        raise LeaseLost(
            "the ingest lease was taken over by another worker",
            detail={"source": "lease", "held": True},
        )
    if _parse_stamp(row[2]) <= _parse_stamp(lease.now):
        raise LeaseLost(
            "the ingest lease expired before this write",
            detail={"source": "lease", "expired": True},
        )


def renew_ingest_lease(
    db: sqlite3.Connection,
    lease: LeaseFence,
    *,
    now: str,
    consumer: str = DEFAULT_CONSUMER,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> LeaseFence:
    """Extend this worker's lease, keeping its token.

    Keeping the token is the whole point.  Re-acquiring would mint a new one
    and any in-flight fence built on the old token would fail, so a renewal
    that looked like a takeover would fence the worker against itself.

    Refuses -- :class:`LeaseLost` -- if the lease is no longer ours, including
    when it expired and a replacement took it.  An expired lease that nobody
    claimed is renewed: that is an uncontested extension of our own hold, not
    a seizure of somebody else's.
    """
    if not db.in_transaction:
        raise BridgeError("renewing the ingest lease needs a write transaction")
    lease_seconds = max(1, min(int(lease_seconds), 3600))
    row = db.execute(
        """SELECT owner, token_sha256 FROM memory_bridge_lease
           WHERE consumer=?""",
        (consumer,),
    ).fetchone()
    if (
        row is None
        or row[0] is None
        or str(row[0]) != str(lease.owner)
        or str(row[1]) != sha256_text(lease.token)
    ):
        raise LeaseLost(
            "the ingest lease cannot be renewed by this worker",
            detail={"source": "lease"},
        )
    expires_at = (_parse_stamp(now) + timedelta(seconds=lease_seconds)).isoformat()
    db.execute(
        """UPDATE memory_bridge_lease SET expires_at=?
           WHERE consumer=? AND owner=? AND token_sha256=?""",
        (expires_at, consumer, str(lease.owner), sha256_text(lease.token)),
    )
    return LeaseFence(owner=lease.owner, token=lease.token, now=now)


def lease_state(db: sqlite3.Connection, *, consumer: str = DEFAULT_CONSUMER) -> dict[str, Any]:
    row = db.execute(
        "SELECT owner, acquired_at, expires_at FROM memory_bridge_lease WHERE consumer=?",
        (consumer,),
    ).fetchone()
    if row is None:
        return {"held": False, "owner": None, "expires_at": None}
    return {
        "held": row[0] is not None,
        "owner": row[0],
        "acquired_at": row[1],
        "expires_at": row[2],
    }


# ---------------------------------------------------------------------------
# Schema 53: the rebuild and cutover audit
# ---------------------------------------------------------------------------

BRIDGE_REBUILD_DDL: tuple[str, ...] = (
    # Every rebuild and every generation cutover, applied or refused.  A
    # refused attempt rolls back everything it touched, so without this row
    # there would be no evidence it ever happened -- and "the operator tried
    # to add a projection family and the history would not validate" is
    # exactly the kind of thing that must not vanish.
    """CREATE TABLE IF NOT EXISTS memory_bridge_rebuilds (
        id INTEGER PRIMARY KEY,
        consumer TEXT NOT NULL,
        attempted_at TEXT NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('rebuild','generation_cutover')),
        outcome TEXT NOT NULL CHECK(outcome IN ('applied','rejected')),
        from_generation_id INTEGER NOT NULL,
        to_generation_id INTEGER,
        from_sequence INTEGER NOT NULL CHECK(from_sequence >= 0),
        result_sequence INTEGER,
        event_count INTEGER NOT NULL CHECK(event_count >= 0),
        halt_code TEXT,
        CHECK((outcome = 'rejected') = (halt_code IS NOT NULL)),
        CHECK((outcome = 'applied') = (to_generation_id IS NOT NULL))
    )""",
)


def rebuild_audit_ready(db: sqlite3.Connection) -> bool:
    row = db.execute(
        """SELECT 1 FROM sqlite_master
           WHERE type='table' AND name='memory_bridge_rebuilds'"""
    ).fetchone()
    return row is not None


def migrate_bridge_v53(db: sqlite3.Connection, *, now: str) -> dict[str, Any]:
    """Add the rebuild and cutover audit.  Additive; nothing is read.

    ``now`` is accepted for symmetry with the other bridge migrations and to
    keep the call site uniform; this one has no row to stamp.
    """
    del now
    if not db.in_transaction:
        raise BridgeError("the bridge migration runs inside a write transaction")
    existed = rebuild_audit_ready(db)
    for statement in BRIDGE_REBUILD_DDL:
        db.execute(statement)
    return {"created": 0 if existed else 1}


# ---------------------------------------------------------------------------
# Schema 54: ingest health and the bounded incident ledger
# ---------------------------------------------------------------------------

BRIDGE_HEALTH_DDL: tuple[str, ...] = (
    # One row per consumer: what the last pass did, and how long it has been
    # since one did anything useful.  A halted bridge was previously visible
    # only to whoever happened to read the cursor; every other failure -- a
    # reader error, a lost lease, a halt that could not be persisted -- was
    # visible nowhere at all once the process exited.
    """CREATE TABLE IF NOT EXISTS memory_bridge_health (
        consumer TEXT PRIMARY KEY,
        updated_at TEXT NOT NULL,
        last_pass_at TEXT NOT NULL,
        last_pass_status TEXT NOT NULL,
        last_progress_at TEXT,
        last_events INTEGER NOT NULL DEFAULT 0 CHECK(last_events >= 0),
        consecutive_failures INTEGER NOT NULL DEFAULT 0
            CHECK(consecutive_failures >= 0),
        caught_up INTEGER CHECK(caught_up IN (0, 1)),
        behind_at_least INTEGER CHECK(behind_at_least IS NULL OR behind_at_least >= 0),
        observed_at TEXT
    )""",
    # Aggregated failures.  Primary key over a closed code set, so a flood
    # collapses into a counter instead of a log the operator has to scroll.
    """CREATE TABLE IF NOT EXISTS memory_bridge_incidents (
        consumer TEXT NOT NULL,
        code TEXT NOT NULL,
        first_seen TEXT NOT NULL,
        last_seen TEXT NOT NULL,
        occurrences INTEGER NOT NULL CHECK(occurrences > 0),
        last_cursor_sequence INTEGER,
        detail TEXT,
        PRIMARY KEY(consumer, code)
    )""",
)


def health_ready(db: sqlite3.Connection) -> bool:
    row = db.execute(
        """SELECT 1 FROM sqlite_master
           WHERE type='table' AND name='memory_bridge_health'"""
    ).fetchone()
    return row is not None


def migrate_bridge_v54(db: sqlite3.Connection, *, now: str) -> dict[str, Any]:
    """Add ingest health and the incident ledger.  Additive; nothing is read."""
    del now
    if not db.in_transaction:
        raise BridgeError("the bridge migration runs inside a write transaction")
    existed = health_ready(db)
    for statement in BRIDGE_HEALTH_DDL:
        db.execute(statement)
    return {"created": 0 if existed else 1}


def _seconds_between(earlier: Any, later: Any) -> float | None:
    try:
        return (_parse_stamp(later) - _parse_stamp(earlier)).total_seconds()
    except (TypeError, ValueError):
        return None


def record_pass(
    db: sqlite3.Connection,
    *,
    status: str,
    now: str,
    consumer: str = DEFAULT_CONSUMER,
    events: int = 0,
    detail: Mapping[str, Any] | None = None,
    caught_up: bool | None = None,
    behind_at_least: int | None = None,
    cursor_sequence: int | None = None,
    heartbeat_seconds: int = HEALTH_HEARTBEAT_SECONDS,
) -> bool:
    """Record what one ingest pass did.  Returns whether a row was written.

    Runs inside the caller's write transaction.  Three rules decide what is
    written, and each of them is a requirement rather than a preference:

    * **A failure is always recorded.**  The whole point is that a reader
      error or a lost lease used to leave no trace once the process exited.
    * **A quiet success is usually not.**  A worker polling every five seconds
      would otherwise commit a write forever while doing nothing; healthy
      passes are written only when something changed or the heartbeat is due.
    * **The status is never softened.**  ``halt_not_recorded`` is stored as
      itself, never as ``halted``: one of those means an operator has to
      clear a durable flag and the other means the flag was never set.
    """
    if not db.in_transaction:
        raise BridgeError("recording pass health runs inside a write transaction")
    if not health_ready(db):
        return False
    status = str(status)
    if status not in BRIDGE_PASS_STATUSES:
        raise BridgeError(f"unknown bridge pass status {status!r}")
    failed = status not in HEALTHY_PASS_STATUSES
    row = db.execute(
        """SELECT last_pass_status, last_pass_at, consecutive_failures,
                  last_progress_at, caught_up, behind_at_least, observed_at
           FROM memory_bridge_health WHERE consumer=?""",
        (consumer,),
    ).fetchone()
    previous_status = None if row is None else str(row[0])
    previous_at = None if row is None else row[1]
    failures = 0 if row is None else int(row[2])
    progress_at = None if row is None else row[3]

    # A pass that did not measure the backlog must not erase the measurement
    # that did.  A run of reader failures otherwise wipes the last known
    # position and reports "unknown" for a store that was measurably behind
    # ten seconds ago -- losing exactly the signal an operator needs while the
    # bridge is failing.  ``observed_at`` carries the age instead.
    observed_at = now
    if caught_up is None and row is not None and row[4] is not None:
        caught_up = bool(row[4])
        behind_at_least = row[5]
        observed_at = row[6]

    changed = previous_status != status
    due = (
        row is None
        or (_seconds_between(previous_at, now) or 0) >= max(1, int(heartbeat_seconds))
    )
    if not (failed or changed or due or int(events) > 0):
        return False

    failures = failures + 1 if failed else 0
    if int(events) > 0 or status in {"idle", "ok"}:
        progress_at = now
    db.execute(
        """INSERT INTO memory_bridge_health(
               consumer, updated_at, last_pass_at, last_pass_status,
               last_progress_at, last_events, consecutive_failures,
               caught_up, behind_at_least, observed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(consumer) DO UPDATE SET
               updated_at=excluded.updated_at,
               last_pass_at=excluded.last_pass_at,
               last_pass_status=excluded.last_pass_status,
               last_progress_at=excluded.last_progress_at,
               last_events=excluded.last_events,
               consecutive_failures=excluded.consecutive_failures,
               caught_up=excluded.caught_up,
               behind_at_least=excluded.behind_at_least,
               observed_at=excluded.observed_at""",
        (
            consumer, now, now, status, progress_at, max(0, int(events)),
            failures,
            (None if caught_up is None else int(bool(caught_up))),
            (None if behind_at_least is None else max(0, int(behind_at_least))),
            observed_at,
        ),
    )
    if failed:
        _record_incident(
            db, code=status, now=now, consumer=consumer, detail=detail,
            cursor_sequence=cursor_sequence,
        )
    return True


def _record_incident(
    db: sqlite3.Connection,
    *,
    code: str,
    now: str,
    consumer: str,
    detail: Mapping[str, Any] | None,
    cursor_sequence: int | None,
) -> None:
    """Aggregate one failure into its row.

    Keyed on the code rather than appended, so ten thousand identical reader
    errors are one row with ``occurrences = 10000``.  The table is bounded by
    construction: the key space is :data:`INCIDENT_CODES`.

    ``detail`` goes through :func:`_safe_detail` like a halt reason does, for
    the same reason -- this row is operator-facing and outlives the failure.
    """
    if code not in INCIDENT_CODES:
        raise BridgeError(f"unknown incident code {code!r}")
    db.execute(
        """INSERT INTO memory_bridge_incidents(
               consumer, code, first_seen, last_seen, occurrences,
               last_cursor_sequence, detail)
           VALUES (?, ?, ?, ?, 1, ?, ?)
           ON CONFLICT(consumer, code) DO UPDATE SET
               last_seen=excluded.last_seen,
               occurrences=memory_bridge_incidents.occurrences + 1,
               last_cursor_sequence=excluded.last_cursor_sequence,
               detail=excluded.detail""",
        (
            consumer, code, now, now,
            (None if cursor_sequence is None else int(cursor_sequence)),
            (_safe_detail(detail) or None),
        ),
    )


def clear_incidents(
    db: sqlite3.Connection, *, consumer: str = DEFAULT_CONSUMER
) -> int:
    """Drop the incident ledger after an operator has looked at it."""
    cursor = db.execute(
        "DELETE FROM memory_bridge_incidents WHERE consumer=?", (consumer,)
    )
    return int(cursor.rowcount or 0)


def should_report_pass(
    previous_status: str | None, status: str, occurrences: int
) -> bool:
    """Whether a worker should print this pass outcome.

    A halted bridge used to print on every poll -- once every five seconds,
    forever, saying the same thing.  That is not visibility; it buries the
    next real event.  The rule here is the usual one for repeated conditions:
    say it when it starts, then on an exponential schedule.

    Powers of two rather than a time window because it is deterministic and
    therefore testable, and because it degrades sensibly whatever the poll
    interval is: the tenth identical failure is quiet, the thousandth is not.
    """
    if previous_status != status:
        return True
    count = max(1, int(occurrences))
    return count & (count - 1) == 0


def bridge_health(
    db: sqlite3.Connection, *, consumer: str = DEFAULT_CONSUMER, now: str | None = None
) -> dict[str, Any]:
    """Ingest health as closed codes, counts and timestamps.

    ``healthy`` is false whenever the last pass failed *or* the consumer is
    halted, so no caller can render a stopped bridge as a running one by
    reading a single field.
    """
    if not health_ready(db):
        return {"known": False, "healthy": None, "incidents": []}
    row = db.execute(
        """SELECT updated_at, last_pass_at, last_pass_status, last_progress_at,
                  last_events, consecutive_failures, caught_up, behind_at_least,
                  observed_at
           FROM memory_bridge_health WHERE consumer=?""",
        (consumer,),
    ).fetchone()
    incidents = [
        {
            "code": str(item[0]),
            "first_seen": item[1],
            "last_seen": item[2],
            "occurrences": int(item[3]),
            "last_cursor_sequence": item[4],
            "detail": item[5],
        }
        for item in db.execute(
            """SELECT code, first_seen, last_seen, occurrences,
                      last_cursor_sequence, detail
               FROM memory_bridge_incidents WHERE consumer=?
               ORDER BY last_seen DESC, code""",
            (consumer,),
        )
    ]
    state = read_cursor(db, consumer=consumer)
    halted = str(state["status"]) == "halted"
    if row is None:
        return {
            "known": False,
            "healthy": (not halted),
            "halted": halted,
            "incidents": incidents,
        }
    status = str(row[2])
    failed = status not in HEALTHY_PASS_STATUSES
    return {
        "known": True,
        "healthy": (not failed) and (not halted),
        "halted": halted,
        "last_pass_at": row[1],
        "last_pass_status": status,
        "last_pass_failed": failed,
        "last_progress_at": row[3],
        "last_events": int(row[4]),
        "consecutive_failures": int(row[5]),
        "caught_up": (None if row[6] is None else bool(row[6])),
        "behind_at_least": row[7],
        "observed_at": row[8],
        "stalled_seconds": (
            None if (row[3] is None or now is None)
            else _seconds_between(row[3], now)
        ),
        "incidents": incidents,
    }


# ---------------------------------------------------------------------------
# Bounded worker ingestion
# ---------------------------------------------------------------------------

def _record_pass_from_summary(
    db: sqlite3.Connection,
    summary: Mapping[str, Any],
    *,
    now: str,
    consumer: str,
    transaction: Any,
    caught_up: bool | None,
    behind_at_least: int | None,
) -> None:
    """Persist what this pass did, without letting that fail the pass.

    Health is diagnostic state.  If recording it raises -- a busy database, a
    store migrated only as far as 53 -- the pass still happened and its real
    outcome still stands, so the exception is swallowed here and nowhere
    else.  Suppressing it around actual ingestion would be a different and
    much worse decision.
    """
    status = str(summary.get("status") or "error")
    if status not in BRIDGE_PASS_STATUSES:
        status = "error"
    detail: dict[str, Any] = {}
    if summary.get("halt_code"):
        detail["halt"] = str(summary["halt_code"])
    if summary.get("attempted_halt_code"):
        detail["attempted"] = str(summary["attempted_halt_code"])
    try:
        with transaction():
            state = read_cursor(db, consumer=consumer)
            record_pass(
                db, status=status, now=now, consumer=consumer,
                events=int(summary.get("events") or 0),
                detail=detail or None, caught_up=caught_up,
                behind_at_least=behind_at_least,
                cursor_sequence=int(state["last_sequence"]),
            )
    except (BridgeError, sqlite3.Error):
        return


def ingest_once(
    db: sqlite3.Connection,
    reader: Any,
    *,
    now: str,
    owner: str,
    consumer: str = DEFAULT_CONSUMER,
    max_events: int = MAX_INGEST_EVENTS_PER_PASS,
    batch_size: int = MAX_INGEST_BATCH,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    transaction: Any = None,
    clock: Any = None,
) -> dict[str, Any]:
    """Run one bounded ingest pass under a fenced lease.

    ``reader(after_sequence, after_event_id, limit)`` returns the next runtime
    batch.  The bridge never imports the runtime module: the caller supplies
    the reader, so the only dependency direction is worker -> runtime.

    A reader may fail, and *how* it fails decides what happens next.  A
    :class:`ReaderFailure` carrying a code is structural -- the adapter's
    history cannot serve this consumer -- and becomes a durable halt that an
    operator must clear, because retrying it forever in silence is how a
    bridge ends up quietly not following anything.  A ``ReaderFailure`` with
    no code, or any exception the adapter did not classify, is operational:
    the pass ends, nothing is halted, and the next pass tries again.  The
    bridge never reads an adapter's message to decide which it was.

    ``transaction`` is a context-manager factory (``Memory._immediate_transaction``)
    used once per batch.  Each batch commits with its cursor, fenced against
    the lease, so a worker that was evicted mid-pass commits nothing.

    ``clock`` returns the current ISO timestamp.  It defaults to the constant
    ``now``; supplying a real clock lets lease renewal and expiry be judged
    against the time a batch actually commits, and lets a test drive a
    takeover deterministically instead of racing one.
    """
    if transaction is None:
        raise BridgeError("ingest_once needs a transaction factory")
    if clock is None:
        def clock() -> str:
            return now
    max_events = max(1, min(int(max_events), 100_000))
    batch_size = max(1, min(int(batch_size), 5_000))
    summary: dict[str, Any] = {
        "status": "idle", "batches": 0, "events": 0, "projected_items": 0,
        "quarantined_items": 0, "ignored_events": 0, "lease": "not_acquired",
        "halt_code": None,
    }

    token: str | None = None
    with transaction():
        state = read_cursor(db, consumer=consumer)
        if str(state["status"]) == "halted":
            summary["status"] = "halted"
            summary["halt_code"] = state["halt_code"]
        else:
            token = acquire_ingest_lease(
                db, owner=owner, now=clock(), consumer=consumer,
                lease_seconds=lease_seconds,
            )
            if token is None:
                summary["status"] = "lease_held_elsewhere"
    if token is None:
        # Nothing was read and nothing was written, but the pass still happened
        # and still failed to ingest.  Recording it is what lets an operator see
        # *how long* a halted bridge has been halted, and what lets a worker
        # escalate its reporting instead of restating the same line every poll.
        _record_pass_from_summary(
            db, summary, now=clock(), consumer=consumer, transaction=transaction,
            caught_up=None, behind_at_least=None,
        )
        return summary
    summary["lease"] = "acquired"

    lease = LeaseFence(owner=str(owner), token=token, now=clock())
    lease_lost = False
    caught_up: bool | None = None
    behind_at_least: int | None = None
    try:
        consumed = 0
        while consumed < max_events:
            state = read_cursor(db, consumer=consumer)
            if str(state["status"]) == "halted":
                summary["status"] = "halted"
                summary["halt_code"] = state["halt_code"]
                break
            limit = min(batch_size, max_events - consumed)
            try:
                events = list(
                    reader(int(state["last_sequence"]), state["last_event_id"], limit)
                )
            except ReaderFailure as failure:
                if not failure.structural:
                    summary["status"] = "reader_unavailable"
                    break
                lease = LeaseFence(owner=lease.owner, token=lease.token, now=clock())
                try:
                    with transaction():
                        outcome = halt_for_reader_failure(
                            db, failure, now=lease.now, consumer=consumer,
                            expected_sequence=int(state["last_sequence"]),
                            lease=lease,
                        )
                except LeaseLost:
                    lease_lost = True
                    summary["status"] = "lease_lost"
                    break
                except BridgeError:
                    # The halt could not be persisted, so it did not happen.
                    # Say so: reporting a halt that is not in the database
                    # would leave an operator clearing a flag nobody set.
                    summary["status"] = "halt_not_recorded"
                    summary["attempted_halt_code"] = failure.code
                    break
                summary["status"] = "halted"
                summary["halt_code"] = outcome["halt_code"]
                break
            except BridgeError:
                raise
            except Exception:
                # The adapter did not classify this, so neither will we.  An
                # unclassified exception is no basis for asserting that the
                # runtime history is structurally unusable.
                summary["status"] = "reader_error"
                break
            if not events:
                caught_up = True
                summary["status"] = "idle" if summary["batches"] == 0 else "ok"
                break
            # A short batch proves the stream is exhausted at this point;
            # a full one proves nothing either way.
            caught_up = len(events) < limit
            try:
                with transaction():
                    # Renew inside the same transaction as the import, keeping
                    # the token, so a pass longer than one lease period stays
                    # fenced instead of quietly running on an expired hold.
                    lease = renew_ingest_lease(
                        db, lease, now=clock(), consumer=consumer,
                        lease_seconds=lease_seconds,
                    )
                    result = import_batch(
                        db, events, now=lease.now, consumer=consumer,
                        expected_sequence=int(state["last_sequence"]),
                        expected_event_id=state["last_event_id"],
                        lease=lease,
                    )
            except LeaseLost:
                # Another worker holds the lease.  This transaction rolled
                # back, so the batch is simply not ours to commit.
                lease_lost = True
                summary["status"] = "lease_lost"
                break
            summary["batches"] += 1
            consumed += len(events)
            if result["status"] == "halted":
                summary["status"] = "halted"
                summary["halt_code"] = result["halt_code"]
                break
            summary["events"] += int(result["events"])
            summary["projected_items"] += int(result["projected_items"])
            summary["quarantined_items"] += int(result["quarantined_items"])
            summary["ignored_events"] += int(result["ignored_events"])
            summary["status"] = "ok"
        else:
            summary["status"] = "bounded"
        if summary["status"] == "bounded":
            # The pass stopped at its own bound, which says nothing about
            # whether work remains.  One small read answers that, and the
            # answer is the difference between 'bounded' meaning
            # 'falling behind' and meaning 'finished on the last event'.
            # The loop's own verdict is not authoritative for a bounded pass:
            # a final full batch proves nothing either way.  Discard it, and
            # let the probe answer or leave the question open.
            caught_up = None
            behind_at_least = None
            try:
                state = read_cursor(db, consumer=consumer)
                probe = list(reader(
                    int(state["last_sequence"]),
                    state["last_event_id"], batch_size,
                ))
            except Exception:
                # Diagnostic only.  A probe failure must not change the
                # outcome of a pass that already did its work.
                probe = None
            if probe is not None:
                caught_up = not probe
                behind_at_least = len(probe)
    finally:
        if lease_lost:
            # Do not touch the lease row: it belongs to somebody else now, and
            # the release statement is scoped to our own token anyway.
            summary["lease"] = "lost"
        else:
            with transaction():
                released = release_ingest_lease(
                    db, owner=owner, token=token, consumer=consumer
                )
            # Report what happened, not what was attempted.  The first cut of
            # this function discarded this return value and always said
            # 'released', which reads as an orderly handover in exactly the
            # case where a worker had been evicted.
            summary["lease"] = "released" if released else "lost"
        _record_pass_from_summary(
            db, summary, now=clock(), consumer=consumer,
            transaction=transaction, caught_up=caught_up,
            behind_at_least=behind_at_least,
        )
    return summary
