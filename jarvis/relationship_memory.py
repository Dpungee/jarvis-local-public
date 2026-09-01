from __future__ import annotations

import hashlib
import sqlite3
import time
from pathlib import Path
from typing import Any

from .embodied_presence import PresenceMode
from .redaction import contains_secret, redact_secrets


RELATIONSHIP_KINDS = frozenset({
    "address_preference",
    "boundary",
    "important_project",
    "joke",
    "promise",
    "shared_experience",
    "topic_preference",
    "tone_preference",
})
_VISIBILITY = frozenset({"private", "companion", "studio"})
RELATIONSHIP_MEMORY_SCHEMA_VERSION = 1
RELATIONSHIP_MEMORY_APPLICATION_ID = 0x4A52454C  # "JREL"

_RELATIONSHIP_MEMORY_COLUMNS = (
    ("id", "INTEGER", 0, None, 1),
    ("kind", "TEXT", 1, None, 0),
    ("subject", "TEXT", 1, None, 0),
    ("value", "TEXT", 1, None, 0),
    ("visibility", "TEXT", 1, None, 0),
    ("source", "TEXT", 1, None, 0),
    ("confidence", "REAL", 1, None, 0),
    ("active", "INTEGER", 1, "1", 0),
    ("supersedes_id", "INTEGER", 0, None, 0),
    ("created_at", "REAL", 1, None, 0),
    ("updated_at", "REAL", 1, None, 0),
    ("expires_at", "REAL", 0, None, 0),
)
_RELATIONSHIP_EVENT_COLUMNS = (
    ("id", "INTEGER", 0, None, 1),
    ("memory_id", "INTEGER", 0, None, 0),
    ("action", "TEXT", 1, None, 0),
    ("value_sha256", "TEXT", 0, None, 0),
    ("created_at", "REAL", 1, None, 0),
)
_TABLE_INFO_PRAGMAS = {
    "relationship_memories": "PRAGMA table_info(relationship_memories)",
    "relationship_memory_events": "PRAGMA table_info(relationship_memory_events)",
}


class RelationshipMemoryError(RuntimeError):
    """A relationship store could not be opened without crossing its boundary."""


class RelationshipMemory:
    """A separate, user-editable store that cannot become operational truth."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        in_memory = str(self.path) == ":memory:"
        if not in_memory:
            from .sqlite_preflight import validate_database_path

            try:
                path_exists = validate_database_path(self.path)
            except OSError as exc:
                raise RelationshipMemoryError(
                    "relationship-memory database could not be inspected safely"
                ) from exc
            if path_exists:
                self._preflight_existing_store()
        if not in_memory:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path))
        try:
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA foreign_keys=ON")
            self._migrate()
        except BaseException:
            self.db.close()
            raise

    def __enter__(self) -> RelationshipMemory:
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def close(self) -> None:
        self.db.close()

    @staticmethod
    def _table_columns(
        db: sqlite3.Connection, table: str
    ) -> tuple[tuple[str, str, int, str | None, int], ...]:
        try:
            query = _TABLE_INFO_PRAGMAS[table]
        except KeyError as exc:
            raise RelationshipMemoryError("unknown relationship-memory table") from exc
        return tuple(
            (
                str(row[1]),
                str(row[2]).upper(),
                int(row[3]),
                None if row[4] is None else str(row[4]),
                int(row[5]),
            )
            for row in db.execute(query).fetchall()
        )

    @classmethod
    def _has_known_schema(cls, db: sqlite3.Connection) -> bool:
        tables = {
            str(row[0])
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if tables - {
            "relationship_memories",
            "relationship_memory_events",
            "sqlite_sequence",
        }:
            return False
        if {
            "relationship_memories",
            "relationship_memory_events",
        } - tables:
            return False
        objects = {
            (str(row[0]), str(row[1]))
            for row in db.execute(
                "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        if objects != {
            ("table", "relationship_memories"),
            ("table", "relationship_memory_events"),
            ("index", "idx_relationship_active"),
        }:
            return False
        if cls._table_columns(db, "relationship_memories") != (
            _RELATIONSHIP_MEMORY_COLUMNS
        ):
            return False
        if cls._table_columns(db, "relationship_memory_events") != (
            _RELATIONSHIP_EVENT_COLUMNS
        ):
            return False
        indexes = {
            str(row[1]): (int(row[2]), int(row[4]))
            for row in db.execute(
                "PRAGMA index_list(relationship_memories)"
            ).fetchall()
        }
        if indexes != {"idx_relationship_active": (1, 1)}:
            return False
        indexed_columns = tuple(
            str(row[2])
            for row in db.execute(
                "PRAGMA index_info(idx_relationship_active)"
            ).fetchall()
        )
        if indexed_columns != ("kind", "subject"):
            return False
        index_row = db.execute(
            """SELECT sql FROM sqlite_master
               WHERE type='index' AND name='idx_relationship_active'"""
        ).fetchone()
        if index_row is None:
            return False
        normalized_index_sql = "".join(str(index_row[0]).casefold().split())
        if normalized_index_sql != (
            "createuniqueindexidx_relationship_activeon"
            "relationship_memories(kind,subject)whereactive=1"
        ):
            return False
        foreign_keys = db.execute(
            "PRAGMA foreign_key_list(relationship_memories)"
        ).fetchall()
        return len(foreign_keys) == 1 and (
            str(foreign_keys[0][2]),
            str(foreign_keys[0][3]),
            str(foreign_keys[0][4]),
        ) == ("relationship_memories", "supersedes_id", "id")

    @classmethod
    def _validate_store_authority(cls, db: sqlite3.Connection) -> None:
        """Validate markers and the exact supported/legacy schema without writes."""
        application_id = int(db.execute("PRAGMA application_id").fetchone()[0])
        version = int(db.execute("PRAGMA user_version").fetchone()[0])
        tables = {
            str(row[0])
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        nonempty = bool(tables - {"sqlite_sequence"})
        known_schema = cls._has_known_schema(db) if nonempty else False
        if application_id not in {0, RELATIONSHIP_MEMORY_APPLICATION_ID}:
            raise RelationshipMemoryError(
                "relationship-memory database belongs to a different application"
            )
        if version > RELATIONSHIP_MEMORY_SCHEMA_VERSION:
            raise RelationshipMemoryError(
                "relationship-memory database schema is newer than this runtime"
            )
        if application_id == 0:
            if version != 0 or (nonempty and not known_schema):
                raise RelationshipMemoryError(
                    "existing unmarked database is not a relationship-memory store"
                )
            return
        if version != RELATIONSHIP_MEMORY_SCHEMA_VERSION or not known_schema:
            raise RelationshipMemoryError(
                "relationship-memory database schema marker is invalid"
            )

    def _preflight_existing_store(self) -> None:
        """Reject unknown database authority before WAL, DDL, or writes."""
        try:
            from .sqlite_preflight import inspection_connection

            with inspection_connection(self.path) as db:
                self._validate_store_authority(db)
        except RelationshipMemoryError:
            raise
        except (OSError, sqlite3.DatabaseError, TypeError, ValueError) as exc:
            raise RelationshipMemoryError(
                "relationship-memory database could not be inspected safely"
            ) from exc

    def _migrate(self) -> None:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self._validate_store_authority(self.db)
            statements = (
            """CREATE TABLE IF NOT EXISTS relationship_memories (
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
            )""",
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_relationship_active
                ON relationship_memories(kind, subject) WHERE active=1""",
            """CREATE TABLE IF NOT EXISTS relationship_memory_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                memory_id INTEGER,
                action TEXT NOT NULL,
                value_sha256 TEXT,
                created_at REAL NOT NULL
            )""",
            )
            for statement in statements:
                self.db.execute(statement)
            self.db.execute(f"PRAGMA application_id={RELATIONSHIP_MEMORY_APPLICATION_ID}")
            self.db.execute(f"PRAGMA user_version={RELATIONSHIP_MEMORY_SCHEMA_VERSION}")
            self.db.commit()
        except BaseException:
            if self.db.in_transaction:
                self.db.rollback()
            raise
        self.db.execute("PRAGMA journal_mode=WAL")

    @staticmethod
    def _text(value: Any, label: str, maximum: int) -> str:
        text = redact_secrets(str(value)).strip()
        if not text or len(text) > maximum:
            raise ValueError(f"{label} must be between 1 and {maximum} characters")
        if contains_secret(str(value)):
            raise ValueError(f"{label} may not contain credentials or secrets")
        return text

    def remember(
        self,
        *,
        kind: str,
        subject: str,
        value: str,
        source: str = "explicit user statement",
        visibility: str = "companion",
        confidence: float = 1.0,
        expires_at: float | None = None,
        confirmed_public: bool = False,
    ) -> int:
        normalized_kind = str(kind).strip().casefold()
        if normalized_kind not in RELATIONSHIP_KINDS:
            raise ValueError("unsupported relationship-memory kind")
        safe_subject = self._text(subject, "subject", 200).casefold()
        safe_value = self._text(value, "value", 4_000)
        safe_source = self._text(source, "source", 500)
        safe_visibility = str(visibility).strip().casefold()
        if safe_visibility not in _VISIBILITY:
            raise ValueError("relationship-memory visibility is invalid")
        if safe_visibility == "studio" and not confirmed_public:
            raise PermissionError("public relationship memory requires explicit confirmation")
        score = float(confidence)
        if not 0.0 <= score <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
        expiry = None if expires_at is None else float(expires_at)
        now = time.time()
        with self.db:
            previous = self.db.execute(
                """SELECT id FROM relationship_memories
                   WHERE kind=? AND subject=? AND active=1""",
                (normalized_kind, safe_subject),
            ).fetchone()
            previous_id = None if previous is None else int(previous["id"])
            if previous_id is not None:
                self.db.execute(
                    "UPDATE relationship_memories SET active=0, updated_at=? WHERE id=?",
                    (now, previous_id),
                )
            cursor = self.db.execute(
                """INSERT INTO relationship_memories(
                       kind, subject, value, visibility, source, confidence,
                       active, supersedes_id, created_at, updated_at, expires_at
                   ) VALUES(?,?,?,?,?,?,1,?,?,?,?)""",
                (
                    normalized_kind,
                    safe_subject,
                    safe_value,
                    safe_visibility,
                    safe_source,
                    score,
                    previous_id,
                    now,
                    now,
                    expiry,
                ),
            )
            memory_id = int(cursor.lastrowid)
            digest = hashlib.sha256(safe_value.encode("utf-8")).hexdigest()
            self.db.execute(
                """INSERT INTO relationship_memory_events(
                       memory_id, action, value_sha256, created_at
                   ) VALUES(?,?,?,?)""",
                (memory_id, "remembered", digest, now),
            )
        return memory_id

    def list_for_mode(self, mode: PresenceMode, *, limit: int = 100) -> list[dict[str, Any]]:
        selected = PresenceMode(mode)
        allowed: tuple[str, ...]
        if selected is PresenceMode.STUDIO:
            allowed = ("studio",)
        elif selected is PresenceMode.COMPANION:
            allowed = ("companion", "studio")
        elif selected is PresenceMode.PRIVATE:
            allowed = ("private", "companion", "studio")
        else:
            allowed = ("companion", "studio")
        placeholders = ",".join("?" for _ in allowed)
        now = time.time()
        rows = self.db.execute(
            f"""SELECT id, kind, subject, value, visibility, source, confidence,
                       created_at, updated_at, expires_at
                FROM relationship_memories
                WHERE active=1 AND visibility IN ({placeholders})
                  AND (expires_at IS NULL OR expires_at>?)
                ORDER BY updated_at DESC, id DESC LIMIT ?""",
            (*allowed, now, max(1, min(int(limit), 500))),
        ).fetchall()
        return [dict(row) for row in rows]

    def history(self, kind: str, subject: str) -> list[dict[str, Any]]:
        rows = self.db.execute(
            """SELECT id, kind, subject, value, visibility, source, confidence,
                      active, supersedes_id, created_at, updated_at, expires_at
               FROM relationship_memories WHERE kind=? AND subject=?
               ORDER BY created_at DESC, id DESC""",
            (str(kind).strip().casefold(), str(subject).strip().casefold()),
        ).fetchall()
        return [dict(row) for row in rows]

    def forget(self, memory_id: int) -> bool:
        normalized = int(memory_id)
        with self.db:
            row = self.db.execute(
                "SELECT value, supersedes_id FROM relationship_memories WHERE id=?",
                (normalized,),
            ).fetchone()
            if row is None:
                return False
            digest = hashlib.sha256(str(row["value"]).encode("utf-8")).hexdigest()
            # Preserve the historical chain when an older record is forgotten.
            # A direct successor now points to the forgotten row's predecessor
            # (or to NULL for the first record) before the FK-protected delete.
            self.db.execute(
                """UPDATE relationship_memories SET supersedes_id=?
                   WHERE supersedes_id=?""",
                (row["supersedes_id"], normalized),
            )
            self.db.execute("DELETE FROM relationship_memories WHERE id=?", (normalized,))
            self.db.execute(
                """INSERT INTO relationship_memory_events(
                       memory_id, action, value_sha256, created_at
                   ) VALUES(NULL,?,?,?)""",
                ("forgotten", digest, time.time()),
            )
        return True

    def forget_all(self) -> int:
        with self.db:
            count = int(self.db.execute(
                "SELECT COUNT(*) FROM relationship_memories"
            ).fetchone()[0])
            self.db.execute("DELETE FROM relationship_memories")
            self.db.execute(
                """INSERT INTO relationship_memory_events(
                       memory_id, action, value_sha256, created_at
                   ) VALUES(NULL,'forgotten_all',NULL,?)""",
                (time.time(),),
            )
        return count
