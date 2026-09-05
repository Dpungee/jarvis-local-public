"""Turn a fresh schema-47 store into a faithful pre-46 fixture, and plant
legacy rows honestly on a live store.

A real store below schema 46 never had a memory spine, and migration 46
refuses to discard an authentic spine it finds below 46: that shape is the
``user_version``-downgrade laundering path (see ``docs/MEMORY_SPINE.md``).
Legacy-migration tests that lower ``user_version`` on a fresh store must call
``strip_spine`` first so the store looks like the one it imitates.

Since schema 47 every ``memories`` row needs lineage: a ``BEFORE INSERT``
trigger aborts a raw ``INSERT INTO memories`` (and ``NEW.id`` is -1 inside
that trigger, so an explicit id is mandatory).  Tests that used to seed a
"legacy row without the guarded provenance API" by raw insert use
``seed_legacy_memory_row`` instead: it appends ``memory.imported`` under the
store's key, inserts the row with an allocated explicit id and its lineage,
and writes NO provenance row, which is exactly the legacy shape.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from jarvis import memory_compaction, memory_spine
from jarvis.memory import now_iso

SPINE_OBJECTS = (
    # Typed-invariant compaction (schema 50) comes down first of everything:
    # ``memory_milestones`` holds a foreign key into ``conversations`` and its
    # spans table one into ``memory_milestones``, and both must be gone before
    # the spine tables they were written alongside.  They are dropped by
    # ``memory_compaction.drop_compaction_tables`` in ``strip_spine`` rather
    # than by a statement here, because that helper REFUSES unless both are
    # empty -- see the docstring below for why the graph's rule does not
    # transfer.
    # The learning ladder (schema 49) goes next: both record tables
    # hold foreign keys into ``memory_spine_events``, which migration 46 drops
    # and recreates, and ``ladder_promotions`` holds one into
    # ``memory_calibration_ledger``, so the child table is dropped before its
    # parent.  The four triggers go before the tables because a trigger whose
    # table is gone is dead weight, and because the append-only pair must not
    # outlive the table they guard.  Migration 49 creates all of it again on
    # the way back up; it never drops any of it, since a sealed epoch and a
    # promotion are records and not projections (M4 design 4.3, H-6).
    "DROP TRIGGER IF EXISTS ladder_promotions_require_spine_event",
    "DROP TRIGGER IF EXISTS memory_calibration_ledger_require_spine_event",
    "DROP TRIGGER IF EXISTS memory_calibration_ledger_append_only",
    "DROP TRIGGER IF EXISTS memory_calibration_ledger_no_delete",
    "DROP TABLE IF EXISTS ladder_promotions",
    "DROP TABLE IF EXISTS memory_calibration_ledger",
    "DROP TABLE IF EXISTS ladder_id_sequence",
    # The temporal graph (schema 48) is dropped next: its edges hold foreign
    # keys into ``memory_claims``, which the 46/47 steps rewrite.
    "DROP TABLE IF EXISTS memory_graph_edges",
    "DROP TABLE IF EXISTS memory_graph_entities",
    "DROP TABLE IF EXISTS memory_graph_entity_sequence",
    "DROP TRIGGER IF EXISTS memories_require_spine_event",
    "DROP TRIGGER IF EXISTS memory_claims_require_spine_event",
    "DROP TRIGGER IF EXISTS memory_spine_events_no_delete",
    "DROP TRIGGER IF EXISTS memory_spine_events_redaction_only",
    "DROP TABLE IF EXISTS memory_spine_head",
    "DROP TABLE IF EXISTS memory_spine_events",
    "DROP TABLE IF EXISTS memory_claim_sequence",
    "DROP TABLE IF EXISTS memory_id_sequence",
)


def strip_spine(connection: sqlite3.Connection) -> None:
    """Remove the spine objects so ``user_version`` can be lowered honestly.

    A store below 46 has no lineage on its ``memories`` rows either, so the
    column is nulled when it exists (a stale id from a dropped spine must not
    look like lineage to the re-link step of migration 47).

    The temporal-graph tables (schema 48) go too, for the same reason and in
    the same call: a store below 48 has no graph exactly as a store below 46
    has no spine, and its edges would otherwise hold foreign keys into claim
    rows the 46/47 steps recreate.  Migration 48 rebuilds the projection from
    the live claim rows on the way back up.

    The compaction objects (schema 50) go too, and they are the one part of
    this that can REFUSE.  ``memory_compaction.drop_compaction_tables``
    asserts both tables are empty first, and raises ``CompactionError`` if
    they are not.  The graph rule deliberately does not transfer: migration 48
    may drop the graph because a graph is DERIVED and is rebuilt from the live
    claim rows on the way back up, whereas a compacted span is the ONLY copy
    of the transcript rows it replaced -- ``compact_conversation`` deleted
    them when it wrote the span.  Dropping a non-empty span table would
    destroy operator data that nothing can reconstruct, so a fixture that
    wants a pre-46 store must compact nothing, and one that has compacted
    something is not a legacy store and must say so loudly rather than be
    quietly emptied.

    The learning-ladder objects (schema 49) go too, and for one extra reason
    beyond the foreign keys: migration 49 **refuses to open** a store whose
    spine records ``ladder.*`` events without the record rows those events
    name (``ladder_records_missing``).  A fixture that dropped the spine and
    kept the ladder tables, or kept the spine and dropped the tables, would
    trip that refusal instead of imitating a pre-46 store — which is the same
    trap ``strip_spine`` already exists to keep tests out of.  There is
    deliberately no ``strip_ladder`` and no call-site edit: every caller that
    honestly wants a pre-46 store wants the whole of this.
    """
    memory_compaction.drop_compaction_tables(connection)
    for statement in SPINE_OBJECTS:
        connection.execute(statement)
    columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(memories)")
    }
    if "spine_event_id" in columns:
        connection.execute("UPDATE memories SET spine_event_id=NULL")


def seed_legacy_memory_row(
    memory: Any,
    *,
    kind: str,
    content: str,
    source: str | None,
    family: str | None = None,
    outcome_status: str | None = None,
    reflection_id: int | None = None,
    created_at: str | None = None,
) -> int:
    """Plant a legacy ``memories`` row on a live store: lineage, no provenance.

    Appends ``memory.imported`` (actor ``system``, permission
    ``test:legacy-seed``, scope global, outcome applied) under the store's
    key, then inserts the row with the allocated explicit id and that event
    as its ``spine_event_id``.  Nothing else is written: no
    ``ordinary_memory_provenance`` or ``lesson_provenance`` row, so the row
    is exactly a legacy import without the guarded provenance API.  Returns
    the memory id.  Runs in its own ``BEGIN IMMEDIATE`` unless the caller
    already holds a transaction.
    """
    db = memory.db
    stamp = str(created_at or now_iso())
    fields = {
        "kind": str(kind),
        "content": str(content),
        "source": source,
        "family": family,
        "outcome_status": outcome_status,
        "reflection_id": None if reflection_id is None else int(reflection_id),
    }
    owns_transaction = not db.in_transaction
    if owns_transaction:
        db.execute("BEGIN IMMEDIATE")
    try:
        memory_id = memory_spine.allocate_memory_id(db)
        event_id = memory_spine.append_event(
            db,
            memory._spine_key,
            kind="memory.imported",
            actor="system",
            source="legacy test seed",
            scope="global",
            permission="test:legacy-seed",
            outcome="applied",
            payload=memory_spine.memory_event_payload(
                memory._spine_key, fields, origin=None, eligible=None
            ),
            now=stamp,
            subject_kind="memory",
            subject_id=memory_id,
        )
        db.execute(
            """INSERT INTO memories(
                   id, created_at, kind, content, source, family,
                   outcome_status, reflection_id, spine_event_id
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                memory_id, stamp, fields["kind"], fields["content"], fields["source"],
                fields["family"], fields["outcome_status"], fields["reflection_id"],
                event_id,
            ),
        )
        if owns_transaction:
            db.commit()
    except BaseException:
        if owns_transaction and db.in_transaction:
            db.rollback()
        raise
    return int(memory_id)
