from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from jarvis import memory_spine as spine

# The store's own foreign keys (enforced: setUp turns PRAGMA foreign_keys on).
_CLAIMS_SQL = """CREATE TABLE memory_claims (
    id INTEGER PRIMARY KEY, memory_id INTEGER NOT NULL UNIQUE,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    claim_key TEXT NOT NULL, subject TEXT NOT NULL,
    predicate TEXT NOT NULL, value TEXT NOT NULL,
    value_sha256 TEXT NOT NULL, source TEXT NOT NULL,
    authority TEXT NOT NULL, confidence REAL NOT NULL,
    status TEXT NOT NULL, valid_from TEXT NOT NULL, valid_until TEXT,
    supersedes_id INTEGER, scope TEXT NOT NULL DEFAULT 'global',
    FOREIGN KEY(memory_id) REFERENCES memories(id),
    FOREIGN KEY(supersedes_id) REFERENCES memory_claims(id)
)"""
_EVENTS_SQL = """CREATE TABLE memory_claim_events (
    id INTEGER PRIMARY KEY, claim_id INTEGER NOT NULL, created_at TEXT NOT NULL,
    status TEXT NOT NULL, reason TEXT NOT NULL, related_claim_id INTEGER,
    FOREIGN KEY(claim_id) REFERENCES memory_claims(id),
    FOREIGN KEY(related_claim_id) REFERENCES memory_claims(id)
)"""
_EVIDENCE_SQL = """CREATE TABLE memory_claim_evidence (
    id INTEGER PRIMARY KEY, claim_id INTEGER NOT NULL, created_at TEXT NOT NULL,
    source TEXT NOT NULL, authority TEXT NOT NULL, confidence REAL NOT NULL,
    evidence_sha256 TEXT NOT NULL, UNIQUE(claim_id, evidence_sha256),
    FOREIGN KEY(claim_id) REFERENCES memory_claims(id)
)"""
# The real memories columns (schema 44+), minus the FTS side tables.
_MEMORIES_SQL = """CREATE TABLE memories (
    id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, kind TEXT NOT NULL,
    content TEXT NOT NULL, source TEXT, family TEXT, outcome_status TEXT,
    reflection_id INTEGER, UNIQUE(kind, content)
)"""
_PROVENANCE_SQL = """CREATE TABLE ordinary_memory_provenance (
    memory_id INTEGER PRIMARY KEY, recorded_at TEXT NOT NULL, origin TEXT NOT NULL,
    eligible INTEGER NOT NULL CHECK(eligible IN (0, 1)),
    content_sha256 TEXT NOT NULL, provenance_sha256 TEXT NOT NULL
)"""
_LESSON_PROVENANCE_SQL = """CREATE TABLE lesson_provenance (
    prediction_id INTEGER PRIMARY KEY, memory_id INTEGER NOT NULL,
    reflection_id INTEGER NOT NULL UNIQUE, verified_at TEXT NOT NULL,
    content_sha256 TEXT NOT NULL, provenance_sha256 TEXT
)"""
_CLAIM_SOURCE = "explicit operator project fact"


def _stamp(offset_seconds: int = 0) -> str:
    base = datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc)
    return (base + timedelta(seconds=offset_seconds)).isoformat()


def _claim_content(subject: str, predicate: str, value: str) -> str:
    return f"{subject} {predicate}: {value}"


def _builder(payload: dict, scope: str) -> str:
    """The content builder the store passes (mirrors ``_claim_memory_content``)."""
    return _claim_content(str(payload["subject"]), str(payload["predicate"]), str(payload["value"]))


class MemorySpineModuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = sqlite3.connect(":memory:", isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        for sql in (
            _CLAIMS_SQL, _EVENTS_SQL, _EVIDENCE_SQL, _MEMORIES_SQL, _PROVENANCE_SQL,
            _LESSON_PROVENANCE_SQL,
        ):
            self.db.execute(sql)
        self.key = spine.load_spine_key(None)

    def tearDown(self) -> None:
        self.db.close()

    # --- fixtures -----------------------------------------------------------------

    def _insert_legacy_claim(self, claim_id: int, subject: str, value: str, status: str = "active") -> None:
        self.db.execute(
            "INSERT INTO memories(id, created_at, kind, content, source) VALUES (?, ?, 'claim', ?, ?)",
            (claim_id, _stamp(), _claim_content(subject, "listen port", value), "operator:fixture"),
        )
        self.db.execute(
            """INSERT INTO memory_claims(id, memory_id, created_at, updated_at, claim_key, subject,
               predicate, value, value_sha256, source, authority, confidence, status, valid_from,
               valid_until, supersedes_id, scope)
               VALUES (?, ?, ?, ?, ?, ?, 'listen port', ?, ?, 'fixture', 'operator', 1.0, ?, ?, NULL, NULL, 'project:1')""",
            (
                claim_id, claim_id, _stamp(), _stamp(), f"{subject.casefold()}|listen port",
                subject, value, spine.sha256_hex(value), status, _stamp(),
            ),
        )

    def _insert_legacy_memory(
        self,
        memory_id: int,
        content: str,
        *,
        kind: str = "fact",
        source: str | None = None,
        family: str | None = None,
        outcome_status: str | None = None,
        reflection_id: int | None = None,
        origin: str | None = None,
        eligible: bool | None = None,
        lesson_digest: str | None = None,
    ) -> None:
        self.db.execute(
            """INSERT INTO memories(id, created_at, kind, content, source, family, outcome_status, reflection_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (memory_id, _stamp(), kind, content, source, family, outcome_status, reflection_id),
        )
        if origin is not None:
            self.db.execute(
                """INSERT INTO ordinary_memory_provenance(memory_id, recorded_at, origin, eligible,
                   content_sha256, provenance_sha256) VALUES (?, ?, ?, ?, ?, ?)""",
                (memory_id, _stamp(), origin, int(bool(eligible)), spine.sha256_hex(content), "p" * 64),
            )
        if lesson_digest is not None:
            self.db.execute(
                """INSERT INTO lesson_provenance(prediction_id, memory_id, reflection_id, verified_at,
                   content_sha256, provenance_sha256) VALUES (?, ?, ?, ?, ?, ?)""",
                (memory_id, memory_id, memory_id, _stamp(), spine.sha256_hex(content), lesson_digest),
            )

    def _migrate(self) -> tuple[dict, dict]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            report46 = spine.migrate_memory_spine_v46(self.db, self.key, now=_stamp(1))
            report47 = spine.migrate_memory_spine_v47(self.db, self.key, now=_stamp(1))
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        return report46, report47

    def _rerun_47(self) -> dict:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            report = spine.migrate_memory_spine_v47(self.db, self.key, now=_stamp(2))
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        return report

    def _append(self, **kwargs) -> int:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            event_id = spine.append_event(self.db, self.key, **kwargs)
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        return event_id

    @contextlib.contextmanager
    def _raw(self) -> Iterator[None]:
        """What a writer that bypasses the runtime can do: drop the triggers,
        edit, and put them back."""
        spine.drop_spine_triggers(self.db)
        try:
            yield
        finally:
            spine.create_spine_triggers(self.db)

    @contextlib.contextmanager
    def _without_foreign_keys(self) -> Iterator[None]:
        """A store without foreign-key enforcement (an older connection)."""
        self.db.execute("PRAGMA foreign_keys=OFF")
        try:
            yield
        finally:
            self.db.execute("PRAGMA foreign_keys=ON")

    def _claim_row(
        self, subject: str, value: str, now: str, *, predicate: str = "listen port",
        supersedes: int | None = None,
    ) -> dict:
        return {
            "claim_key": f"{subject.casefold()}|{predicate}", "subject": subject,
            "predicate": predicate, "value": value, "value_sha256": spine.sha256_hex(value),
            "source": _CLAIM_SOURCE, "authority": "operator",
            "confidence": 1.0, "status": "active", "valid_from": now, "valid_until": None,
            "supersedes_id": supersedes,
        }

    def _create_claim(
        self, claim_id: int, subject: str, value: str, now: str, *, supersedes: int | None = None
    ) -> int:
        """The writer's protocol at 47: allocate the claim id, append the
        creating event, insert the backing memory row with that event as
        lineage, then the claim row, its event row, and its evidence."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            allocated = spine.allocate_claim_id(self.db)
            self.assertEqual(allocated, claim_id)
            row = self._claim_row(subject, value, now, supersedes=supersedes)
            event_id = spine.append_event(
                self.db, self.key, kind="claim.created", actor="operator",
                source=row["source"], scope="project:1", permission="autonomous:interactive",
                outcome="applied", subject_kind="claim", subject_id=claim_id,
                payload=spine.claim_event_payload(row, at=now), now=now, conversation_id=7,
            )
            memory_id = spine.allocate_memory_id(self.db)
            self.db.execute(
                """INSERT INTO memories(id, created_at, kind, content, source, spine_event_id)
                   VALUES (?, ?, 'claim', ?, ?, ?)""",
                (
                    memory_id, now, _claim_content(subject, "listen port", value),
                    f"operator:{_CLAIM_SOURCE}", event_id,
                ),
            )
            self.db.execute(
                """INSERT INTO memory_claims(id, memory_id, created_at, updated_at, claim_key, subject,
                   predicate, value, value_sha256, source, authority, confidence, status, valid_from,
                   valid_until, supersedes_id, scope, spine_event_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'project:1', ?)""",
                (
                    claim_id, memory_id, now, now, row["claim_key"], subject, "listen port", value,
                    row["value_sha256"], row["source"], "operator", 1.0, "active", now, None,
                    supersedes, event_id,
                ),
            )
            self.db.execute(
                """INSERT INTO memory_claim_events(claim_id, created_at, status, reason, related_claim_id, spine_event_id)
                   VALUES (?, ?, 'active', 'new strongest claim', ?, ?)""",
                (claim_id, now, supersedes, event_id),
            )
            self.db.execute(
                """INSERT INTO memory_claim_evidence(claim_id, created_at, source, authority, confidence, evidence_sha256)
                   VALUES (?, ?, ?, 'operator', 1.0, ?)""",
                (claim_id, now, _CLAIM_SOURCE, spine.sha256_hex(f"{subject}|{value}")),
            )
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        return event_id

    def _set_status(self, claim_id: int, status: str, now: str, *, reason: str, related: int | None) -> int:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute(
                "UPDATE memory_claims SET status=?, valid_until=?, updated_at=? WHERE id=?",
                (status, now if status == "superseded" else None, now, claim_id),
            )
            row = self.db.execute("SELECT * FROM memory_claims WHERE id=?", (claim_id,)).fetchone()
            kind = {"active": "claim.reasserted", "disputed": "claim.disputed", "superseded": "claim.superseded"}[status]
            event_id = spine.append_event(
                self.db, self.key, kind=kind, actor="operator", source=_CLAIM_SOURCE,
                scope="project:1", permission="p", outcome="applied", subject_kind="claim",
                subject_id=claim_id,
                payload=spine.claim_status_payload(row, at=now, reason=reason, related_claim_id=related),
                now=now,
            )
            self.db.execute(
                """INSERT INTO memory_claim_events(claim_id, created_at, status, reason, related_claim_id, spine_event_id)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (claim_id, now, status, reason, related, event_id),
            )
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        return event_id

    def _create_memory(
        self,
        content: str,
        now: str,
        *,
        kind: str = "fact",
        source: str | None = "operator note",
        origin: str | None = "explicit_operator_memory",
        eligible: bool | None = True,
    ) -> tuple[int, int]:
        """The ordinary writer's protocol: check, allocate, append, insert."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            existing = self.db.execute(
                "SELECT id FROM memories WHERE kind=? AND content=?", (kind, content)
            ).fetchone()
            if existing is not None:
                memory_id = int(existing["id"])
                self.db.execute(
                    """INSERT INTO ordinary_memory_provenance(memory_id, recorded_at, origin, eligible,
                       content_sha256, provenance_sha256) VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(memory_id) DO UPDATE SET origin=excluded.origin, eligible=excluded.eligible""",
                    (memory_id, now, origin, int(bool(eligible)), spine.sha256_hex(content), "p" * 64),
                )
                event_id = spine.append_event(
                    self.db, self.key, kind="memory.reasserted", actor="operator", source=source or "",
                    scope="global", permission="p", outcome="applied", subject_kind="memory",
                    subject_id=memory_id,
                    payload={"origin": origin, "eligible": eligible,
                             "content_digest": spine.content_digest(self.key, content)},
                    now=now,
                )
                self.db.execute("COMMIT")
                return memory_id, event_id
            memory_id = spine.allocate_memory_id(self.db)
            fields = {"kind": kind, "content": content, "source": source, "family": None,
                      "outcome_status": None, "reflection_id": None}
            event_id = spine.append_event(
                self.db, self.key, kind="memory.created", actor="operator", source=source or "",
                scope="global", permission="p", outcome="applied", subject_kind="memory",
                subject_id=memory_id,
                payload=spine.memory_event_payload(self.key, fields, origin=origin, eligible=eligible, at=now),
                now=now,
            )
            self.db.execute(
                "INSERT INTO memories(id, created_at, kind, content, source, spine_event_id) VALUES (?, ?, ?, ?, ?, ?)",
                (memory_id, now, kind, content, source, event_id),
            )
            if origin is not None:
                self.db.execute(
                    """INSERT INTO ordinary_memory_provenance(memory_id, recorded_at, origin, eligible,
                       content_sha256, provenance_sha256) VALUES (?, ?, ?, ?, ?, ?)""",
                    (memory_id, now, origin, int(bool(eligible)), spine.sha256_hex(content), "p" * 64),
                )
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        return memory_id, event_id

    def _create_lesson(self, content: str, now: str, *, reflection_id: int, digest: str) -> tuple[int, int]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            memory_id = spine.allocate_memory_id(self.db)
            fields = {"kind": "lesson", "content": content, "source": f"verified reflection:{reflection_id}",
                      "family": "tool", "outcome_status": "complete", "reflection_id": reflection_id}
            event_id = spine.append_event(
                self.db, self.key, kind="lesson.created", actor="runtime", source=fields["source"],
                scope="global", permission="runtime", outcome="applied", subject_kind="memory",
                subject_id=memory_id,
                payload=spine.memory_event_payload(
                    self.key, fields, origin=None, eligible=None, provenance_sha256=digest, at=now
                ),
                now=now,
            )
            self.db.execute(
                """INSERT INTO memories(id, created_at, kind, content, source, family, outcome_status,
                   reflection_id, spine_event_id) VALUES (?, ?, 'lesson', ?, ?, 'tool', 'complete', ?, ?)""",
                (memory_id, now, content, fields["source"], reflection_id, event_id),
            )
            self.db.execute(
                """INSERT INTO lesson_provenance(prediction_id, memory_id, reflection_id, verified_at,
                   content_sha256, provenance_sha256) VALUES (?, ?, ?, ?, ?, ?)""",
                (reflection_id, memory_id, reflection_id, now, spine.sha256_hex(content), digest),
            )
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        return memory_id, event_id

    def _kinds(self) -> list[str]:
        return [row[0] for row in self.db.execute("SELECT kind FROM memory_spine_events ORDER BY id")]

    def _memory_of(self, claim_id: int) -> int:
        return int(self.db.execute("SELECT memory_id FROM memory_claims WHERE id=?", (claim_id,)).fetchone()[0])

    def _apply(self, plan: dict | None, *, content_builder=_builder) -> dict:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            result = spine.apply_claim_projection(
                self.db, self.key, plan, content_builder=content_builder, now=_stamp(50)
            )
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        return result

    def _snapshot(self) -> tuple:
        claims = [tuple(row) for row in self.db.execute("SELECT * FROM memory_claims ORDER BY id")]
        memories = [tuple(row) for row in self.db.execute("SELECT * FROM memories ORDER BY id")]
        events = int(self.db.execute("SELECT COUNT(*) FROM memory_spine_events").fetchone()[0])
        return claims, memories, events

    # --- migration and backfill ---------------------------------------------

    def test_migration_backfills_claims_and_verifies(self) -> None:
        self._insert_legacy_claim(1, "Kestrel relay", "8080", status="superseded")
        self._insert_legacy_claim(2, "Kestrel relay", "9090")
        report46, report47 = self._migrate()
        self.assertEqual(report46, {"claims_backfilled": 2})
        self.assertEqual(
            report47,
            {"memories_imported": 0, "orphan_claim_rows": 0, "claim_rows_linked": 2,
             "memories_relinked": 0, "events_table_rebuilt": 0},
        )
        self.assertEqual(self._kinds(), ["spine.genesis", "claim.imported", "claim.imported"])
        linked = [row[0] for row in self.db.execute("SELECT spine_event_id FROM memory_claims ORDER BY id")]
        self.assertEqual(linked, [2, 3])
        # A claim's backing row carries the claim's own event, never a memory event.
        backing = [row[0] for row in self.db.execute("SELECT spine_event_id FROM memories ORDER BY id")]
        self.assertEqual(backing, [2, 3])
        self.assertEqual(self.db.execute("SELECT next_id FROM memory_claim_sequence").fetchone()[0], 3)
        self.assertEqual(self.db.execute("SELECT next_id FROM memory_id_sequence").fetchone()[0], 3)
        verification = spine.verify_spine(self.db, self.key)
        self.assertTrue(verification["ok"], verification["problems"])
        self.assertTrue(verification["chain_ok"])
        self.assertEqual(
            (verification["memory_rows"], verification["claim_backing_rows"], verification["memory_events"]),
            (2, 2, 0),
        )
        rebuild = spine.rebuild_claim_projection(self.db, self.key)
        self.assertTrue(rebuild["ok"], rebuild["divergences"])
        self.assertEqual((rebuild["rows_live"], rebuild["rows_rebuilt"]), (2, 2))
        memory_rebuild = spine.rebuild_memory_projection(self.db, self.key)
        self.assertTrue(memory_rebuild["ok"], memory_rebuild["divergences"])
        self.assertEqual((memory_rebuild["rows_live"], memory_rebuild["rows_rebuilt"]), (0, 0))

    def test_migration_47_backfills_memories_and_links_claim_rows(self) -> None:
        self._insert_legacy_claim(1, "Kestrel relay", "9090")
        self._insert_legacy_memory(2, "The relay rack is in row B", source="operator",
                                   origin="explicit_operator_memory", eligible=True)
        self._insert_legacy_memory(3, "A legacy note without provenance")
        self._insert_legacy_memory(4, "Retry the deploy after a lease timeout", kind="lesson",
                                   source="verified reflection:4", family="tool", outcome_status="complete",
                                   reflection_id=4, lesson_digest="a" * 64)
        self._insert_legacy_memory(5, "Orphan box listen port: 1", kind="claim", source="operator:orphan")
        self._insert_legacy_memory(6, "vault note body", kind="vault", source="notes/rack.md",
                                   origin="verified_vault_note", eligible=True)
        _report46, report47 = self._migrate()
        self.assertEqual(
            report47,
            {"memories_imported": 5, "orphan_claim_rows": 1, "claim_rows_linked": 1,
             "memories_relinked": 0, "events_table_rebuilt": 0},
        )
        self.assertEqual(self._kinds(), ["spine.genesis", "claim.imported"] + ["memory.imported"] * 5)
        imported = self.db.execute(
            """SELECT subject_id, actor, permission, scope, payload_json FROM memory_spine_events
               WHERE kind='memory.imported' ORDER BY id"""
        ).fetchall()
        self.assertEqual([row["subject_id"] for row in imported], [2, 3, 4, 5, 6])
        self.assertEqual({(row["actor"], row["permission"], row["scope"]) for row in imported},
                         {("system", "migration", "global")})
        payloads = {row["subject_id"]: json.loads(row["payload_json"]) for row in imported}
        self.assertEqual((payloads[2]["origin"], payloads[2]["eligible"]), ("explicit_operator_memory", True))
        self.assertEqual((payloads[3]["origin"], payloads[3]["eligible"]), (None, None))
        self.assertEqual(payloads[4]["kind"], "lesson")
        self.assertEqual(payloads[4]["provenance_sha256"], "a" * 64)
        self.assertEqual(payloads[4]["reflection_id"], 4)
        self.assertEqual(payloads[5]["kind"], "claim")
        self.assertEqual(payloads[6]["content_length"], len("vault note body"))
        self.assertEqual(payloads[6]["content_digest"], spine.content_digest(self.key, "vault note body"))
        # Digest-only: no content, and no unkeyed digest of it, is on the spine.
        dump = " ".join(str(row[0] or "") for row in self.db.execute("SELECT payload_json FROM memory_spine_events"))
        for text in ("row B", "legacy note", "lease timeout", "Orphan box", "vault note body"):
            self.assertNotIn(text, dump)
        self.assertNotIn(spine.sha256_hex("vault note body"), dump)
        lineage = {row[0]: row[1] for row in self.db.execute("SELECT id, spine_event_id FROM memories")}
        self.assertEqual(lineage[1], 2)  # the claim's event
        self.assertEqual(sorted(lineage[i] for i in (2, 3, 4, 5, 6)), [3, 4, 5, 6, 7])
        self.assertEqual(self.db.execute("SELECT next_id FROM memory_id_sequence").fetchone()[0], 7)
        verification = spine.verify_spine(self.db, self.key)
        self.assertTrue(verification["ok"], verification["problems"])
        self.assertEqual(
            (verification["memory_rows"], verification["claim_backing_rows"], verification["memory_events"]),
            (6, 1, 5),
        )
        memory_rebuild = spine.rebuild_memory_projection(self.db, self.key)
        self.assertTrue(memory_rebuild["ok"], memory_rebuild["divergences"])
        self.assertEqual((memory_rebuild["rows_live"], memory_rebuild["rows_rebuilt"]), (5, 5))
        claim_rebuild = spine.rebuild_claim_projection(self.db, self.key, content_builder=_builder)
        self.assertTrue(claim_rebuild["ok"], claim_rebuild["divergences"])
        # The trigger is in place: a lineage-less row can no longer be inserted.
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT INTO memories(id, created_at, kind, content) VALUES (99, ?, 'fact', 'x')", (_stamp(),))

    def test_migration_47_is_idempotent_and_relinks_only_matching_digests(self) -> None:
        self._insert_legacy_claim(1, "Kestrel relay", "9090")
        self._insert_legacy_memory(2, "The relay rack is in row B", origin="explicit_operator_memory", eligible=True)
        self._insert_legacy_memory(3, "A legacy note without provenance")
        self._migrate()
        events_before = self.db.execute("SELECT COUNT(*) FROM memory_spine_events").fetchone()[0]
        self.assertEqual(
            self._rerun_47(),
            {"memories_imported": 0, "orphan_claim_rows": 0, "claim_rows_linked": 0,
             "memories_relinked": 0, "events_table_rebuilt": 0},
        )
        # A downgrade that lost the lineage column re-links by digest, appending nothing.
        self.db.execute("UPDATE memories SET spine_event_id=NULL")
        report = self._rerun_47()
        self.assertEqual((report["memories_relinked"], report["claim_rows_linked"], report["memories_imported"]), (2, 1, 0))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM memory_spine_events").fetchone()[0], events_before)
        self.assertTrue(spine.verify_spine(self.db, self.key)["ok"])
        # A lineage id that names no admissible event is treated as unlinked.
        self.db.execute("UPDATE memories SET spine_event_id=1 WHERE id=3")
        self.assertEqual(self._rerun_47()["memories_relinked"], 1)
        self.assertTrue(spine.verify_spine(self.db, self.key)["ok"])
        # Edited content under a nulled lineage is the laundering shape: refused.
        self.db.execute("UPDATE memories SET spine_event_id=NULL, content='edited out of band' WHERE id=3")
        self.db.execute("BEGIN IMMEDIATE")
        with self.assertRaises(spine.SpineError) as refused:
            spine.migrate_memory_spine_v47(self.db, self.key, now=_stamp(3))
        self.db.execute("ROLLBACK")
        self.assertIn("content differs", str(refused.exception))
        self.assertEqual(refused.exception.code, "digest_mismatch")
        self.db.execute("UPDATE memories SET content='A legacy note without provenance' WHERE id=3")
        self.assertEqual(self._rerun_47()["memories_relinked"], 1)
        # An id named by a deletion receipt must never come back.
        self._append(
            kind="memory.deleted", actor="runtime", source="vault", scope="global", permission="runtime",
            outcome="applied", subject_kind="memory", subject_id=3,
            payload=spine.memory_deleted_payload(self.key, [(3, "A legacy note without provenance")], reason="test"),
            now=_stamp(4),
        )
        self.db.execute("UPDATE memories SET spine_event_id=NULL WHERE id=3")
        self.db.execute("BEGIN IMMEDIATE")
        with self.assertRaises(spine.SpineError) as refused:
            spine.migrate_memory_spine_v47(self.db, self.key, now=_stamp(5))
        self.db.execute("ROLLBACK")
        self.assertIn("deleted id", str(refused.exception))
        self.assertEqual(refused.exception.code, "deleted_id_live")
        with self._raw():
            self.db.execute("DELETE FROM memories WHERE id=3")
            self.db.execute("DELETE FROM ordinary_memory_provenance WHERE memory_id=3")
        # More than one creating event for an id is refused too.
        fields = {"kind": "fact", "content": "The relay rack is in row B", "source": None,
                  "family": None, "outcome_status": None, "reflection_id": None}
        self._append(
            kind="memory.created", actor="operator", source="s", scope="global", permission="p",
            outcome="applied", subject_kind="memory", subject_id=2,
            payload=spine.memory_event_payload(self.key, fields, origin=None, eligible=None), now=_stamp(6),
        )
        self.db.execute("UPDATE memories SET spine_event_id=NULL WHERE id=2")
        self.db.execute("BEGIN IMMEDIATE")
        with self.assertRaises(spine.SpineError) as refused:
            spine.migrate_memory_spine_v47(self.db, self.key, now=_stamp(7))
        self.db.execute("ROLLBACK")
        self.assertIn("more than one creating", str(refused.exception))
        self.assertEqual(refused.exception.code, "duplicate_creating_event")

    def test_migration_47_refuses_an_inauthentic_head(self) -> None:
        self._insert_legacy_claim(1, "Kestrel relay", "9090")
        self._migrate()
        other = spine.load_spine_key(None)
        self.db.execute("BEGIN IMMEDIATE")
        with self.assertRaises(spine.SpineError):
            spine.migrate_memory_spine_v47(self.db, other, now=_stamp(3))
        self.db.execute("ROLLBACK")
        head = self.db.execute("SELECT head_mac FROM memory_spine_head").fetchone()[0]
        self.db.execute("UPDATE memory_spine_head SET head_mac=?", ("0" * 64,))
        self.db.execute("BEGIN IMMEDIATE")
        with self.assertRaises(spine.SpineError):
            spine.migrate_memory_spine_v47(self.db, self.key, now=_stamp(3))
        self.db.execute("ROLLBACK")
        self.db.execute("UPDATE memory_spine_head SET head_mac=?", (head,))
        self.assertEqual(self._rerun_47()["memories_imported"], 0)

    def test_migration_47_widens_a_schema_46_events_table_in_place(self) -> None:
        self._insert_legacy_claim(1, "Kestrel relay", "9090")
        self._insert_legacy_memory(2, "The relay rack is in row B")
        # Build the store as slice 1 wrote it: the closed kind list of 46.
        self.db.execute("BEGIN IMMEDIATE")
        spine.migrate_memory_spine_v46(self.db, self.key, now=_stamp(1))
        rows = [tuple(row) for row in self.db.execute("SELECT * FROM memory_spine_events ORDER BY id")]
        spine.drop_spine_triggers(self.db)
        self.db.execute("DROP TABLE memory_spine_events")
        self.db.execute(
            spine._EVENT_TABLE_SQL
            .replace(",'memory.imported','memory.created','memory.reasserted',\n        'memory.updated','memory.deleted','lesson.created'", "")
            .replace("'projection','proposal','memory'", "'projection','proposal'")
        )
        columns = ", ".join(spine._EVENT_COLUMNS)
        self.db.executemany(
            f"INSERT INTO memory_spine_events({columns}) VALUES ({', '.join('?' for _ in spine._EVENT_COLUMNS)})",
            rows,
        )
        for name in spine._V46_TRIGGERS:
            self.db.execute(spine._TRIGGER_SQL[name])
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(
                "INSERT INTO memory_spine_events(id, created_at, kind, actor, source, scope, permission, outcome, payload_sha256, prev_sha256, event_sha256) "
                "VALUES (99, 'x', 'memory.created', 'system', 's', 'global', 'p', 'applied', ?, ?, ?)",
                ("0" * 64, "0" * 64, "0" * 64),
            )
        report = spine.migrate_memory_spine_v47(self.db, self.key, now=_stamp(2))
        self.db.execute("COMMIT")
        self.assertEqual((report["events_table_rebuilt"], report["memories_imported"]), (1, 1))
        self.assertEqual(
            [tuple(row) for row in self.db.execute("SELECT * FROM memory_spine_events ORDER BY id")][: len(rows)],
            rows,
        )
        verification = spine.verify_spine(self.db, self.key)
        self.assertTrue(verification["ok"], verification["problems"])
        self.assertEqual(self._rerun_47()["events_table_rebuilt"], 0)

    def test_claim_rows_require_a_creating_event(self) -> None:
        self._migrate()
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(
                """INSERT INTO memory_claims(id, memory_id, created_at, updated_at, claim_key, subject,
                   predicate, value, value_sha256, source, authority, confidence, status, valid_from)
                   VALUES (9, 9, ?, ?, 'k', 's', 'p', 'v', ?, 'src', 'operator', 1.0, 'active', ?)""",
                (_stamp(), _stamp(), spine.sha256_hex("v"), _stamp()),
            )
        # A creating event that names a different claim id is not lineage either.
        event_id = self._append(
            kind="claim.created", actor="operator", source="s", scope="global",
            permission="p", outcome="applied", subject_kind="claim", subject_id=99,
            payload=spine.claim_event_payload(self._claim_row("s", "v", _stamp()), at=_stamp()),
            now=_stamp(2),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(
                """INSERT INTO memory_claims(id, memory_id, created_at, updated_at, claim_key, subject,
                   predicate, value, value_sha256, source, authority, confidence, status, valid_from, spine_event_id)
                   VALUES (9, 9, ?, ?, 'k', 's', 'p', 'v', ?, 'src', 'operator', 1.0, 'active', ?, ?)""",
                (_stamp(), _stamp(), spine.sha256_hex("v"), _stamp(), event_id),
            )

    def test_memory_rows_require_a_creating_event(self) -> None:
        self._migrate()
        fields = {"kind": "fact", "content": "x", "source": None, "family": None,
                  "outcome_status": None, "reflection_id": None}
        payload = spine.memory_event_payload(self.key, fields, origin=None, eligible=None)
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT INTO memories(id, created_at, kind, content) VALUES (50, ?, 'fact', 'x')", (_stamp(),))
        event_id = self._append(
            kind="memory.created", actor="operator", source="s", scope="global", permission="p",
            outcome="applied", subject_kind="memory", subject_id=51, payload=payload, now=_stamp(2),
        )
        # Wrong subject id, an implicit id, and a non-creating memory event all abort.
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT INTO memories(id, created_at, kind, content, spine_event_id) VALUES (52, ?, 'fact', 'x', ?)", (_stamp(), event_id))
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT INTO memories(created_at, kind, content, spine_event_id) VALUES (?, 'fact', 'x', ?)", (_stamp(), event_id))
        reassert = self._append(
            kind="memory.reasserted", actor="operator", source="s", scope="global", permission="p",
            outcome="noop", subject_kind="memory", subject_id=51,
            payload={"origin": None, "eligible": None, "content_digest": payload["content_digest"]}, now=_stamp(3),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT INTO memories(id, created_at, kind, content, spine_event_id) VALUES (51, ?, 'fact', 'x', ?)", (_stamp(), reassert))
        self.db.execute("INSERT INTO memories(id, created_at, kind, content, spine_event_id) VALUES (51, ?, 'fact', 'x', ?)", (_stamp(), event_id))
        # A claim's backing row is admitted on the claim's creating event; an ordinary row is not.
        claim_event = self._append(
            kind="claim.created", actor="operator", source="s", scope="project:1", permission="p",
            outcome="applied", subject_kind="claim", subject_id=77,
            payload=spine.claim_event_payload(self._claim_row("Kestrel relay", "1", _stamp()), at=_stamp()),
            now=_stamp(4),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT INTO memories(id, created_at, kind, content, spine_event_id) VALUES (53, ?, 'fact', 'y', ?)", (_stamp(), claim_event))
        self.db.execute("INSERT INTO memories(id, created_at, kind, content, spine_event_id) VALUES (53, ?, 'claim', 'y', ?)", (_stamp(), claim_event))
        # The same event cannot back two memories rows.
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT INTO memories(id, created_at, kind, content, spine_event_id) VALUES (54, ?, 'claim', 'z', ?)", (_stamp(), claim_event))

    # --- append-only and the chain ------------------------------------------

    def test_events_are_append_only_and_chained(self) -> None:
        self._migrate()
        first = self._create_claim(1, "Kestrel relay", "9090", _stamp(2))
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("DELETE FROM memory_spine_events WHERE id=?", (first,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("UPDATE memory_spine_events SET actor='model' WHERE id=?", (first,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(
                "UPDATE memory_spine_events SET payload_json='{}' WHERE id=?", (first,)
            )
        verification = spine.verify_spine(self.db, self.key)
        self.assertTrue(verification["ok"], verification["problems"])
        # A writer that bypasses the trigger (drops it) is still caught by the
        # keyed digest and by the trigger-presence check.
        self.db.execute("DROP TRIGGER memory_spine_events_redaction_only")
        self.db.execute("UPDATE memory_spine_events SET outcome='rejected' WHERE id=?", (first,))
        verification = spine.verify_spine(self.db, self.key)
        self.assertFalse(verification["ok"])
        self.assertFalse(verification["triggers_ok"])
        self.assertFalse(verification["chain_ok"])
        self.assertTrue(any("keyed digest mismatch" in text for text in verification["problems"]))
        # A different key never verifies the chain.
        self.db.execute("UPDATE memory_spine_events SET outcome='applied' WHERE id=?", (first,))
        other = spine.verify_spine(self.db, spine.load_spine_key(None))
        self.assertFalse(other["ok"])

    def test_removed_tail_is_detected_and_blocks_further_appends(self) -> None:
        self._migrate()
        self._create_claim(1, "Kestrel relay", "9090", _stamp(2))
        second = self._append(
            kind="proposal.not_stored", actor="runtime", source="s", scope="project:1",
            permission="p", outcome="applied", payload={"command_sha256": "a" * 64},
            now=_stamp(3),
        )
        # Drop the triggers (a raw writer can) and remove the newest event:
        # the chain is still self-consistent, but the keyed head names event 3.
        spine.drop_spine_triggers(self.db)
        self.db.execute("DELETE FROM memory_spine_events WHERE id=?", (second,))
        verification = spine.verify_spine(self.db, self.key)
        self.assertFalse(verification["ok"])
        self.assertFalse(verification["head_ok"])
        self.assertTrue(any("tail removed" in text for text in verification["problems"]))
        # The runtime refuses to chain onto the truncated tail (it would
        # otherwise re-create a clean head over the laundered history).
        with self.assertRaises(spine.SpineError):
            self._append(
                kind="proposal.not_stored", actor="runtime", source="s", scope="project:1",
                permission="p", outcome="applied", payload={"command_sha256": "b" * 64},
                now=_stamp(4),
            )
        # Deleting the head row does not help either.
        self.db.execute("DELETE FROM memory_spine_head")
        with self.assertRaises(spine.SpineError):
            self._append(
                kind="proposal.not_stored", actor="runtime", source="s", scope="project:1",
                permission="p", outcome="applied", payload={"command_sha256": "b" * 64},
                now=_stamp(4),
            )
        # An emptied spine never verifies.
        self.db.execute("DELETE FROM memory_spine_events")
        spine.create_spine_triggers(self.db)
        verification = spine.verify_spine(self.db, self.key)
        self.assertFalse(verification["ok"])
        self.assertTrue(any("no genesis" in text for text in verification["problems"]))

    def test_bogus_redaction_referents_are_detected(self) -> None:
        self._migrate()
        first = self._create_claim(1, "Kestrel relay", "9090", _stamp(2))
        self.db.execute("BEGIN IMMEDIATE")
        proposal = spine.append_event(
            self.db, self.key, kind="proposal.not_stored", actor="runtime", source="s",
            scope="project:1", permission="p", outcome="applied",
            payload={"command_sha256": "b" * 64}, now=_stamp(4),
        )
        tombstone = spine.append_event(
            self.db, self.key, kind="claim.tombstoned", actor="operator", source="erase",
            scope="project:1", permission="p", outcome="applied",
            payload={"at": _stamp(5), "claim_key": "other|key", "removed_claim_ids": [99]},
            now=_stamp(5),
        )
        self.db.execute("COMMIT")
        spine.drop_spine_triggers(self.db)
        # A redaction citing a non-tombstone.
        self.db.execute(
            "UPDATE memory_spine_events SET payload_json=NULL, payload_salt=NULL, redacted_by_event_id=? WHERE id=?",
            (proposal, first),
        )
        verification = spine.verify_spine(self.db, self.key)
        self.assertTrue(any("without a valid later tombstone" in text for text in verification["problems"]))
        # A redaction citing a tombstone that names another claim.
        self.db.execute(
            "UPDATE memory_spine_events SET redacted_by_event_id=? WHERE id=?", (tombstone, first)
        )
        verification = spine.verify_spine(self.db, self.key)
        self.assertTrue(any("does not name its claim" in text for text in verification["problems"]))

    def test_migration_refuses_to_discard_an_authentic_spine(self) -> None:
        self._migrate()
        self._create_claim(1, "Kestrel relay", "9090", _stamp(2))
        self._create_memory("An aside kept across the strip", _stamp(3), origin=None, eligible=None)
        # A manual user_version downgrade would re-run the migration; with an
        # authentic head under the same key it must refuse rather than launder
        # the projection into a clean chain.
        self.db.execute("BEGIN IMMEDIATE")
        with self.assertRaises(spine.SpineError):
            spine.migrate_memory_spine_v46(self.db, self.key, now=_stamp(9))
        self.db.execute("ROLLBACK")
        # A wrong key (or a tampered head) is not authentic: the migration
        # proceeds and the old spine is discarded, which verify then reports
        # as a key mismatch against the genesis fingerprint of the new chain.
        other = spine.load_spine_key(None)
        self.db.execute("BEGIN IMMEDIATE")
        spine.migrate_memory_spine_v46(self.db, other, now=_stamp(9))
        report = spine.migrate_memory_spine_v47(self.db, other, now=_stamp(9))
        self.db.execute("COMMIT")
        # The claim's backing row is linked to the re-imported claim event, and
        # the aside is imported afresh: the stripped spine records no memories,
        # so this is the legacy path, not the downgrade-laundering one.
        self.assertEqual((report["claim_rows_linked"], report["memories_imported"]), (1, 1))
        verification = spine.verify_spine(self.db, other)
        self.assertTrue(verification["ok"], verification["problems"])
        mismatch = spine.verify_spine(self.db, self.key)
        self.assertFalse(mismatch["ok"])
        self.assertFalse(mismatch["key_ok"])
        self.assertTrue(any("key mismatch" in text for text in mismatch["problems"]))

    def test_sequence_tamper_is_refused_and_reported(self) -> None:
        self._migrate()
        self._create_claim(1, "Kestrel relay", "9090", _stamp(2))
        self.db.execute("UPDATE memory_claim_sequence SET next_id=1")
        with self.assertRaises(spine.SpineError):
            spine.allocate_claim_id(self.db)
        verification = spine.verify_spine(self.db, self.key)
        self.assertTrue(any("claim sequence is behind" in text for text in verification["problems"]))
        self.assertFalse(verification["chain_ok"])
        self.db.execute("UPDATE memory_claim_sequence SET next_id=2")
        self.assertEqual(spine.allocate_claim_id(self.db), 2)
        self.db.execute("UPDATE memory_id_sequence SET next_id=1")
        with self.assertRaises(spine.SpineError):
            spine.allocate_memory_id(self.db)
        verification = spine.verify_spine(self.db, self.key)
        self.assertFalse(verification["memory_sequence_ok"])
        self.assertTrue(any("memory sequence is behind" in text for text in verification["problems"]))
        self.db.execute("UPDATE memory_id_sequence SET next_id=2")
        self.assertEqual(spine.allocate_memory_id(self.db), 2)

    def test_unredaction_from_a_backup_is_detected(self) -> None:
        self._migrate()
        self._create_claim(1, "Kestrel relay", "9090", _stamp(2))
        memory_id = self._memory_of(1)
        backup = self.db.execute(
            "SELECT id, payload_json, payload_salt FROM memory_spine_events WHERE id=2"
        ).fetchone()
        self.db.execute("BEGIN IMMEDIATE")
        self.db.execute("DELETE FROM memory_claim_events WHERE claim_id=1")
        self.db.execute("DELETE FROM memory_claim_evidence WHERE claim_id=1")
        self.db.execute("DELETE FROM memory_claims WHERE id=1")
        self.db.execute("DELETE FROM memories WHERE id=?", (memory_id,))
        targets = spine.events_to_redact(self.db, "project:1", "kestrel relay|listen port")
        tombstone = spine.append_event(
            self.db, self.key, kind="claim.tombstoned", actor="operator", source="erase",
            scope="project:1", permission="p", outcome="applied",
            payload={"at": _stamp(4), "claim_key": "kestrel relay|listen port",
                     "removed_claim_ids": [1], "redacted_event_ids": targets,
                     "removed_memory_ids": [memory_id]},
            now=_stamp(4),
        )
        spine.redact_claim_key_events(self.db, "project:1", "kestrel relay|listen port", tombstone)
        self.db.execute("COMMIT")
        self.assertTrue(spine.verify_spine(self.db, self.key)["ok"])
        spine.drop_spine_triggers(self.db)
        self.db.execute(
            "UPDATE memory_spine_events SET payload_json=?, payload_salt=?, redacted_by_event_id=NULL WHERE id=?",
            (backup["payload_json"], backup["payload_salt"], backup["id"]),
        )
        verification = spine.verify_spine(self.db, self.key)
        self.assertFalse(verification["ok"])
        self.assertTrue(any("not redacted by tombstone" in text for text in verification["problems"]))
        self.assertIn("9090", str(backup["payload_json"]))

    def test_clock_is_monotonic_and_ids_are_explicit(self) -> None:
        self._migrate()
        earlier = _stamp(-3600)
        event_id = self._append(
            kind="proposal.not_stored", actor="runtime", source="s", scope="project:1",
            permission="p", outcome="applied", payload={"command_sha256": "a" * 64},
            now=earlier,
        )
        stamp = self.db.execute(
            "SELECT created_at FROM memory_spine_events WHERE id=?", (event_id,)
        ).fetchone()[0]
        self.assertGreater(stamp, _stamp(1))
        self.assertEqual(event_id, 2)
        with self.assertRaises(spine.SpineError):
            self._append(
                kind="not.a.kind", actor="runtime", source="s", scope="global",
                permission="p", outcome="applied", payload={}, now=_stamp(5),
            )
        with self.assertRaises(spine.SpineError):
            spine.append_event(
                self.db, self.key, kind="proposal.not_stored", actor="runtime", source="s",
                scope="global", permission="p", outcome="applied", payload={}, now=_stamp(5),
            )  # outside a transaction

    def test_payload_schemas_and_size_are_closed(self) -> None:
        self._migrate()
        with self.assertRaises(spine.SpineError):
            self._append(
                kind="claim.created", actor="operator", source="s", scope="global",
                permission="p", outcome="applied", subject_kind="claim", subject_id=1,
                payload={"claim_key": "k"}, now=_stamp(2),
            )
        with self.assertRaises(spine.SpineError):
            self._append(
                kind="claim.superseded", actor="operator", source="s", scope="global",
                permission="p", outcome="applied", payload={"at": "x", "claim_key": "k", "claim_id": 1,
                                                            "status": "superseded", "extra": 1},
                now=_stamp(2),
            )
        with self.assertRaises(spine.SpineError):
            self._append(
                kind="proposal.not_stored", actor="runtime", source="s", scope="global",
                permission="p", outcome="applied", payload={"blob": "x" * (17 * 1024)},
                now=_stamp(2),
            )

    def test_memory_payload_schemas_are_closed_and_digest_only(self) -> None:
        self._migrate()
        fields = {"kind": "fact", "content": "The relay rack is in row B", "source": "operator",
                  "family": None, "outcome_status": None, "reflection_id": None}
        good = spine.memory_event_payload(self.key, fields, origin="explicit_operator_memory", eligible=True)
        self.assertEqual(sorted(good), sorted(spine._MEMORY_REQUIRED_KEYS))
        self.assertNotIn("content", good)
        self.assertEqual(good["content_length"], len(fields["content"]))

        def refused(kind: str, payload: dict, **extra) -> None:
            with self.assertRaises(spine.SpineError):
                self._append(kind=kind, actor="operator", source="s", scope="global", permission="p",
                             outcome="applied", subject_kind="memory", subject_id=1, payload=payload,
                             now=_stamp(2), **extra)

        refused("memory.created", {**good, "content": "The relay rack is in row B"})
        refused("memory.created", {name: value for name, value in good.items() if name != "origin"})
        refused("memory.created", {**good, "content_digest": "zz"})
        refused("memory.created", {**good, "content_digest": spine.sha256_hex("x").upper()})
        refused("memory.created", {**good, "eligible": "yes"})
        refused("memory.created", {**good, "content_length": -1})
        refused("memory.created", {**good, "content_length": True})
        refused("memory.created", {**good, "reflection_id": "4"})
        refused("memory.created", {**good, "provenance_sha256": "short"})
        refused("lesson.created", good)  # provenance_sha256 required
        refused("lesson.created", {**good, "provenance_sha256": None})
        refused("memory.reasserted", {"origin": None, "eligible": None,
                                      "content_digest": good["content_digest"], "extra": 1})
        refused("memory.reasserted", {"origin": None, "eligible": None})
        refused("memory.deleted", {"ids": [1, 2], "content_digests": [good["content_digest"]], "reason": "r"})
        refused("memory.deleted", {"ids": [], "content_digests": [], "reason": "r"})
        refused("memory.deleted", {"ids": [1], "content_digests": [good["content_digest"]], "reason": "r",
                                   "content": "x"})
        refused("memory.deleted", {"ids": ["1"], "content_digests": [good["content_digest"]], "reason": "r"})
        refused("projection.rebuilt", {"rows_before": "x", "rows_after": 1, "divergences_fixed": 0,
                                       "removed_ids": []}, )
        refused("projection.rebuilt", {"rows_before": 1, "rows_after": 1, "divergences_fixed": 0,
                                       "removed_ids": ["a"]})
        refused("claim.tombstoned", {"at": "x", "claim_key": "k", "removed_claim_ids": [1],
                                     "removed_memory_ids": ["a"]})
        # Valid shapes are accepted, and the helpers produce them.
        self._append(kind="memory.created", actor="operator", source="s", scope="global", permission="p",
                     outcome="applied", subject_kind="memory", subject_id=1,
                     payload={**good, "at": _stamp(2)}, now=_stamp(2))
        self._append(kind="lesson.created", actor="runtime", source="s", scope="global", permission="runtime",
                     outcome="applied", subject_kind="memory", subject_id=2,
                     payload=spine.memory_event_payload(self.key, {**fields, "kind": "lesson"}, origin=None,
                                                        eligible=None, provenance_sha256="b" * 64),
                     now=_stamp(3))
        # One deletion receipt names at most MEMORY_DELETED_MAX_IDS rows and stays under the cap.
        self.assertEqual(spine.MEMORY_DELETED_MAX_IDS, 128)
        bounded = spine.memory_deleted_payload(
            self.key, [(i, str(i)) for i in range(1, spine.MEMORY_DELETED_MAX_IDS + 1)], reason="r"
        )
        self.assertLess(len(spine.canonical(bounded).encode("utf-8")), spine.MAX_PAYLOAD_BYTES)
        spine.validate_payload("memory.deleted", bounded)
        refused("memory.deleted", spine.memory_deleted_payload(
            self.key, [(i, str(i)) for i in range(1, spine.MEMORY_DELETED_MAX_IDS + 2)], reason="r"
        ))
        deleted = spine.memory_deleted_payload(self.key, [(1, "x"), {"id": 2, "content": "y"}], reason="vault re-index")
        self.assertEqual(deleted["ids"], [1, 2])
        self.assertEqual(deleted["content_digests"], [spine.content_digest(self.key, "x"), spine.content_digest(self.key, "y")])
        self._append(kind="memory.deleted", actor="runtime", source="s", scope="global", permission="runtime",
                     outcome="applied", subject_kind="memory", subject_id=2, payload=deleted, now=_stamp(4))
        self._append(kind="claim.tombstoned", actor="operator", source="erase", scope="global", permission="p",
                     outcome="applied", payload={"at": "x", "claim_key": "k", "removed_claim_ids": [1],
                                                 "removed_memory_ids": [7]}, now=_stamp(5))
        self.assertEqual(spine.memory_sequence_floor(self.db), 7)
        # The content digest is keyed and domain-tagged: not sha256, not an event MAC.
        content = "9090"
        digest = spine.content_digest(self.key, content)
        self.assertEqual(digest, hmac.new(self.key, b"jarvis-memory-content\0" + content.encode("utf-8"),
                                          hashlib.sha256).hexdigest())
        self.assertNotEqual(digest, spine.sha256_hex(content))
        self.assertNotEqual(digest, spine.content_digest(spine.load_spine_key(None), content))
        self.assertNotEqual(digest, hmac.new(self.key, content.encode("utf-8"), hashlib.sha256).hexdigest())

    # --- verification of memory lineage ------------------------------------------

    def test_verify_catches_memory_lineage_problems(self) -> None:
        self._migrate()
        memory_a, event_a = self._create_memory("The relay rack is in row B", _stamp(2))
        self._create_claim(1, "Kestrel relay", "9090", _stamp(3))
        backing = self._memory_of(1)
        clean = spine.verify_spine(self.db, self.key)
        self.assertTrue(clean["ok"], clean["problems"])
        # A lineage-less row: a projection fault (chain intact).
        with self._raw():
            self.db.execute("INSERT INTO memories(id, created_at, kind, content) VALUES (90, ?, 'fact', 'raw')", (_stamp(),))
        report = spine.verify_spine(self.db, self.key)
        self.assertFalse(report["ok"])
        self.assertFalse(report["memory_lineage_ok"])
        self.assertIn("memory 90: no creating spine event", report["problems"])
        # An id above the sequence also endangers the allocator: a store fault, not lineage.
        self.assertFalse(report["memory_sequence_ok"])
        self.assertFalse(report["chain_ok"])
        self.db.execute("UPDATE memory_id_sequence SET next_id=91")
        report = spine.verify_spine(self.db, self.key)
        self.assertEqual(report["problems"], ["memory 90: no creating spine event"])
        self.assertTrue(report["chain_ok"])
        self.db.execute("DELETE FROM memories WHERE id=90")
        # A deleted id that comes back.
        memory_b, _event_b = self._create_memory("A note to be removed", _stamp(4), origin=None, eligible=None)
        self.db.execute("BEGIN IMMEDIATE")
        self.db.execute("DELETE FROM memories WHERE id=?", (memory_b,))
        spine.append_event(
            self.db, self.key, kind="memory.deleted", actor="runtime", source="vault", scope="global",
            permission="runtime", outcome="applied", subject_kind="memory", subject_id=memory_b,
            payload=spine.memory_deleted_payload(self.key, [(memory_b, "A note to be removed")], reason="stale"),
            now=_stamp(5),
        )
        self.db.execute("COMMIT")
        self.assertTrue(spine.verify_spine(self.db, self.key)["ok"])
        self.assertGreater(self.db.execute("SELECT next_id FROM memory_id_sequence").fetchone()[0], memory_b)
        with self._raw():
            self.db.execute(
                "INSERT INTO memories(id, created_at, kind, content, spine_event_id) VALUES (?, ?, 'fact', 'A note to be removed', ?)",
                (memory_b, _stamp(), _event_b),
            )
        report = spine.verify_spine(self.db, self.key)
        self.assertIn(f"memory {memory_b}: live row with a deleted id", report["problems"])
        self.assertTrue(report["chain_ok"])
        self.db.execute("DELETE FROM memories WHERE id=?", (memory_b,))
        # A backing row that does not carry its claim's event, then one that is missing.
        with self._raw():
            self.db.execute("UPDATE memories SET spine_event_id=NULL WHERE id=?", (backing,))
        report = spine.verify_spine(self.db, self.key)
        self.assertIn(f"memory {backing}: lineage is not its claim's event", report["problems"])
        self.assertTrue(report["chain_ok"])
        claim_event = self.db.execute("SELECT spine_event_id FROM memory_claims WHERE id=1").fetchone()[0]
        self.db.execute("UPDATE memories SET spine_event_id=? WHERE id=?", (claim_event, backing))
        self.assertTrue(spine.verify_spine(self.db, self.key)["ok"])
        row = self.db.execute("SELECT * FROM memories WHERE id=?", (backing,)).fetchone()
        with self._without_foreign_keys():
            self.db.execute("DELETE FROM memories WHERE id=?", (backing,))
        report = spine.verify_spine(self.db, self.key)
        self.assertIn("claim 1: backing memory row missing", report["problems"])
        self.assertTrue(report["chain_ok"])
        self.db.execute(
            "INSERT INTO memories(id, created_at, kind, content, source, spine_event_id) VALUES (?, ?, 'claim', ?, ?, ?)",
            (row["id"], row["created_at"], row["content"], row["source"], row["spine_event_id"]),
        )
        self.assertTrue(spine.verify_spine(self.db, self.key)["ok"])
        # A tampered memories trigger.
        self.db.execute("DROP TRIGGER memories_require_spine_event")
        self.db.execute(
            "CREATE TRIGGER memories_require_spine_event BEFORE INSERT ON memories WHEN 0 "
            "BEGIN SELECT RAISE(ABORT, 'never'); END"
        )
        report = spine.verify_spine(self.db, self.key)
        self.assertFalse(report["triggers_ok"])
        self.assertFalse(report["chain_ok"])
        self.assertIn("trigger memories_require_spine_event is missing or altered", report["problems"])
        spine.create_spine_triggers(self.db)
        self.assertTrue(spine.verify_spine(self.db, self.key)["ok"])
        # A second creating event for a live memory id is a writer fault on the chain.
        fields = {"kind": "fact", "content": "The relay rack is in row B", "source": "operator note",
                  "family": None, "outcome_status": None, "reflection_id": None}
        self._append(
            kind="memory.created", actor="operator", source="s", scope="global", permission="p",
            outcome="applied", subject_kind="memory", subject_id=memory_a,
            payload=spine.memory_event_payload(self.key, fields, origin=None, eligible=None), now=_stamp(6),
        )
        report = spine.verify_spine(self.db, self.key)
        self.assertFalse(report["chain_ok"])
        self.assertTrue(any(f"duplicate creating event for memory {memory_a} (first {event_a})" in text
                            for text in report["problems"]))

    # --- rebuild equivalence ----------------------------------------------------

    def test_rebuild_reports_out_of_band_edits(self) -> None:
        self._migrate()
        self._create_claim(1, "Kestrel relay", "9090", _stamp(2))
        clean = spine.rebuild_claim_projection(self.db, self.key, content_builder=_builder)
        self.assertTrue(clean["ok"], clean["divergences"])
        self.db.execute("UPDATE memory_claims SET value='9999' WHERE id=1")
        report = spine.rebuild_claim_projection(self.db, self.key)
        self.assertFalse(report["ok"])
        self.assertEqual(report["divergences"][0]["claim_id"], 1)
        self.assertIn("value", report["divergences"][0]["detail"])
        # The CLI prints details verbatim: a detail names the field, never a value.
        self.assertEqual(report["divergences"][0]["detail"], "value: differs")
        self.db.execute("UPDATE memory_claims SET subject='Osprey relay', source='someone else' WHERE id=1")
        backing = self._memory_of(1)
        self.db.execute("UPDATE memories SET content='Osprey relay listen port: 9999' WHERE id=?", (backing,))
        report = spine.rebuild_claim_projection(self.db, self.key, content_builder=_builder)
        details = sorted(d["detail"] for d in report["divergences"])
        self.assertEqual(details, [
            "backing memory content differs from the spine", "source: differs",
            "subject: differs", "value: differs",
        ])
        dump = json.dumps(report["divergences"])
        for secret in ("9999", "9090", "Kestrel", "Osprey", "someone else", _CLAIM_SOURCE):
            self.assertNotIn(secret, dump)
        # Metadata may be shown.
        self.db.execute("UPDATE memories SET content=? WHERE id=?", (_claim_content("Kestrel relay", "listen port", "9090"), backing))
        self.db.execute("UPDATE memory_claims SET subject='Kestrel relay', source=?, value='9090', status='disputed' WHERE id=1", (_CLAIM_SOURCE,))
        report = spine.rebuild_claim_projection(self.db, self.key, content_builder=_builder)
        self.assertEqual([d["detail"] for d in report["divergences"]], ["status: live='disputed' rebuilt='active'"])

    def test_memory_rebuild_flags_out_of_band_edits(self) -> None:
        self._migrate()
        first, _ = self._create_memory("The relay rack is in row B", _stamp(2))
        second, _ = self._create_memory("An unverified aside", _stamp(3), origin="unverified", eligible=False)
        lesson, _ = self._create_lesson("Retry the deploy after a lease timeout", _stamp(4),
                                        reflection_id=4, digest="b" * 64)
        vault, _ = self._create_memory("vault note body", _stamp(5), kind="vault", source="notes/rack.md",
                                       origin="verified_vault_note", eligible=True)
        self._create_claim(1, "Kestrel relay", "9090", _stamp(6))
        clean = spine.rebuild_memory_projection(self.db, self.key)
        self.assertTrue(clean["ok"], clean["divergences"])
        self.assertEqual((clean["rows_live"], clean["rows_rebuilt"]), (4, 4))
        # An out-of-band content edit: the keyed digest differs, and the report never shows content.
        self.db.execute("UPDATE memories SET content='edited out of band' WHERE id=?", (first,))
        report = spine.rebuild_memory_projection(self.db, self.key)
        self.assertEqual({(d["memory_id"], d["kind"]) for d in report["divergences"]}, {(first, "field")})
        self.assertEqual({d["detail"].split(":")[0] for d in report["divergences"]},
                         {"content_digest", "content_length"})
        self.assertNotIn("edited", json.dumps(report["divergences"]))
        self.assertNotIn("row B", json.dumps(report["divergences"]))
        self.db.execute("UPDATE memories SET content='The relay rack is in row B' WHERE id=?", (first,))
        # An out-of-band eligibility change.
        self.db.execute("UPDATE ordinary_memory_provenance SET eligible=0, origin='unverified' WHERE memory_id=?", (first,))
        report = spine.rebuild_memory_projection(self.db, self.key)
        self.assertEqual({d["detail"].split(":")[0] for d in report["divergences"]}, {"origin", "eligible"})
        self.db.execute("UPDATE ordinary_memory_provenance SET eligible=1, origin='explicit_operator_memory' WHERE memory_id=?", (first,))
        # A lesson whose provenance digest changed, then one whose provenance row is gone.
        self.db.execute("UPDATE lesson_provenance SET provenance_sha256=? WHERE memory_id=?", ("c" * 64, lesson))
        report = spine.rebuild_memory_projection(self.db, self.key)
        self.assertEqual([(d["memory_id"], d["kind"], d["detail"]) for d in report["divergences"]],
                         [(lesson, "provenance", "lesson provenance digest differs")])
        self.db.execute("DELETE FROM lesson_provenance WHERE memory_id=?", (lesson,))
        report = spine.rebuild_memory_projection(self.db, self.key)
        self.assertEqual(report["divergences"][0]["detail"], "lesson provenance row missing")
        self.db.execute(
            "INSERT INTO lesson_provenance(prediction_id, memory_id, reflection_id, verified_at, content_sha256, provenance_sha256) VALUES (4, ?, 4, ?, ?, ?)",
            (lesson, _stamp(), spine.sha256_hex("Retry the deploy after a lease timeout"), "b" * 64),
        )
        self.assertTrue(spine.rebuild_memory_projection(self.db, self.key)["ok"])
        # A source edit is reported without echoing either source.
        self.db.execute("UPDATE memories SET source='someone else' WHERE id=?", (first,))
        report = spine.rebuild_memory_projection(self.db, self.key)
        self.assertEqual(report["divergences"][0]["detail"], "source: differs")
        self.db.execute("UPDATE memories SET source='operator note' WHERE id=?", (first,))
        # A duplicate write that upgrades eligibility is receipted and replays.
        self._create_memory("An unverified aside", _stamp(7), origin="explicit_operator_memory", eligible=True)
        self.assertEqual(self._kinds()[-1], "memory.reasserted")
        self.assertTrue(spine.rebuild_memory_projection(self.db, self.key)["ok"])
        # A vault content change without its receipt diverges; with it, replays.
        self.db.execute("UPDATE memories SET content='vault note body v2' WHERE id=?", (vault,))
        self.assertFalse(spine.rebuild_memory_projection(self.db, self.key)["ok"])
        fields = {"kind": "vault", "content": "vault note body v2", "source": "notes/rack.md",
                  "family": None, "outcome_status": None, "reflection_id": None}
        self._append(kind="memory.updated", actor="runtime", source="notes/rack.md", scope="global",
                     permission="runtime:indexer", outcome="applied", subject_kind="memory", subject_id=vault,
                     payload=spine.memory_event_payload(self.key, fields, origin="verified_vault_note", eligible=True),
                     now=_stamp(8))
        self.assertTrue(spine.rebuild_memory_projection(self.db, self.key)["ok"])
        # A vault delete with its receipt: the shadow drops the row too.
        self.db.execute("BEGIN IMMEDIATE")
        self.db.execute("DELETE FROM ordinary_memory_provenance WHERE memory_id=?", (vault,))
        self.db.execute("DELETE FROM memories WHERE id=?", (vault,))
        spine.append_event(self.db, self.key, kind="memory.deleted", actor="runtime", source="vault", scope="global",
                           permission="runtime:indexer", outcome="applied", subject_kind="memory", subject_id=vault,
                           payload=spine.memory_deleted_payload(self.key, [(vault, "vault note body v2")], reason="stale"),
                           now=_stamp(9))
        self.db.execute("COMMIT")
        report = spine.rebuild_memory_projection(self.db, self.key)
        self.assertTrue(report["ok"], report["divergences"])
        self.assertEqual((report["rows_live"], report["rows_rebuilt"]), (3, 3))
        self.assertTrue(spine.verify_spine(self.db, self.key)["ok"])
        # A row removed without a receipt, and a row inserted without one.
        self.db.execute("DELETE FROM ordinary_memory_provenance WHERE memory_id=?", (second,))
        self.db.execute("DELETE FROM memories WHERE id=?", (second,))
        with self._raw():
            self.db.execute("INSERT INTO memories(id, created_at, kind, content) VALUES (90, ?, 'fact', 'raw')", (_stamp(),))
        report = spine.rebuild_memory_projection(self.db, self.key)
        kinds = {(d["memory_id"], d["kind"]) for d in report["divergences"] if d["kind"] != "verify"}
        self.assertEqual(kinds, {(second, "missing_in_live"), (90, "missing_in_rebuild")})
        self.assertTrue(any(d["kind"] == "verify" for d in report["divergences"]))
        self.db.execute("DELETE FROM memories WHERE id=90")
        # A claim's backing row is lineage-checked here, not compared.
        backing = self._memory_of(1)
        with self._raw():
            self.db.execute("UPDATE memories SET spine_event_id=NULL WHERE id=?", (backing,))
        report = spine.rebuild_memory_projection(self.db, self.key)
        self.assertIn((backing, "lineage"), {(d["memory_id"], d["kind"]) for d in report["divergences"]})
        # An update for an id the spine never created is an order divergence.
        self._append(kind="memory.updated", actor="runtime", source="s", scope="global", permission="p",
                     outcome="applied", subject_kind="memory", subject_id=999,
                     payload=spine.memory_event_payload(self.key, fields, origin=None, eligible=None), now=_stamp(10))
        report = spine.rebuild_memory_projection(self.db, self.key)
        self.assertIn((999, "order"), {(d["memory_id"], d["kind"]) for d in report["divergences"]})

    def test_status_after_images_replay_and_tombstones_erase(self) -> None:
        self._migrate()
        self._create_claim(1, "Kestrel relay", "8080", _stamp(2))
        self._create_claim(2, "Kestrel relay", "9090", _stamp(3))
        memory_ids = [self._memory_of(1), self._memory_of(2)]
        # Supersede claim 1 with an after-image, as the writer would.
        self._set_status(1, "superseded", _stamp(3), reason="update", related=2)
        report = spine.rebuild_claim_projection(self.db, self.key, content_builder=_builder)
        self.assertTrue(report["ok"], report["divergences"])
        # Erase the key: remove rows, tombstone (naming the backing rows too), redact earlier payloads.
        self.db.execute("BEGIN IMMEDIATE")
        self.db.execute("DELETE FROM memory_claim_events WHERE claim_id IN (1, 2)")
        self.db.execute("DELETE FROM memory_claim_evidence WHERE claim_id IN (1, 2)")
        self.db.execute("DELETE FROM memory_claims WHERE id IN (1, 2)")
        self.db.execute("DELETE FROM memories WHERE id IN (?, ?)", memory_ids)
        tombstone = spine.append_event(
            self.db, self.key, kind="claim.tombstoned", actor="operator", source="erase",
            scope="project:1", permission="p", outcome="applied",
            payload={"at": _stamp(4), "claim_key": "kestrel relay|listen port", "removed_claim_ids": [1, 2],
                     "removed_memory_ids": memory_ids},
            now=_stamp(4),
        )
        redacted = spine.redact_claim_key_events(self.db, "project:1", "kestrel relay|listen port", tombstone)
        self.db.execute("COMMIT")
        self.assertEqual(len(redacted), 3)
        for event_id in redacted:
            row = self.db.execute(
                "SELECT payload_json, payload_salt, redacted_by_event_id FROM memory_spine_events WHERE id=?",
                (event_id,),
            ).fetchone()
            self.assertIsNone(row["payload_json"])
            self.assertIsNone(row["payload_salt"])
            self.assertEqual(row["redacted_by_event_id"], tombstone)
        dump = " ".join(
            str(row[0] or "") for row in self.db.execute("SELECT payload_json FROM memory_spine_events")
        )
        self.assertNotIn("9090", dump)
        self.assertNotIn("8080", dump)
        verification = spine.verify_spine(self.db, self.key)
        self.assertTrue(verification["ok"], verification["problems"])
        self.assertEqual(verification["redacted"], 3)
        report = spine.rebuild_claim_projection(self.db, self.key)
        self.assertTrue(report["ok"], report["divergences"])
        self.assertEqual((report["rows_live"], report["rows_rebuilt"]), (0, 0))
        self.assertTrue(spine.rebuild_memory_projection(self.db, self.key)["ok"])
        # A second redaction, or a redaction naming another key's tombstone, aborts.
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(
                "UPDATE memory_spine_events SET payload_json=NULL, payload_salt=NULL, redacted_by_event_id=? WHERE id=?",
                (tombstone, redacted[0]),
            )
        # Neither erased id is ever reused; live rows with erased ids are reported.
        self.assertEqual(spine.allocate_claim_id(self.db), 3)
        self.assertEqual(spine.memory_sequence_floor(self.db), max(memory_ids))
        self.assertEqual(spine.allocate_memory_id(self.db), max(memory_ids) + 1)
        with self._raw():
            self.db.execute("INSERT INTO memories(id, created_at, kind, content) VALUES (?, ?, 'claim', 'x')", (memory_ids[0], _stamp()))
            self.db.execute(
                """INSERT INTO memory_claims(id, memory_id, created_at, updated_at, claim_key, subject,
                   predicate, value, value_sha256, source, authority, confidence, status, valid_from, scope, spine_event_id)
                   VALUES (1, ?, ?, ?, 'k', 's', 'p', 'v', ?, 'src', 'operator', 1.0, 'active', ?, 'project:1', 2)""",
                (memory_ids[0], _stamp(), _stamp(), spine.sha256_hex("v"), _stamp()),
            )
        verification = spine.verify_spine(self.db, self.key)
        self.assertFalse(verification["ok"])
        self.assertTrue(any("tombstoned id" in text for text in verification["problems"]))
        self.assertIn(f"memory {memory_ids[0]}: live row with a deleted id", verification["problems"])

    # --- apply -------------------------------------------------------------------

    def _plant_three_divergences(self) -> tuple[int, int]:
        """The exit-test recipe: an edit, an out-of-band insert, a delete."""
        self._create_claim(1, "Kestrel relay", "8080", _stamp(2))
        self._create_claim(2, "Kestrel relay", "9090", _stamp(3))
        self._set_status(1, "superseded", _stamp(3), reason="update", related=2)
        self._create_claim(3, "Osprey relay", "7070", _stamp(4))
        ghost_memory = int(self.db.execute("SELECT next_id FROM memory_id_sequence").fetchone()[0])
        ghost_claim = int(self.db.execute("SELECT next_id FROM memory_claim_sequence").fetchone()[0])
        self.db.execute("UPDATE memory_claims SET value='9999' WHERE id=3")
        with self._raw():
            self.db.execute(
                "INSERT INTO memories(id, created_at, kind, content, source) VALUES (?, ?, 'claim', ?, 'operator:ghost')",
                (ghost_memory, _stamp(), _claim_content("Ghost box", "listen port", "1")),
            )
            self.db.execute(
                """INSERT INTO memory_claims(id, memory_id, created_at, updated_at, claim_key, subject,
                   predicate, value, value_sha256, source, authority, confidence, status, valid_from, scope)
                   VALUES (?, ?, ?, ?, 'ghost box|listen port', 'Ghost box', 'listen port', '1', ?, 'ghost', 'operator', 1.0, 'active', ?, 'project:1')""",
                (ghost_claim, ghost_memory, _stamp(), _stamp(), spine.sha256_hex("1"), _stamp()),
            )
        self.db.execute("UPDATE memory_id_sequence SET next_id=next_id+1")
        self.db.execute("UPDATE memory_claim_sequence SET next_id=next_id+1")
        # The delete, done the only way foreign keys allow: references nulled,
        # dependents removed, then the row (its backing row survives).
        self.db.execute("UPDATE memory_claim_events SET related_claim_id=NULL WHERE related_claim_id=2")
        self.db.execute("DELETE FROM memory_claim_evidence WHERE claim_id=2")
        self.db.execute("DELETE FROM memory_claim_events WHERE claim_id=2")
        self.db.execute("DELETE FROM memory_claims WHERE id=2")
        return ghost_claim, ghost_memory

    def test_apply_reconciles_an_edit_an_insert_and_a_delete(self) -> None:
        self._migrate()
        ghost_claim, ghost_memory = self._plant_three_divergences()
        backing_of_2 = int(self.db.execute("SELECT id FROM memories WHERE content=?", (_claim_content("Kestrel relay", "listen port", "9090"),)).fetchone()[0])
        plan = spine.rebuild_claim_projection(self.db, self.key, content_builder=_builder)
        self.assertFalse(plan["ok"])
        planted = {(d["claim_id"], d["kind"]) for d in plan["divergences"] if d["kind"] != "verify"}
        self.assertEqual(planted, {(3, "field"), (ghost_claim, "missing_in_rebuild"), (2, "missing_in_live")})
        self.assertTrue(plan["verification"]["chain_ok"])
        self.assertFalse(plan["verification"]["ok"])
        result = self._apply(plan)
        self.assertEqual(result["removed_ids"], [ghost_claim])
        self.assertEqual(result["removed_memory_ids"], [ghost_memory])
        self.assertEqual(result["recreated_ids"], [2])
        self.assertEqual(result["updated_ids"], [3])
        self.assertEqual(result["lost_evidence_claim_ids"], [2])
        self.assertEqual((result["rows_before"], result["rows_after"]), (3, 3))
        self.assertEqual(self.db.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(result["divergences_fixed"], len(plan["divergences"]))
        after = spine.rebuild_claim_projection(self.db, self.key, content_builder=_builder)
        self.assertTrue(after["ok"], after["divergences"])
        verification = spine.verify_spine(self.db, self.key)
        self.assertTrue(verification["ok"], verification["problems"])
        self.assertTrue(spine.rebuild_memory_projection(self.db, self.key)["ok"])
        # The receipt is the last event and carries the counts, never values.
        last = self.db.execute("SELECT id, kind, subject_kind, payload_json FROM memory_spine_events ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual((last["id"], last["kind"], last["subject_kind"]), (result["event_id"], "projection.rebuilt", "projection"))
        payload = json.loads(last["payload_json"])
        self.assertEqual((payload["removed_ids"], payload["recreated_ids"], payload["updated_ids"]), ([ghost_claim], [2], [3]))
        self.assertNotIn("9090", last["payload_json"])
        # The edit is reverted, with the backing row's source; the ghost is gone;
        # claim 2 is back on its surviving backing row with its claim events replayed.
        claim3 = self.db.execute("SELECT c.value, m.source FROM memory_claims c JOIN memories m ON m.id=c.memory_id WHERE c.id=3").fetchone()
        self.assertEqual(tuple(claim3), ("7070", f"operator:{_CLAIM_SOURCE}"))
        self.assertIsNone(self.db.execute("SELECT 1 FROM memory_claims WHERE id=?", (ghost_claim,)).fetchone())
        self.assertIsNone(self.db.execute("SELECT 1 FROM memories WHERE id=?", (ghost_memory,)).fetchone())
        claim2 = self.db.execute("SELECT memory_id, value, status, created_at, spine_event_id FROM memory_claims WHERE id=2").fetchone()
        self.assertEqual((claim2["memory_id"], claim2["value"], claim2["status"], claim2["created_at"]), (backing_of_2, "9090", "active", _stamp(3)))
        events = self.db.execute("SELECT status, reason, spine_event_id FROM memory_claim_events WHERE claim_id=2 ORDER BY id").fetchall()
        self.assertEqual([tuple(row) for row in events], [("active", "new strongest claim", claim2["spine_event_id"])])
        self.assertEqual(self.db.execute("SELECT status FROM memory_claims WHERE id=1").fetchone()[0], "superseded")
        # Nothing to do is not an error, and the sequences stayed ahead.
        self.assertEqual(self._apply(None)["divergences_fixed"], 0)
        self.assertGreater(self.db.execute("SELECT next_id FROM memory_id_sequence").fetchone()[0], ghost_memory)

    def test_apply_refuses_failed_verify_order_divergence_and_stale_plan(self) -> None:
        self._migrate()
        self._create_claim(1, "Kestrel relay", "8080", _stamp(2))
        event_id = self._create_claim(2, "Kestrel relay", "9090", _stamp(3))
        with self.assertRaises(spine.SpineError) as refused:
            spine.apply_claim_projection(self.db, self.key, None, content_builder=_builder, now=_stamp(9))
        self.assertEqual(refused.exception.code, "not_in_transaction")
        # A stale plan: the store changed after the operator saw the dry run.
        plan = spine.rebuild_claim_projection(self.db, self.key, content_builder=_builder)
        self.db.execute("UPDATE memory_claims SET value='9999' WHERE id=2")
        before = self._snapshot()
        with self.assertRaises(spine.SpineError) as refused:
            self._apply(plan)
        self.assertEqual(refused.exception.code, "stale_plan")
        self.assertEqual(self._snapshot(), before)
        # A missing content builder when a row must be recreated.
        self.db.execute("UPDATE memory_claims SET value='9090' WHERE id=2")
        self.db.execute("DELETE FROM memory_claim_evidence WHERE claim_id=2")
        self.db.execute("DELETE FROM memory_claim_events WHERE claim_id=2")
        self.db.execute("DELETE FROM memory_claims WHERE id=2")
        before = self._snapshot()
        with self.assertRaises(spine.SpineError) as refused:
            self._apply(None, content_builder=None)
        self.assertEqual(refused.exception.code, "missing_content_builder")
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(self._apply(None)["recreated_ids"], [2])
        # A tampered chain: the spine is wrong, not the projection.
        with self._raw():
            self.db.execute("UPDATE memory_spine_events SET actor='model' WHERE id=?", (event_id,))
        self.db.execute("UPDATE memory_claims SET value='9999' WHERE id=2")
        before = self._snapshot()
        with self.assertRaises(spine.SpineError) as refused:
            self._apply(None)
        self.assertEqual(refused.exception.code, "verify_failed")
        self.assertEqual(self._snapshot(), before)
        with self._raw():
            self.db.execute("UPDATE memory_spine_events SET actor='operator' WHERE id=?", (event_id,))
        self.assertEqual(self._apply(None)["updated_ids"], [2])
        # An order divergence (a status event for a claim the spine never created).
        self._append(
            kind="claim.superseded", actor="operator", source="s", scope="project:1", permission="p",
            outcome="applied", subject_kind="claim", subject_id=999,
            payload={"at": _stamp(5), "claim_key": "k", "claim_id": 999, "status": "superseded"}, now=_stamp(5),
        )
        before = self._snapshot()
        with self.assertRaises(spine.SpineError) as refused:
            self._apply(None)
        self.assertEqual(refused.exception.code, "history_inconsistent")
        self.assertEqual(self._snapshot(), before)

    def test_apply_recreation_reuses_a_surviving_backing_row_or_allocates_a_fresh_id(self) -> None:
        self._migrate()
        self._create_claim(1, "Kestrel relay", "8080", _stamp(2))
        self._create_claim(2, "Osprey relay", "7070", _stamp(3))
        self._set_status(1, "disputed", _stamp(4), reason="equal-authority values conflict", related=2)
        self._set_status(1, "active", _stamp(5), reason="matching claim promoted by stronger evidence", related=None)
        backing_1, backing_2 = self._memory_of(1), self._memory_of(2)
        # Claim 1: row and events gone on a connection without foreign keys, so
        # its evidence and backing row survive -> reused, history replayed.
        with self._without_foreign_keys():
            self.db.execute("DELETE FROM memory_claim_events WHERE claim_id=1")
            self.db.execute("DELETE FROM memory_claims WHERE id=1")
        # Claim 2: everything gone, backing row too -> a fresh memory id, never the old one.
        self.db.execute("DELETE FROM memory_claim_events WHERE claim_id=2")
        self.db.execute("DELETE FROM memory_claim_evidence WHERE claim_id=2")
        self.db.execute("DELETE FROM memory_claims WHERE id=2")
        self.db.execute("DELETE FROM memories WHERE id=?", (backing_2,))
        result = self._apply(None)
        self.assertEqual(result["recreated_ids"], [1, 2])
        self.assertEqual(result["lost_evidence_claim_ids"], [2])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM memory_claim_evidence WHERE claim_id=1").fetchone()[0], 1)
        self.assertEqual(self.db.execute("PRAGMA foreign_key_check").fetchall(), [])
        rows = {int(row["id"]): row for row in self.db.execute("SELECT id, memory_id, status, updated_at FROM memory_claims")}
        self.assertEqual(rows[1]["memory_id"], backing_1)
        self.assertEqual(rows[1]["status"], "active")
        self.assertEqual(rows[1]["updated_at"], _stamp(5))
        self.assertNotEqual(rows[2]["memory_id"], backing_2)
        self.assertGreater(rows[2]["memory_id"], backing_2)
        history = [tuple(row) for row in self.db.execute(
            "SELECT status, reason FROM memory_claim_events WHERE claim_id=1 ORDER BY id"
        )]
        self.assertEqual(history, [
            ("active", "new strongest claim"),
            ("disputed", "equal-authority values conflict"),
            ("active", "matching claim promoted by stronger evidence"),
        ])
        backing = self.db.execute("SELECT created_at, kind, source, spine_event_id FROM memories WHERE id=?", (rows[2]["memory_id"],)).fetchone()
        self.assertEqual(tuple(backing), (_stamp(3), "claim", f"operator:{_CLAIM_SOURCE}", 3))
        self.assertTrue(spine.rebuild_claim_projection(self.db, self.key, content_builder=_builder)["ok"])
        self.assertTrue(spine.verify_spine(self.db, self.key)["ok"])

    def test_apply_recreates_a_chain_middle_row_under_foreign_keys(self) -> None:
        self._migrate()
        self.assertEqual(self.db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        created_1 = self._create_claim(1, "Kestrel relay", "8080", _stamp(2))
        self._create_claim(2, "Kestrel relay", "9090", _stamp(3), supersedes=1)
        superseded_1 = self._set_status(
            1, "superseded", _stamp(3), reason="superseded by a newer version from the same source", related=2
        )
        self._create_claim(3, "Kestrel relay", "9191", _stamp(4), supersedes=2)
        self._set_status(
            2, "superseded", _stamp(4), reason="superseded by a newer version from the same source", related=3
        )
        self.assertTrue(spine.rebuild_claim_projection(self.db, self.key, content_builder=_builder)["ok"])
        # An out-of-band delete of claim 1, done the only way foreign keys
        # allow: the references to it nulled first, its dependents removed.
        self.db.execute("UPDATE memory_claims SET supersedes_id=NULL WHERE supersedes_id=1")
        self.db.execute("UPDATE memory_claim_events SET related_claim_id=NULL WHERE related_claim_id=1")
        self.db.execute("DELETE FROM memory_claim_evidence WHERE claim_id=1")
        self.db.execute("DELETE FROM memory_claim_events WHERE claim_id=1")
        self.db.execute("DELETE FROM memory_claims WHERE id=1")
        plan = spine.rebuild_claim_projection(self.db, self.key, content_builder=_builder)
        planted = {(d["claim_id"], d["kind"], d["detail"]) for d in plan["divergences"] if d["kind"] != "verify"}
        self.assertEqual(planted, {
            (1, "missing_in_live", "spine history has no live row"),
            (2, "field", "supersedes_id: live=None rebuilt=1"),
        })
        # Claim 2's reference to claim 1 can only be written after claim 1 exists again.
        result = self._apply(plan)
        self.assertEqual((result["recreated_ids"], result["updated_ids"], result["removed_ids"]), ([1], [2], []))
        self.assertEqual(result["lost_evidence_claim_ids"], [1])
        chain = {
            int(row["id"]): (row["supersedes_id"], row["status"], row["valid_until"])
            for row in self.db.execute("SELECT id, supersedes_id, status, valid_until FROM memory_claims")
        }
        self.assertEqual(chain, {
            1: (None, "superseded", _stamp(3)), 2: (1, "superseded", _stamp(4)), 3: (2, "active", None),
        })
        events = [tuple(row) for row in self.db.execute(
            "SELECT status, related_claim_id, spine_event_id FROM memory_claim_events WHERE claim_id=1 ORDER BY id"
        )]
        self.assertEqual(events, [("active", None, created_1), ("superseded", 2, superseded_1)])
        self.assertEqual(self.db.execute("PRAGMA foreign_key_check").fetchall(), [])
        after = spine.rebuild_claim_projection(self.db, self.key, content_builder=_builder)
        self.assertTrue(after["ok"], after["divergences"])
        verification = spine.verify_spine(self.db, self.key)
        self.assertTrue(verification["ok"], verification["problems"])
        self.assertEqual(self._apply(None)["divergences_fixed"], 0)

    def test_memory_rebuild_ignores_the_history_of_an_adopted_orphan_backing_row(self) -> None:
        self._migrate()
        self._create_claim(1, "Kestrel relay", "9090", _stamp(2))
        orphan = self._memory_of(1)
        # The claim row goes out of band and the store is re-migrated with its
        # lineage lost: the backing row is a legacy orphan and gets memory.imported.
        self.db.execute("DELETE FROM memory_claim_evidence WHERE claim_id=1")
        self.db.execute("DELETE FROM memory_claim_events WHERE claim_id=1")
        self.db.execute("DELETE FROM memory_claims WHERE id=1")
        self.db.execute("UPDATE memories SET spine_event_id=NULL WHERE id=?", (orphan,))
        report = self._rerun_47()
        self.assertEqual((report["memories_imported"], report["orphan_claim_rows"]), (1, 1))
        self.assertTrue(spine.verify_spine(self.db, self.key)["ok"])
        self.assertEqual(spine.rebuild_memory_projection(self.db, self.key)["rows_live"], 1)
        # The claim writer asserts the same fact again and adopts the orphan
        # as its backing row: the row's lineage becomes the claim's event.
        self.db.execute("BEGIN IMMEDIATE")
        claim_id = spine.allocate_claim_id(self.db)
        row = self._claim_row("Kestrel relay", "9090", _stamp(5))
        event_id = spine.append_event(
            self.db, self.key, kind="claim.created", actor="operator", source=row["source"],
            scope="project:1", permission="p", outcome="applied", subject_kind="claim",
            subject_id=claim_id, payload=spine.claim_event_payload(row, at=_stamp(5)), now=_stamp(5),
        )
        self.db.execute(
            "UPDATE memories SET source=?, spine_event_id=? WHERE id=?",
            (f"operator:{_CLAIM_SOURCE}", event_id, orphan),
        )
        self.db.execute(
            """INSERT INTO memory_claims(id, memory_id, created_at, updated_at, claim_key, subject,
               predicate, value, value_sha256, source, authority, confidence, status, valid_from,
               valid_until, supersedes_id, scope, spine_event_id)
               VALUES (?, ?, ?, ?, ?, 'Kestrel relay', 'listen port', '9090', ?, ?, 'operator', 1.0,
                       'active', ?, NULL, NULL, 'project:1', ?)""",
            (claim_id, orphan, _stamp(5), _stamp(5), row["claim_key"], row["value_sha256"],
             row["source"], _stamp(5), event_id),
        )
        self.db.execute("COMMIT")
        verification = spine.verify_spine(self.db, self.key)
        self.assertTrue(verification["ok"], verification["problems"])
        # The row is outside the memory projection now; its history is not a divergence.
        report = spine.rebuild_memory_projection(self.db, self.key)
        self.assertTrue(report["ok"], report["divergences"])
        self.assertEqual((report["rows_live"], report["rows_rebuilt"]), (0, 0))
        # Its lineage is still checked against the claim's event.
        with self._raw():
            self.db.execute("UPDATE memories SET spine_event_id=NULL WHERE id=?", (orphan,))
        report = spine.rebuild_memory_projection(self.db, self.key)
        self.assertEqual(
            {(d["memory_id"], d["kind"]) for d in report["divergences"] if d["kind"] != "verify"},
            {(orphan, "lineage")},
        )

    def test_migration_47_refuses_a_lineage_less_row_once_memories_are_on_the_spine(self) -> None:
        self._insert_legacy_memory(2, "A legacy note")
        self._migrate()  # a real 46 store: the note is imported
        self.assertEqual(self._kinds().count("memory.imported"), 1)
        events_before = self.db.execute("SELECT COUNT(*) FROM memory_spine_events").fetchone()[0]
        # A user_version downgrade over an out-of-band insert: refused, nothing appended.
        planted = int(self.db.execute("SELECT next_id FROM memory_id_sequence").fetchone()[0])
        with self._raw():
            self.db.execute(
                "INSERT INTO memories(id, created_at, kind, content) VALUES (?, ?, 'fact', 'planted after the fact')",
                (planted, _stamp()),
            )
        self.db.execute("UPDATE memory_id_sequence SET next_id=next_id+1")
        self.db.execute("BEGIN IMMEDIATE")
        with self.assertRaises(spine.SpineError) as refused:
            spine.migrate_memory_spine_v47(self.db, self.key, now=_stamp(3))
        self.db.execute("ROLLBACK")
        self.assertEqual(refused.exception.code, "lineage_missing")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM memory_spine_events").fetchone()[0], events_before)
        self.assertIsNone(self.db.execute("SELECT spine_event_id FROM memories WHERE id=?", (planted,)).fetchone()[0])
        self.assertIn(f"memory {planted}: no creating spine event", spine.verify_spine(self.db, self.key)["problems"])
        self.db.execute("DELETE FROM memories WHERE id=?", (planted,))
        # The legitimate paths on the same store are untouched: a row with a
        # creating event re-links by digest, and the migration is a no-op otherwise.
        self.db.execute("UPDATE memories SET spine_event_id=NULL WHERE id=2")
        self.assertEqual(self._rerun_47()["memories_relinked"], 1)
        self.assertEqual(self._rerun_47()["memories_relinked"], 0)
        self.assertTrue(spine.verify_spine(self.db, self.key)["ok"])

    def test_recent_events_never_print_values(self) -> None:
        self._migrate()
        self._create_claim(1, "Kestrel relay", "9090", _stamp(2))
        items = spine.recent_events(self.db, limit=5)
        self.assertEqual(items[0]["kind"], "claim.created")
        self.assertIn("value", items[0]["payload_keys"])
        self.assertNotIn("9090", json.dumps(items))

    def test_key_sidecar_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "jarvis.db"
            key = spine.load_spine_key(db_path)
            self.assertEqual(len(key), 32)
            self.assertEqual(spine.load_spine_key(db_path), key)
            sidecar = Path(str(db_path) + spine.KEY_SIDECAR_SUFFIX)
            self.assertTrue(sidecar.exists())
            sidecar.write_bytes(b"short")
            with self.assertRaises(spine.SpineError):
                spine.load_spine_key(db_path)
            sidecar.unlink()
            with self.assertRaises(spine.SpineError):
                spine.load_spine_key(db_path, create=False)


if __name__ == "__main__":
    unittest.main()
