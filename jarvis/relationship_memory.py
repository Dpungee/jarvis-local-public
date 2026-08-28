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


class RelationshipMemory:
    """A separate, user-editable store that cannot become operational truth."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path))
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self._migrate()

    def __enter__(self) -> RelationshipMemory:
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def close(self) -> None:
        self.db.close()

    def _migrate(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS relationship_memories (
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
            CREATE UNIQUE INDEX IF NOT EXISTS idx_relationship_active
                ON relationship_memories(kind, subject) WHERE active=1;
            CREATE TABLE IF NOT EXISTS relationship_memory_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                memory_id INTEGER,
                action TEXT NOT NULL,
                value_sha256 TEXT,
                created_at REAL NOT NULL
            );
            """
        )
        self.db.commit()

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
                "SELECT value FROM relationship_memories WHERE id=?", (normalized,)
            ).fetchone()
            if row is None:
                return False
            digest = hashlib.sha256(str(row["value"]).encode("utf-8")).hexdigest()
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

