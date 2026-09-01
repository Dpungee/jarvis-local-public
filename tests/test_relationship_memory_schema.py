from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis.embodied_presence import PresenceMode
from jarvis.relationship_memory import (
    RELATIONSHIP_MEMORY_APPLICATION_ID,
    RELATIONSHIP_MEMORY_SCHEMA_VERSION,
    RelationshipMemory,
    RelationshipMemoryError,
)


_LEGACY_V0_SCHEMA = """
CREATE TABLE relationship_memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    subject TEXT NOT NULL,
    value TEXT NOT NULL,
    visibility TEXT NOT NULL,
    source TEXT NOT NULL,
    confidence REAL NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    supersedes_id INTEGER,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    expires_at REAL,
    FOREIGN KEY(supersedes_id) REFERENCES relationship_memories(id)
);
CREATE UNIQUE INDEX idx_relationship_active
    ON relationship_memories(kind, subject) WHERE active=1;
CREATE TABLE relationship_memory_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id INTEGER,
    action TEXT NOT NULL,
    value_sha256 TEXT,
    created_at REAL NOT NULL
);
"""


class RelationshipMemorySchemaTests(unittest.TestCase):
    def test_actual_connection_rechecks_authority_after_preflight_race(self) -> None:
        with RelationshipMemory(self.path):
            pass
        original_preflight = RelationshipMemory._preflight_existing_store
        tampered_bytes: list[bytes] = []

        def preflight_then_replace(instance: RelationshipMemory) -> None:
            original_preflight(instance)
            db = sqlite3.connect(instance.path)
            try:
                db.execute("CREATE TABLE future_race(value TEXT)")
                db.execute(
                    f"PRAGMA user_version={RELATIONSHIP_MEMORY_SCHEMA_VERSION + 1}"
                )
                db.commit()
            finally:
                db.close()
            tampered_bytes.append(instance.path.read_bytes())

        with patch.object(
            RelationshipMemory,
            "_preflight_existing_store",
            preflight_then_replace,
        ):
            with self.assertRaisesRegex(RelationshipMemoryError, "newer"):
                RelationshipMemory(self.path)

        self.assertEqual(self.path.read_bytes(), tampered_bytes[0])
        db = sqlite3.connect(self.path)
        try:
            self.assertEqual(
                int(db.execute("PRAGMA user_version").fetchone()[0]),
                RELATIONSHIP_MEMORY_SCHEMA_VERSION + 1,
            )
            self.assertIsNotNone(
                db.execute(
                    "SELECT name FROM sqlite_master WHERE name='future_race'"
                ).fetchone()
            )
        finally:
            db.close()

    def test_future_schema_in_hot_wal_is_rejected_without_recovery(self):
        from tests.sqlite_crash_fixture import create_future_schema_in_hot_wal, snapshot_directory
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "relationship.db"
            with RelationshipMemory(path):
                pass
            create_future_schema_in_hot_wal(
                path, user_version=RELATIONSHIP_MEMORY_SCHEMA_VERSION + 1
            )
            before = snapshot_directory(path)
            with self.assertRaisesRegex(RelationshipMemoryError, "newer"):
                RelationshipMemory(path)
            self.assertEqual(snapshot_directory(path), before)

    def test_future_hot_journal_is_rejected_without_recovery_or_sidecar_changes(self):
        from tests.sqlite_crash_fixture import create_hot_future_database, snapshot_directory
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "relationship.db"
            create_hot_future_database(
                path,
                user_version=RELATIONSHIP_MEMORY_SCHEMA_VERSION + 1,
                application_id=RELATIONSHIP_MEMORY_APPLICATION_ID,
            )
            before = snapshot_directory(path)
            with self.assertRaisesRegex(RelationshipMemoryError, "newer"):
                RelationshipMemory(path)
            self.assertEqual(snapshot_directory(path), before)

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / "relationship.db"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _snapshot(self) -> tuple[bytes, list[str]]:
        return (
            self.path.read_bytes(),
            sorted(item.name for item in self.root.iterdir()),
        )

    def _assert_unchanged(self, snapshot: tuple[bytes, list[str]]) -> None:
        before_bytes, before_files = snapshot
        self.assertEqual(self.path.read_bytes(), before_bytes)
        self.assertEqual(
            sorted(item.name for item in self.root.iterdir()),
            before_files,
        )

    def test_exact_legacy_v0_store_migrates_without_losing_records(self) -> None:
        db = sqlite3.connect(self.path)
        try:
            # The unversioned implementation enabled WAL before creating its
            # two tables, so exercise the exact on-disk legacy state.
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(_LEGACY_V0_SCHEMA)
            db.execute(
                """INSERT INTO relationship_memories(
                       kind, subject, value, visibility, source, confidence,
                       active, supersedes_id, created_at, updated_at, expires_at
                   ) VALUES('tone_preference','conversation','Stay concise',
                            'companion','explicit user statement',1.0,
                            1,NULL,1.0,1.0,NULL)"""
            )
            db.commit()
            self.assertEqual(int(db.execute("PRAGMA application_id").fetchone()[0]), 0)
            self.assertEqual(int(db.execute("PRAGMA user_version").fetchone()[0]), 0)
        finally:
            db.close()

        with RelationshipMemory(self.path) as memory:
            self.assertEqual(
                [row["value"] for row in memory.list_for_mode(PresenceMode.COMPANION)],
                ["Stay concise"],
            )
            self.assertEqual(
                int(memory.db.execute("PRAGMA application_id").fetchone()[0]),
                RELATIONSHIP_MEMORY_APPLICATION_ID,
            )
            self.assertEqual(
                int(memory.db.execute("PRAGMA user_version").fetchone()[0]),
                RELATIONSHIP_MEMORY_SCHEMA_VERSION,
            )

        # The newly marked schema remains usable on a normal restart.
        with RelationshipMemory(self.path) as restarted:
            self.assertEqual(len(restarted.history("tone_preference", "conversation")), 1)

    def test_unmarked_non_relationship_database_is_rejected_without_mutation(self) -> None:
        db = sqlite3.connect(self.path)
        try:
            db.execute("CREATE TABLE unrelated_private_data(value TEXT)")
            db.execute("INSERT INTO unrelated_private_data(value) VALUES('leave me alone')")
            db.commit()
        finally:
            db.close()
        snapshot = self._snapshot()

        with self.assertRaisesRegex(RelationshipMemoryError, "unmarked"):
            RelationshipMemory(self.path)

        self._assert_unchanged(snapshot)

    def test_foreign_application_database_is_rejected_without_mutation(self) -> None:
        db = sqlite3.connect(self.path)
        try:
            db.execute("PRAGMA application_id=305419896")
            db.execute("CREATE TABLE foreign_state(value TEXT)")
            db.commit()
        finally:
            db.close()
        snapshot = self._snapshot()

        with self.assertRaisesRegex(RelationshipMemoryError, "different application"):
            RelationshipMemory(self.path)

        self._assert_unchanged(snapshot)

    def test_future_version_is_byte_for_byte_unchanged(self) -> None:
        db = sqlite3.connect(self.path)
        try:
            db.execute(f"PRAGMA application_id={RELATIONSHIP_MEMORY_APPLICATION_ID}")
            db.execute(f"PRAGMA user_version={RELATIONSHIP_MEMORY_SCHEMA_VERSION + 1}")
            db.execute("CREATE TABLE future_relationship_state(value TEXT)")
            db.execute("INSERT INTO future_relationship_state(value) VALUES('future')")
            db.commit()
        finally:
            db.close()
        snapshot = self._snapshot()

        with self.assertRaisesRegex(RelationshipMemoryError, "newer"):
            RelationshipMemory(self.path)

        self._assert_unchanged(snapshot)


if __name__ == "__main__":
    unittest.main()
