"""The memory spine: one append-only, keyed, hash-chained event contract.

Every derived memory row (slice 1: the claim projection; slice 2: ordinary
memories and lessons) is produced by an event on the spine and carries that
event's id.  Events are never updated or deleted; the only permitted change is
a tombstone-backed redaction that nulls a payload together with its salt.  The
chain digest is a keyed MAC over the canonical JSON of every immutable column,
and a keyed head record names the newest event, so ``verify`` means
*authentic and complete* against a writer who has the database file but not
the key sidecar: no event altered, inserted, reordered, or removed.  A writer
who also holds the key can rewrite the chain; that is the honest bound of a
local single-user store.

Memory events (schema 47) carry digests only, never content: the row stays
the content authority, and the spine gives it lineage, tamper evidence (a
keyed content digest that cannot be brute-forced without the sidecar), and
deletion receipts.  The claim projection can additionally be reconciled in
place from the spine (``apply_claim_projection``).

This module owns only SQL on a caller-supplied connection; ``Memory`` owns
``user_version``, transactions, locking, and the key.  See
``docs/MEMORY_SPINE.md`` and the M2 design.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

SPINE_SCHEMA_VERSION = 49
MAX_PAYLOAD_BYTES = 16 * 1024
GENESIS_PREV_SHA256 = "0" * 64
KEY_SIDECAR_SUFFIX = ".memory-spine.key"

SPINE_KINDS: frozenset[str] = frozenset({
    "spine.genesis",
    "claim.imported",
    "claim.created",
    "claim.reasserted",
    "claim.superseded",
    "claim.disputed",
    "claim.retracted",
    "claim.tombstoned",
    "proposal.not_stored",
    "proposal.confirmed",
    "conversation.deleted",
    # Schema 49: one transcript span reduced to a milestone (VTMF M5 half A).
    # Compaction is the only path that moves durable operator bytes out of
    # ``messages``, so unlike the graph it earns a kind of its own.
    "transcript.compacted",
    "projection.rebuilt",
    "memory.imported",
    "memory.created",
    "memory.reasserted",
    "memory.updated",
    "memory.deleted",
    "lesson.created",
    # Schema 48 (red team R-6): the proof's own evidence table, receipted.
    # `lesson_applications` decides whether a document is promoted, and before
    # this it carried no digest at all -- so rows planted by raw SQL before a
    # seal manufactured a complete proof with every verifier still green.
    "lesson.applied",
    # Schema 48: the learning ladder.  Digest-only like every memory kind, and
    # carrying no confirmation-code material of any sort -- ``ladder.staged``
    # publishes the boolean ``token_required`` and nothing else about it, so
    # the code lives only in the ``ladder_promotions`` row the operator reads.
    "ladder.calibration_sealed",
    "ladder.candidate",
    "ladder.staged",
    "ladder.approved",
    "ladder.rolled_back",
    "ladder.withdrawn",
    "ladder.grandfathered",
})
LADDER_KINDS: frozenset[str] = frozenset({
    "ladder.calibration_sealed", "ladder.candidate", "ladder.staged",
    "ladder.approved", "ladder.rolled_back", "ladder.withdrawn",
    "ladder.grandfathered",
})
#: The two transitions only a typed operator command may produce.  Structural,
#: not conventional: ``append_event`` raises on any other actor, which is what
#: makes "approval and rollback are operator-typed only" assertable by looking
#: at the chain alone.
LADDER_OPERATOR_KINDS: frozenset[str] = frozenset({
    "ladder.approved", "ladder.rolled_back",
})
CLAIM_CREATING_KINDS: frozenset[str] = frozenset({"claim.imported", "claim.created"})
CLAIM_STATUS_KINDS: frozenset[str] = frozenset({
    "claim.reasserted", "claim.superseded", "claim.disputed", "claim.retracted",
})
PROPOSAL_KINDS: frozenset[str] = frozenset({"proposal.not_stored", "proposal.confirmed"})
REDACTABLE_KINDS: frozenset[str] = CLAIM_CREATING_KINDS | CLAIM_STATUS_KINDS | PROPOSAL_KINDS
MEMORY_CREATING_KINDS: frozenset[str] = frozenset({
    "memory.imported", "memory.created", "lesson.created",
})
MEMORY_KINDS: frozenset[str] = MEMORY_CREATING_KINDS | frozenset({
    "memory.reasserted", "memory.updated", "memory.deleted",
})
SPINE_ACTORS: frozenset[str] = frozenset({
    "operator", "runtime", "model", "worker", "companion", "system",
})
SPINE_OUTCOMES: frozenset[str] = frozenset({"applied", "rejected", "noop"})
SPINE_SUBJECT_KINDS: frozenset[str] = frozenset({
    "claim", "conversation", "projection", "proposal", "memory",
    # Schema 48: a promotion row, a sealed calibration-ledger row, and the
    # lesson an application receipt is about.
    "ladder", "calibration", "lesson",
})
_CLAIM_PAYLOAD_KEYS: frozenset[str] = frozenset({
    "at", "claim_key", "subject", "predicate", "value", "value_sha256",
    "source", "authority", "confidence", "status", "valid_from", "valid_until",
    "supersedes_id", "original_created_at",
})
_STATUS_PAYLOAD_KEYS: frozenset[str] = frozenset({
    "at", "claim_key", "claim_id", "reason", "related_claim_id", "status",
    "valid_until", "confidence", "authority", "source",
})
_TOMBSTONE_REQUIRED_KEYS: frozenset[str] = frozenset({"at", "claim_key", "removed_claim_ids"})
_TOMBSTONE_PAYLOAD_KEYS: frozenset[str] = _TOMBSTONE_REQUIRED_KEYS | frozenset({
    "redacted_event_ids", "transcript_copies", "removed_memory_ids",
    # Entities the erase orphaned in the graph projection (schema 48).
    "removed_entity_ids",
    # Milestones the erase removed whole, and their spans (schema 49): a
    # milestone naming a tombstoned claim key is deleted rather than edited,
    # because neither M5 table permits an UPDATE.  Capped at
    # MILESTONE_TOMBSTONE_MAX_IDS and chunked past it.
    "removed_milestone_ids", "removed_span_handles",
})
# Digest-only after-image of a memories row: never the content itself.
_MEMORY_REQUIRED_KEYS: frozenset[str] = frozenset({
    "kind", "content_digest", "content_length", "source", "family",
    "outcome_status", "reflection_id", "origin", "eligible",
})
_MEMORY_PAYLOAD_KEYS: frozenset[str] = _MEMORY_REQUIRED_KEYS | frozenset({"at", "provenance_sha256"})
_LESSON_REQUIRED_KEYS: frozenset[str] = _MEMORY_REQUIRED_KEYS | frozenset({"provenance_sha256"})
_REASSERT_REQUIRED_KEYS: frozenset[str] = frozenset({"origin", "eligible", "content_digest"})
_REASSERT_PAYLOAD_KEYS: frozenset[str] = _REASSERT_REQUIRED_KEYS | frozenset({
    "at", "kind", "provenance_sha256",
})
_DELETED_REQUIRED_KEYS: frozenset[str] = frozenset({"ids", "content_digests", "reason"})
_DELETED_PAYLOAD_KEYS: frozenset[str] = _DELETED_REQUIRED_KEYS | frozenset({
    "at", "kind", "transcript_copies",
})
_REBUILT_REQUIRED_KEYS: frozenset[str] = frozenset({
    "rows_before", "rows_after", "divergences_fixed", "removed_ids",
})
_REBUILT_PAYLOAD_KEYS: frozenset[str] = _REBUILT_REQUIRED_KEYS | frozenset({
    "at", "removed_memory_ids", "recreated_ids", "updated_ids", "lost_evidence_claim_ids",
    # Schema 48: the graph projection reuses this receipt rather than adding a
    # spine kind of its own.  "projection" is "claims" when absent.
    "projection", "entities", "excluded", "removed_entity_ids", "graph_reprojected",
})
#: ``spine rebuild-milestones`` reuses ``projection.rebuilt`` rather than
#: adding a kind: a derived rebuild reuses the receipt and only a real
#: mutation earns a kind (M3's rule, applied to M5).
_REBUILT_PROJECTIONS: frozenset[str] = frozenset({"claims", "graph", "milestones"})
# --- schema 48: the learning ladder's seven payload contracts -------------
# Only integers, families, derived skill names, timestamps, digests, booleans
# and closed-set reason codes.  No document text, no lesson text, no operator
# prose, and no confirmation-code material of any kind.
_LADDER_SEALED_REQUIRED_KEYS: frozenset[str] = frozenset({
    "at", "family", "epoch", "n", "successes", "brier", "calibration_error",
    "unverified_at_seal", "first_prediction_id", "last_prediction_id",
    "coverage_digest",
})
_LADDER_SEALED_PAYLOAD_KEYS: frozenset[str] = _LADDER_SEALED_REQUIRED_KEYS | frozenset({
    "mean_predicted", "evidence_applicable", "evidence_successes",
    "applied_n", "applied_successes", "unapplied_n", "unapplied_successes",
    "refused_stagings", "refused_approvals", "withdrawals", "screened_components",
})
_APPLIED_REQUIRED_KEYS: frozenset[str] = frozenset({
    "at", "prediction_id", "family", "project_id", "lesson_ids",
    "applications_digest", "count",
})
_APPLIED_PAYLOAD_KEYS: frozenset[str] = _APPLIED_REQUIRED_KEYS
_LADDER_SUBJECT_KEYS: frozenset[str] = frozenset({
    "at", "family", "project_id", "skill_name",
})
_LADDER_CANDIDATE_REQUIRED_KEYS: frozenset[str] = _LADDER_SUBJECT_KEYS | frozenset({
    "lesson_ids", "reuse_count", "context_count", "proof_sha256", "epoch",
    "gate_allowed", "ledger_monotone",
})
_LADDER_CANDIDATE_PAYLOAD_KEYS: frozenset[str] = (
    _LADDER_CANDIDATE_REQUIRED_KEYS | frozenset({"brier", "calibration_error", "attempts"})
)
_LADDER_STAGED_REQUIRED_KEYS: frozenset[str] = _LADDER_SUBJECT_KEYS | frozenset({
    "staged_sha256", "verified_outcomes", "tools_count", "oracles_count",
    "token_required",
})
_LADDER_STAGED_PAYLOAD_KEYS: frozenset[str] = _LADDER_STAGED_REQUIRED_KEYS | frozenset({
    "prior_sha256", "stage_reason",
})
_LADDER_APPROVED_REQUIRED_KEYS: frozenset[str] = _LADDER_SUBJECT_KEYS | frozenset({
    "approved_sha256", "proof_sha256", "epoch", "gate_allowed", "ledger_monotone",
})
_LADDER_APPROVED_PAYLOAD_KEYS: frozenset[str] = (
    _LADDER_APPROVED_REQUIRED_KEYS
    # Optional, int | None: the promotion this approval retired from the live
    # slot.  `idx_ladder_promotions_one_live` is UNIQUE over
    # (project_id, skill_name) across approved and unapproved_legacy, so a
    # second approval of the same skill must retire whatever held the slot --
    # approved as readily as legacy -- and the receipt should name what it
    # displaced.  None when the approval was the first for that skill.
    | frozenset({"prior_sha256", "superseded_promotion_id"})
)
_LADDER_GRANDFATHERED_REQUIRED_KEYS: frozenset[str] = _LADDER_SUBJECT_KEYS | frozenset({
    "approved_sha256", "source",
})
_LADDER_GRANDFATHERED_PAYLOAD_KEYS: frozenset[str] = _LADDER_GRANDFATHERED_REQUIRED_KEYS
_LADDER_ROLLED_BACK_REQUIRED_KEYS: frozenset[str] = _LADDER_SUBJECT_KEYS | frozenset({
    "restored_sha256", "removed_sha256", "reason",
})
_LADDER_ROLLED_BACK_PAYLOAD_KEYS: frozenset[str] = (
    _LADDER_ROLLED_BACK_REQUIRED_KEYS
    # Optional, int | None: the promotion this rollback brought back to
    # `approved`.  The counterpart of `superseded_promotion_id` on
    # `ladder.approved` -- one records what an approval retired, the other what
    # a rollback reinstated.  Rolling back a superseding approval has to undo
    # the retirement too, or the restored bytes sit in the live root with no
    # approved row to serve them.  None on an ordinary first-level rollback.
    | frozenset({"reinstated_promotion_id"})
)
_LADDER_WITHDRAWN_REQUIRED_KEYS: frozenset[str] = _LADDER_SUBJECT_KEYS | frozenset({
    "reason",
})
_LADDER_WITHDRAWN_PAYLOAD_KEYS: frozenset[str] = _LADDER_WITHDRAWN_REQUIRED_KEYS | frozenset({
    "withdrawn_sha256",
})
_LADDER_PAYLOAD_KEYS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "ladder.calibration_sealed": (
        _LADDER_SEALED_PAYLOAD_KEYS, _LADDER_SEALED_REQUIRED_KEYS,
    ),
    "ladder.candidate": (
        _LADDER_CANDIDATE_PAYLOAD_KEYS, _LADDER_CANDIDATE_REQUIRED_KEYS,
    ),
    "ladder.staged": (_LADDER_STAGED_PAYLOAD_KEYS, _LADDER_STAGED_REQUIRED_KEYS),
    "ladder.approved": (_LADDER_APPROVED_PAYLOAD_KEYS, _LADDER_APPROVED_REQUIRED_KEYS),
    "ladder.grandfathered": (
        _LADDER_GRANDFATHERED_PAYLOAD_KEYS, _LADDER_GRANDFATHERED_REQUIRED_KEYS,
    ),
    "ladder.rolled_back": (
        _LADDER_ROLLED_BACK_PAYLOAD_KEYS, _LADDER_ROLLED_BACK_REQUIRED_KEYS,
    ),
    "ladder.withdrawn": (
        _LADDER_WITHDRAWN_PAYLOAD_KEYS, _LADDER_WITHDRAWN_REQUIRED_KEYS,
    ),
}
#: The subject kind each M4 event must name, so a promotion event can never be
#: filed against a claim or a memory, and an application receipt can only be
#: filed against the lesson it is about.
_LADDER_SUBJECT_KIND: dict[str, str] = {
    kind: ("calibration" if kind == "ladder.calibration_sealed" else "ladder")
    for kind in LADDER_KINDS
}
_LADDER_SUBJECT_KIND["lesson.applied"] = "lesson"
#: Every kind the M4 structural rules govern: no model actor, a required
#: subject kind, and a positive subject id.
_M4_STRUCTURED_KINDS: frozenset[str] = LADDER_KINDS | frozenset({"lesson.applied"})
#: Digest-valued keys, checked as 64 lowercase hex or NULL where nullable.
_LADDER_DIGEST_KEYS: frozenset[str] = frozenset({
    "coverage_digest", "proof_sha256", "staged_sha256", "prior_sha256",
    "approved_sha256", "restored_sha256", "removed_sha256", "withdrawn_sha256",
    "applications_digest",
})
#: Keys whose value must be a non-negative integer.
_LADDER_COUNT_KEYS: frozenset[str] = frozenset({
    "epoch", "n", "successes", "evidence_applicable", "evidence_successes",
    "applied_n", "applied_successes", "unapplied_n", "unapplied_successes",
    "refused_stagings", "refused_approvals", "withdrawals", "screened_components",
    "unverified_at_seal", "first_prediction_id", "last_prediction_id",
    "reuse_count", "context_count", "verified_outcomes", "tools_count",
    "oracles_count", "attempts", "project_id", "prediction_id", "count",
    "superseded_promotion_id", "reinstated_promotion_id",
})
#: Keys whose value must be a probability-scale float or NULL.
_LADDER_RATE_KEYS: frozenset[str] = frozenset({
    "mean_predicted", "brier", "calibration_error",
})
#: Keys whose value must be a strict boolean.
_LADDER_FLAG_KEYS: frozenset[str] = frozenset({
    "gate_allowed", "ledger_monotone", "token_required",
})
#: Keys whose value must be a closed-set-shaped code, never prose.
_LADDER_CODE_KEYS: frozenset[str] = frozenset({"reason", "source", "stage_reason"})
_LADDER_CODE = re.compile(r"[a-z][a-z0-9_]{0,39}\Z")
_LADDER_FAMILY = re.compile(r"[a-z][a-z0-9_]{0,39}\Z")
_LADDER_SKILL_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+){0,15}\Z")
_LADDER_MAX_LESSON_IDS = 10
#: Nothing about a confirmation code may ever enter the chain.  The closed key
#: sets already exclude it; this is the second lock, so a future edit that adds
#: such a key to a set still fails loudly instead of silently persisting a
#: cleartext approval value in an append-only table.
_LADDER_FORBIDDEN_KEY = re.compile(r"token|code|secret|password|credential")
# The three exclusion categories a graph receipt may break "excluded" down by
# (memory_graph.EXCLUSION_KINDS; named here so payload validation needs no
# import of the graph module).
_EXCLUDED_CATEGORY_KEYS: frozenset[str] = frozenset({
    "excluded_predicate", "subject_private", "subject_too_long",
})
_MEMORY_EQUIVALENCE_FIELDS: tuple[str, ...] = (
    "kind", "content_digest", "content_length", "source", "family",
    "outcome_status", "reflection_id", "origin", "eligible",
)
_EQUIVALENCE_COLUMNS: tuple[str, ...] = (
    "scope", "claim_key", "subject", "predicate", "value", "value_sha256",
    "status", "authority", "confidence", "source", "valid_from", "valid_until",
    "supersedes_id",
)
# Claim fields whose values a divergence detail may show.  Everything else
# (claim key, subject, predicate, value, its unkeyed digest, source) is
# operator text or brute-forceable and is reported as "differs" only.
_CLAIM_METADATA_FIELDS: frozenset[str] = frozenset({
    "scope", "status", "authority", "confidence", "valid_from", "valid_until",
    "supersedes_id",
})
# One memory.deleted names at most this many rows (128 ids with 64-hex
# digests stay well inside MAX_PAYLOAD_BYTES); writers chunk larger deletes.
MEMORY_DELETED_MAX_IDS = 128
#: One ``claim.tombstoned`` names at most this many milestones and span
#: handles; past the cap the writer emits further chunked events, exactly as
#: the memory path does.  A handle is ~30 characters, so 128 of each stays
#: well inside ``MAX_PAYLOAD_BYTES`` (VTMF M5 design 2.8, M-10).
MILESTONE_TOMBSTONE_MAX_IDS = 128
#: The shape of a rehydration handle, for validating
#: ``removed_span_handles`` without importing ``memory_compaction`` (which
#: imports this module).  ``[0-9]`` and ``re.ASCII`` deliberately, not ``\d``:
#: ``\d`` matches Unicode digits, so a confusable handle would validate here
#: and then fail to resolve.  Kept in step with
#: ``memory_compaction.HANDLE_PATTERN``, which a test asserts.
_SPAN_HANDLE_SHAPE = re.compile(
    r"\Amem:span/[0-9]{1,18}/[0-9]{1,9}/[0-9a-f]{12}\Z", re.ASCII
)
_EVENT_COLUMNS: tuple[str, ...] = (
    "id", "created_at", "kind", "actor", "source", "scope", "permission",
    "conversation_id", "subject_kind", "subject_id", "parent_event_id", "outcome",
    "payload_json", "payload_salt", "payload_sha256", "prev_sha256", "event_sha256",
    "redacted_by_event_id",
)
_CONTENT_DIGEST_TAG = b"jarvis-memory-content\0"
_APPLICATIONS_DIGEST_TAG = b"jarvis-lesson-applications\0"

_EVENT_TABLE_SQL = """CREATE TABLE memory_spine_events (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN (
        'spine.genesis','claim.imported','claim.created','claim.reasserted',
        'claim.superseded','claim.disputed','claim.retracted','claim.tombstoned',
        'proposal.not_stored','proposal.confirmed','conversation.deleted',
        'transcript.compacted','projection.rebuilt','memory.imported','memory.created','memory.reasserted',
        'memory.updated','memory.deleted','lesson.created','lesson.applied',
        'ladder.calibration_sealed','ladder.candidate','ladder.staged',
        'ladder.approved','ladder.rolled_back','ladder.withdrawn',
        'ladder.grandfathered')),
    actor TEXT NOT NULL CHECK(actor IN
        ('operator','runtime','model','worker','companion','system')),
    source TEXT NOT NULL,
    scope TEXT NOT NULL,
    permission TEXT NOT NULL,
    conversation_id INTEGER,
    subject_kind TEXT CHECK(subject_kind IS NULL OR subject_kind IN
        ('claim','conversation','projection','proposal','memory',
         'ladder','calibration','lesson')),
    subject_id INTEGER,
    parent_event_id INTEGER,
    outcome TEXT NOT NULL CHECK(outcome IN ('applied','rejected','noop')),
    payload_json TEXT,
    payload_salt TEXT,
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64),
    prev_sha256 TEXT NOT NULL CHECK(length(prev_sha256)=64),
    event_sha256 TEXT NOT NULL CHECK(length(event_sha256)=64),
    redacted_by_event_id INTEGER
)"""
_HEAD_TABLE_SQL = """CREATE TABLE memory_spine_head (
    id INTEGER PRIMARY KEY CHECK(id=1),
    last_event_id INTEGER NOT NULL,
    last_event_sha256 TEXT NOT NULL CHECK(length(last_event_sha256)=64),
    head_mac TEXT NOT NULL CHECK(length(head_mac)=64)
)"""
_NO_DELETE_TRIGGER_SQL = """CREATE TRIGGER memory_spine_events_no_delete
BEFORE DELETE ON memory_spine_events
BEGIN SELECT RAISE(ABORT, 'memory spine events are append-only'); END"""
_REDACTION_ONLY_TRIGGER_SQL = """CREATE TRIGGER memory_spine_events_redaction_only
BEFORE UPDATE ON memory_spine_events
WHEN NOT (
    NEW.id IS OLD.id AND NEW.created_at IS OLD.created_at AND NEW.kind IS OLD.kind
    AND NEW.actor IS OLD.actor AND NEW.source IS OLD.source AND NEW.scope IS OLD.scope
    AND NEW.permission IS OLD.permission
    AND NEW.conversation_id IS OLD.conversation_id
    AND NEW.subject_kind IS OLD.subject_kind AND NEW.subject_id IS OLD.subject_id
    AND NEW.parent_event_id IS OLD.parent_event_id
    AND NEW.payload_sha256 IS OLD.payload_sha256 AND NEW.prev_sha256 IS OLD.prev_sha256
    AND NEW.event_sha256 IS OLD.event_sha256 AND NEW.outcome IS OLD.outcome
    AND OLD.payload_json IS NOT NULL AND NEW.payload_json IS NULL
    AND OLD.payload_salt IS NOT NULL AND NEW.payload_salt IS NULL
    AND OLD.redacted_by_event_id IS NULL AND NEW.redacted_by_event_id IS NOT NULL
    AND NEW.redacted_by_event_id > OLD.id
    AND OLD.kind IN ('claim.imported','claim.created','claim.superseded',
                     'claim.reasserted','claim.disputed','claim.retracted',
                     'proposal.not_stored','proposal.confirmed')
    AND EXISTS (SELECT 1 FROM memory_spine_events AS t
                WHERE t.id = NEW.redacted_by_event_id AND t.kind = 'claim.tombstoned'
                  AND t.scope = OLD.scope
                  AND json_extract(t.payload_json,'$.claim_key')
                      = json_extract(OLD.payload_json,'$.claim_key'))
)
BEGIN SELECT RAISE(ABORT, 'memory spine events accept only one tombstone redaction'); END"""
_CLAIM_LINEAGE_TRIGGER_SQL = """CREATE TRIGGER memory_claims_require_spine_event
BEFORE INSERT ON memory_claims
WHEN NEW.spine_event_id IS NULL OR NOT EXISTS (
    SELECT 1 FROM memory_spine_events AS e
    WHERE e.id = NEW.spine_event_id
      AND e.kind IN ('claim.imported','claim.created')
      AND e.subject_kind = 'claim' AND e.subject_id = NEW.id)
BEGIN SELECT RAISE(ABORT, 'memory claim rows require a spine event'); END"""
# A memories row is produced either by a memory-creating event that names it
# or, for a claim's backing row, by the claim's own creating event (the claim
# row does not exist yet when its backing row is inserted, so verify
# cross-checks the two lineage columns afterwards).  NEW.id is -1 inside a
# BEFORE INSERT trigger when the writer omits the id, so implicit ids abort:
# every memories id comes from memory_id_sequence.
_MEMORY_LINEAGE_TRIGGER_SQL = """CREATE TRIGGER memories_require_spine_event
BEFORE INSERT ON memories
WHEN NEW.spine_event_id IS NULL OR NOT EXISTS (
    SELECT 1 FROM memory_spine_events AS e
    WHERE e.id = NEW.spine_event_id
      AND ((e.kind IN ('memory.imported','memory.created','lesson.created')
            AND e.subject_kind = 'memory' AND e.subject_id = NEW.id)
           OR (NEW.kind = 'claim'
               AND e.kind IN ('claim.imported','claim.created')
               AND e.subject_kind = 'claim')))
BEGIN SELECT RAISE(ABORT, 'memory rows require a spine event'); END"""
_TRIGGER_SQL: dict[str, str] = {
    "memory_spine_events_no_delete": _NO_DELETE_TRIGGER_SQL,
    "memory_spine_events_redaction_only": _REDACTION_ONLY_TRIGGER_SQL,
    "memory_claims_require_spine_event": _CLAIM_LINEAGE_TRIGGER_SQL,
    "memories_require_spine_event": _MEMORY_LINEAGE_TRIGGER_SQL,
}
# Migration 46 runs before memories.spine_event_id exists; the memories
# trigger references that column, so 46 creates only the slice 1 triggers.
_V46_TRIGGERS: tuple[str, ...] = (
    "memory_spine_events_no_delete",
    "memory_spine_events_redaction_only",
    "memory_claims_require_spine_event",
)
# Dependents of a claim row, in foreign-key order (the erase order); tables a
# store does not have are skipped.
_CLAIM_DEPENDENT_TABLES: tuple[str, ...] = (
    # The graph edge's primary key is the claim id, so the generic delete
    # below reaches it; edges go first because their entities are foreign-key
    # targets and the orphan sweep runs afterwards (memory_graph.delete_edges).
    "memory_graph_edges",
    "memory_claim_clock_statistics",
    "memory_claim_observations",
    "memory_claim_evidence",
    "memory_claim_events",
)
_MEMORY_DEPENDENT_TABLES: tuple[str, ...] = (
    "memory_retrievals",
    "memory_statistics",
    "memory_embeddings",
    "memory_embedding_leases",
    "ordinary_memory_provenance",
    "lesson_provenance",
    "ordinary_memory_quality_assessments",
)
# Dry-run divergence kinds that mean the spine, not the projection, is wrong.
_APPLY_REFUSAL_KINDS: frozenset[str] = frozenset({"payload", "order", "redaction"})


class SpineError(RuntimeError):
    """A spine write or verification could not proceed safely.

    ``code`` is a fixed machine-readable reason (never operator-derived
    text) for callers that report refusals; ``None`` for slice 1 sites.
    """

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


# --- canonical form and digests -------------------------------------------

def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def payload_digest(salt: str, payload: Mapping[str, Any]) -> str:
    return sha256_hex(f"{salt}\n{canonical(dict(payload))}")


def event_digest(key: bytes, fields: Mapping[str, Any]) -> str:
    """Keyed MAC over the canonical JSON of every immutable column."""
    immutable = {
        name: fields.get(name)
        for name in (
            "id", "created_at", "kind", "actor", "source", "scope", "permission",
            "conversation_id", "subject_kind", "subject_id", "parent_event_id",
            "outcome", "payload_sha256", "prev_sha256",
        )
    }
    return hmac.new(key, canonical(immutable).encode("utf-8"), hashlib.sha256).hexdigest()


def key_fingerprint(key: bytes) -> str:
    """An unkeyed digest of the key, recorded in the genesis payload so a
    verification failure can say 'wrong key' instead of 'tampered'."""
    return hashlib.sha256(b"jarvis-memory-spine-key\0" + key).hexdigest()


def head_mac(key: bytes, last_event_id: int, last_event_sha256: str) -> str:
    """Keyed MAC over the newest event, so removing the tail is detectable."""
    payload = {"last_event_id": int(last_event_id), "last_event_sha256": str(last_event_sha256)}
    return hmac.new(key, canonical(payload).encode("utf-8"), hashlib.sha256).hexdigest()


def content_digest(key: bytes, content: str) -> str:
    """Keyed digest of a memories row's content: HMAC-SHA256 under the spine
    key, domain-tagged so it never shares a preimage with an event or head
    MAC.  A digest-only payload therefore cannot be brute-forced by anyone
    holding the database without the sidecar, even for low-entropy content
    such as a port number."""
    return hmac.new(
        key, _CONTENT_DIGEST_TAG + str(content).encode("utf-8"), hashlib.sha256
    ).hexdigest()


def lesson_applications_digest(
    key: bytes, rows: Iterable[tuple[int, int, int]]
) -> str:
    """Keyed digest binding the **identity** of a prediction's lesson applications.

    Each row is ``(id, prediction_id, memory_id)`` -- the three columns that are
    immutable once the row exists.  Sorted by id, canonical JSON, HMAC-SHA256
    under the spine key with its own domain tag, so it shares no preimage with
    an event, a head, or a content digest, and a holder of the database file
    without the key sidecar can neither forge nor brute-force it.

    **The verdict is deliberately not digested.**
    ``record_lesson_applications`` runs when a lesson is *matched*, which is
    before the turn resolves: at that instant ``resolved_at`` and ``successful``
    are both NULL, and ``resolve_prediction`` stamps them later.  A digest over
    ``successful`` would therefore freeze ``null`` and disagree with every
    re-check from the first resolve onward -- turning R-6's fix into R-1's
    failure mode.  The digest binds *which rows are the evidence*; whether they
    count is re-derived live by the proof's own clauses every time.
    """
    material = sorted(
        (int(application_id), int(prediction_id), int(memory_id))
        for application_id, prediction_id, memory_id in rows
    )
    return hmac.new(
        key,
        _APPLICATIONS_DIGEST_TAG + canonical(material).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _field(row: Mapping[str, Any], name: str, default: Any = None) -> Any:
    return row[name] if name in row.keys() else default


def _is_hex_digest(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        ch in "0123456789abcdef" for ch in value
    )


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def memory_event_payload(
    key: bytes,
    row: Mapping[str, Any],
    *,
    origin: str | None,
    eligible: bool | None,
    provenance_sha256: str | None = None,
    at: str | None = None,
) -> dict[str, Any]:
    """Digest-only after-image of a memories row for ``memory.imported``,
    ``memory.created``, ``memory.updated``, and ``lesson.created``.

    ``row`` carries the memories columns (``kind``, ``content``, ``source``,
    ``family``, ``outcome_status``, ``reflection_id``); ``origin`` and
    ``eligible`` are the ordinary provenance (``None`` for lessons and for
    rows without a provenance row); ``provenance_sha256`` is a lesson's
    ``lesson_provenance`` digest.  The content never leaves the row: only its
    keyed digest and its length in characters do.
    """
    content = str(row["content"])
    reflection_id = _field(row, "reflection_id")
    payload: dict[str, Any] = {
        "kind": str(row["kind"]),
        "content_digest": content_digest(key, content),
        "content_length": len(content),
        "source": None if _field(row, "source") is None else str(row["source"]),
        "family": None if _field(row, "family") is None else str(row["family"]),
        "outcome_status": (
            None if _field(row, "outcome_status") is None else str(row["outcome_status"])
        ),
        "reflection_id": None if reflection_id is None else int(reflection_id),
        "origin": None if origin is None else str(origin),
        "eligible": None if eligible is None else bool(eligible),
    }
    if provenance_sha256 is not None:
        payload["provenance_sha256"] = str(provenance_sha256)
    if at is not None:
        payload["at"] = str(at)
    return payload


def memory_deleted_payload(
    key: bytes,
    rows: Iterable[Any],
    *,
    reason: str,
    at: str | None = None,
) -> dict[str, Any]:
    """Payload for one ``memory.deleted`` event naming every removed row by id
    and keyed content digest (``rows``: ``(id, content)`` pairs or mappings
    with ``id`` and ``content``)."""
    ids: list[int] = []
    digests: list[str] = []
    for item in rows:
        if isinstance(item, Mapping):
            memory_id, content = item["id"], item["content"]
        else:
            memory_id, content = item
        ids.append(int(memory_id))
        digests.append(content_digest(key, str(content)))
    payload: dict[str, Any] = {
        "ids": ids, "content_digests": digests, "reason": str(reason)[:200],
    }
    if at is not None:
        payload["at"] = str(at)
    return payload


def load_spine_key(db_path: str | Path | None, *, create: bool = True) -> bytes:
    """Load (or create) the 32-byte spine key beside the database.

    An in-memory or unnamed store gets an ephemeral key: its spine cannot
    outlive the process anyway.  A malformed or unreadable sidecar raises
    ``SpineError``; nothing ever silently replaces a real key.
    """
    if db_path is None or str(db_path) in {"", ":memory:"}:
        return secrets.token_bytes(32)
    sidecar = Path(str(db_path) + KEY_SIDECAR_SUFFIX)
    try:
        if sidecar.exists():
            raw = sidecar.read_bytes().strip()
            if len(raw) != 64:
                raise SpineError("memory spine key sidecar is malformed")
            try:
                return bytes.fromhex(raw.decode("ascii"))
            except (UnicodeDecodeError, ValueError):
                raise SpineError("memory spine key sidecar is malformed") from None
        if not create:
            raise SpineError("memory spine key sidecar is missing")
        key = secrets.token_bytes(32)
        sidecar.write_bytes(key.hex().encode("ascii"))
        try:
            sidecar.chmod(0o600)
        except OSError:
            pass
        return key
    except OSError as exc:
        raise SpineError(f"memory spine key sidecar unavailable: {exc}") from exc


# --- schema ---------------------------------------------------------------

def _table_columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in db.execute(f"PRAGMA table_info({table})").fetchall()}


def _table_exists(db: sqlite3.Connection, table: str) -> bool:
    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def migrate_memory_spine_v46(db: sqlite3.Connection, key: bytes, *, now: str) -> dict[str, int]:
    """Create the spine, the head record, the claim-id sequence, the lineage
    columns, and the triggers; backfill every existing claim as
    ``claim.imported``.

    ``Memory`` calls this inside its migration transaction when
    ``user_version < 46``; partial spine objects from an interrupted run are
    dropped first because no spine row is authoritative below 46.
    """
    if _table_exists(db, "memory_spine_head") and _table_exists(db, "memory_spine_events"):
        head = db.execute(
            "SELECT last_event_id, last_event_sha256, head_mac FROM memory_spine_head WHERE id=1"
        ).fetchone()
        if head is not None and hmac.compare_digest(
            head_mac(key, int(head[0]), str(head[1])), str(head[2])
        ):
            # user_version below 46 with an authentic keyed head can only be
            # a manual downgrade: re-importing the projection would launder
            # tampered claims into a clean chain and drop every receipt.
            raise SpineError(
                "an authentic memory spine is present below schema 46; refusing to discard it "
                "(a real store below 46 has no spine; see docs/MEMORY_SPINE.md)"
            )
    for trigger in _TRIGGER_SQL:
        db.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    db.execute("DROP TABLE IF EXISTS memory_spine_events")
    db.execute("DROP TABLE IF EXISTS memory_spine_head")
    db.execute("DROP TABLE IF EXISTS memory_claim_sequence")
    # A re-migration from below 46 discards the whole spine, so lineage the
    # memories rows kept from a discarded spine (a stripped fixture) must
    # not survive it either; migration 47 backfills it afresh.
    db.execute("DROP TABLE IF EXISTS memory_id_sequence")
    if "spine_event_id" in _table_columns(db, "memories"):
        db.execute("UPDATE memories SET spine_event_id=NULL")
    db.execute(_EVENT_TABLE_SQL)
    db.execute(_HEAD_TABLE_SQL)
    _create_event_indexes(db)
    db.execute(
        """CREATE TABLE memory_claim_sequence (
            id INTEGER PRIMARY KEY CHECK(id=1),
            next_id INTEGER NOT NULL CHECK(next_id > 0)
        )"""
    )
    if "spine_event_id" not in _table_columns(db, "memory_claims"):
        db.execute("ALTER TABLE memory_claims ADD COLUMN spine_event_id INTEGER")
    if "spine_event_id" not in _table_columns(db, "memory_claim_events"):
        db.execute("ALTER TABLE memory_claim_events ADD COLUMN spine_event_id INTEGER")
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_claims_spine_event "
        "ON memory_claims(spine_event_id)"
    )
    if _table_exists(db, "memory_fact_proposals"):
        proposal_columns = _table_columns(db, "memory_fact_proposals")
        if "command_salt" not in proposal_columns:
            db.execute("ALTER TABLE memory_fact_proposals ADD COLUMN command_salt TEXT")
        if "spine_event_id" not in proposal_columns:
            db.execute("ALTER TABLE memory_fact_proposals ADD COLUMN spine_event_id INTEGER")
    highest = db.execute("SELECT COALESCE(MAX(id), 0) FROM memory_claims").fetchone()[0]
    db.execute(
        "INSERT INTO memory_claim_sequence(id, next_id) VALUES (1, ?)",
        (int(highest) + 1,),
    )
    claims = db.execute(
        """SELECT id, scope, created_at, claim_key, subject, predicate, value,
                  value_sha256, source, authority, confidence, status,
                  valid_from, valid_until, supersedes_id
           FROM memory_claims ORDER BY id"""
    ).fetchall()
    append_event(
        db, key,
        kind="spine.genesis", actor="system", source="memory spine migration",
        scope="global", permission="migration", outcome="applied",
        payload={
            "schema_version": SPINE_SCHEMA_VERSION,
            "claims_backfilled": len(claims),
            "key_fingerprint": key_fingerprint(key),
        },
        now=now,
    )
    for row in claims:
        payload = {
            "at": str(row["created_at"]),
            "claim_key": str(row["claim_key"]),
            "subject": str(row["subject"]),
            "predicate": str(row["predicate"]),
            "value": str(row["value"]),
            "value_sha256": str(row["value_sha256"]),
            "source": str(row["source"]),
            "authority": str(row["authority"]),
            "confidence": float(row["confidence"]),
            "status": str(row["status"]),
            "valid_from": str(row["valid_from"]),
            "valid_until": row["valid_until"],
            "supersedes_id": row["supersedes_id"],
            "original_created_at": str(row["created_at"]),
        }
        event_id = append_event(
            db, key,
            kind="claim.imported", actor="system", source="memory spine migration",
            scope=str(row["scope"]), permission="migration", outcome="applied",
            subject_kind="claim", subject_id=int(row["id"]), payload=payload, now=now,
        )
        db.execute(
            "UPDATE memory_claims SET spine_event_id=? WHERE id=?",
            (event_id, int(row["id"])),
        )
    for name in _V46_TRIGGERS:
        db.execute(_TRIGGER_SQL[name])
    return {"claims_backfilled": len(claims)}


def _create_event_indexes(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_spine_events_subject "
        "ON memory_spine_events(subject_kind, subject_id, id)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_spine_events_kind "
        "ON memory_spine_events(kind, id)"
    )


def _normalized_table_sql(text: str) -> str:
    # ``ALTER TABLE ... RENAME`` stores the name double-quoted.
    return " ".join(str(text or "").replace('"', "").split())


def _rebuild_events_table(db: sqlite3.Connection) -> int:
    """Widen the closed CHECK lists of the events table by copying it (SQLite
    cannot alter a CHECK).  Rows are copied column for column, so every keyed
    digest still verifies.  Returns 1 when a copy was needed.

    **The rename is done with re-parsing switched off, which is the whole
    hazard rather than one kind of it.**  ``ALTER TABLE ... RENAME`` normally
    re-parses every object in the schema and rewrites references to the table
    being renamed.  That is what broke this twice:

    * first on TRIGGERS -- M4 added two on other tables referencing
      ``memory_spine_events``, which no list here knew about, so the rename
      died with ``no such table`` **after** the old table was dropped;
    * then on VIEWS, found by red team, with the same symptom and no data loss
      but a store that cannot be opened at all.

    The first fix discovered triggers from ``sqlite_master``.  That was the
    class of the *object that broke us*, not the class of the *hazard*, so it
    left views out and would have left the next kind out too.
    ``PRAGMA legacy_alter_table`` makes the rename a pure rename that touches
    no other object, so triggers, views, foreign keys and any kind nobody has
    thought of are all covered without being enumerated -- which is the only
    version of this fix that stops the pattern instead of adding to it.

    Measured against a real pre-M5 store: with a view present the enumerated
    fix fails and this one succeeds; with a foreign key from another table
    both succeed; with both present only this one succeeds.
    """
    row = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='memory_spine_events'"
    ).fetchone()
    if row is not None and _normalized_table_sql(row[0]) == _normalized_table_sql(_EVENT_TABLE_SQL):
        return 0

    # An events table carrying a column this build does not know about must
    # not be silently narrowed: the copy below names columns, and a column
    # missing from that list would be dropped along with its data and no
    # message.  Preserving it is impossible -- the new table has nowhere to
    # put it -- so the honest move is to refuse and say which column.
    live_columns = [str(name) for name in _table_columns(db, "memory_spine_events")]
    unknown = [name for name in live_columns if name not in _EVENT_COLUMNS]
    if unknown:
        raise SpineError(
            "memory_spine_events carries column(s) this build does not know: "
            + ", ".join(sorted(unknown))
            + "; refusing to rebuild the table rather than drop them "
              "(see docs/MEMORY_SPINE.md)",
            code="events_table_unknown_columns",
        )
    # Copy only what the live table actually has, so a build that is AHEAD of
    # the store (a column added in this release) still migrates instead of
    # failing on a SELECT for a column that does not exist yet.
    copied = [name for name in _EVENT_COLUMNS if name in live_columns]
    columns = ", ".join(copied)

    previous_legacy = int(db.execute("PRAGMA legacy_alter_table").fetchone()[0])
    db.execute("PRAGMA legacy_alter_table=ON")
    if int(db.execute("PRAGMA legacy_alter_table").fetchone()[0]) != 1:
        # Fail loudly rather than proceed into the re-parse that bricks a
        # store: a rename we cannot make safe is one we do not attempt.
        raise SpineError(
            "PRAGMA legacy_alter_table could not be enabled; refusing to "
            "rebuild the events table, because the rename would re-parse "
            "every trigger and view in the schema",
            code="legacy_alter_table_unavailable",
        )
    try:
        db.execute("DROP TABLE IF EXISTS memory_spine_events__v47")
        db.execute(
            _EVENT_TABLE_SQL.replace(
                "CREATE TABLE memory_spine_events (",
                "CREATE TABLE memory_spine_events__v47 (", 1
            )
        )
        db.execute(
            f"INSERT INTO memory_spine_events__v47({columns}) "
            f"SELECT {columns} FROM memory_spine_events ORDER BY id"
        )
        before = int(db.execute("SELECT COUNT(*) FROM memory_spine_events").fetchone()[0])
        after = int(db.execute("SELECT COUNT(*) FROM memory_spine_events__v47").fetchone()[0])
        if before != after:
            raise SpineError("memory spine events could not be copied for schema 47")
        db.execute("DROP TABLE memory_spine_events")
        db.execute("ALTER TABLE memory_spine_events__v47 RENAME TO memory_spine_events")
        _create_event_indexes(db)
    finally:
        # Connection-scoped, so it must not leak into whatever the caller does
        # next in the same transaction.
        db.execute(f"PRAGMA legacy_alter_table={'ON' if previous_legacy else 'OFF'}")
    return 1


def migrate_memory_spine_v47(db: sqlite3.Connection, key: bytes, *, now: str) -> dict[str, int]:
    """Put ordinary memories and lessons on the spine (schema 47).

    Adds ``memories.spine_event_id`` (unique) and ``memory_id_sequence``,
    links every claim backing row to its claim's event, imports every other
    row as ``memory.imported`` (actor ``system``, permission ``migration``),
    widens the events table's closed lists, and recreates every trigger
    (``Memory._migrate`` drops them all below 47).

    Idempotent and laundering-proof: a lineage-less row whose id already has
    exactly one creating event is re-linked to it only when the row's keyed
    content digest equals the latest digest the spine knows for that id; a
    differing digest, more than one creating event, or an id named by a
    deletion receipt raises ``SpineError`` (that shape is a ``user_version``
    downgrade over edited rows).  An authentic head at 46 is the normal
    input; a head that does not verify under the key is refused, never
    discarded (the below-46 refusal in migration 46 is unchanged).
    """
    if not (
        _table_exists(db, "memory_spine_events")
        and _table_exists(db, "memory_spine_head")
        and _table_exists(db, "memory_claim_sequence")
    ):
        raise SpineError(
            "memory spine is missing; migration 47 requires the schema 46 spine",
            code="spine_missing",
        )
    head = db.execute(
        "SELECT last_event_id, last_event_sha256, head_mac FROM memory_spine_head WHERE id=1"
    ).fetchone()
    if head is None or not hmac.compare_digest(
        head_mac(key, int(head[0]), str(head[1])), str(head[2])
    ):
        raise SpineError(
            "memory spine head does not verify at schema 46; refusing migration 47 "
            "(wrong key sidecar or tampered spine; see docs/MEMORY_SPINE.md)",
            code="head_unverified",
        )
    counts = {
        "memories_imported": 0,
        "orphan_claim_rows": 0,
        "claim_rows_linked": 0,
        "memories_relinked": 0,
        "events_table_rebuilt": 0,
    }
    for trigger in _TRIGGER_SQL:
        db.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    counts["events_table_rebuilt"] = _rebuild_events_table(db)
    if "spine_event_id" not in _table_columns(db, "memories"):
        db.execute("ALTER TABLE memories ADD COLUMN spine_event_id INTEGER")
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_spine_event "
        "ON memories(spine_event_id)"
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS memory_id_sequence (
            id INTEGER PRIMARY KEY CHECK(id=1),
            next_id INTEGER NOT NULL CHECK(next_id > 0)
        )"""
    )
    # Claim backing rows: the claim's own creating event is their lineage.
    backing_rows = db.execute(
        """SELECT m.id, m.spine_event_id, c.id AS claim_id, c.spine_event_id AS claim_event
           FROM memories AS m JOIN memory_claims AS c ON c.memory_id = m.id
           ORDER BY m.id"""
    ).fetchall()
    for row in backing_rows:
        if row["claim_event"] is None:
            raise SpineError(
                f"claim {int(row['claim_id'])} has no spine event; the schema 46 backfill is incomplete",
                code="backfill_incomplete",
            )
        if row["spine_event_id"] is None or int(row["spine_event_id"]) != int(row["claim_event"]):
            db.execute(
                "UPDATE memories SET spine_event_id=? WHERE id=?",
                (int(row["claim_event"]), int(row["id"])),
            )
            counts["claim_rows_linked"] += 1
    # What the spine already knows about each memory id (a re-run after a
    # downgrade): creating events, the latest content digest, deletions.
    history: dict[int, dict[str, Any]] = {}

    def entry(memory_id: int) -> dict[str, Any]:
        return history.setdefault(memory_id, {"creating": [], "digest": None, "deleted": False})

    for row in db.execute(
        """SELECT id, kind, subject_id, payload_json FROM memory_spine_events
           WHERE kind IN ('memory.imported','memory.created','lesson.created',
                          'memory.updated','memory.deleted','claim.tombstoned')
           ORDER BY id"""
    ).fetchall():
        kind = str(row["kind"])
        payload = _payload_of(row)
        subject_id = int(row["subject_id"]) if row["subject_id"] is not None else None
        if kind in MEMORY_CREATING_KINDS and subject_id is not None:
            known = entry(subject_id)
            known["creating"].append(int(row["id"]))
            known["digest"] = payload.get("content_digest") if payload else None
        elif kind == "memory.updated" and subject_id is not None and payload:
            entry(subject_id)["digest"] = payload.get("content_digest")
        elif kind == "memory.deleted" and payload:
            for removed in payload.get("ids") or []:
                if _is_int(removed):
                    entry(removed)["deleted"] = True
        elif kind == "claim.tombstoned" and payload:
            for removed in payload.get("removed_memory_ids") or []:
                if _is_int(removed):
                    entry(removed)["deleted"] = True
    has_provenance = _table_exists(db, "ordinary_memory_provenance")
    has_lessons = _table_exists(db, "lesson_provenance")
    # A spine that already records memories proves the store was at 47: a
    # lineage-less row with no creating event is then an out-of-band insert
    # brought here by a user_version downgrade, not a legacy row to import.
    spine_records_memories = db.execute(
        """SELECT 1 FROM memory_spine_events
           WHERE kind IN ('memory.imported','memory.created','lesson.created',
                          'memory.reasserted','memory.updated','memory.deleted')
           LIMIT 1"""
    ).fetchone() is not None
    rows = db.execute(
        """SELECT id, created_at, kind, content, source, family, outcome_status,
                  reflection_id, spine_event_id
           FROM memories AS m
           WHERE NOT EXISTS (SELECT 1 FROM memory_claims AS c WHERE c.memory_id = m.id)
           ORDER BY id"""
    ).fetchall()
    for row in rows:
        memory_id = int(row["id"])
        known = history.get(memory_id)
        creating = list(known["creating"]) if known else []
        if row["spine_event_id"] is not None and int(row["spine_event_id"]) in creating:
            continue
        if known and known["deleted"]:
            raise SpineError(
                f"memory {memory_id}: a deleted id is live again; refusing migration 47",
                code="deleted_id_live",
            )
        if len(creating) > 1:
            raise SpineError(
                f"memory {memory_id}: more than one creating spine event; refusing migration 47",
                code="duplicate_creating_event",
            )
        if len(creating) == 1:
            if known["digest"] != content_digest(key, str(row["content"])):
                raise SpineError(
                    f"memory {memory_id}: content differs from its spine history; refusing "
                    "migration 47 (a schema downgrade over an edited row)",
                    code="digest_mismatch",
                )
            db.execute(
                "UPDATE memories SET spine_event_id=? WHERE id=?", (creating[0], memory_id)
            )
            counts["memories_relinked"] += 1
            continue
        if spine_records_memories:
            raise SpineError(
                f"memory {memory_id}: no lineage and no creating event on a spine that already "
                "records memories; refusing migration 47 (a schema downgrade over an "
                "out-of-band row)",
                code="lineage_missing",
            )
        origin: str | None = None
        eligible: bool | None = None
        if has_provenance:
            provenance = db.execute(
                "SELECT origin, eligible FROM ordinary_memory_provenance WHERE memory_id=?",
                (memory_id,),
            ).fetchone()
            if provenance is not None:
                origin = str(provenance["origin"])
                eligible = bool(int(provenance["eligible"]))
        lesson_digest: str | None = None
        if has_lessons and str(row["kind"]) == "lesson":
            lesson = db.execute(
                """SELECT provenance_sha256 FROM lesson_provenance
                   WHERE memory_id=? AND provenance_sha256 IS NOT NULL
                   ORDER BY prediction_id LIMIT 1""",
                (memory_id,),
            ).fetchone()
            if lesson is not None:
                lesson_digest = str(lesson["provenance_sha256"])
        payload = memory_event_payload(
            key, row, origin=origin, eligible=eligible,
            provenance_sha256=lesson_digest, at=str(row["created_at"]),
        )
        event_id = append_event(
            db, key,
            kind="memory.imported", actor="system", source="memory spine migration",
            scope="global", permission="migration", outcome="applied",
            subject_kind="memory", subject_id=memory_id, payload=payload, now=now,
        )
        db.execute("UPDATE memories SET spine_event_id=? WHERE id=?", (event_id, memory_id))
        counts["memories_imported"] += 1
        if str(row["kind"]) == "claim":
            counts["orphan_claim_rows"] += 1
    _advance_sequence(db, "memory_id_sequence", memory_sequence_floor(db))
    create_spine_triggers(db)
    return counts


def migrate_memory_spine_v49(
    db: sqlite3.Connection, key: bytes, *, now: str
) -> dict[str, int]:
    """Widen the events table for ``transcript.compacted`` (spine schema 49).

    One thing only, and deliberately less than migration 47 does.  SQLite
    cannot alter a CHECK, so the table is copied column for column and every
    keyed digest still verifies; the copy compares the stored SQL against
    :data:`_EVENT_TABLE_SQL` and does nothing when they already match, so a
    re-migration over a widened store touches no row.

    **The caller drops and recreates the spine triggers around this call**, as
    ``Memory._migrate_v50`` does.  The rebuild ends in ``ALTER TABLE ...
    RENAME`` and SQLite re-parses every trigger in the schema on a rename; two
    of them sit on OTHER tables and reference ``memory_spine_events``, which
    does not exist between the drop and the rename.  That bracketing cannot
    live in here because it must span whatever else the caller does in the
    same transaction.

    **No head verification, unlike migration 47, and that is the point.**  47
    refuses ``head_unverified`` because it *imports rows onto the spine* and
    must not do so under a key that cannot vouch for the chain.  This
    migration writes no event and changes no row's content, so refusing here
    would newly prevent an operator with a swapped sidecar from opening a
    store they can open today -- and their honest signal is ``verify_spine``
    reporting the mismatch, not a store that will not start.  ``key`` is
    accepted for call-site symmetry with 46 and 47 and to report which key the
    store was on; it signs nothing here.

    Idempotent.  Returns ``{"events_table_rebuilt": 0 or 1}``.
    """
    if not _table_exists(db, "memory_spine_events"):
        raise SpineError(
            "memory spine is missing; migration 49 requires the schema 46 spine",
            code="spine_missing",
        )
    if not isinstance(key, (bytes, bytearray)):
        raise SpineError("migration 49 needs the spine key")
    return {
        "events_table_rebuilt": _rebuild_events_table(db),
        "at": str(now),
    }


def drop_spine_triggers(db: sqlite3.Connection) -> None:
    """Remove the spine triggers before legacy migrations re-run.

    ``user_version < 46`` proves no spine row is authoritative, but a store
    that was once at 46 and is being re-migrated (a downgraded test copy)
    still carries the lineage trigger, which would abort the legacy claim
    backfills that run before the spine exists.  ``migrate_memory_spine_v46``
    recreates everything afterwards.
    """
    for trigger in _TRIGGER_SQL:
        db.execute(f"DROP TRIGGER IF EXISTS {trigger}")


def spine_ready(db: sqlite3.Connection) -> bool:
    """True once every spine object of the current schema exists (47: the
    memory id sequence and the memories lineage column included)."""
    return (
        _table_exists(db, "memory_spine_events")
        and _table_exists(db, "memory_spine_head")
        and _table_exists(db, "memory_claim_sequence")
        and _table_exists(db, "memory_id_sequence")
        and "spine_event_id" in _table_columns(db, "memories")
    )


def create_spine_triggers(db: sqlite3.Connection) -> None:
    """(Re)create every spine trigger; each is dropped first so a same-name
    trigger with another body cannot survive."""
    for name, sql in _TRIGGER_SQL.items():
        db.execute(f"DROP TRIGGER IF EXISTS {name}")
        db.execute(sql)


def allocate_claim_id(db: sqlite3.Connection) -> int:
    """Explicit, never-reused claim ids (an erased id must not come back)."""
    row = db.execute("SELECT next_id FROM memory_claim_sequence WHERE id=1").fetchone()
    if row is None:
        raise SpineError("memory claim sequence is missing")
    claim_id = int(row[0])
    if claim_id <= sequence_floor(db):
        raise SpineError("memory claim sequence is behind the store; run spine verify")
    db.execute("UPDATE memory_claim_sequence SET next_id=? WHERE id=1", (claim_id + 1,))
    return claim_id


def sequence_floor(db: sqlite3.Connection) -> int:
    """The highest claim id the store has ever used: live rows and every
    claim-creating event (erased ids included)."""
    live = db.execute("SELECT COALESCE(MAX(id), 0) FROM memory_claims").fetchone()[0]
    seen = db.execute(
        """SELECT COALESCE(MAX(subject_id), 0) FROM memory_spine_events
           WHERE kind IN ('claim.imported','claim.created') AND subject_kind='claim'"""
    ).fetchone()[0]
    return max(int(live or 0), int(seen or 0))


def _as_int(value: Any) -> int:
    return int(value) if _is_int(value) else 0


def memory_sequence_floor(db: sqlite3.Connection) -> int:
    """The highest memories id the store has ever used: live rows, every
    memory-creating event, and every id a deletion receipt names (a vault
    re-index ``memory.deleted`` or the ``removed_memory_ids`` of a claim
    tombstone), so a deleted id never comes back."""
    live = db.execute("SELECT COALESCE(MAX(id), 0) FROM memories").fetchone()[0]
    seen = db.execute(
        """SELECT COALESCE(MAX(subject_id), 0) FROM memory_spine_events
           WHERE kind IN ('memory.imported','memory.created','lesson.created')
             AND subject_kind='memory'"""
    ).fetchone()[0]
    deleted = db.execute(
        """SELECT MAX(j.value) FROM memory_spine_events AS e,
               json_each(e.payload_json, '$.ids') AS j
           WHERE e.kind='memory.deleted' AND e.payload_json IS NOT NULL"""
    ).fetchone()[0]
    erased = db.execute(
        """SELECT MAX(j.value) FROM memory_spine_events AS e,
               json_each(e.payload_json, '$.removed_memory_ids') AS j
           WHERE e.kind='claim.tombstoned' AND e.payload_json IS NOT NULL"""
    ).fetchone()[0]
    return max(_as_int(live), _as_int(seen), _as_int(deleted), _as_int(erased))


def allocate_memory_id(db: sqlite3.Connection) -> int:
    """Explicit, never-reused memories ids (a deleted or re-indexed id must
    not come back).  Allocate only after the existence check: the lineage
    trigger aborts implicit ids, and ``INSERT OR IGNORE`` cannot skip it."""
    row = db.execute("SELECT next_id FROM memory_id_sequence WHERE id=1").fetchone()
    if row is None:
        raise SpineError("memory id sequence is missing")
    memory_id = int(row[0])
    if memory_id <= memory_sequence_floor(db):
        raise SpineError("memory id sequence is behind the store; run spine verify")
    db.execute("UPDATE memory_id_sequence SET next_id=? WHERE id=1", (memory_id + 1,))
    return memory_id


def _advance_sequence(db: sqlite3.Connection, table: str, floor: int) -> None:
    """Move a sequence past ``floor``; never backwards."""
    row = db.execute(f"SELECT next_id FROM {table} WHERE id=1").fetchone()
    current = int(row[0]) if row is not None else 0
    wanted = max(current, int(floor) + 1)
    if row is None:
        db.execute(f"INSERT INTO {table}(id, next_id) VALUES (1, ?)", (wanted,))
    elif wanted != current:
        db.execute(f"UPDATE {table} SET next_id=? WHERE id=1", (wanted,))


# --- writing ---------------------------------------------------------------

def _parse_stamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _monotonic_stamp(db: sqlite3.Connection, now: str) -> str:
    """The spine clock never goes backwards: a new event is stamped at least
    one microsecond after the previous event."""
    requested = _parse_stamp(now)
    row = db.execute(
        "SELECT created_at FROM memory_spine_events ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return requested.isoformat()
    latest = _parse_stamp(str(row[0]))
    if requested <= latest:
        requested = latest + timedelta(microseconds=1)
    return requested.isoformat()


def _check_payload_types(kind: str, clean: Mapping[str, Any]) -> None:
    """Type bounds for the digest-only memory payloads, the tombstone's memory
    ids, and the rebuild receipt, so a malformed event never reaches the
    chain and a payload can never smuggle content under a digest key."""

    def fail(name: str) -> None:
        raise SpineError(f"spine payload for {kind} is malformed: {name}")

    def optional_str(name: str) -> None:
        if clean[name] is not None and not isinstance(clean[name], str):
            fail(name)

    def int_list(name: str) -> None:
        value = clean[name]
        if not isinstance(value, list) or not all(_is_int(item) for item in value):
            fail(name)

    if kind in MEMORY_CREATING_KINDS or kind == "memory.updated":
        if not isinstance(clean["kind"], str) or not clean["kind"]:
            fail("kind")
        if not _is_hex_digest(clean["content_digest"]):
            fail("content_digest")
        if not _is_int(clean["content_length"]) or clean["content_length"] < 0:
            fail("content_length")
        for name in ("source", "family", "outcome_status", "origin"):
            optional_str(name)
        if clean["reflection_id"] is not None and not _is_int(clean["reflection_id"]):
            fail("reflection_id")
        if clean["eligible"] is not None and not isinstance(clean["eligible"], bool):
            fail("eligible")
        provenance = clean.get("provenance_sha256")
        if provenance is not None and not _is_hex_digest(provenance):
            fail("provenance_sha256")
        if kind == "lesson.created" and provenance is None:
            fail("provenance_sha256")
    elif kind == "memory.reasserted":
        if not _is_hex_digest(clean["content_digest"]):
            fail("content_digest")
        optional_str("origin")
        if clean["eligible"] is not None and not isinstance(clean["eligible"], bool):
            fail("eligible")
        provenance = clean.get("provenance_sha256")
        if provenance is not None and not _is_hex_digest(provenance):
            fail("provenance_sha256")
    elif kind == "memory.deleted":
        ids, digests = clean["ids"], clean["content_digests"]
        if (
            not isinstance(ids, list)
            or not ids
            or len(ids) > MEMORY_DELETED_MAX_IDS
            or not all(_is_int(item) and item > 0 for item in ids)
        ):
            fail("ids")
        if not isinstance(digests, list) or len(digests) != len(ids) or not all(
            _is_hex_digest(item) for item in digests
        ):
            fail("content_digests")
        if not isinstance(clean["reason"], str):
            fail("reason")
        if "transcript_copies" in clean and (
            not _is_int(clean["transcript_copies"]) or clean["transcript_copies"] < 0
        ):
            fail("transcript_copies")
    elif kind == "projection.rebuilt":
        for name in ("rows_before", "rows_after", "divergences_fixed"):
            if not _is_int(clean[name]) or clean[name] < 0:
                fail(name)
        for name in ("removed_ids", "removed_memory_ids", "recreated_ids", "updated_ids",
                     "lost_evidence_claim_ids", "removed_entity_ids"):
            if name in clean:
                int_list(name)
        # Schema 48 graph keys: typed, not merely accepted, so a receipt can
        # never carry an unvalidated shape into the chain.
        if "projection" in clean and clean["projection"] not in _REBUILT_PROJECTIONS:
            fail("projection")
        if "entities" in clean and (
            not _is_int(clean["entities"]) or clean["entities"] < 0
        ):
            fail("entities")
        if "graph_reprojected" in clean and not isinstance(
            clean["graph_reprojected"], bool
        ):
            fail("graph_reprojected")
        if "excluded" in clean:
            excluded = clean["excluded"]
            if _is_int(excluded):
                if excluded < 0:
                    fail("excluded")
            elif isinstance(excluded, Mapping):
                if set(excluded) != _EXCLUDED_CATEGORY_KEYS or not all(
                    _is_int(value) and value >= 0 for value in excluded.values()
                ):
                    fail("excluded")
            else:
                fail("excluded")
    elif kind == "claim.tombstoned":
        for name in ("removed_memory_ids", "removed_entity_ids"):
            if name in clean:
                int_list(name)
        # Schema 49.  These were added to the key set and to payload_keys but
        # never type-checked, so `removed_milestone_ids=["x"]` was accepted
        # while its neighbour `removed_memory_ids=["x"]` was rejected -- the
        # gap an independent review found with exactly that control.
        #
        # The cap is enforced here, not merely documented, which is what makes
        # the writer's chunking mandatory: an over-long list now raises rather
        # than being silently truncated into an incomplete audit trail.
        if "removed_milestone_ids" in clean:
            int_list("removed_milestone_ids")
            if len(clean["removed_milestone_ids"]) > MILESTONE_TOMBSTONE_MAX_IDS:
                fail("removed_milestone_ids")
        if "removed_span_handles" in clean:
            handles = clean["removed_span_handles"]
            if not isinstance(handles, list) or not all(
                isinstance(item, str) and _SPAN_HANDLE_SHAPE.match(item)
                for item in handles
            ):
                fail("removed_span_handles")
            if len(handles) > MILESTONE_TOMBSTONE_MAX_IDS:
                fail("removed_span_handles")
    elif kind in _M4_STRUCTURED_KINDS:
        _check_ladder_payload_types(kind, clean, fail)
    elif kind == "transcript.compacted":
        for name in ("span_sha256", "span_unkeyed_sha256", "summary_sha256",
                     "invariants_sha256", "key_fingerprint"):
            if not _is_hex_digest(clean[name]):
                fail(name)
        if not isinstance(clean["handle"], str) or not clean["handle"]:
            fail("handle")
        for name in ("seq", "first_message_id", "last_message_id",
                     "message_count", "source_chars", "stored_bytes",
                     "summary_chars"):
            if not _is_int(clean[name]) or int(clean[name]) < 0:
                fail(name)
        if int(clean["message_count"]) < 1:
            fail("message_count")
        if int(clean["last_message_id"]) < int(clean["first_message_id"]):
            fail("last_message_id")
        # M5 ships no model-authored summary (ruling M-16); the column keeps
        # 'model' so a later phase needs no table rebuild, but no event may
        # claim it yet.
        if clean["author"] != "runtime":
            fail("author")
        if not isinstance(clean["screened"], bool):
            fail("screened")
        event_range = clean["event_range"]
        if (
            not isinstance(event_range, Mapping)
            or set(event_range) != {"after", "through"}
            or not all(_is_int(event_range[name]) for name in ("after", "through"))
            or int(event_range["through"]) < int(event_range["after"])
        ):
            fail("event_range")
        for name in ("excluded_by_screen", "span_has_proposal"):
            if name in clean and (not _is_int(clean[name]) or int(clean[name]) < 0):
                fail(name)
        if "model" in clean and clean["model"] is not None:
            fail("model")
    elif kind == "conversation.deleted":
        for name in ("messages_removed", "milestones_removed", "spans_removed"):
            if name in clean and (not _is_int(clean[name]) or int(clean[name]) < 0):
                fail(name)


_CLAIM_REQUIRED_KEYS: frozenset[str] = frozenset({
    "at", "claim_key", "subject", "predicate", "value", "value_sha256",
    "source", "authority", "confidence", "status", "valid_from",
})
_STATUS_REQUIRED_KEYS: frozenset[str] = frozenset({
    "at", "claim_key", "claim_id", "status",
})
#: Kinds whose payload is deliberately open: the genesis record and the three
#: receipts whose shape is owned by their writer rather than by this contract.
#: Listed rather than implied, so :func:`payload_keys` can tell "no contract"
#: apart from "a contract that forbids everything".
#: ``conversation.deleted`` LEFT this set at schema 49.  M5's M-9 was written
#: against an older ``validate_payload`` that ended ``else: extra, missing =
#: set(), set()``; the M4 rewrite replaced that with this set plus
#: :func:`payload_keys`, so the fix is a removal here rather than a new branch
#: on a fall-through that no longer exists.  Adding the branch without this
#: removal would have left the contract inert and E-6 passing on whatever
#: payload the test itself wrote.
UNCONSTRAINED_PAYLOAD_KINDS: frozenset[str] = frozenset({
    "spine.genesis", "proposal.not_stored", "proposal.confirmed",
})

#: ``transcript.compacted`` (schema 49).  Digest-only: no content, no summary
#: text, no claim value ever enters the payload.  ``summary_sha256`` and
#: ``invariants_sha256`` are keyed ``content_digest`` values over the exact
#: stored strings, so ``spine verify`` can prove a milestone row was not edited
#: out of band without the spine ever holding the text.
_COMPACTED_REQUIRED_KEYS: frozenset[str] = frozenset({
    "seq", "handle", "span_sha256", "span_unkeyed_sha256", "summary_sha256",
    "invariants_sha256", "key_fingerprint", "first_message_id",
    "last_message_id", "message_count", "source_chars", "stored_bytes",
    "summary_chars", "author", "screened", "event_range",
})
_COMPACTED_PAYLOAD_KEYS: frozenset[str] = _COMPACTED_REQUIRED_KEYS | frozenset({
    "at", "model", "reduction_ratio", "excluded_by_screen", "span_has_proposal",
})
#: ``conversation.deleted`` gained a contract at schema 49.  ``at`` stays
#: OPTIONAL: ``Memory.delete_conversation`` writes ``{"messages_removed": N}``
#: with no ``at`` today, and making it required would refuse the live writer.
_CONVERSATION_DELETED_REQUIRED_KEYS: frozenset[str] = frozenset({"messages_removed"})
_CONVERSATION_DELETED_PAYLOAD_KEYS: frozenset[str] = (
    _CONVERSATION_DELETED_REQUIRED_KEYS
    | frozenset({"at", "milestones_removed", "spans_removed"})
)


def _check_ladder_payload_types(
    kind: str, clean: Mapping[str, Any], fail: Any
) -> None:
    """Type every ladder payload key, so a receipt can never carry an
    unvalidated shape -- or a scrap of prose -- into the chain."""
    for name, value in clean.items():
        if _LADDER_FORBIDDEN_KEY.search(name) and name != "token_required":
            fail(name)
        if name in _LADDER_DIGEST_KEYS:
            if value is not None and not _is_hex_digest(value):
                fail(name)
        elif name in _LADDER_COUNT_KEYS:
            if value is not None and (not _is_int(value) or value < 0):
                fail(name)
        elif name in _LADDER_RATE_KEYS:
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not 0.0 <= float(value) <= 1.0
            ):
                fail(name)
        elif name in _LADDER_FLAG_KEYS:
            if not isinstance(value, bool):
                fail(name)
        elif name in _LADDER_CODE_KEYS:
            if value is not None and (
                not isinstance(value, str) or not _LADDER_CODE.fullmatch(value)
            ):
                fail(name)
        elif name == "family":
            if not isinstance(value, str) or not _LADDER_FAMILY.fullmatch(value):
                fail(name)
        elif name == "skill_name":
            if (
                not isinstance(value, str)
                or len(value) > 63
                or not _LADDER_SKILL_NAME.fullmatch(value)
            ):
                fail(name)
        elif name == "at":
            if not isinstance(value, str) or not value or len(value) > 40:
                fail(name)
        elif name == "lesson_ids":
            if (
                not isinstance(value, list)
                or len(value) > _LADDER_MAX_LESSON_IDS
                or not all(_is_int(item) and item > 0 for item in value)
            ):
                fail(name)
        else:  # pragma: no cover - unreachable while the key sets are closed
            fail(name)
    if kind == "ladder.calibration_sealed":
        if int(clean["successes"]) > int(clean["n"]) or int(clean["n"]) < 1:
            fail("successes")
        if int(clean["first_prediction_id"]) > int(clean["last_prediction_id"]):
            fail("first_prediction_id")
        if int(clean["epoch"]) < 1:
            fail("epoch")
    elif kind == "ladder.staged" and clean["token_required"] is not True:
        # A staged promotion always needs the operator's confirmation code.
        fail("token_required")
    elif kind == "ladder.rolled_back" and (
        clean["restored_sha256"] is None and clean["removed_sha256"] is None
    ):
        fail("removed_sha256")
    elif kind == "lesson.applied" and int(clean["count"]) < 1:
        # A receipt for no applications is not a receipt.
        fail("count")


def payload_keys(kind: str) -> tuple[frozenset[str], frozenset[str]]:
    """``(required, allowed)`` payload key names for one spine kind.

    The single published source of the names, so a *reader* of a payload can
    take them from the same place the validator does.  The closed key sets
    already stop a writer inventing a name -- an unknown key is refused loudly
    -- but nothing stops a reader inventing one, and that side fails silently:
    the lookup simply misses and the check falls through to whatever a missing
    value compares as.  That is not hypothetical; it cost a debugging cycle on
    2026-09-04 when a verifier read ``application_ids_sha256`` while this
    contract required ``applications_digest``, and it survived only because
    comparing a digest against the empty string happened to fail closed.

    ``validate_payload`` calls this, so the two can never disagree.

    Raises ``SpineError`` for an unknown kind and for a kind in
    :data:`UNCONSTRAINED_PAYLOAD_KINDS`, rather than returning empty sets that
    a caller could read as "this payload may carry nothing".
    """
    if kind not in SPINE_KINDS:
        raise SpineError(f"unknown spine event kind {kind!r}")
    if kind in UNCONSTRAINED_PAYLOAD_KINDS:
        raise SpineError(f"spine kind {kind!r} has no closed payload contract")
    if kind in CLAIM_CREATING_KINDS:
        return _CLAIM_REQUIRED_KEYS, _CLAIM_PAYLOAD_KEYS
    if kind in CLAIM_STATUS_KINDS:
        return _STATUS_REQUIRED_KEYS, _STATUS_PAYLOAD_KEYS
    if kind == "claim.tombstoned":
        return _TOMBSTONE_REQUIRED_KEYS, _TOMBSTONE_PAYLOAD_KEYS
    if kind in MEMORY_CREATING_KINDS or kind == "memory.updated":
        required = (
            _LESSON_REQUIRED_KEYS if kind == "lesson.created" else _MEMORY_REQUIRED_KEYS
        )
        return required, _MEMORY_PAYLOAD_KEYS
    if kind == "memory.reasserted":
        return _REASSERT_REQUIRED_KEYS, _REASSERT_PAYLOAD_KEYS
    if kind == "memory.deleted":
        return _DELETED_REQUIRED_KEYS, _DELETED_PAYLOAD_KEYS
    if kind == "projection.rebuilt":
        return _REBUILT_REQUIRED_KEYS, _REBUILT_PAYLOAD_KEYS
    if kind == "transcript.compacted":
        return _COMPACTED_REQUIRED_KEYS, _COMPACTED_PAYLOAD_KEYS
    if kind == "conversation.deleted":
        return (_CONVERSATION_DELETED_REQUIRED_KEYS,
                _CONVERSATION_DELETED_PAYLOAD_KEYS)
    if kind == "lesson.applied":
        return _APPLIED_REQUIRED_KEYS, _APPLIED_PAYLOAD_KEYS
    allowed, required = _LADDER_PAYLOAD_KEYS[kind]
    return required, allowed


def validate_payload(kind: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise SpineError("spine payload must be a mapping")
    clean = dict(payload)
    if kind in SPINE_KINDS and kind not in UNCONSTRAINED_PAYLOAD_KINDS:
        required, allowed = payload_keys(kind)
        extra = set(clean) - allowed
        missing = required - set(clean)
    else:
        extra, missing = set(), set()
    if extra or missing:
        raise SpineError(
            f"spine payload for {kind} has extra keys {sorted(extra)} "
            f"or missing keys {sorted(missing)}"
        )
    _check_payload_types(kind, clean)
    encoded = canonical(clean)
    if len(encoded.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise SpineError("spine payload exceeds the size bound")
    return clean


def append_event(
    db: sqlite3.Connection,
    key: bytes,
    *,
    kind: str,
    actor: str,
    source: str,
    scope: str,
    permission: str,
    outcome: str,
    payload: Mapping[str, Any],
    now: str,
    conversation_id: int | None = None,
    subject_kind: str | None = None,
    subject_id: int | None = None,
    parent_event_id: int | None = None,
) -> int:
    """Append one event inside the caller's write transaction; return its id.

    The keyed head record is updated in the same transaction, so a chain can
    only be extended, never shortened, without the key.
    """
    if kind not in SPINE_KINDS:
        raise SpineError(f"unknown spine event kind {kind!r}")
    if actor not in SPINE_ACTORS:
        raise SpineError(f"unknown spine actor {actor!r}")
    if outcome not in SPINE_OUTCOMES:
        raise SpineError(f"unknown spine outcome {outcome!r}")
    if subject_kind is not None and subject_kind not in SPINE_SUBJECT_KINDS:
        raise SpineError(f"unknown spine subject kind {subject_kind!r}")
    if kind in _M4_STRUCTURED_KINDS:
        # Structural, not conventional: no model ever appears on the ladder
        # path, approval and rollback are operator-typed only, and every
        # ladder event names the row it is the lineage of.
        if actor == "model":
            raise SpineError(f"a model may never append {kind}")
        if kind in LADDER_OPERATOR_KINDS and actor != "operator":
            raise SpineError(f"{kind} requires actor 'operator', not {actor!r}")
        expected_subject = _LADDER_SUBJECT_KIND[kind]
        if subject_kind != expected_subject:
            raise SpineError(
                f"{kind} requires subject kind {expected_subject!r}, not {subject_kind!r}"
            )
        if subject_id is None or not _is_int(subject_id) or int(subject_id) < 1:
            raise SpineError(f"{kind} requires a positive subject id")
    if not db.in_transaction:
        raise SpineError("spine events are appended inside a write transaction")
    clean = validate_payload(kind, payload)
    last = db.execute(
        "SELECT id, event_sha256 FROM memory_spine_events ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if last is not None:
        # Never chain onto a truncated or foreign tail: the keyed head must
        # exist, verify, and name the current last row.
        head = db.execute(
            "SELECT last_event_id, last_event_sha256, head_mac FROM memory_spine_head WHERE id=1"
        ).fetchone()
        if (
            head is None
            or not hmac.compare_digest(head_mac(key, int(head[0]), str(head[1])), str(head[2]))
            or int(head[0]) != int(last[0])
            or str(head[1]) != str(last[1])
        ):
            raise SpineError(
                "memory spine head does not verify; refusing to append (run spine verify)"
            )
    event_id = 1 if last is None else int(last[0]) + 1
    prev = GENESIS_PREV_SHA256 if last is None else str(last[1])
    salt = secrets.token_hex(16)
    fields: dict[str, Any] = {
        "id": event_id,
        "created_at": _monotonic_stamp(db, now),
        "kind": kind,
        "actor": actor,
        "source": str(source)[:200],
        "scope": str(scope),
        "permission": str(permission)[:80],
        "conversation_id": int(conversation_id) if conversation_id is not None else None,
        "subject_kind": subject_kind,
        "subject_id": int(subject_id) if subject_id is not None else None,
        "parent_event_id": int(parent_event_id) if parent_event_id is not None else None,
        "outcome": outcome,
        "payload_sha256": payload_digest(salt, clean),
        "prev_sha256": prev,
    }
    fields["event_sha256"] = event_digest(key, fields)
    db.execute(
        """INSERT INTO memory_spine_events(
               id, created_at, kind, actor, source, scope, permission,
               conversation_id, subject_kind, subject_id, parent_event_id, outcome,
               payload_json, payload_salt, payload_sha256, prev_sha256, event_sha256
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            fields["id"], fields["created_at"], kind, actor, fields["source"],
            fields["scope"], fields["permission"], fields["conversation_id"],
            subject_kind, fields["subject_id"], fields["parent_event_id"], outcome,
            canonical(clean), salt, fields["payload_sha256"], prev,
            fields["event_sha256"],
        ),
    )
    db.execute(
        """INSERT INTO memory_spine_head(id, last_event_id, last_event_sha256, head_mac)
           VALUES (1, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
               last_event_id=excluded.last_event_id,
               last_event_sha256=excluded.last_event_sha256,
               head_mac=excluded.head_mac""",
        (event_id, fields["event_sha256"], head_mac(key, event_id, fields["event_sha256"])),
    )
    return event_id


def events_to_redact(
    db: sqlite3.Connection, scope: str, claim_key: str
) -> list[int]:
    """Ids the next tombstone for this key will redact (recorded in its
    payload so verification can prove the redaction was carried out)."""
    rows = db.execute(
        """SELECT id FROM memory_spine_events
           WHERE scope = ? AND payload_json IS NOT NULL
             AND kind IN ('claim.imported','claim.created','claim.superseded',
                          'claim.reasserted','claim.disputed','claim.retracted',
                          'proposal.not_stored','proposal.confirmed')
             AND json_extract(payload_json, '$.claim_key') = ?
           ORDER BY id""",
        (str(scope), str(claim_key)),
    ).fetchall()
    return [int(row[0]) for row in rows]


def redact_claim_key_events(
    db: sqlite3.Connection, scope: str, claim_key: str, tombstone_event_id: int
) -> list[int]:
    """Null the payloads of every earlier event about one claim key; the
    tombstone must already exist (the trigger checks it)."""
    rows = db.execute(
        """SELECT id FROM memory_spine_events
           WHERE id < ? AND scope = ? AND payload_json IS NOT NULL
             AND kind IN ('claim.imported','claim.created','claim.superseded',
                          'claim.reasserted','claim.disputed','claim.retracted',
                          'proposal.not_stored','proposal.confirmed')
             AND json_extract(payload_json, '$.claim_key') = ?
           ORDER BY id""",
        (int(tombstone_event_id), str(scope), str(claim_key)),
    ).fetchall()
    redacted: list[int] = []
    for row in rows:
        db.execute(
            """UPDATE memory_spine_events
               SET payload_json=NULL, payload_salt=NULL, redacted_by_event_id=?
               WHERE id=?""",
            (int(tombstone_event_id), int(row[0])),
        )
        redacted.append(int(row[0]))
    return redacted


def latest_event_id(
    db: sqlite3.Connection, *, kind: str, subject_kind: str, subject_id: int
) -> int | None:
    """The newest event of one kind about one subject (for backward lineage)."""
    row = db.execute(
        """SELECT id FROM memory_spine_events
           WHERE kind=? AND subject_kind=? AND subject_id=?
           ORDER BY id DESC LIMIT 1""",
        (str(kind), str(subject_kind), int(subject_id)),
    ).fetchone()
    return int(row[0]) if row is not None else None


# --- verification ----------------------------------------------------------

def _normalized_sql(text: str) -> str:
    return " ".join(str(text or "").split())


def _payload_of(row: Mapping[str, Any]) -> dict[str, Any] | None:
    if row["payload_json"] is None:
        return None
    try:
        payload = json.loads(str(row["payload_json"]))
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def verify_spine(db: sqlite3.Connection, key: bytes) -> dict[str, Any]:
    """Recompute the keyed chain and the head record, and check lineage and
    redactions; never repairs anything.

    ``chain_ok`` is true when every check except lineage passes (claim
    lineage, memory lineage, and the backing-row cross-check): a projection
    with out-of-band rows fails ``ok`` but still has an authentic spine,
    which is exactly what ``apply_claim_projection`` reconciles.
    """
    report: dict[str, Any] = {
        "ok": True,
        "events": 0,
        "redacted": 0,
        "problems": [],
        "triggers_ok": True,
        "lineage_ok": True,
        "head_ok": True,
        "key_ok": True,
        "chain_ok": True,
        "memory_rows": 0,
        "memory_events": 0,
        "memory_lineage_ok": True,
        "memory_sequence_ok": True,
        "claim_backing_rows": 0,
        # The learning ladder's four counters (design 2.4).  They are counts,
        # not verdicts: ``verify_calibration_ledger`` owns the ladder's own
        # integrity checks, and what belongs here is the pair of numbers that
        # says whether the record tables and the chain still describe the same
        # world.  A ladder lineage fault is reported through ``lineage_ok``
        # and never through ``chain_ok``, exactly as a claim or memory lineage
        # fault is: a projection with out-of-band rows fails ``ok`` while the
        # spine underneath it is still authentic.
        "ledger_rows": 0,
        "ledger_events": 0,
        "ladder_rows": 0,
        "ladder_events": 0,
        "ladder_lineage_ok": True,
    }

    def problem(text: str, *, lineage: bool = False) -> None:
        report["ok"] = False
        report["problems"].append(text)
        if not lineage:
            report["chain_ok"] = False

    if not spine_ready(db):
        problem("spine tables are missing")
        return report
    for name, sql in _TRIGGER_SQL.items():
        row = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)
        ).fetchone()
        if row is None or _normalized_sql(row[0]) != _normalized_sql(sql):
            report["triggers_ok"] = False
            problem(f"trigger {name} is missing or altered")
    rows = db.execute(
        """SELECT id, created_at, kind, actor, source, scope, permission,
                  conversation_id, subject_kind, subject_id, parent_event_id, outcome,
                  payload_json, payload_salt, payload_sha256, prev_sha256, event_sha256,
                  redacted_by_event_id
           FROM memory_spine_events ORDER BY id"""
    ).fetchall()
    events_by_id = {int(row["id"]): row for row in rows}
    prev = GENESIS_PREV_SHA256
    expected_id = 1
    last_stamp: datetime | None = None
    tombstoned_claim_ids: set[int] = set()
    deleted_memory_ids: set[int] = set()
    creating_seen: dict[tuple[str, int], int] = {}
    for row in rows:
        report["events"] += 1
        event_id = int(row["id"])
        kind = str(row["kind"])
        if kind in MEMORY_KINDS:
            report["memory_events"] += 1
        if kind in CLAIM_CREATING_KINDS or kind in MEMORY_CREATING_KINDS:
            # Ids are never reused, so a second creating event for one
            # subject is always a writer fault, live or not.
            subject = (str(row["subject_kind"] or ""), int(row["subject_id"] or 0))
            first = creating_seen.get(subject)
            if first is None:
                creating_seen[subject] = event_id
            else:
                problem(
                    f"event {event_id}: duplicate creating event for {subject[0]} "
                    f"{subject[1]} (first {first})"
                )
        if event_id != expected_id:
            problem(f"event {event_id}: id gap (expected {expected_id})")
            expected_id = event_id
        expected_id += 1
        if str(row["prev_sha256"]) != prev:
            problem(f"event {event_id}: chain link broken")
        fields = {name: row[name] for name in row.keys()}
        if event_digest(key, fields) != str(row["event_sha256"]):
            problem(f"event {event_id}: keyed digest mismatch")
        prev = str(row["event_sha256"])
        try:
            stamp = _parse_stamp(str(row["created_at"]))
        except ValueError:
            problem(f"event {event_id}: unparsable timestamp")
            stamp = last_stamp
        if last_stamp is not None and stamp is not None and stamp <= last_stamp:
            problem(f"event {event_id}: clock not monotonic")
        if stamp is not None:
            last_stamp = stamp
        if row["payload_json"] is None:
            report["redacted"] += 1
            if row["payload_salt"] is not None:
                problem(f"event {event_id}: redacted payload keeps its salt")
            if str(row["kind"]) not in REDACTABLE_KINDS:
                problem(f"event {event_id}: kind {row['kind']} may not be redacted")
            tombstone_id = row["redacted_by_event_id"]
            tombstone = (
                events_by_id.get(int(tombstone_id)) if tombstone_id is not None else None
            )
            tombstone_payload = _payload_of(tombstone) if tombstone is not None else None
            if (
                tombstone is None
                or int(tombstone["id"]) <= event_id
                or str(tombstone["kind"]) != "claim.tombstoned"
                or tombstone_payload is None
                or str(tombstone["scope"]) != str(row["scope"])
            ):
                problem(f"event {event_id}: redaction without a valid later tombstone")
            else:
                removed = {
                    item for item in (tombstone_payload.get("removed_claim_ids") or [])
                    if isinstance(item, int)
                }
                listed = {
                    item for item in (tombstone_payload.get("redacted_event_ids") or [])
                    if isinstance(item, int)
                }
                names_claim = row["subject_id"] is not None and int(row["subject_id"]) in removed
                if str(row["kind"]) in PROPOSAL_KINDS:
                    if event_id not in listed:
                        problem(
                            f"event {event_id}: proposal redaction not listed by its tombstone"
                        )
                elif not names_claim and event_id not in listed:
                    problem(
                        f"event {event_id}: redaction by a tombstone that does not "
                        "name its claim"
                    )
        else:
            if row["payload_salt"] is None:
                problem(f"event {event_id}: payload without salt")
            else:
                payload = _payload_of(row)
                if payload is None or payload_digest(
                    str(row["payload_salt"]), payload
                ) != str(row["payload_sha256"]):
                    problem(f"event {event_id}: payload digest mismatch")
                elif str(row["kind"]) == "claim.tombstoned":
                    for removed in payload.get("removed_claim_ids") or []:
                        if isinstance(removed, int):
                            tombstoned_claim_ids.add(removed)
                    for removed in payload.get("removed_memory_ids") or []:
                        if _is_int(removed):
                            deleted_memory_ids.add(int(removed))
                elif kind in MEMORY_KINDS or kind == "projection.rebuilt":
                    try:
                        validate_payload(kind, payload)
                    except SpineError:
                        problem(f"event {event_id}: malformed {kind} payload")
                    else:
                        if kind == "memory.deleted":
                            for removed in payload.get("ids") or []:
                                deleted_memory_ids.add(int(removed))
            if row["redacted_by_event_id"] is not None:
                problem(f"event {event_id}: redaction marker on a live payload")
    if not rows:
        problem("spine has no genesis event (empty or emptied)")
    elif str(rows[0]["kind"]) != "spine.genesis":
        problem("first event is not the genesis event")
    else:
        genesis = _payload_of(rows[0])
        recorded = str((genesis or {}).get("key_fingerprint") or "")
        if recorded and recorded != key_fingerprint(key):
            report["key_ok"] = False
            problem("key mismatch: the sidecar is not the key this spine was written with")
    # Every tombstone must have carried out its redactions: each earlier
    # claim event in its scope whose claim id it names, and every id it lists,
    # must be redacted by it (an un-redaction from a backup is caught here).
    for row in rows:
        if str(row["kind"]) != "claim.tombstoned":
            continue
        tombstone_payload = _payload_of(row)
        if tombstone_payload is None:
            continue
        tombstone_id = int(row["id"])
        removed = {
            item for item in (tombstone_payload.get("removed_claim_ids") or [])
            if isinstance(item, int)
        }
        listed = {
            item for item in (tombstone_payload.get("redacted_event_ids") or [])
            if isinstance(item, int)
        }
        for earlier in rows:
            earlier_id = int(earlier["id"])
            if earlier_id >= tombstone_id:
                break
            covered = earlier_id in listed or (
                str(earlier["kind"]) in (CLAIM_CREATING_KINDS | CLAIM_STATUS_KINDS)
                and str(earlier["scope"]) == str(row["scope"])
                and earlier["subject_id"] is not None
                and int(earlier["subject_id"]) in removed
            )
            if covered and (
                earlier["payload_json"] is not None
                or earlier["redacted_by_event_id"] is None
                or int(earlier["redacted_by_event_id"]) != tombstone_id
            ):
                problem(
                    f"event {earlier_id}: not redacted by tombstone {tombstone_id} "
                    "(restored or never redacted)"
                )
    sequence = db.execute("SELECT next_id FROM memory_claim_sequence WHERE id=1").fetchone()
    if sequence is None:
        problem("claim sequence row is missing")
    elif int(sequence[0]) <= sequence_floor(db):
        problem("claim sequence is behind the store (an erased id could be reused)")
    # The keyed head record must name the newest event: a removed tail
    # would otherwise leave a shorter but self-consistent chain.
    head = db.execute(
        "SELECT last_event_id, last_event_sha256, head_mac FROM memory_spine_head WHERE id=1"
    ).fetchone()
    if head is None:
        if rows:
            report["head_ok"] = False
            problem("head record is missing")
    else:
        expected_mac = head_mac(key, int(head["last_event_id"]), str(head["last_event_sha256"]))
        if not hmac.compare_digest(expected_mac, str(head["head_mac"])):
            report["head_ok"] = False
            problem("head record digest mismatch")
        elif not rows or int(head["last_event_id"]) != int(rows[-1]["id"]) or str(
            head["last_event_sha256"]
        ) != str(rows[-1]["event_sha256"]):
            report["head_ok"] = False
            problem("head record does not name the newest event (tail removed or replaced)")
    # Lineage: every claim row was produced by a claim-creating event that
    # names it, and no live claim id was ever tombstoned.
    claims = db.execute(
        "SELECT id, memory_id, spine_event_id FROM memory_claims ORDER BY id"
    ).fetchall()
    claims_by_memory: dict[int, tuple[int, int | None]] = {}
    for claim in claims:
        claim_id = int(claim["id"])
        event_id = claim["spine_event_id"]
        event = events_by_id.get(int(event_id)) if event_id is not None else None
        if (
            event is None
            or str(event["kind"]) not in CLAIM_CREATING_KINDS
            or str(event["subject_kind"] or "") != "claim"
            or int(event["subject_id"] or 0) != claim_id
        ):
            report["lineage_ok"] = False
            problem(f"claim {claim_id}: no creating spine event", lineage=True)
        if claim_id in tombstoned_claim_ids:
            report["lineage_ok"] = False
            problem(f"claim {claim_id}: live row with a tombstoned id", lineage=True)
        if claim["memory_id"] is not None:
            claims_by_memory[int(claim["memory_id"])] = (
                claim_id, int(event_id) if event_id is not None else None
            )
    # Memory lineage: a claim's backing row carries exactly the claim's
    # event; every other row a memory-creating event that names it; no live
    # memory id was ever deleted; the memory sequence is ahead of them all.
    live_memory_ids: set[int] = set()
    for row in db.execute("SELECT id, kind, spine_event_id FROM memories ORDER BY id").fetchall():
        report["memory_rows"] += 1
        memory_id = int(row["id"])
        live_memory_ids.add(memory_id)
        event_id = row["spine_event_id"]
        linked = claims_by_memory.get(memory_id)
        if linked is not None:
            report["claim_backing_rows"] += 1
            if event_id is None or linked[1] is None or int(event_id) != linked[1]:
                report["memory_lineage_ok"] = False
                problem(f"memory {memory_id}: lineage is not its claim's event", lineage=True)
        else:
            event = events_by_id.get(int(event_id)) if event_id is not None else None
            if (
                event is None
                or str(event["kind"]) not in MEMORY_CREATING_KINDS
                or str(event["subject_kind"] or "") != "memory"
                or int(event["subject_id"] or 0) != memory_id
            ):
                report["memory_lineage_ok"] = False
                problem(f"memory {memory_id}: no creating spine event", lineage=True)
        if memory_id in deleted_memory_ids:
            report["memory_lineage_ok"] = False
            problem(f"memory {memory_id}: live row with a deleted id", lineage=True)
    for memory_id, (claim_id, _event) in sorted(claims_by_memory.items()):
        if memory_id not in live_memory_ids:
            report["lineage_ok"] = False
            problem(f"claim {claim_id}: backing memory row missing", lineage=True)
    sequence = db.execute("SELECT next_id FROM memory_id_sequence WHERE id=1").fetchone()
    if sequence is None:
        report["memory_sequence_ok"] = False
        problem("memory sequence row is missing")
    elif int(sequence[0]) <= memory_sequence_floor(db):
        report["memory_sequence_ok"] = False
        problem("memory sequence is behind the store (a deleted id could be reused)")
    _verify_ladder_lineage(db, report, problem)
    return report


def _verify_ladder_lineage(
    db: sqlite3.Connection, report: dict[str, Any], problem: Any
) -> None:
    """Count the ladder's rows and events, and check lineage both ways.

    Skipped entirely on a store below schema 49, which has neither table and
    is not thereby faulty.  A planted row -- one inserted with the lineage
    trigger suspended -- is caught here on read, which is the half of the
    laundering defence that does not depend on the trigger being present.
    """
    for table, key in (
        ("memory_calibration_ledger", "ledger_rows"),
        ("ladder_promotions", "ladder_rows"),
    ):
        if not _table_exists(db, table):
            return
        report[key] = int(
            db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        )
    report["ledger_events"] = int(db.execute(
        "SELECT COUNT(*) FROM memory_spine_events WHERE kind=?",
        ("ladder.calibration_sealed",),
    ).fetchone()[0])
    report["ladder_events"] = int(db.execute(
        """SELECT COUNT(*) FROM memory_spine_events
           WHERE kind IN (SELECT value FROM json_each(?))""",
        (json.dumps(sorted(LADDER_KINDS - {"ladder.calibration_sealed"})),),
    ).fetchone()[0])

    def ladder_problem(text: str) -> None:
        report["ladder_lineage_ok"] = False
        problem(text, lineage=True)

    for row in db.execute(
        """SELECT id, spine_event_id FROM memory_calibration_ledger ORDER BY id"""
    ):
        event = db.execute(
            """SELECT kind, subject_kind, subject_id FROM memory_spine_events
               WHERE id=?""",
            (int(row["spine_event_id"]),),
        ).fetchone()
        if (
            event is None
            or str(event["kind"]) != "ladder.calibration_sealed"
            or str(event["subject_kind"] or "") != "calibration"
            or int(event["subject_id"] or 0) != int(row["id"])
        ):
            ladder_problem(f"calibration epoch {int(row['id'])}: no sealing event")
    for row in db.execute(
        "SELECT id, spine_event_id FROM ladder_promotions ORDER BY id"
    ):
        event = db.execute(
            """SELECT kind, subject_kind, subject_id FROM memory_spine_events
               WHERE id=?""",
            (int(row["spine_event_id"]),),
        ).fetchone()
        if (
            event is None
            or str(event["kind"]) not in {"ladder.candidate", "ladder.grandfathered"}
            or str(event["subject_kind"] or "") != "ladder"
            or int(event["subject_id"] or 0) != int(row["id"])
        ):
            ladder_problem(f"ladder promotion {int(row['id'])}: no creating event")
    for event in db.execute(
        """SELECT id, kind, subject_id FROM memory_spine_events
           WHERE subject_kind IN ('ladder','calibration') ORDER BY id"""
    ):
        kind = str(event["kind"])
        if kind == "ladder.calibration_sealed":
            table = "memory_calibration_ledger"
        elif kind in {"ladder.candidate", "ladder.grandfathered"}:
            table = "ladder_promotions"
        else:
            # A status event names a subject the creating event already
            # vouched for; it creates no row of its own.
            continue
        backing = db.execute(
            f"SELECT 1 FROM {table} WHERE id=? AND spine_event_id=?",
            (int(event["subject_id"] or 0), int(event["id"])),
        ).fetchone()
        if backing is None:
            ladder_problem(
                f"spine event {int(event['id'])} ({kind}): no ladder row"
            )


# --- rebuild ---------------------------------------------------------------

def _replay_claims(
    db: sqlite3.Connection,
) -> tuple[dict[int, dict[str, Any]], list[dict[str, Any]], set[int]]:
    """Replay the claim events in id order into a shadow projection keyed by
    claim id.  Each shadow row keeps its creating payload (``_payload``), the
    creating event id (``_event_id``), and its status history
    (``_history``: ``(event_id, payload)`` pairs) for recreation."""
    divergences: list[dict[str, Any]] = []
    shadow: dict[int, dict[str, Any]] = {}
    rows = db.execute(
        """SELECT id, kind, scope, subject_id, payload_json, redacted_by_event_id
           FROM memory_spine_events ORDER BY id"""
    ).fetchall()
    redacted_without_tombstone: set[int] = set()
    for row in rows:
        kind = str(row["kind"])
        if row["payload_json"] is None:
            if kind in CLAIM_CREATING_KINDS and row["subject_id"] is not None:
                redacted_without_tombstone.add(int(row["subject_id"]))
            continue
        try:
            payload = json.loads(str(row["payload_json"]))
        except ValueError:
            divergences.append(
                {"claim_id": None, "kind": "payload", "detail": f"event {row['id']} unreadable"}
            )
            continue
        if kind in CLAIM_CREATING_KINDS:
            claim_id = int(row["subject_id"] or 0)
            shadow[claim_id] = {
                "_payload": payload,
                "_event_id": int(row["id"]),
                "_history": [],
                "scope": str(row["scope"]),
                "claim_key": payload.get("claim_key"),
                "subject": payload.get("subject"),
                "predicate": payload.get("predicate"),
                "value": payload.get("value"),
                "value_sha256": payload.get("value_sha256"),
                "status": payload.get("status"),
                "authority": payload.get("authority"),
                "confidence": payload.get("confidence"),
                "source": payload.get("source"),
                "valid_from": payload.get("valid_from"),
                "valid_until": payload.get("valid_until"),
                "supersedes_id": payload.get("supersedes_id"),
            }
        elif kind in CLAIM_STATUS_KINDS:
            claim_id = int(payload.get("claim_id") or 0)
            target = shadow.get(claim_id)
            if target is None:
                divergences.append(
                    {"claim_id": claim_id, "kind": "order", "detail": f"status event {row['id']} before creation"}
                )
                continue
            for name in ("status", "valid_until", "confidence", "authority", "source"):
                if name in payload:
                    target[name] = payload[name]
            target["_history"].append((int(row["id"]), payload))
        elif kind == "claim.tombstoned":
            for removed in payload.get("removed_claim_ids") or []:
                if isinstance(removed, int):
                    shadow.pop(removed, None)
                    redacted_without_tombstone.discard(removed)
    return shadow, divergences, redacted_without_tombstone


def rebuild_claim_projection(
    db: sqlite3.Connection,
    key: bytes,
    *,
    content_builder: Any = None,
) -> dict[str, Any]:
    """Replay the spine into a shadow projection and compare it with the live
    claim rows.  Dry run only: nothing in the live tables changes.

    Returns ``{"ok", "rows_live", "rows_rebuilt", "divergences"}`` where each
    divergence is ``{"claim_id", "kind", "detail"}``.  ``content_builder``
    (payload, scope) -> expected backing memory content lets the caller check
    that the backing row was not edited out of band.
    """
    verification = verify_spine(db, key)
    divergences: list[dict[str, Any]] = []
    if not verification["ok"]:
        for text in verification["problems"]:
            divergences.append({"claim_id": None, "kind": "verify", "detail": text})
    shadow, replay_divergences, redacted_without_tombstone = _replay_claims(db)
    divergences.extend(replay_divergences)
    for claim_id in sorted(redacted_without_tombstone):
        if claim_id in shadow:
            continue
        divergences.append(
            {"claim_id": claim_id, "kind": "redaction", "detail": "creation redacted without tombstone"}
        )
    live_rows = db.execute(
        f"SELECT id, {', '.join(_EQUIVALENCE_COLUMNS)} FROM memory_claims ORDER BY id"
    ).fetchall()
    live: dict[int, dict[str, Any]] = {
        int(row["id"]): {name: row[name] for name in _EQUIVALENCE_COLUMNS} for row in live_rows
    }

    def same(left: Any, right: Any) -> bool:
        if isinstance(left, float) or isinstance(right, float):
            try:
                return abs(float(left) - float(right)) < 1e-9
            except (TypeError, ValueError):
                return False
        return left == right

    for claim_id in sorted(set(live) | set(shadow)):
        if claim_id not in shadow:
            divergences.append({"claim_id": claim_id, "kind": "missing_in_rebuild", "detail": "live row has no spine history"})
            continue
        if claim_id not in live:
            divergences.append({"claim_id": claim_id, "kind": "missing_in_live", "detail": "spine history has no live row"})
            continue
        for name in _EQUIVALENCE_COLUMNS:
            if not same(live[claim_id].get(name), shadow[claim_id].get(name)):
                divergences.append(
                    {"claim_id": claim_id, "kind": "field",
                     "detail": _claim_field_detail(name, live[claim_id].get(name), shadow[claim_id].get(name))}
                )
        memory_row = db.execute(
            "SELECT m.content FROM memories AS m JOIN memory_claims AS c ON c.memory_id=m.id WHERE c.id=?",
            (claim_id,),
        ).fetchone()
        if memory_row is None:
            divergences.append({"claim_id": claim_id, "kind": "backing_memory", "detail": "live claim has no backing memory row"})
        elif content_builder is not None:
            try:
                # Either variant is legal: two claim keys can render the same
                # canonical content, and the second row then carries the keyed
                # suffix (M3 §6.3).  A dry run reports, never crashes.
                variants = _expected_backing_content(content_builder, shadow[claim_id])
            except SpineError:
                variants = None
            if variants is not None and str(memory_row[0]) not in variants:
                divergences.append({"claim_id": claim_id, "kind": "backing_memory", "detail": "backing memory content differs from the spine"})
    return {
        "ok": not divergences,
        "rows_live": len(live),
        "rows_rebuilt": len(shadow),
        "divergences": divergences,
        "verification": verification,
    }


def _claim_field_detail(name: str, live: Any, rebuilt: Any) -> str:
    """A divergence detail names the field; a claim's value, subject,
    predicate, key, source, or unkeyed value digest never appears (the CLI
    prints details verbatim)."""
    if name in _CLAIM_METADATA_FIELDS:
        return f"{name}: live={live!r} rebuilt={rebuilt!r}"
    return f"{name}: differs"


def _memory_field_detail(name: str, live: Any, rebuilt: Any) -> str:
    # Keyed digests and metadata may be shown; a source can be operator text.
    if name == "source":
        return "source: differs"
    return f"{name}: live={live!r} rebuilt={rebuilt!r}"


def rebuild_memory_projection(db: sqlite3.Connection, key: bytes) -> dict[str, Any]:
    """Replay the memory events into a shadow keyed by memory id and compare
    it with the live ``memories`` rows.  Dry run only.

    The comparison tuple is ``(kind, content_digest, content_length, source,
    family, outcome_status, reflection_id, origin, eligible)`` with the live
    digest recomputed under the key, ``origin``/``eligible`` from
    ``ordinary_memory_provenance`` (``None`` without a row), plus for lessons
    the presence of a ``lesson_provenance`` row carrying a digest and that
    digest itself.  A claim's backing row is covered by the claim rebuild and
    is only lineage-checked here, even when it has a memory history (a legacy
    orphan row imported by migration 47 and later adopted by the claim
    writer keeps its ``memory.imported`` event; the claim's event is its
    lineage from then on).  Returns ``{"ok", "rows_live",
    "rows_rebuilt", "divergences", "verification"}`` with divergences
    ``{"memory_id", "kind", "detail"}``; details name fields and digests,
    never content.
    """
    verification = verify_spine(db, key)
    divergences: list[dict[str, Any]] = []
    for text in verification["problems"]:
        divergences.append({"memory_id": None, "kind": "verify", "detail": text})
    shadow: dict[int, dict[str, Any]] = {}
    events = db.execute(
        """SELECT id, kind, subject_id, outcome, payload_json FROM memory_spine_events
           WHERE kind IN ('memory.imported','memory.created','lesson.created',
                          'memory.reasserted','memory.updated','memory.deleted')
           ORDER BY id"""
    ).fetchall()
    for row in events:
        event_id = int(row["id"])
        kind = str(row["kind"])
        subject_id = int(row["subject_id"]) if row["subject_id"] is not None else None
        payload = _payload_of(row)
        if payload is None:
            divergences.append(
                {"memory_id": subject_id, "kind": "payload", "detail": f"event {event_id} unreadable"}
            )
            continue
        if kind in MEMORY_CREATING_KINDS:
            entry = {name: payload.get(name) for name in _MEMORY_EQUIVALENCE_FIELDS}
            entry["_event_id"] = event_id
            entry["_provenance"] = set()
            if payload.get("provenance_sha256"):
                entry["_provenance"].add(str(payload["provenance_sha256"]))
            shadow[int(subject_id or 0)] = entry
        elif kind == "memory.updated":
            target = shadow.get(subject_id) if subject_id is not None else None
            if target is None:
                divergences.append(
                    {"memory_id": subject_id, "kind": "order", "detail": f"update event {event_id} before creation"}
                )
                continue
            for name in _MEMORY_EQUIVALENCE_FIELDS:
                target[name] = payload.get(name)
            if payload.get("provenance_sha256"):
                target["_provenance"].add(str(payload["provenance_sha256"]))
        elif kind == "memory.reasserted":
            target = shadow.get(subject_id) if subject_id is not None else None
            if target is None:
                divergences.append(
                    {"memory_id": subject_id, "kind": "order", "detail": f"reassertion event {event_id} before creation"}
                )
                continue
            if str(row["outcome"]) == "applied":
                target["origin"] = payload.get("origin")
                target["eligible"] = payload.get("eligible")
                if payload.get("provenance_sha256"):
                    target["_provenance"].add(str(payload["provenance_sha256"]))
        elif kind == "memory.deleted":
            for removed in payload.get("ids") or []:
                if not _is_int(removed):
                    continue
                if int(removed) not in shadow:
                    divergences.append(
                        {"memory_id": int(removed), "kind": "order", "detail": f"deletion event {event_id} names a memory without history"}
                    )
                    continue
                shadow.pop(int(removed))
    has_provenance = _table_exists(db, "ordinary_memory_provenance")
    provenance_columns = (
        "omp.origin AS origin, omp.eligible AS eligible" if has_provenance
        else "NULL AS origin, NULL AS eligible"
    )
    provenance_join = (
        " LEFT JOIN ordinary_memory_provenance AS omp ON omp.memory_id = m.id"
        if has_provenance else ""
    )
    live_rows = db.execute(
        f"""SELECT m.id, m.kind, m.content, m.source, m.family, m.outcome_status,
                   m.reflection_id, m.spine_event_id,
                   c.id AS claim_id, c.spine_event_id AS claim_event, {provenance_columns}
            FROM memories AS m
            LEFT JOIN memory_claims AS c ON c.memory_id = m.id{provenance_join}
            ORDER BY m.id"""
    ).fetchall()
    lesson_digests: dict[int, set[str]] = {}
    if _table_exists(db, "lesson_provenance"):
        for row in db.execute(
            "SELECT memory_id, provenance_sha256 FROM lesson_provenance WHERE provenance_sha256 IS NOT NULL"
        ).fetchall():
            lesson_digests.setdefault(int(row["memory_id"]), set()).add(str(row["provenance_sha256"]))
    live: dict[int, dict[str, Any]] = {}
    for row in live_rows:
        memory_id = int(row["id"])
        if row["claim_id"] is not None:
            # Outside the memory projection: the claim rebuild owns it.
            shadow.pop(memory_id, None)
            if (
                row["spine_event_id"] is None
                or row["claim_event"] is None
                or int(row["spine_event_id"]) != int(row["claim_event"])
            ):
                divergences.append(
                    {"memory_id": memory_id, "kind": "lineage", "detail": "claim backing row lineage is not its claim's event"}
                )
            continue
        content = str(row["content"])
        live[memory_id] = {
            "kind": str(row["kind"]),
            "content_digest": content_digest(key, content),
            "content_length": len(content),
            "source": None if row["source"] is None else str(row["source"]),
            "family": None if row["family"] is None else str(row["family"]),
            "outcome_status": None if row["outcome_status"] is None else str(row["outcome_status"]),
            "reflection_id": None if row["reflection_id"] is None else int(row["reflection_id"]),
            "origin": None if row["origin"] is None else str(row["origin"]),
            "eligible": None if row["eligible"] is None else bool(int(row["eligible"])),
            "_provenance": lesson_digests.get(memory_id, set()),
        }
    for memory_id in sorted(set(live) | set(shadow)):
        if memory_id not in shadow:
            divergences.append({"memory_id": memory_id, "kind": "missing_in_rebuild", "detail": "live row has no spine history"})
            continue
        if memory_id not in live:
            divergences.append({"memory_id": memory_id, "kind": "missing_in_live", "detail": "spine history has no live row"})
            continue
        for name in _MEMORY_EQUIVALENCE_FIELDS:
            if live[memory_id].get(name) != shadow[memory_id].get(name):
                divergences.append(
                    {"memory_id": memory_id, "kind": "field",
                     "detail": _memory_field_detail(name, live[memory_id].get(name), shadow[memory_id].get(name))}
                )
        expected = shadow[memory_id]["_provenance"]
        actual = live[memory_id]["_provenance"]
        if expected and not actual:
            divergences.append({"memory_id": memory_id, "kind": "provenance", "detail": "lesson provenance row missing"})
        elif actual and not expected:
            divergences.append({"memory_id": memory_id, "kind": "provenance", "detail": "lesson provenance row without spine history"})
        elif expected - actual:
            divergences.append({"memory_id": memory_id, "kind": "provenance", "detail": "lesson provenance digest differs"})
    return {
        "ok": not divergences,
        "rows_live": len(live),
        "rows_rebuilt": len(shadow),
        "divergences": divergences,
        "verification": verification,
    }


# --- apply ----------------------------------------------------------------

def _divergence_signature(report: Mapping[str, Any]) -> list[tuple[str, str]]:
    return sorted(
        {(str(item.get("claim_id")), str(item.get("kind"))) for item in report.get("divergences", [])}
    )


def _claim_creation_reason(status: str) -> str:
    # The reason the writer records with a creating claim event.
    return "new strongest claim" if status == "active" else "conflicts with stronger claim"


def _claim_source_text(shadow_row: Mapping[str, Any]) -> str:
    return f"{shadow_row.get('authority')}:{shadow_row.get('source')}"[:2_000]


def _delete_claim_rows(db: sqlite3.Connection, claim_ids: list[int]) -> list[int]:
    """Delete claim rows with their dependents and backing memory rows in the
    erase order; tables a store does not have are skipped.  Returns the
    removed memory ids."""
    if not claim_ids:
        return []
    placeholders = ",".join("?" for _ in claim_ids)
    if _table_exists(db, "memory_fact_proposals") and "claim_id" in _table_columns(
        db, "memory_fact_proposals"
    ):
        db.execute(
            f"UPDATE memory_fact_proposals SET claim_id=NULL WHERE claim_id IN ({placeholders})",
            claim_ids,
        )
    if _table_exists(db, "memory_claim_events"):
        db.execute(
            f"UPDATE memory_claim_events SET related_claim_id=NULL "
            f"WHERE related_claim_id IN ({placeholders})",
            claim_ids,
        )
    for table in _CLAIM_DEPENDENT_TABLES:
        if _table_exists(db, table):
            db.execute(f"DELETE FROM {table} WHERE claim_id IN ({placeholders})", claim_ids)
    db.execute(
        f"UPDATE memory_claims SET supersedes_id=NULL WHERE supersedes_id IN ({placeholders})",
        claim_ids,
    )
    memory_ids = [
        int(row[0]) for row in db.execute(
            f"SELECT memory_id FROM memory_claims WHERE id IN ({placeholders}) ORDER BY id",
            claim_ids,
        ).fetchall()
        if row[0] is not None
    ]
    db.execute(f"DELETE FROM memory_claims WHERE id IN ({placeholders})", claim_ids)
    if not memory_ids:
        return []
    memory_placeholders = ",".join("?" for _ in memory_ids)
    still_referenced = {
        int(row[0]) for row in db.execute(
            f"SELECT memory_id FROM memory_claims WHERE memory_id IN ({memory_placeholders})",
            memory_ids,
        ).fetchall()
    }
    memory_ids = [memory_id for memory_id in memory_ids if memory_id not in still_referenced]
    if not memory_ids:
        return []
    memory_placeholders = ",".join("?" for _ in memory_ids)
    for table in _MEMORY_DEPENDENT_TABLES:
        if _table_exists(db, table):
            db.execute(
                f"DELETE FROM {table} WHERE memory_id IN ({memory_placeholders})", memory_ids
            )
    db.execute(f"DELETE FROM memories WHERE id IN ({memory_placeholders})", memory_ids)
    return memory_ids


def _backing_variants(expected: Any) -> tuple[str, ...] | None:
    """Normalise a content builder's answer to the variants a backing row may
    hold.  A builder that returns one string keeps working (M2 callers); the
    store returns ``backing_content_variants(...)`` — ``(canonical, keyed)`` —
    because two claim keys can render the same canonical content and the
    second row then carries a keyed suffix (M3 §6.3)."""
    if expected is None:
        return None
    if isinstance(expected, str):
        return (expected,)
    variants = tuple(str(item) for item in expected if item is not None)
    return variants or None


def _expected_backing_content(
    content_builder: Any, shadow_row: Mapping[str, Any]
) -> tuple[str, ...] | None:
    """The contents a backing row may legally hold, canonical first."""
    if content_builder is None:
        return None
    try:
        expected = content_builder(shadow_row["_payload"], shadow_row["scope"])
    except Exception as exc:  # noqa: BLE001 - the builder is caller code
        raise SpineError(
            "write_conflict: backing memory content could not be built", code="write_conflict"
        ) from exc
    return _backing_variants(expected)


def _chosen_backing_content(
    db: sqlite3.Connection, claim_id: int, variants: Sequence[str]
) -> str:
    """The variant a writer would have chosen for this claim.

    The keyed one exactly when the canonical content is already bound to a
    backing row of a *different* claim — the same test ``_remember_claim_locked``
    makes — so a rebuild reproduces the writer's choice from the live rows and
    never from a payload.
    """
    canonical = str(variants[0])
    if len(variants) < 2:
        return canonical
    row = db.execute(
        """SELECT c.id FROM memories AS m JOIN memory_claims AS c ON c.memory_id = m.id
           WHERE m.kind='claim' AND m.content=? LIMIT 1""",
        (canonical,),
    ).fetchone()
    if row is not None and int(row[0]) != int(claim_id):
        return str(variants[1])
    return canonical


def _update_claim_row(
    db: sqlite3.Connection,
    claim_id: int,
    shadow_row: Mapping[str, Any],
    *,
    content_builder: Any,
    now: str,
) -> None:
    """Write the spine's after-image over one live claim row and its backing
    memory row.  ``scope`` is trigger-immutable and handled by recreation;
    ``supersedes_id`` is a reference to another claim row and is written by
    the final pass of ``apply_claim_projection`` once every row exists."""
    db.execute(
        """UPDATE memory_claims
           SET claim_key=?, subject=?, predicate=?, value=?, value_sha256=?, status=?,
               authority=?, confidence=?, source=?, valid_from=?, valid_until=?,
               spine_event_id=?, updated_at=?
           WHERE id=?""",
        (
            shadow_row.get("claim_key"), shadow_row.get("subject"), shadow_row.get("predicate"),
            shadow_row.get("value"), shadow_row.get("value_sha256"), shadow_row.get("status"),
            shadow_row.get("authority"), shadow_row.get("confidence"), shadow_row.get("source"),
            shadow_row.get("valid_from"), shadow_row.get("valid_until"),
            int(shadow_row["_event_id"]), str(now), claim_id,
        ),
    )
    claim = db.execute(
        "SELECT memory_id, created_at, spine_event_id FROM memory_claims WHERE id=?", (claim_id,)
    ).fetchone()
    source_text = _claim_source_text(shadow_row)
    expected = _expected_backing_content(content_builder, shadow_row)
    backing = db.execute(
        "SELECT id, content, source, spine_event_id FROM memories WHERE id=?",
        (int(claim["memory_id"]),),
    ).fetchone()
    if backing is None:
        carrier = db.execute(
            "SELECT id, content, source, spine_event_id FROM memories WHERE spine_event_id=?",
            (int(claim["spine_event_id"]),),
        ).fetchone()
        if carrier is not None:
            db.execute(
                "UPDATE memory_claims SET memory_id=? WHERE id=?", (int(carrier["id"]), claim_id)
            )
            backing = carrier
        else:
            if expected is None:
                raise SpineError(
                    "missing_content_builder: a backing memory row must be reinstated",
                    code="missing_content_builder",
                )
            db.execute(
                """INSERT INTO memories(id, created_at, kind, content, source, spine_event_id)
                   VALUES (?, ?, 'claim', ?, ?, ?)""",
                (
                    int(claim["memory_id"]), str(claim["created_at"]),
                    _chosen_backing_content(db, claim_id, expected), source_text,
                    int(claim["spine_event_id"]),
                ),
            )
            return
    memory_id = int(backing["id"])
    if str(backing["source"] or "") != source_text:
        db.execute("UPDATE memories SET source=? WHERE id=?", (source_text, memory_id))
    if expected is not None and str(backing["content"]) not in expected:
        db.execute(
            "UPDATE memories SET content=? WHERE id=?",
            (_chosen_backing_content(db, claim_id, expected), memory_id),
        )
    if backing["spine_event_id"] is None or int(backing["spine_event_id"]) != int(claim["spine_event_id"]):
        db.execute(
            "UPDATE memories SET spine_event_id=? WHERE id=?",
            (int(claim["spine_event_id"]), memory_id),
        )


def _insert_claim_event(
    db: sqlite3.Connection,
    claim_id: int,
    *,
    created_at: str,
    status: str,
    reason: str,
    related_claim_id: Any,
    spine_event_id: int,
) -> None:
    related = int(related_claim_id) if _is_int(related_claim_id) else None
    if related is not None and db.execute(
        "SELECT 1 FROM memory_claims WHERE id=?", (related,)
    ).fetchone() is None:
        related = None  # the related row is gone; the spine still names it
    db.execute(
        """INSERT INTO memory_claim_events(
               claim_id, created_at, status, reason, related_claim_id, spine_event_id
           ) VALUES (?, ?, ?, ?, ?, ?)""",
        (claim_id, str(created_at), str(status), str(reason)[:200], related, int(spine_event_id)),
    )


def _recreate_claim_row(
    db: sqlite3.Connection,
    claim_id: int,
    shadow_row: Mapping[str, Any],
    *,
    content_builder: Any,
    now: str,
) -> tuple[int, bool]:
    """Recreate one claim row from its spine history: the backing memory row
    (reused when one still carries the creating event or the expected claim
    content, else inserted with an allocated id) and the claim row with the
    creating event as lineage.  ``supersedes_id`` and the claim events are
    written by ``apply_claim_projection``'s final pass, after every row
    they may reference exists.  Returns ``(memory_id, evidence_lost)``."""
    payload = shadow_row["_payload"]
    scope = str(shadow_row["scope"])
    event_id = int(shadow_row["_event_id"])
    created_at = str(
        payload.get("original_created_at") or payload.get("at") or shadow_row.get("valid_from") or now
    )
    expected = _expected_backing_content(content_builder, shadow_row)
    if expected is None:
        raise SpineError(
            "missing_content_builder: recreating a claim needs its backing memory content",
            code="missing_content_builder",
        )
    source_text = _claim_source_text(shadow_row)
    content = _chosen_backing_content(db, claim_id, expected)
    backing = db.execute(
        "SELECT id, spine_event_id FROM memories WHERE spine_event_id=?", (event_id,)
    ).fetchone()
    if backing is None:
        for variant in expected:
            candidate = db.execute(
                "SELECT id, spine_event_id FROM memories WHERE kind='claim' AND content=?",
                (variant,),
            ).fetchone()
            if candidate is not None and (
                candidate["spine_event_id"] is None
                or int(candidate["spine_event_id"]) == event_id
            ):
                backing = candidate
                break
    if backing is not None:
        memory_id = int(backing["id"])
        db.execute(
            "UPDATE memories SET created_at=?, content=?, source=?, spine_event_id=? WHERE id=?",
            (created_at, content, source_text, event_id, memory_id),
        )
    else:
        memory_id = allocate_memory_id(db)
        db.execute(
            """INSERT INTO memories(id, created_at, kind, content, source, spine_event_id)
               VALUES (?, ?, 'claim', ?, ?, ?)""",
            (memory_id, created_at, content, source_text, event_id),
        )
    history: list[tuple[int, dict[str, Any]]] = list(shadow_row.get("_history") or [])
    updated_at = str(history[-1][1].get("at") or created_at) if history else created_at
    db.execute(
        """INSERT INTO memory_claims(
               id, memory_id, created_at, updated_at, scope, claim_key, subject, predicate,
               value, value_sha256, source, authority, confidence, status, valid_from,
               valid_until, supersedes_id, spine_event_id
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)""",
        (
            claim_id, memory_id, created_at, updated_at, scope, shadow_row.get("claim_key"),
            shadow_row.get("subject"), shadow_row.get("predicate"), shadow_row.get("value"),
            shadow_row.get("value_sha256"), shadow_row.get("source"), shadow_row.get("authority"),
            shadow_row.get("confidence"), shadow_row.get("status"), shadow_row.get("valid_from"),
            shadow_row.get("valid_until"), event_id,
        ),
    )
    evidence_lost = True
    if _table_exists(db, "memory_claim_evidence"):
        evidence_lost = int(db.execute(
            "SELECT COUNT(*) FROM memory_claim_evidence WHERE claim_id=?", (claim_id,)
        ).fetchone()[0]) == 0
    return memory_id, evidence_lost


def _replay_claim_events(
    db: sqlite3.Connection,
    claim_id: int,
    shadow_row: Mapping[str, Any],
    *,
    now: str,
) -> None:
    """Replay a recreated claim's events into ``memory_claim_events`` exactly
    as the writer recorded them: the creation row, then one row per status
    event whose status differs from the tracked status (the writer's
    ``_set_claim_status_locked`` returns early otherwise)."""
    if not _table_exists(db, "memory_claim_events"):
        return
    payload = shadow_row["_payload"]
    event_id = int(shadow_row["_event_id"])
    created_at = str(
        payload.get("original_created_at") or payload.get("at") or shadow_row.get("valid_from") or now
    )
    db.execute("DELETE FROM memory_claim_events WHERE claim_id=?", (claim_id,))
    tracked = str(payload.get("status"))
    _insert_claim_event(
        db, claim_id, created_at=str(payload.get("at") or created_at), status=tracked,
        reason=_claim_creation_reason(tracked), related_claim_id=payload.get("supersedes_id"),
        spine_event_id=event_id,
    )
    for history_event_id, history_payload in shadow_row.get("_history") or []:
        status = str(history_payload.get("status"))
        if status == tracked:
            continue
        _insert_claim_event(
            db, claim_id, created_at=str(history_payload.get("at") or now), status=status,
            reason=str(history_payload.get("reason") or "replayed from the memory spine"),
            related_claim_id=history_payload.get("related_claim_id"),
            spine_event_id=history_event_id,
        )
        tracked = status


def apply_claim_projection(
    db: sqlite3.Connection,
    key: bytes,
    plan: Mapping[str, Any] | None,
    *,
    content_builder: Any,
    now: str,
    actor: str = "operator",
    permission: str = "operator:cli",
    source: str = "spine rebuild-claims --apply",
    post_apply: Any = None,
) -> dict[str, Any]:
    """Reconcile the live claim projection with the spine, in place, inside
    the caller's write transaction (design 12.6 item 5).

    ``plan`` is the dry-run report the operator confirmed (or ``None``); the
    dry run is re-run here and must show the same divergence set.  Refusals
    raise ``SpineError`` with a fixed ``code`` and change nothing:
    ``not_in_transaction``, ``verify_failed`` (the chain, head, key,
    triggers, redactions, or sequences fail: the spine, not the projection,
    is wrong), ``history_inconsistent`` (payload / order / redaction
    divergences), ``stale_plan``, ``missing_content_builder``,
    ``write_conflict`` (an integrity error while writing), and
    ``residual_divergence`` (the dry run still diverges afterwards; the
    caller rolls back).  Lineage problems are what apply fixes, so they do
    not refuse.

    ``post_apply`` is an optional zero-argument callable invoked after
    reconciliation and after the residual check, before the receipt is
    appended; the mapping it returns is merged into the ``projection.rebuilt``
    payload and must use only keys the receipt already accepts.  It exists so
    a derived projection reconciled in the same transaction (schema 48: the
    graph) is recorded in the receipt that describes that transaction rather
    than in a second one.

    Order (safe under ``PRAGMA foreign_keys=ON``): deletions (rows without
    spine history, and rows whose immutable scope diverges) in the erase
    order; then recreations with the backing row's ``created_at`` equal to
    the claim's and the creating event as lineage; then field updates from
    the shadow including ``memories.source`` and the backing content; then a
    final pass that writes every ``supersedes_id`` and replays the recreated
    claims' events, once every row they may reference exists.  Evidence is
    never recreated (``lost_evidence_claim_ids``).  ``projection.rebuilt`` is
    appended last.  The caller clears its recall cache and checkpoints after
    commit.
    """
    if not db.in_transaction:
        raise SpineError(
            "not_in_transaction: apply runs inside the caller's write transaction",
            code="not_in_transaction",
        )
    before = rebuild_claim_projection(db, key, content_builder=content_builder)
    if not before["verification"].get("chain_ok"):
        raise SpineError(
            "verify_failed: the memory spine does not verify; refusing to apply",
            code="verify_failed",
        )
    if any(item["kind"] in _APPLY_REFUSAL_KINDS for item in before["divergences"]):
        raise SpineError(
            "history_inconsistent: the spine history has payload, order, or redaction "
            "divergences; refusing to apply",
            code="history_inconsistent",
        )
    if plan is not None and _divergence_signature(plan) != _divergence_signature(before):
        raise SpineError(
            "stale_plan: the store changed since the dry run; run rebuild-claims again",
            code="stale_plan",
        )
    shadow, _replay_divergences, _redacted = _replay_claims(db)
    live_rows = {
        int(row["id"]): row
        for row in db.execute(
            "SELECT id, scope, supersedes_id, spine_event_id FROM memory_claims"
        ).fetchall()
    }
    to_delete: set[int] = set()
    to_recreate: set[int] = set()
    to_update: set[int] = set()
    for item in before["divergences"]:
        claim_id = item.get("claim_id")
        kind = str(item.get("kind"))
        if not _is_int(claim_id):
            continue
        if kind == "missing_in_rebuild":
            to_delete.add(int(claim_id))
        elif kind == "missing_in_live":
            to_recreate.add(int(claim_id))
        elif kind == "field" and str(item.get("detail", "")).startswith("scope:"):
            to_delete.add(int(claim_id))
            to_recreate.add(int(claim_id))
        elif kind in {"field", "backing_memory"}:
            to_update.add(int(claim_id))
    to_update -= to_delete
    # A live row whose lineage column was edited out of band, or whose
    # backing row is missing or mislinked, is reported by verify only; write
    # its after-image too so nothing lineage-shaped is left for the re-run.
    for claim_id, row in live_rows.items():
        if claim_id in to_delete or claim_id not in shadow:
            continue
        if row["spine_event_id"] is None or int(row["spine_event_id"]) != shadow[claim_id]["_event_id"]:
            to_update.add(claim_id)
        elif row["supersedes_id"] is not None and int(row["supersedes_id"]) in to_delete:
            to_update.add(claim_id)
    for text in before["verification"]["problems"]:
        if text.startswith("claim ") and ("backing memory row missing" in text):
            try:
                to_update.add(int(text.split()[1].rstrip(":")))
            except ValueError:
                pass
    updated: list[int] = []
    recreated: list[int] = []
    lost_evidence: list[int] = []
    try:
        removed_memory_ids = _delete_claim_rows(db, sorted(to_delete))
        for claim_id in sorted(to_recreate):
            if claim_id not in shadow:
                continue
            _memory_id, evidence_lost = _recreate_claim_row(
                db, claim_id, shadow[claim_id], content_builder=content_builder, now=now
            )
            recreated.append(claim_id)
            if evidence_lost:
                lost_evidence.append(claim_id)
        for claim_id in sorted(to_update):
            if claim_id in shadow and claim_id in live_rows:
                _update_claim_row(
                    db, claim_id, shadow[claim_id], content_builder=content_builder, now=now
                )
                updated.append(claim_id)
        # Final pass: references between claim rows, now that every row a
        # reference may name exists (a recreated predecessor included).
        for claim_id in sorted(set(recreated) | set(updated)):
            supersedes_id = shadow[claim_id].get("supersedes_id")
            db.execute(
                "UPDATE memory_claims SET supersedes_id=? WHERE id=? AND supersedes_id IS NOT ?",
                (supersedes_id, claim_id, supersedes_id),
            )
        for claim_id in recreated:
            _replay_claim_events(db, claim_id, shadow[claim_id], now=now)
        # Every surviving backing row carries its claim's event.
        db.execute(
            """UPDATE memories SET spine_event_id=(
                   SELECT c.spine_event_id FROM memory_claims AS c WHERE c.memory_id=memories.id)
               WHERE EXISTS (SELECT 1 FROM memory_claims AS c
                             WHERE c.memory_id=memories.id
                               AND c.spine_event_id IS NOT memories.spine_event_id)"""
        )
        _advance_sequence(db, "memory_claim_sequence", sequence_floor(db))
        _advance_sequence(db, "memory_id_sequence", memory_sequence_floor(db))
    except sqlite3.IntegrityError as exc:
        raise SpineError(
            f"write_conflict: the projection could not be written ({exc})", code="write_conflict"
        ) from exc
    after = rebuild_claim_projection(db, key, content_builder=content_builder)
    if after["divergences"]:
        raise SpineError(
            "residual_divergence: the projection still diverges after apply; rolled back",
            code="residual_divergence",
        )
    payload = {
        "at": str(now),
        "rows_before": int(before["rows_live"]),
        "rows_after": int(after["rows_live"]),
        "divergences_fixed": len(before["divergences"]),
        "removed_ids": sorted(to_delete),
        "removed_memory_ids": list(removed_memory_ids),
        "recreated_ids": recreated,
        "updated_ids": updated,
        "lost_evidence_claim_ids": lost_evidence,
    }
    if post_apply is not None:
        try:
            extra = post_apply()
        except SpineError:
            raise
        except Exception as exc:  # noqa: BLE001 - the hook is caller code
            raise SpineError(
                f"write_conflict: the post-apply projection failed ({exc})",
                code="write_conflict",
            ) from exc
        if extra:
            if not isinstance(extra, Mapping):
                raise SpineError(
                    "write_conflict: post_apply must return a mapping",
                    code="write_conflict",
                )
            unknown = set(extra) - _REBUILT_PAYLOAD_KEYS
            if unknown:
                raise SpineError(
                    "write_conflict: post_apply returned keys the rebuild receipt "
                    f"does not accept {sorted(unknown)}",
                    code="write_conflict",
                )
            payload.update(dict(extra))
    event_id = append_event(
        db, key,
        kind="projection.rebuilt", actor=actor, source=source, scope="global",
        permission=permission, outcome="applied", subject_kind="projection",
        payload=payload, now=now,
    )
    return {
        "ok": True,
        "event_id": int(event_id),
        "rows_before": payload["rows_before"],
        "rows_after": payload["rows_after"],
        "divergences_fixed": payload["divergences_fixed"],
        "removed_ids": payload["removed_ids"],
        "removed_memory_ids": payload["removed_memory_ids"],
        "updated_ids": updated,
        "recreated_ids": recreated,
        "lost_evidence_claim_ids": lost_evidence,
    }


def recent_events(db: sqlite3.Connection, limit: int = 20) -> list[dict[str, Any]]:
    """Recent events with payload *keys* only; values never leave the store."""
    bounded = max(1, min(int(limit), 500))
    rows = db.execute(
        """SELECT id, created_at, kind, actor, source, scope, outcome, subject_kind,
                  subject_id, parent_event_id, payload_json, redacted_by_event_id
           FROM memory_spine_events ORDER BY id DESC LIMIT ?""",
        (bounded,),
    ).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        keys: list[str] = []
        if row["payload_json"] is not None:
            try:
                keys = sorted(json.loads(str(row["payload_json"])).keys())
            except (ValueError, AttributeError):
                keys = []
        items.append(
            {
                "id": int(row["id"]),
                "created_at": str(row["created_at"]),
                "kind": str(row["kind"]),
                "actor": str(row["actor"]),
                "source": str(row["source"]),
                "scope": str(row["scope"]),
                "outcome": str(row["outcome"]),
                "subject_kind": row["subject_kind"],
                "subject_id": row["subject_id"],
                "parent_event_id": row["parent_event_id"],
                "payload_keys": keys,
                "redacted": row["payload_json"] is None,
            }
        )
    return items


def claim_event_payload(row: Mapping[str, Any], *, at: str) -> dict[str, Any]:
    """Payload for a claim-creating event from a full claim row."""
    return {
        "at": str(at),
        "claim_key": str(row["claim_key"]),
        "subject": str(row["subject"]),
        "predicate": str(row["predicate"]),
        "value": str(row["value"]),
        "value_sha256": str(row["value_sha256"]),
        "source": str(row["source"]),
        "authority": str(row["authority"]),
        "confidence": float(row["confidence"]),
        "status": str(row["status"]),
        "valid_from": str(row["valid_from"]),
        "valid_until": row["valid_until"],
        "supersedes_id": row["supersedes_id"],
    }


def claim_status_payload(
    row: Mapping[str, Any], *, at: str, reason: str, related_claim_id: int | None
) -> dict[str, Any]:
    """After-image payload for a status event."""
    return {
        "at": str(at),
        "claim_key": str(row["claim_key"]),
        "claim_id": int(row["id"]),
        "reason": str(reason)[:200],
        "related_claim_id": related_claim_id,
        "status": str(row["status"]),
        "valid_until": row["valid_until"],
        "confidence": float(row["confidence"]),
        "authority": str(row["authority"]),
        "source": str(row["source"]),
    }


def fts_secure_delete(db: sqlite3.Connection, table: str) -> bool:
    """Ask an FTS5 index to scrub deleted tokens from its segments.

    Returns False on SQLite builds without the option (before 3.43), where the
    tokens of a deleted row stay in the index until an ``optimize``.
    """
    if sqlite3.sqlite_version_info < (3, 43, 0):
        return False
    try:
        db.execute(f"INSERT INTO {table}({table}, rank) VALUES ('secure-delete', 1)")
    except sqlite3.OperationalError:
        return False
    return True


__all__ = [
    "SpineError", "SPINE_KINDS", "SPINE_ACTORS", "SPINE_OUTCOMES",
    "SPINE_SCHEMA_VERSION", "CLAIM_CREATING_KINDS", "CLAIM_STATUS_KINDS",
    "MEMORY_CREATING_KINDS", "MEMORY_KINDS", "MEMORY_DELETED_MAX_IDS",
    "LADDER_KINDS", "LADDER_OPERATOR_KINDS", "SPINE_SUBJECT_KINDS",
    "lesson_applications_digest", "payload_keys", "UNCONSTRAINED_PAYLOAD_KINDS",
    "KEY_SIDECAR_SUFFIX", "MAX_PAYLOAD_BYTES", "load_spine_key",
    "migrate_memory_spine_v46", "migrate_memory_spine_v47", "drop_spine_triggers",
    "create_spine_triggers", "spine_ready",
    "allocate_claim_id", "allocate_memory_id", "sequence_floor", "memory_sequence_floor",
    "append_event", "redact_claim_key_events", "events_to_redact",
    "latest_event_id", "verify_spine", "rebuild_claim_projection",
    "rebuild_memory_projection", "apply_claim_projection",
    "recent_events", "claim_event_payload", "claim_status_payload",
    "memory_event_payload", "memory_deleted_payload", "content_digest",
    "fts_secure_delete", "sha256_hex", "canonical", "head_mac",
]
_ = Sequence  # kept for typing of future sequence-taking helpers
