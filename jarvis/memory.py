from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
import struct
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Iterator
from uuid import uuid4

from .claim_clock import (
    DEFAULT_HAZARD_PER_DAY,
    MIN_HAZARD_PAIRS,
    age_days as claim_age_days,
    effective_confidence as claim_effective_confidence,
    estimate_hazard as estimate_claim_hazard,
    protected_predicate,
    source_key as claim_source_key,
)
from .redaction import (
    contains_secret, is_redacted_descriptor, is_sensitive_key, redact_secrets,
)
from .specialists import (
    SPECIALISTS,
    SPECIALIST_BY_KEY,
    specialist_for_consultation_prompt,
    specialist_for_family,
    specialist_for_prompt,
    specialist_for_scheduled_prompt,
)
from .vault import Vault, VaultNote


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def training_prompt_split(prompt: str, task_kind: str) -> str:
    """Keep every response for one normalized task prompt in the same data split."""
    split_key = json.dumps(
        {
            "prompt": " ".join(str(prompt).casefold().split()),
            "task_kind": str(task_kind).strip().casefold(),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    bucket = int(hashlib.sha256(split_key.encode("utf-8")).hexdigest()[:8], 16) % 100
    return "train" if bucket < 80 else "validation" if bucket < 90 else "test"


SCHEMA_VERSION = 36


_PERSISTENT_READ_APPROVAL_TOOLS = frozenset({
    "computer_list_files",
    "computer_read_file",
    "computer_search_files",
    "computer_storage_report",
})
_PAIRING_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
_PAIRING_PBKDF2_ROUNDS = 150_000
DEFAULT_BUSY_TIMEOUT_MS = 5_000
DEFAULT_LEASE_SECONDS = 3_600
TERMINAL_TASK_STATUSES = frozenset({"done", "failed", "cancelled"})
MAX_WORKER_ID_CHARS = 500
MAX_SEARCH_QUERY_CHARS = 5_000
MAX_TASK_RESULT_CHARS = 100_000
MAX_TASK_ERROR_CHARS = 10_000
MAX_MEMORY_QUERY_TERMS = 8
MAX_MEMORY_SEARCH_CANDIDATES = 2_000
MAX_QUERY_EMBEDDING_CACHE = 2_048


class ModelBudgetExceeded(RuntimeError):
    """Raised before a provider call would exceed one request-lineage budget."""

CLAIM_AUTHORITIES = frozenset({"external", "learned", "verified", "operator"})
_CLAIM_AUTHORITY_WEIGHT = {
    "external": 10,
    "learned": 30,
    "verified": 70,
    "operator": 100,
}
_EXPLICIT_USER_POSTAL_CODE = re.compile(
    r"(?:\bmy\s+zip(?:\s*code)?\s*(?:is|=|:)?\s*"
    r"|\bzip(?:\s*code)?\s+is\s+"
    r"|\bi\s+(?:live|reside|am\s+located)\s+(?:in|near)\s+"
    r"(?:zip(?:\s*code)?\s*)?)"
    r"([0-9]{5})(?:-[0-9]{4})?\b",
    re.I,
)

_MEMORY_SEARCH_STOPWORDS = frozenset({
    "about", "after", "also", "been", "before", "could", "does", "explain",
    "from", "have", "into", "just", "more", "please", "should", "tell", "than",
    "that", "their", "there", "these", "they", "this", "using", "what", "when",
    "where", "which", "with", "would", "your",
})
_LIKE_LITERAL_EDGE_CHARS = "\"'`.,!?;:()[]{}<>"
_AMBIGUOUS_LEARNING_REFERENCE = re.compile(
    r"\b(?:all|any|some)\s+of\s+(?:those|these|them)\b|"
    r"\b(?:do|add|install|remove|delete|upload|send|build|fix)\s+(?:it|that|those|these|them)\b",
    re.I,
)
_ACTION_LEARNING_REQUEST = re.compile(
    r"^\s*(?:(?:ok|okay|now|please|also)\b[, ]*)*"
    r"(?:i\s+(?:want|need)\s+you\s+to\s+|can\s+you\s+|go\s+(?:and\s+)?)?"
    r"(?:add|install|remove|delete|upload|send|clean|organize|publish|deploy)\b",
    re.I,
)


def _validated_learning_topic(topic: Any) -> str:
    """Return a durable subject, rejecting commands and unresolved anaphora."""
    normalized = redact_secrets(" ".join(str(topic).strip().split()))
    if not normalized:
        raise ValueError("Learning topic must not be empty")
    if len(normalized) > 500:
        raise ValueError("Learning topic exceeds the 500 character limit")
    if (
        _AMBIGUOUS_LEARNING_REFERENCE.search(normalized)
        or _ACTION_LEARNING_REQUEST.search(normalized)
    ):
        raise ValueError(
            "Learning topic must be a self-contained subject, not an action or unresolved reference"
        )
    return normalized


def _bounded_persisted_text(value: Any, limit: int, label: str) -> str:
    text = str(value)
    if len(text) <= limit:
        return text
    suffix = f"\n...[{label} clipped before persistence]"
    return text[: max(0, limit - len(suffix))] + suffix


def _redacted_json_value(value: Any) -> Any:
    """Recursively redact strings while preserving valid structured JSON."""
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = redact_secrets(str(raw_key))
            sensitive_key = is_sensitive_key(str(raw_key))
            protected_descriptor = is_redacted_descriptor(item)
            cleaned[key] = (
                "[REDACTED]"
                if sensitive_key and not protected_descriptor
                else _redacted_json_value(item)
            )
        return cleaned
    if isinstance(value, (list, tuple, set)):
        return [_redacted_json_value(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_secrets(str(value))


def _redacted_json_text(value: Any, *, default: Any = str) -> str:
    return json.dumps(
        _redacted_json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=default,
    )


def _validated_nonsecret_metadata(value: Any, label: str) -> str:
    text = str(value).strip()
    if contains_secret(text):
        raise ValueError(f"{label} must not contain a credential or secret")
    return text


def _validated_worker_id(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("Worker id must be a string")
    owner = value.strip()
    if not owner:
        raise ValueError("Worker id must not be empty")
    if len(owner) > MAX_WORKER_ID_CHARS:
        raise ValueError(f"Worker id exceeds {MAX_WORKER_ID_CHARS} characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in owner):
        raise ValueError("Worker id contains control characters")
    if contains_secret(owner):
        raise ValueError("Worker id must not contain a credential or secret")
    return owner




def _as_utc(value: datetime | None = None) -> datetime:
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _bounded_limit(value: int, maximum: int) -> int:
    return max(0, min(int(value), maximum))


def _normalize_memory_token(token: str) -> str:
    """Apply a deliberately small amount of stemming for durable-memory lookup."""
    token = token.casefold()
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 4 and token.endswith("es") and token[-3] in "sxz":
        return token[:-2]
    if len(token) > 3 and token.endswith("s") and not token.endswith(("ss", "us", "is")):
        return token[:-1]
    return token


def _memory_tokens(value: str, *, meaningful_only: bool) -> list[str]:
    tokens = [
        _normalize_memory_token(token)
        for token in re.findall(r"[^\W_]+", str(value).casefold(), flags=re.UNICODE)
    ]
    if meaningful_only:
        tokens = [
            token
            for token in tokens
            if len(token) >= 2 and token not in _MEMORY_SEARCH_STOPWORDS
        ]
    return tokens


def _memory_query_terms(query: str) -> list[str]:
    """Return bounded, de-duplicated terms while retaining query order."""
    terms: list[str] = []
    for token in _memory_tokens(query, meaningful_only=True):
        if token not in terms:
            terms.append(token)
        if len(terms) >= MAX_MEMORY_QUERY_TERMS:
            break
    return terms


def _memory_like_terms(query: str, semantic_terms: list[str]) -> list[str]:
    """Keep SQL wildcard characters literal without weakening semantic ranking."""
    literal_terms = []
    for raw in query.casefold().split():
        raw = raw.strip(_LIKE_LITERAL_EDGE_CHARS)
        if len(raw) > 2 and ("%" in raw or "_" in raw):
            literal_terms.append(raw)
    literal_semantic_terms = set(
        _memory_tokens(" ".join(literal_terms), meaningful_only=True)
    )
    semantic_candidates: list[str] = []
    for term in semantic_terms:
        if term in literal_semantic_terms:
            continue
        semantic_candidates.append(term)
        if len(term) > 3 and term.endswith("y") and term[-2] not in "aeiou":
            # LIKE cannot apply the token normalization used by the final ranker.
            semantic_candidates.append(term[:-1] + "ies")
    ordered = [*literal_terms, *semantic_candidates]
    unique: list[str] = []
    for term in ordered:
        if term not in unique:
            unique.append(term)
        if len(unique) >= MAX_MEMORY_QUERY_TERMS * 2:
            break
    return unique


def _memory_fts_query(query: str, query_terms: list[str]) -> str | None:
    """Build a literal OR query; wildcard-bearing input keeps the LIKE fallback."""
    if not query_terms or "%" in query or "_" in query:
        return None
    quoted = [f'"{term.replace(chr(34), chr(34) * 2)}"' for term in query_terms]
    return " OR ".join(quoted)


def _rank_memory_rows(
    rows: list[sqlite3.Row],
    query_terms: list[str],
    *,
    keep_id: bool = False,
) -> list[dict[str, Any]]:
    """Rank candidates by query coverage, phrase fidelity, then BM25 relevance."""
    if not rows or not query_terms:
        return []

    documents = [_memory_tokens(row["content"], meaningful_only=False) for row in rows]
    document_frequencies = Counter(
        term
        for tokens in documents
        for term in set(tokens).intersection(query_terms)
    )
    average_length = sum(len(tokens) for tokens in documents) / max(1, len(documents))
    normalized_query = " ".join(query_terms)
    scored: list[tuple[tuple[float, ...], sqlite3.Row]] = []

    for row, tokens in zip(rows, documents, strict=True):
        row_keys = set(row.keys())
        frequencies = Counter(tokens)
        matched = [term for term in query_terms if frequencies[term]]
        if not matched:
            # The SQL prefilter is substring-based; discard accidental partial matches.
            continue
        coverage = len(matched) / len(query_terms)
        normalized_document = " ".join(tokens)
        exact_document = float(normalized_document == normalized_query)
        phrase_match = float(normalized_query in normalized_document)
        length_ratio = len(tokens) / max(1.0, average_length)
        bm25 = 0.0
        for term in matched:
            frequency = frequencies[term]
            frequency_weight = (
                frequency * 2.2
                / (frequency + 1.2 * (0.25 + 0.75 * length_ratio))
            )
            inverse_frequency = math.log(
                1.0
                + (len(rows) - document_frequencies[term] + 0.5)
                / (document_frequencies[term] + 0.5)
            )
            bm25 += inverse_frequency * frequency_weight
        density = len(matched) / max(1, len(tokens))
        resolved_uses = (
            max(0, int(row["utility_resolved"] or 0))
            if "utility_resolved" in row_keys
            else 0
        )
        successful_uses = (
            max(0, int(row["utility_successes"] or 0))
            if "utility_successes" in row_keys
            else 0
        )
        observed_utility = (successful_uses + 2.0) / (resolved_uses + 4.0)
        learned_utility = 0.5 + (
            observed_utility - 0.5
        ) * min(1.0, resolved_uses / 10.0)
        score = (
            coverage,
            exact_document,
            phrase_match,
            learned_utility,
            bm25,
            density,
            float(row["id"]),
        )
        scored.append((score, row))

    scored.sort(key=lambda item: item[0], reverse=True)
    results: list[dict[str, Any]] = []
    for _, row in scored:
        result = dict(row)
        result.pop("utility_resolved", None)
        result.pop("utility_successes", None)
        memory_id = result.pop("id", None)
        if keep_id and memory_id is not None:
            result["memory_id"] = int(memory_id)
        results.append(result)
    return results


class Memory:
    ORDINARY_MEMORY_PROVENANCE_ORIGINS = frozenset({
        "explicit_operator_memory",
        "explicit_user_feedback",
        "verified_learning",
        "verified_vault_note",
        "verified_import",
    })
    SCREEN_COMPANION_LEARNING_CATEGORIES = frozenset({
        "coding", "general", "navigation", "organization", "research", "writing",
    })
    SCREEN_COMPANION_LEARNING_DECISIONS = frozenset({"accepted", "dismissed"})
    SCREEN_COMPANION_LEARNING_OUTCOMES = frozenset({
        "complete", "failed", "incomplete",
    })
    SCREEN_COMPANION_LEARNING_EVIDENCE = frozenset({
        "cited_sources", "failure_observed", "process_evidence", "tool_success",
    })
    SCREEN_COMPANION_PREDICTION_ORIGINS = frozenset({
        "companion_action", "companion_suggestion",
    })
    PREDICTION_FAMILIES = frozenset({
        "code_build", "code_fix", "code_refactor", "code_test", "deep_research",
        "learning_brief", "file_ops", "desktop_file_ops", "external_publish",
        "security_analysis", "conversation",
    })
    PREDICTION_FAILURE_CLASSES = frozenset({
        "misread_spec", "wrong_target_file", "verification_absent",
        "verification_vacuous", "tool_denied_policy", "approval_required",
        "budget_exhausted", "model_unavailable", "model_hallucinated_api",
        "research_no_authoritative_source", "edit_conflict_hash", "probe_failed",
        "cancelled", "unknown",
    })
    PREDICTION_ORIGINS = frozenset({
        "companion_action", "companion_suggestion", "interactive", "worker",
        "proactive", "practice",
    })
    PREDICTION_VERIFICATION = frozenset({
        "process_evidence", "cited_sources", "tool_success", "not_applicable",
    })

    def __init__(
        self,
        path: Path,
        *,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        worker_id: str | None = None,
    ) -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.worker_id = _validated_worker_id(
            f"{os.getpid()}:{uuid4().hex}" if worker_id is None else worker_id
        )
        self._closed = False
        self._claim_clock_ready = False
        self.vault: Vault | None = None
        busy_timeout_ms = max(100, min(int(busy_timeout_ms), 120_000))
        self.db = sqlite3.connect(
            str(self.path),
            timeout=busy_timeout_ms / 1000,
            isolation_level=None,
        )
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
            if str(self.path) != ":memory:":
                self.db.execute("PRAGMA journal_mode=WAL").fetchone()
            self.db.execute("PRAGMA synchronous=FULL")
            self._migrate()
            self._claim_clock_ready = True
        except BaseException:
            self.db.close()
            self._closed = True
            raise

    def configure_vault(self, vault_dir: Path | None) -> None:
        """Attach the optional human-readable mirror without changing SQLite authority."""
        self.vault = Vault(vault_dir)

    def begin_vault_task(self) -> None:
        if self.vault is not None:
            self.vault.begin_task()

    def _mirror_vault_note(
        self,
        kind: str,
        title: str,
        body: str,
        *,
        tags: tuple[str, ...] = (),
        links: tuple[str, ...] = (),
        source: str | None = None,
    ) -> None:
        if self.vault is None or not self.vault.enabled:
            return
        try:
            self.vault.write_note(
                kind, title, body, tags=tags, links=links, source=source
            )
        except Exception:
            # The Obsidian vault is a derived convenience mirror. A broken or
            # unavailable mirror must never roll back the canonical SQLite row.
            return

    def sync_vault_notes(self, notes: list[VaultNote]) -> dict[str, int]:
        """Synchronize derived vault search records into canonical memory indexing."""
        desired = {
            f"vault:{note.kind}:"
            f"{hashlib.sha256(note.relative_path.encode('utf-8')).hexdigest()}": (
                f"Vault note: {note.title}\n{note.search_text}".strip()
            )
            for note in notes
            # Lesson notes are a human-readable mirror of canonical lesson rows.
            # Re-importing them as ordinary ``vault`` memories would bypass the
            # provenance, family, outcome, and calibration checks in
            # ``match_lessons``. Human-edited lesson notes must remain inert too.
            if str(note.kind).casefold() != "lessons"
        }
        inserted = 0
        updated = 0
        removed = 0
        with self._immediate_transaction():
            existing = {
                str(row["source"]): (int(row["id"]), str(row["content"]))
                for row in self.db.execute(
                    "SELECT id, source, content FROM memories WHERE kind='vault'"
                ).fetchall()
                if row["source"] is not None
            }
            stale_ids = [
                memory_id
                for source, (memory_id, _content) in existing.items()
                if source not in desired
            ]
            if stale_ids:
                placeholders = ",".join("?" for _ in stale_ids)
                for table in (
                    "memory_retrievals",
                    "memory_statistics",
                    "memory_embeddings",
                    "memory_embedding_leases",
                    "ordinary_memory_provenance",
                ):
                    self.db.execute(
                        f"DELETE FROM {table} WHERE memory_id IN ({placeholders})",
                        stale_ids,
                    )
                self.db.execute(
                    f"DELETE FROM memories WHERE id IN ({placeholders})", stale_ids
                )
                removed = len(stale_ids)
            stamp = now_iso()
            for source, content in desired.items():
                current = existing.get(source)
                if current is None:
                    cursor = self.db.execute(
                        """INSERT INTO memories(created_at, kind, content, source)
                           VALUES (?, 'vault', ?, ?)""",
                        (stamp, content, source),
                    )
                    self._set_ordinary_memory_provenance_locked(
                        int(cursor.lastrowid),
                        origin="verified_vault_note",
                        eligible=True,
                    )
                    inserted += 1
                elif current[1] != content:
                    self.db.execute(
                        "UPDATE memories SET content=? WHERE id=?",
                        (content, current[0]),
                    )
                    self._set_ordinary_memory_provenance_locked(
                        int(current[0]),
                        origin="verified_vault_note",
                        eligible=True,
                    )
                    updated += 1
                else:
                    self._set_ordinary_memory_provenance_locked(
                        int(current[0]),
                        origin="verified_vault_note",
                        eligible=True,
                    )
        return {
            "notes": len(desired),
            "inserted": inserted,
            "updated": updated,
            "removed": removed,
        }

    def vault_index_status(
        self, notes: list[VaultNote], *, model: str | None = None
    ) -> dict[str, int | bool]:
        desired = {
            f"vault:{note.kind}:"
            f"{hashlib.sha256(note.relative_path.encode('utf-8')).hexdigest()}":
            f"Vault note: {note.title}\n{note.search_text}".strip()
            for note in notes
            if str(note.kind).casefold() != "lessons"
        }
        existing = {
            str(row["source"]): str(row["content"])
            for row in self.db.execute(
                "SELECT source, content FROM memories WHERE kind='vault'"
            ).fetchall()
            if row["source"] is not None
        }
        semantic_indexed = 0
        if model:
            for source, content in desired.items():
                row = self.db.execute(
                    """SELECT e.content_sha256
                       FROM memories AS m
                       JOIN memory_embeddings AS e ON e.memory_id=m.id
                       WHERE m.kind='vault' AND m.source=? AND e.model=?""",
                    (source, str(model)),
                ).fetchone()
                if row is not None and str(row["content_sha256"]) == hashlib.sha256(
                    content.encode("utf-8")
                ).hexdigest():
                    semantic_indexed += 1
        records_fresh = desired == existing
        semantic_fresh = model is None or semantic_indexed == len(desired)
        return {
            "notes": len(desired),
            "indexed": len(existing),
            "semantic_indexed": semantic_indexed,
            "fresh": records_fresh and semantic_fresh,
        }

    @property
    def closed(self) -> bool:
        return self._closed

    def __enter__(self) -> "Memory":
        if self._closed:
            raise RuntimeError("Memory database is closed")
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        if self.db.in_transaction:
            self.db.rollback()
        self.db.close()
        self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("Memory database is closed")

    @contextmanager
    def _immediate_transaction(self) -> Iterator[None]:
        self._ensure_open()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            if self.db.in_transaction:
                self.db.rollback()
            raise
        else:
            self.db.commit()

    def _migrate(self) -> None:
        version = int(self.db.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"Database schema version {version} is newer than supported version {SCHEMA_VERSION}"
            )
        with self._immediate_transaction():
            if version < 1:
                self._migrate_v1()
                version = 1
            if version < 2:
                self._migrate_v2()
                version = 2
            if version < 3:
                self._migrate_v3()
                version = 3
            if version < 4:
                self._migrate_v4()
                version = 4
            if version < 5:
                self._migrate_v5()
                version = 5
            if version < 6:
                self._migrate_v6()
                version = 6
            if version < 7:
                self._migrate_v7()
                version = 7
            if version < 8:
                self._migrate_v8()
                version = 8
            if version < 9:
                self._migrate_v9()
                version = 9
            if version < 10:
                self._migrate_v10()
                version = 10
            if version < 11:
                self._migrate_v11()
                version = 11
            if version < 12:
                self._migrate_v12()
                version = 12
            if version < 13:
                self._migrate_v13()
                version = 13
            if version < 14:
                self._migrate_v14()
                version = 14
            if version < 15:
                self._migrate_v15()
                version = 15
            if version < 16:
                self._migrate_v16()
                version = 16
            if version < 17:
                self._migrate_v17()
                version = 17
            if version < 18:
                self._migrate_v18()
                version = 18
            if version < 19:
                self._migrate_v19()
                version = 19
            if version < 20:
                self._migrate_v20()
                version = 20
            if version < 21:
                self._migrate_v21()
                version = 21
            if version < 22:
                self._migrate_v22()
                version = 22
            if version < 23:
                self._migrate_v23()
                version = 23
            if version < 24:
                self._migrate_v24()
                version = 24
            if version < 25:
                self._migrate_v25()
                version = 25
            if version < 26:
                self._migrate_v26()
                version = 26
            if version < 27:
                self._migrate_v27()
                version = 27
            if version < 28:
                self._migrate_v28()
                version = 28
            if version < 29:
                self._migrate_v29()
                version = 29
            if version < 30:
                self._migrate_v30()
                version = 30
            if version < 31:
                self._migrate_v31()
                version = 31
            if version < 32:
                self._migrate_v32()
                version = 32
            if version < 33:
                self._migrate_v33()
                version = 33
            if version < 34:
                self._migrate_v34()
                version = 34
            if version < 35:
                self._migrate_v35()
                version = 35
            if version < 36:
                self._migrate_v36()
                version = 36
            self.db.execute(f"PRAGMA user_version={version}")

    def _migrate_v1(self) -> None:
        statements = (
            """CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, title TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY, conversation_id INTEGER NOT NULL,
                created_at TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
                FOREIGN KEY(conversation_id) REFERENCES conversations(id)
            )""",
            """CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, kind TEXT NOT NULL,
                content TEXT NOT NULL, source TEXT, UNIQUE(kind, content)
            )""",
            """CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                status TEXT NOT NULL, prompt TEXT NOT NULL, result TEXT
            )""",
            """CREATE TABLE IF NOT EXISTS learning_topics (
                id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, topic TEXT NOT NULL UNIQUE,
                interval_hours INTEGER NOT NULL, next_run TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1
            )""",
            "CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, id)",
            "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, id)",
        )
        for statement in statements:
            self.db.execute(statement)

    def _migrate_v2(self) -> None:
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(tasks)")}
        additions = {
            "available_at": "TEXT",
            "lease_owner": "TEXT",
            "lease_expires_at": "TEXT",
            "attempt_count": "INTEGER NOT NULL DEFAULT 0",
            "max_attempts": "INTEGER NOT NULL DEFAULT 3",
            "last_error": "TEXT",
            "idempotency_key": "TEXT",
        }
        for name, definition in additions.items():
            if name not in columns:
                self.db.execute(f"ALTER TABLE tasks ADD COLUMN {name} {definition}")
        self.db.execute(
            "UPDATE tasks SET available_at=COALESCE(available_at, created_at) WHERE status='queued'"
        )
        self.db.execute(
            "UPDATE tasks SET lease_expires_at=COALESCE(lease_expires_at, updated_at) "
            "WHERE status='running'"
        )
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_claim ON tasks(status, available_at, id)"
        )
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_lease ON tasks(status, lease_expires_at)"
        )
        self.db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_idempotency "
            "ON tasks(idempotency_key) WHERE idempotency_key IS NOT NULL"
        )

    def _migrate_v3(self) -> None:
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS learning_runs (
                id INTEGER PRIMARY KEY,
                topic_id INTEGER NOT NULL,
                scheduled_for TEXT NOT NULL,
                task_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(topic_id, scheduled_for),
                FOREIGN KEY(topic_id) REFERENCES learning_topics(id),
                FOREIGN KEY(task_id) REFERENCES tasks(id)
            )"""
        )
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_learning_runs_task ON learning_runs(task_id)"
        )

    def _migrate_v4(self) -> None:
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS training_examples (
                id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL,
                conversation_id INTEGER,
                prompt TEXT NOT NULL,
                response TEXT NOT NULL,
                model TEXT NOT NULL,
                profile TEXT NOT NULL,
                task_kind TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                quality_score REAL NOT NULL CHECK(quality_score >= 0 AND quality_score <= 1),
                verified INTEGER NOT NULL CHECK(verified IN (0, 1)),
                split TEXT NOT NULL CHECK(split IN ('train', 'validation', 'test')),
                content_hash TEXT NOT NULL UNIQUE,
                FOREIGN KEY(conversation_id) REFERENCES conversations(id)
            )"""
        )
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_training_export "
            "ON training_examples(verified, quality_score, split, id)"
        )
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS evaluation_cases (
                id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL,
                name TEXT NOT NULL UNIQUE,
                prompt TEXT NOT NULL,
                expected_contains_json TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1))
            )"""
        )

    def _migrate_v5(self) -> None:
        """Add the proactive-assistant control plane without rewriting legacy data."""
        statements = (
            """CREATE TABLE IF NOT EXISTS runtime_control (
                id INTEGER PRIMARY KEY CHECK(id=1),
                state TEXT NOT NULL CHECK(state IN ('running', 'paused', 'stopped')),
                updated_at TEXT NOT NULL, reason TEXT
            )""",
            """CREATE TABLE IF NOT EXISTS activity_log (
                id INTEGER PRIMARY KEY, created_at TEXT NOT NULL,
                category TEXT NOT NULL, action TEXT NOT NULL, status TEXT NOT NULL,
                task_id INTEGER, details_json TEXT NOT NULL DEFAULT '{}'
            )""",
            """CREATE TABLE IF NOT EXISTS goals (
                id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('goal', 'project')),
                title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL CHECK(status IN ('active', 'paused', 'completed', 'cancelled')),
                priority INTEGER NOT NULL DEFAULT 50
            )""",
            """CREATE TABLE IF NOT EXISTS journal_entries (
                id INTEGER PRIMARY KEY, goal_id INTEGER NOT NULL,
                created_at TEXT NOT NULL, kind TEXT NOT NULL, content TEXT NOT NULL,
                task_id INTEGER, FOREIGN KEY(goal_id) REFERENCES goals(id)
            )""",
            """CREATE TABLE IF NOT EXISTS preferences (
                id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                name TEXT NOT NULL UNIQUE, value TEXT NOT NULL, source TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 1.0 CHECK(confidence >= 0 AND confidence <= 1),
                active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1))
            )""",
            """CREATE TABLE IF NOT EXISTS reflections (
                id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, task_id INTEGER,
                conversation_id INTEGER, status TEXT NOT NULL, summary TEXT NOT NULL,
                mistakes TEXT NOT NULL, improvements TEXT NOT NULL,
                tool_calls INTEGER NOT NULL DEFAULT 0
            )""",
            """CREATE TABLE IF NOT EXISTS approved_subjects (
                id INTEGER PRIMARY KEY, created_at TEXT NOT NULL,
                subject TEXT NOT NULL UNIQUE, notes TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1))
            )""",
            """CREATE TABLE IF NOT EXISTS proactive_backlog (
                id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('research', 'ideas', 'prototype')),
                subject_id INTEGER NOT NULL, goal_id INTEGER,
                instructions TEXT NOT NULL DEFAULT '', priority INTEGER NOT NULL DEFAULT 50,
                interval_hours INTEGER NOT NULL DEFAULT 168, next_run TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
                FOREIGN KEY(subject_id) REFERENCES approved_subjects(id),
                FOREIGN KEY(goal_id) REFERENCES goals(id)
            )""",
            """CREATE TABLE IF NOT EXISTS proactive_runs (
                id INTEGER PRIMARY KEY, backlog_id INTEGER NOT NULL, task_id INTEGER NOT NULL,
                created_at TEXT NOT NULL, completed_at TEXT, status TEXT NOT NULL,
                result_summary TEXT,
                FOREIGN KEY(backlog_id) REFERENCES proactive_backlog(id)
            )""",
            """CREATE TABLE IF NOT EXISTS approvals (
                id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                fingerprint TEXT NOT NULL, action TEXT NOT NULL, resource TEXT NOT NULL,
                reason TEXT NOT NULL, status TEXT NOT NULL
                    CHECK(status IN ('pending', 'approved', 'denied', 'consumed', 'expired')),
                expires_at TEXT, decided_at TEXT, task_id INTEGER,
                scope TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS self_snapshots (
                id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, snapshot_json TEXT NOT NULL
            )""",
            "CREATE INDEX IF NOT EXISTS idx_activity_created ON activity_log(created_at, id)",
            "CREATE INDEX IF NOT EXISTS idx_journal_goal ON journal_entries(goal_id, id)",
            "CREATE INDEX IF NOT EXISTS idx_reflections_task ON reflections(task_id, id)",
            "CREATE INDEX IF NOT EXISTS idx_backlog_due ON proactive_backlog(enabled, next_run, priority)",
            "CREATE INDEX IF NOT EXISTS idx_proactive_runs_task ON proactive_runs(task_id)",
            "CREATE INDEX IF NOT EXISTS idx_approvals_fingerprint ON approvals(fingerprint, status, id)",
        )
        for statement in statements:
            self.db.execute(statement)
        task_columns = {row["name"] for row in self.db.execute("PRAGMA table_info(tasks)")}
        if "goal_id" not in task_columns:
            self.db.execute("ALTER TABLE tasks ADD COLUMN goal_id INTEGER")
        if "backlog_id" not in task_columns:
            self.db.execute("ALTER TABLE tasks ADD COLUMN backlog_id INTEGER")
        self.db.execute(
            "INSERT OR IGNORE INTO runtime_control(id, state, updated_at, reason) "
            "VALUES (1, 'running', ?, NULL)",
            (now_iso(),),
        )

    def _migrate_v6(self) -> None:
        """Bind every live approval to an explicit execution scope."""
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(approvals)")}
        if "scope" not in columns:
            self.db.execute(
                "ALTER TABLE approvals ADD COLUMN scope TEXT NOT NULL DEFAULT 'legacy'"
            )
            stamp = now_iso()
            self.db.execute(
                "UPDATE approvals SET status='expired', updated_at=? "
                "WHERE scope='legacy' AND status IN ('pending', 'approved')",
                (stamp,),
            )
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_approvals_scope "
            "ON approvals(scope, fingerprint, status, id)"
        )

    def _migrate_v7(self) -> None:
        """Bind parked tasks to the exact approval they are awaiting."""
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(tasks)")}
        if "awaiting_approval_id" not in columns:
            self.db.execute("ALTER TABLE tasks ADD COLUMN awaiting_approval_id INTEGER")
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_awaiting_approval "
            "ON tasks(awaiting_approval_id) WHERE awaiting_approval_id IS NOT NULL"
        )

    def _migrate_v8(self) -> None:
        """Record bounded run predictions and outcomes for competence measurement."""
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS task_predictions (
                id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL,
                task_id INTEGER,
                conversation_id INTEGER,
                origin TEXT NOT NULL,
                family TEXT NOT NULL,
                profile TEXT NOT NULL,
                model TEXT NOT NULL,
                predicted_success REAL NOT NULL,
                predicted_steps INTEGER NOT NULL,
                predicted_verification TEXT NOT NULL,
                basis TEXT NOT NULL,
                resolved_at TEXT,
                actual_status TEXT,
                actual_steps INTEGER,
                evidence_ok INTEGER,
                failure_class TEXT
            )"""
        )
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_predictions_family "
            "ON task_predictions(family, resolved_at)"
        )
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_predictions_open "
            "ON task_predictions(id) WHERE resolved_at IS NULL"
        )

    def _migrate_v9(self) -> None:
        """Persist prompt-free provider latency and token measurements."""
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS model_call_metrics (
                id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                profile TEXT NOT NULL,
                latency_ms INTEGER NOT NULL CHECK(latency_ms >= 0),
                prompt_tokens INTEGER CHECK(prompt_tokens IS NULL OR prompt_tokens >= 0),
                completion_tokens INTEGER CHECK(completion_tokens IS NULL OR completion_tokens >= 0),
                success INTEGER NOT NULL CHECK(success IN (0, 1)),
                failure_kind TEXT
            )"""
        )
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_model_call_metrics_created "
            "ON model_call_metrics(created_at, id)"
        )

    def _migrate_v10(self) -> None:
        """Add explicit project ownership and per-task model routing metadata."""
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS agent_projects (
                id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                name TEXT NOT NULL,
                relative_path TEXT NOT NULL UNIQUE,
                enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1))
            )"""
        )
        stamp = now_iso()
        self.db.execute(
            """INSERT OR IGNORE INTO agent_projects(
                id, created_at, updated_at, name, relative_path, enabled
            ) VALUES (1, ?, ?, 'Default workspace', '.', 1)""",
            (stamp, stamp),
        )
        conversation_columns = {
            row["name"] for row in self.db.execute("PRAGMA table_info(conversations)")
        }
        if conversation_columns:
            if "project_id" not in conversation_columns:
                self.db.execute(
                    "ALTER TABLE conversations ADD COLUMN project_id INTEGER NOT NULL DEFAULT 1"
                )
            self.db.execute(
                "CREATE INDEX IF NOT EXISTS idx_conversations_project "
                "ON conversations(project_id, id)"
            )
        task_columns = {row["name"] for row in self.db.execute("PRAGMA table_info(tasks)")}
        if task_columns:
            if "project_id" not in task_columns:
                self.db.execute(
                    "ALTER TABLE tasks ADD COLUMN project_id INTEGER NOT NULL DEFAULT 1"
                )
            if "requested_model" not in task_columns:
                self.db.execute("ALTER TABLE tasks ADD COLUMN requested_model TEXT")
            self.db.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id, status, id)"
            )

    def _migrate_v11(self) -> None:
        """Add calibrated learning, immutable repair drafts, and gated initiative state."""
        memory_columns = {
            row["name"] for row in self.db.execute("PRAGMA table_info(memories)")
        }
        if memory_columns:
            for name, definition in {
                "family": "TEXT",
                "outcome_status": "TEXT",
                "reflection_id": "INTEGER",
            }.items():
                if name not in memory_columns:
                    self.db.execute(f"ALTER TABLE memories ADD COLUMN {name} {definition}")
            self.db.execute(
                "CREATE INDEX IF NOT EXISTS idx_memories_lessons "
                "ON memories(kind, family, outcome_status, id)"
            )
        task_columns = {
            row["name"] for row in self.db.execute("PRAGMA table_info(tasks)")
        }
        if task_columns and "initiative_event_id" not in task_columns:
            self.db.execute("ALTER TABLE tasks ADD COLUMN initiative_event_id INTEGER")
        statements = (
            """CREATE TABLE IF NOT EXISTS lesson_applications (
                id INTEGER PRIMARY KEY, created_at TEXT NOT NULL,
                prediction_id INTEGER NOT NULL, memory_id INTEGER NOT NULL,
                family TEXT NOT NULL, rank INTEGER NOT NULL,
                resolved_at TEXT, successful INTEGER,
                UNIQUE(prediction_id, memory_id)
            )""",
            """CREATE TABLE IF NOT EXISTS self_repair_proposals (
                id INTEGER PRIMARY KEY, created_at TEXT NOT NULL,
                trigger_text TEXT NOT NULL, failing_tests_json TEXT NOT NULL,
                diff_text TEXT NOT NULL, diff_sha256 TEXT NOT NULL,
                verification_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('proposed', 'voided')),
                void_reason TEXT, candidate_path TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS recovery_attestations (
                id INTEGER PRIMARY KEY, created_at TEXT NOT NULL,
                runtime_sha256 TEXT NOT NULL, schema_version INTEGER NOT NULL,
                passed INTEGER NOT NULL CHECK(passed IN (0, 1)),
                evidence_json TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS work_domains (
                id INTEGER PRIMARY KEY, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, name TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL CHECK(kind IN ('research', 'workspace_project', 'maintenance')),
                project_id INTEGER NOT NULL, max_tasks_per_day INTEGER NOT NULL,
                standing_authorization INTEGER NOT NULL CHECK(standing_authorization IN (0, 1)),
                enabled INTEGER NOT NULL CHECK(enabled IN (0, 1))
            )""",
            """CREATE TABLE IF NOT EXISTS initiative_events (
                id INTEGER PRIMARY KEY, created_at TEXT NOT NULL,
                signal_key TEXT NOT NULL UNIQUE, signal_kind TEXT NOT NULL,
                tier INTEGER NOT NULL CHECK(tier IN (0, 1)),
                domain_id INTEGER, project_id INTEGER NOT NULL,
                summary TEXT NOT NULL, evidence_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('observed', 'queued', 'running', 'done', 'failed', 'blocked')),
                task_id INTEGER, completed_at TEXT, result_summary TEXT
            )""",
            "CREATE INDEX IF NOT EXISTS idx_lesson_applications_prediction ON lesson_applications(prediction_id, rank)",
            "CREATE INDEX IF NOT EXISTS idx_recovery_attestations_created ON recovery_attestations(created_at, id)",
            "CREATE INDEX IF NOT EXISTS idx_work_domains_project ON work_domains(project_id, enabled)",
            "CREATE INDEX IF NOT EXISTS idx_initiative_events_created ON initiative_events(created_at, id)",
        )
        for statement in statements:
            self.db.execute(statement)

    def _migrate_v12(self) -> None:
        """Add persistent purpose-bound specialists and peer-blind delegation metadata."""
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS specialist_agents (
                agent_key TEXT PRIMARY KEY, name TEXT NOT NULL, purpose TEXT NOT NULL,
                model_profile TEXT NOT NULL, families_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('ready', 'working')),
                active_task_id INTEGER, completed_tasks INTEGER NOT NULL DEFAULT 0,
                failed_tasks INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                last_started_at TEXT, last_reported_at TEXT
            )"""
        )
        stamp = now_iso()
        for specialist in SPECIALISTS:
            self.db.execute(
                """INSERT OR IGNORE INTO specialist_agents(
                       agent_key, name, purpose, model_profile, families_json,
                       status, active_task_id, completed_tasks, failed_tasks,
                       created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, 'ready', NULL, 0, 0, ?, ?)""",
                (
                    specialist.key, specialist.name, specialist.purpose,
                    specialist.model_profile,
                    json.dumps(specialist.families, separators=(",", ":")),
                    stamp, stamp,
                ),
            )
            self.db.execute(
                """UPDATE specialist_agents
                   SET name=?, purpose=?, model_profile=?, families_json=?, updated_at=?
                   WHERE agent_key=?""",
                (
                    specialist.name, specialist.purpose, specialist.model_profile,
                    json.dumps(specialist.families, separators=(",", ":")),
                    stamp, specialist.key,
                ),
            )
        task_columns = {
            row["name"] for row in self.db.execute("PRAGMA table_info(tasks)")
        }
        if task_columns:
            additions = {
                "specialist_key": "TEXT",
                "delegated_by": "TEXT",
                "parent_conversation_id": "INTEGER",
            }
            for name, definition in additions.items():
                if name not in task_columns:
                    self.db.execute(f"ALTER TABLE tasks ADD COLUMN {name} {definition}")
            self.db.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_specialist "
                "ON tasks(specialist_key, status, id)"
            )
            self.db.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_parent_conversation "
                "ON tasks(parent_conversation_id, id)"
            )

    def _migrate_v13(self) -> None:
        """Add neural recall plus outcome-grounded, automatically learned utility."""
        statements = (
            """CREATE TABLE IF NOT EXISTS memory_embeddings (
                memory_id INTEGER NOT NULL, model TEXT NOT NULL,
                dimensions INTEGER NOT NULL CHECK(dimensions BETWEEN 1 AND 4096),
                content_sha256 TEXT NOT NULL, embedding_json TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY(memory_id, model),
                FOREIGN KEY(memory_id) REFERENCES memories(id)
            )""",
            """CREATE TABLE IF NOT EXISTS memory_retrievals (
                id INTEGER PRIMARY KEY, created_at TEXT NOT NULL,
                prediction_id INTEGER NOT NULL, conversation_id INTEGER,
                family TEXT NOT NULL, query_sha256 TEXT NOT NULL,
                memory_id INTEGER NOT NULL, rank INTEGER NOT NULL,
                channel TEXT NOT NULL CHECK(channel IN ('lexical', 'semantic', 'hybrid')),
                resolved_at TEXT, successful INTEGER CHECK(successful IN (0, 1)),
                UNIQUE(prediction_id, memory_id),
                FOREIGN KEY(memory_id) REFERENCES memories(id)
            )""",
            """CREATE TABLE IF NOT EXISTS memory_statistics (
                memory_id INTEGER PRIMARY KEY,
                retrievals INTEGER NOT NULL DEFAULT 0,
                resolved INTEGER NOT NULL DEFAULT 0,
                successes INTEGER NOT NULL DEFAULT 0,
                failures INTEGER NOT NULL DEFAULT 0,
                utility REAL NOT NULL DEFAULT 0.5 CHECK(utility BETWEEN 0 AND 1),
                last_retrieved_at TEXT, last_resolved_at TEXT,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(memory_id) REFERENCES memories(id)
            )""",
            "CREATE INDEX IF NOT EXISTS idx_memory_embeddings_model ON memory_embeddings(model, memory_id)",
            "CREATE INDEX IF NOT EXISTS idx_memory_retrievals_prediction ON memory_retrievals(prediction_id, rank)",
            "CREATE INDEX IF NOT EXISTS idx_memory_retrievals_memory ON memory_retrievals(memory_id, resolved_at)",
            "CREATE INDEX IF NOT EXISTS idx_memory_statistics_utility ON memory_statistics(utility DESC, resolved DESC)",
        )
        for statement in statements:
            self.db.execute(statement)

    def _migrate_v14(self) -> None:
        """Add append-only temporal claims with explicit conflict history."""
        statements = (
            """CREATE TABLE IF NOT EXISTS memory_claims (
                id INTEGER PRIMARY KEY, memory_id INTEGER NOT NULL UNIQUE,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                claim_key TEXT NOT NULL, subject TEXT NOT NULL,
                predicate TEXT NOT NULL, value TEXT NOT NULL,
                value_sha256 TEXT NOT NULL, source TEXT NOT NULL,
                authority TEXT NOT NULL CHECK(authority IN
                    ('external', 'learned', 'verified', 'operator')),
                confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
                status TEXT NOT NULL CHECK(status IN
                    ('active', 'disputed', 'superseded')),
                valid_from TEXT NOT NULL, valid_until TEXT,
                supersedes_id INTEGER,
                FOREIGN KEY(memory_id) REFERENCES memories(id),
                FOREIGN KEY(supersedes_id) REFERENCES memory_claims(id)
            )""",
            """CREATE TABLE IF NOT EXISTS memory_claim_evidence (
                id INTEGER PRIMARY KEY, claim_id INTEGER NOT NULL,
                created_at TEXT NOT NULL, source TEXT NOT NULL,
                authority TEXT NOT NULL CHECK(authority IN
                    ('external', 'learned', 'verified', 'operator')),
                confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
                evidence_sha256 TEXT NOT NULL,
                UNIQUE(claim_id, evidence_sha256),
                FOREIGN KEY(claim_id) REFERENCES memory_claims(id)
            )""",
            """CREATE TABLE IF NOT EXISTS memory_claim_events (
                id INTEGER PRIMARY KEY, claim_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN
                    ('active', 'disputed', 'superseded')),
                reason TEXT NOT NULL, related_claim_id INTEGER,
                FOREIGN KEY(claim_id) REFERENCES memory_claims(id),
                FOREIGN KEY(related_claim_id) REFERENCES memory_claims(id)
            )""",
            "CREATE INDEX IF NOT EXISTS idx_memory_claims_key ON memory_claims(claim_key, status, id)",
            "CREATE INDEX IF NOT EXISTS idx_memory_claims_memory ON memory_claims(memory_id)",
            "CREATE INDEX IF NOT EXISTS idx_memory_claim_events_claim ON memory_claim_events(claim_id, id)",
            "CREATE INDEX IF NOT EXISTS idx_memory_claim_evidence_claim ON memory_claim_evidence(claim_id, id)",
        )
        for statement in statements:
            self.db.execute(statement)
        preference_columns = {
            row["name"] for row in self.db.execute("PRAGMA table_info(preferences)")
        }
        if {"name", "value", "source", "confidence", "active"}.issubset(
            preference_columns
        ):
            legacy_preferences = self.db.execute(
                """SELECT name, value, source, confidence, updated_at
                   FROM preferences WHERE active=1 ORDER BY id"""
            ).fetchall()
            for row in legacy_preferences:
                safe_source = redact_secrets(
                    str(row["source"] or "legacy preference")
                )[:100]
                authority = (
                    "operator"
                    if safe_source.casefold() in {
                        "user", "explicit user preference", "explicit user feedback"
                    }
                    else "verified"
                    if safe_source.casefold().startswith("verified")
                    else "learned"
                )
                raw_confidence = float(row["confidence"] or 0.0)
                confidence = raw_confidence if math.isfinite(raw_confidence) else 0.0
                self._remember_claim_locked(
                    "user",
                    f"preference:{str(row['name']).casefold()[:100]}",
                    redact_secrets(str(row["value"]))[:2_000],
                    source=safe_source or "legacy preference",
                    authority=authority,
                    confidence=max(0.0, min(confidence, 1.0)),
                    stamp=str(row["updated_at"] or now_iso()),
                )

    def _migrate_v15(self) -> None:
        """Persist accepted Presence turns without replaying uncertain effects."""
        statements = (
            """CREATE TABLE IF NOT EXISTS presence_jobs (
                job_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                conversation_id INTEGER NOT NULL, project_id INTEGER NOT NULL,
                prompt TEXT NOT NULL,
                attachments_json TEXT NOT NULL DEFAULT '[]',
                model_override TEXT NOT NULL CHECK(model_override IN
                    ('auto', 'fast', 'reasoning', 'coding', 'deep')),
                status TEXT NOT NULL CHECK(status IN
                    ('queued', 'running', 'completed', 'failed',
                     'cancelled', 'interrupted')),
                lease_owner TEXT, started_at TEXT, finished_at TEXT,
                cancel_requested INTEGER NOT NULL DEFAULT 0
                    CHECK(cancel_requested IN (0, 1)),
                last_error TEXT,
                FOREIGN KEY(conversation_id) REFERENCES conversations(id),
                FOREIGN KEY(project_id) REFERENCES agent_projects(id)
            )""",
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_presence_jobs_live_conversation
               ON presence_jobs(conversation_id)
               WHERE status IN ('queued', 'running')""",
            """CREATE INDEX IF NOT EXISTS idx_presence_jobs_status_created
               ON presence_jobs(status, created_at, job_id)""",
        )
        for statement in statements:
            self.db.execute(statement)

    def _migrate_v16(self) -> None:
        """Add one-time Presence pairing and revocable remote sessions."""
        statements = (
            """CREATE TABLE IF NOT EXISTS presence_pairing_codes (
                id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
                label TEXT NOT NULL, code_salt BLOB NOT NULL,
                code_digest BLOB NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending','consumed','revoked')),
                consumed_at TEXT
            )""",
            """CREATE INDEX IF NOT EXISTS idx_presence_pairing_status_expiry
               ON presence_pairing_codes(status, expires_at, id)""",
            """CREATE TABLE IF NOT EXISTS presence_sessions (
                session_id TEXT PRIMARY KEY, session_digest TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL, revoked_at TEXT,
                label TEXT NOT NULL, pairing_code_id INTEGER NOT NULL,
                FOREIGN KEY(pairing_code_id) REFERENCES presence_pairing_codes(id)
            )""",
            """CREATE INDEX IF NOT EXISTS idx_presence_sessions_live
               ON presence_sessions(revoked_at, expires_at, created_at)""",
        )
        for statement in statements:
            self.db.execute(statement)

    def _migrate_v17(self) -> None:
        """Lease neural indexing and store vectors as bounded float32 blobs."""
        columns = {
            row["name"] for row in self.db.execute("PRAGMA table_info(memory_embeddings)")
        }
        for name, definition in {
            "embedding_blob": "BLOB",
            "vector_norm": "REAL",
        }.items():
            if name not in columns:
                self.db.execute(
                    f"ALTER TABLE memory_embeddings ADD COLUMN {name} {definition}"
                )
        statements = (
            """CREATE TABLE IF NOT EXISTS memory_embedding_leases (
                memory_id INTEGER NOT NULL, model TEXT NOT NULL,
                content_sha256 TEXT NOT NULL, lease_owner TEXT,
                lease_expires_at TEXT NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT, updated_at TEXT NOT NULL,
                PRIMARY KEY(memory_id, model),
                FOREIGN KEY(memory_id) REFERENCES memories(id)
            )""",
            """CREATE INDEX IF NOT EXISTS idx_memory_embedding_leases_due
               ON memory_embedding_leases(model, lease_expires_at, memory_id)""",
        )
        for statement in statements:
            self.db.execute(statement)

    def _migrate_v18(self) -> None:
        """Index exact memory and session text without changing canonical records."""
        # A few very old/recovered databases can contain only the task/approval
        # control plane while still advertising a later schema version. FTS
        # external-content tables require their canonical sources at creation,
        # so restore those sources first. The source rows remain authoritative;
        # the FTS tables below are always derived and rebuildable.
        source_statements = (
            """CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, title TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY, conversation_id INTEGER NOT NULL,
                created_at TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
                FOREIGN KEY(conversation_id) REFERENCES conversations(id)
            )""",
            """CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, kind TEXT NOT NULL,
                content TEXT NOT NULL, source TEXT, UNIQUE(kind, content)
            )""",
            "CREATE INDEX IF NOT EXISTS idx_messages_conversation "
            "ON messages(conversation_id, id)",
        )
        for statement in source_statements:
            self.db.execute(statement)
        statements = (
            """CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                   content, source, content='memories', content_rowid='id',
                   tokenize='unicode61 remove_diacritics 2'
               )""",
            """CREATE VIRTUAL TABLE IF NOT EXISTS message_fts USING fts5(
                   content, content='messages', content_rowid='id',
                   tokenize='unicode61 remove_diacritics 2'
               )""",
            """CREATE TRIGGER IF NOT EXISTS memories_fts_insert AFTER INSERT ON memories BEGIN
                   INSERT INTO memory_fts(rowid, content, source)
                   VALUES (new.id, new.content, new.source);
               END""",
            """CREATE TRIGGER IF NOT EXISTS memories_fts_delete AFTER DELETE ON memories BEGIN
                   INSERT INTO memory_fts(memory_fts, rowid, content, source)
                   VALUES ('delete', old.id, old.content, old.source);
               END""",
            """CREATE TRIGGER IF NOT EXISTS memories_fts_update AFTER UPDATE ON memories BEGIN
                   INSERT INTO memory_fts(memory_fts, rowid, content, source)
                   VALUES ('delete', old.id, old.content, old.source);
                   INSERT INTO memory_fts(rowid, content, source)
                   VALUES (new.id, new.content, new.source);
               END""",
            """CREATE TRIGGER IF NOT EXISTS messages_fts_insert AFTER INSERT ON messages BEGIN
                   INSERT INTO message_fts(rowid, content) VALUES (new.id, new.content);
               END""",
            """CREATE TRIGGER IF NOT EXISTS messages_fts_delete AFTER DELETE ON messages BEGIN
                   INSERT INTO message_fts(message_fts, rowid, content)
                   VALUES ('delete', old.id, old.content);
               END""",
            """CREATE TRIGGER IF NOT EXISTS messages_fts_update AFTER UPDATE ON messages BEGIN
                   INSERT INTO message_fts(message_fts, rowid, content)
                   VALUES ('delete', old.id, old.content);
                   INSERT INTO message_fts(rowid, content) VALUES (new.id, new.content);
               END""",
        )
        for statement in statements:
            self.db.execute(statement)
        self.db.execute("INSERT INTO memory_fts(memory_fts) VALUES ('rebuild')")
        self.db.execute("INSERT INTO message_fts(message_fts) VALUES ('rebuild')")

    def _migrate_v19(self) -> None:
        """Cache bounded query vectors without retaining raw user queries."""
        statements = (
            """CREATE TABLE IF NOT EXISTS memory_query_embeddings (
                query_sha256 TEXT NOT NULL, model TEXT NOT NULL,
                dimensions INTEGER NOT NULL CHECK(dimensions BETWEEN 1 AND 4096),
                embedding_blob BLOB NOT NULL, vector_norm REAL NOT NULL,
                created_at TEXT NOT NULL, last_used_at TEXT NOT NULL,
                hit_count INTEGER NOT NULL DEFAULT 0 CHECK(hit_count >= 0),
                PRIMARY KEY(query_sha256, model, dimensions)
            )""",
            """CREATE INDEX IF NOT EXISTS idx_memory_query_embeddings_lru
               ON memory_query_embeddings(last_used_at, query_sha256)""",
        )
        for statement in statements:
            self.db.execute(statement)

    def _migrate_v20(self) -> None:
        """Persist timestamped claim observations and learned volatility fits."""
        statements = (
            """CREATE TABLE IF NOT EXISTS memory_claim_observations (
                id INTEGER PRIMARY KEY,
                claim_id INTEGER NOT NULL,
                claim_key TEXT NOT NULL,
                predicate TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                value_sha256 TEXT NOT NULL,
                source_key TEXT NOT NULL,
                authority TEXT NOT NULL CHECK(authority IN
                    ('external', 'learned', 'verified', 'operator')),
                confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
                FOREIGN KEY(claim_id) REFERENCES memory_claims(id)
            )""",
            """CREATE TABLE IF NOT EXISTS memory_claim_volatility (
                predicate TEXT PRIMARY KEY,
                hazard_per_day REAL NOT NULL CHECK(hazard_per_day >= 0),
                pair_count INTEGER NOT NULL CHECK(pair_count >= 0),
                vocabulary_size INTEGER NOT NULL CHECK(vocabulary_size >= 2),
                fitted_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS memory_claim_clock_statistics (
                claim_id INTEGER PRIMARY KEY,
                reads INTEGER NOT NULL DEFAULT 0 CHECK(reads >= 0),
                stale_reads INTEGER NOT NULL DEFAULT 0 CHECK(stale_reads >= 0),
                last_effective_confidence REAL NOT NULL
                    CHECK(last_effective_confidence BETWEEN 0 AND 1),
                last_clock_status TEXT NOT NULL,
                last_read_at TEXT NOT NULL,
                FOREIGN KEY(claim_id) REFERENCES memory_claims(id)
            )""",
            """CREATE INDEX IF NOT EXISTS idx_claim_observations_predicate
               ON memory_claim_observations(predicate, observed_at, id)""",
            """CREATE INDEX IF NOT EXISTS idx_claim_observations_claim
               ON memory_claim_observations(claim_key, observed_at, id)""",
            """CREATE INDEX IF NOT EXISTS idx_claim_observations_value
               ON memory_claim_observations(claim_id, observed_at, id)""",
        )
        for statement in statements:
            self.db.execute(statement)
        # Legacy claims cannot recover every historical confirmation, but one
        # conservative observation preserves their latest known support time.
        legacy = self.db.execute(
            """SELECT c.id, c.claim_key, c.predicate, c.updated_at,
                      c.value_sha256, c.authority, c.confidence
               FROM memory_claims AS c
               WHERE NOT EXISTS (
                   SELECT 1 FROM memory_claim_observations AS o WHERE o.claim_id=c.id
               )"""
        ).fetchall()
        self.db.executemany(
            """INSERT INTO memory_claim_observations(
                   claim_id, claim_key, predicate, observed_at, value_sha256,
                   source_key, authority, confidence
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    int(row["id"]), str(row["claim_key"]),
                    self._claim_clock_predicate(str(row["predicate"])),
                    str(row["updated_at"]), str(row["value_sha256"]),
                    claim_source_key(str(row["authority"])),
                    str(row["authority"]), float(row["confidence"]),
                )
                for row in legacy
            ],
        )

    def _migrate_v21(self) -> None:
        """Add a durable model budget shared by a request and its specialists."""
        task_columns = {
            row["name"] for row in self.db.execute("PRAGMA table_info(tasks)")
        }
        if task_columns and "model_budget_scope" not in task_columns:
            self.db.execute("ALTER TABLE tasks ADD COLUMN model_budget_scope TEXT")
        metric_columns = {
            row["name"]
            for row in self.db.execute("PRAGMA table_info(model_call_metrics)")
        }
        if metric_columns and "budget_scope" not in metric_columns:
            self.db.execute("ALTER TABLE model_call_metrics ADD COLUMN budget_scope TEXT")
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS model_call_budget_events (
                id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL,
                completed_at TEXT,
                budget_scope TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('reserved', 'completed')),
                estimated_prompt_tokens INTEGER NOT NULL
                    CHECK(estimated_prompt_tokens >= 0),
                prompt_tokens INTEGER CHECK(prompt_tokens IS NULL OR prompt_tokens >= 0),
                completion_tokens INTEGER
                    CHECK(completion_tokens IS NULL OR completion_tokens >= 0),
                success INTEGER CHECK(success IS NULL OR success IN (0, 1))
            )"""
        )
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_model_budget_scope "
            "ON model_call_budget_events(budget_scope, id)"
        )
        if task_columns:
            self.db.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_model_budget_scope "
                "ON tasks(model_budget_scope, id) WHERE model_budget_scope IS NOT NULL"
            )

    def _migrate_v22(self) -> None:
        """Add reversible exact-effect grants for a tiny read-only tool allowlist."""
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS persistent_approval_grants (
                id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                effect_fingerprint TEXT NOT NULL UNIQUE,
                action TEXT NOT NULL,
                resource TEXT NOT NULL,
                reason TEXT NOT NULL,
                source_approval_id INTEGER,
                revoked_at TEXT,
                FOREIGN KEY(source_approval_id) REFERENCES approvals(id)
            )"""
        )
        self.db.execute(
            """CREATE INDEX IF NOT EXISTS idx_persistent_approval_grants_live
               ON persistent_approval_grants(revoked_at, id)"""
        )

    def _migrate_v23(self) -> None:
        """Add expiring, exact-effect grants restricted to one conversation scope."""
        columns = {
            row["name"]
            for row in self.db.execute(
                "PRAGMA table_info(persistent_approval_grants)"
            )
        }
        if "grant_kind" not in columns:
            self.db.execute(
                """ALTER TABLE persistent_approval_grants
                   ADD COLUMN grant_kind TEXT NOT NULL DEFAULT 'always'
                   CHECK(grant_kind IN ('always', 'session'))"""
            )
        if "scope" not in columns:
            self.db.execute(
                "ALTER TABLE persistent_approval_grants ADD COLUMN scope TEXT"
            )
        if "expires_at" not in columns:
            self.db.execute(
                "ALTER TABLE persistent_approval_grants ADD COLUMN expires_at TEXT"
            )
        self.db.execute(
            """CREATE INDEX IF NOT EXISTS idx_persistent_approval_session
               ON persistent_approval_grants(grant_kind, scope, expires_at, revoked_at)"""
        )

    def _migrate_v24(self) -> None:
        """Canonicalize pending storage reports so equivalent retries share approval."""
        from .approvals import approval_resource

        rows = self.db.execute(
            """SELECT id, action, resource, scope
               FROM approvals
               WHERE status='pending' AND action='access_private_files'"""
        ).fetchall()
        stamp = now_iso()
        for row in rows:
            try:
                parsed = json.loads(str(row["resource"]))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(parsed, dict) or parsed.get("tool") != "computer_storage_report":
                continue
            arguments = parsed.get("arguments")
            if not isinstance(arguments, dict):
                continue
            resolved_path = arguments.get("resolved_path")
            if not isinstance(resolved_path, str) or not resolved_path.strip():
                continue
            canonical_resource = approval_resource(
                "computer_storage_report",
                {
                    "path": resolved_path,
                    "limit": 100,
                    "resolved_path": resolved_path,
                },
            )
            fingerprint = self.approval_fingerprint(
                str(row["action"]), canonical_resource, str(row["scope"])
            )
            self.db.execute(
                """UPDATE approvals
                   SET resource=?, fingerprint=?, updated_at=?
                   WHERE id=? AND status='pending'""",
                (canonical_resource, fingerprint, stamp, int(row["id"])),
            )

    def _migrate_v25(self) -> None:
        """Persist image descriptors while keeping raw attachment bytes out of SQLite."""
        columns = {
            row["name"] for row in self.db.execute("PRAGMA table_info(presence_jobs)")
        }
        if columns and "attachments_json" not in columns:
            self.db.execute(
                "ALTER TABLE presence_jobs ADD COLUMN attachments_json TEXT NOT NULL DEFAULT '[]'"
            )

    def _migrate_v26(self) -> None:
        """Add durable project-scoped recurring jobs created through conversation."""
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS scheduled_jobs (
                id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                project_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                prompt TEXT NOT NULL,
                interval_minutes INTEGER NOT NULL
                    CHECK(interval_minutes BETWEEN 1 AND 525600),
                next_run_at TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
                last_run_at TEXT,
                last_task_id INTEGER,
                FOREIGN KEY(project_id) REFERENCES agent_projects(id),
                FOREIGN KEY(last_task_id) REFERENCES tasks(id)
            )"""
        )
        self.db.execute(
            """CREATE INDEX IF NOT EXISTS idx_scheduled_jobs_due
               ON scheduled_jobs(enabled, next_run_at, project_id, id)"""
        )

    def _migrate_v27(self) -> None:
        """Persist prompt-free end-to-end Presence latency and routing telemetry."""
        columns = {
            row["name"] for row in self.db.execute("PRAGMA table_info(presence_jobs)")
        }
        if columns and "metrics_json" not in columns:
            self.db.execute(
                "ALTER TABLE presence_jobs ADD COLUMN metrics_json TEXT NOT NULL DEFAULT '{}'"
            )

    def _migrate_v28(self) -> None:
        """Persist one bounded, conversation-scoped goal ledger for safe resumption."""
        statements = (
            """CREATE TABLE IF NOT EXISTS conversation_goals (
                id INTEGER PRIMARY KEY,
                conversation_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN
                    ('active', 'incomplete', 'complete', 'cancelled', 'superseded')),
                family TEXT NOT NULL,
                goal_text TEXT NOT NULL,
                context_json TEXT NOT NULL DEFAULT '[]',
                last_result_summary TEXT,
                retryable INTEGER NOT NULL DEFAULT 0 CHECK(retryable IN (0, 1)),
                resume_count INTEGER NOT NULL DEFAULT 0 CHECK(resume_count >= 0),
                FOREIGN KEY(conversation_id) REFERENCES conversations(id)
            )""",
            """CREATE INDEX IF NOT EXISTS idx_conversation_goals_current
               ON conversation_goals(conversation_id, state, id)""",
        )
        for statement in statements:
            self.db.execute(statement)

    def _migrate_v29(self) -> None:
        """Attach one bounded semantic contract to a conversation goal."""
        columns = {
            row["name"] for row in self.db.execute("PRAGMA table_info(conversation_goals)")
        }
        if columns and "contract_json" not in columns:
            self.db.execute(
                "ALTER TABLE conversation_goals "
                "ADD COLUMN contract_json TEXT NOT NULL DEFAULT '{}'"
            )

    def _migrate_v30(self) -> None:
        """Make verified lessons depend on exact reflection/prediction evidence."""
        required_columns = {
            "memories": {
                "id", "content", "kind", "source", "family", "outcome_status",
                "reflection_id",
            },
            "reflections": {
                "id", "created_at", "task_id", "conversation_id", "status",
                "summary", "mistakes", "improvements", "tool_calls",
            },
            "task_predictions": {
                "id", "created_at", "task_id", "conversation_id", "origin",
                "family", "profile", "model", "predicted_success",
                "predicted_steps", "predicted_verification", "basis",
                "resolved_at", "actual_status", "actual_steps", "evidence_ok",
                "failure_class",
            },
            "memory_claims": {"memory_id", "status"},
            "memory_embeddings": {"memory_id", "content_sha256"},
            "conversation_goals": {
                "id", "conversation_id", "updated_at", "state", "contract_json",
            },
        }
        required_tables = {
            "memory_fts", "memory_retrievals", "memory_statistics",
            "lesson_applications",
        }
        present_tables = {
            str(row["name"])
            for row in self.db.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            )
        }
        problems: list[str] = []
        for table, expected in required_columns.items():
            observed = {
                str(row["name"])
                for row in self.db.execute(f"PRAGMA table_info({table})")
            }
            missing = sorted(expected - observed)
            if missing:
                problems.append(f"{table} missing {', '.join(missing)}")
        missing_tables = sorted(required_tables - present_tables)
        if missing_tables:
            problems.append("missing tables " + ", ".join(missing_tables))
        if problems:
            raise RuntimeError(
                "Database schema version 29 is inconsistent; refusing an unsafe "
                "partial migration: " + "; ".join(problems)
            )
        reflection_columns = {
            str(row["name"])
            for row in self.db.execute("PRAGMA table_info(reflections)")
        }
        if reflection_columns and "prediction_id" not in reflection_columns:
            self.db.execute(
                "ALTER TABLE reflections ADD COLUMN prediction_id INTEGER"
            )
            reflection_columns.add("prediction_id")
        if "prediction_id" in reflection_columns:
            self.db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_reflections_prediction "
                "ON reflections(prediction_id) WHERE prediction_id IS NOT NULL"
            )
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS lesson_provenance (
                prediction_id INTEGER PRIMARY KEY,
                memory_id INTEGER NOT NULL,
                reflection_id INTEGER NOT NULL UNIQUE,
                verified_at TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,
                provenance_sha256 TEXT,
                FOREIGN KEY(memory_id) REFERENCES memories(id),
                FOREIGN KEY(reflection_id) REFERENCES reflections(id),
                FOREIGN KEY(prediction_id) REFERENCES task_predictions(id)
            )"""
        )
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_lesson_provenance_memory "
            "ON lesson_provenance(memory_id)"
        )
        # Backfill is deliberately fail-closed. Legacy rows that cannot prove an
        # exact successful prediction remain stored for audit but are ineligible
        # for retrieval.
        rows = self.db.execute(
            """SELECT id, content, source, family, outcome_status, reflection_id
               FROM memories
               WHERE kind='lesson' AND reflection_id IS NOT NULL
                  AND family IS NOT NULL AND outcome_status IS NOT NULL
               ORDER BY id"""
        ).fetchall()
        candidates: list[tuple[sqlite3.Row, sqlite3.Row]] = []
        for row in rows:
            reflection = self.db.execute(
                """SELECT summary, mistakes, improvements
                   FROM reflections WHERE id=?""",
                (int(row["reflection_id"]),),
            ).fetchone()
            if reflection is None:
                continue
            expected_content = self._canonical_reflection_lesson_content(
                family=str(row["family"]),
                outcome_status=str(row["outcome_status"]),
                summary=str(reflection["summary"] or ""),
                mistakes=str(reflection["mistakes"] or ""),
                improvements=str(reflection["improvements"] or ""),
            )
            expected_source = f"verified reflection:{int(row['reflection_id'])}"
            if (
                expected_content is None
                or str(row["content"]) != expected_content
                or str(row["source"] or "") != expected_source
            ):
                continue
            prediction = self._lesson_prediction_for_reflection(
                int(row["reflection_id"]),
                family=str(row["family"]),
                outcome_status=str(row["outcome_status"]),
                allow_legacy_inference=True,
                bind_legacy_inference=False,
            )
            if prediction is None:
                continue
            candidates.append((row, prediction))

        prediction_counts = Counter(int(prediction["id"]) for _, prediction in candidates)
        reflection_counts = Counter(int(row["reflection_id"]) for row, _ in candidates)
        for row, prediction in candidates:
            prediction_id = int(prediction["id"])
            reflection_id = int(row["reflection_id"])
            if prediction_counts[prediction_id] != 1 or reflection_counts[reflection_id] != 1:
                continue
            reflection = self.db.execute(
                "SELECT prediction_id FROM reflections WHERE id=?",
                (reflection_id,),
            ).fetchone()
            if reflection is None:
                continue
            if reflection["prediction_id"] is None:
                cursor = self.db.execute(
                    """UPDATE reflections SET prediction_id=?
                       WHERE id=? AND prediction_id IS NULL
                         AND NOT EXISTS (
                             SELECT 1 FROM reflections AS bound
                             WHERE bound.prediction_id=? AND bound.id<>?
                         )""",
                    (prediction_id, reflection_id, prediction_id, reflection_id),
                )
                if cursor.rowcount != 1:
                    continue
            elif int(reflection["prediction_id"]) != prediction_id:
                continue
            material = self._lesson_provenance_material(
                int(row["id"]), prediction_id, reflection_id
            )
            if material is None:
                continue
            self.db.execute(
                """INSERT OR IGNORE INTO lesson_provenance(
                       prediction_id, memory_id, reflection_id, verified_at,
                       content_sha256, provenance_sha256
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    prediction_id, int(row["id"]), reflection_id, now_iso(),
                    hashlib.sha256(str(row["content"]).encode("utf-8")).hexdigest(),
                    self._lesson_provenance_digest(material),
                ),
            )

    def _migrate_v31(self) -> None:
        """Bind lessons to canonical provenance and remove them from neural recall."""
        columns = {
            str(row["name"])
            for row in self.db.execute("PRAGMA table_info(lesson_provenance)")
        }
        if "provenance_sha256" not in columns:
            self.db.execute(
                "ALTER TABLE lesson_provenance ADD COLUMN provenance_sha256 TEXT"
            )
        rows = self.db.execute(
            """SELECT prediction_id, memory_id, reflection_id, content_sha256
               FROM lesson_provenance ORDER BY prediction_id"""
        ).fetchall()
        self.db.execute("UPDATE lesson_provenance SET provenance_sha256=NULL")
        for row in rows:
            content = self.db.execute(
                "SELECT content FROM memories WHERE id=?",
                (int(row["memory_id"]),),
            ).fetchone()
            if content is None or str(row["content_sha256"] or "") != hashlib.sha256(
                str(content["content"]).encode("utf-8")
            ).hexdigest():
                continue
            material = self._lesson_provenance_material(
                int(row["memory_id"]),
                int(row["prediction_id"]),
                int(row["reflection_id"]),
            )
            if material is None:
                continue
            self.db.execute(
                """UPDATE lesson_provenance SET provenance_sha256=?
                   WHERE prediction_id=? AND memory_id=? AND reflection_id=?""",
                (
                    self._lesson_provenance_digest(material),
                    int(row["prediction_id"]),
                    int(row["memory_id"]),
                    int(row["reflection_id"]),
                ),
            )
        # Lessons have a dedicated, provenance-checked retrieval path. Derived
        # generic indexes must not preserve legacy or forged lesson eligibility.
        self.db.execute(
            "DELETE FROM memory_embeddings WHERE memory_id IN "
            "(SELECT id FROM memories WHERE kind='lesson')"
        )
        self.db.execute(
            "DELETE FROM memory_embedding_leases WHERE memory_id IN "
            "(SELECT id FROM memories WHERE kind='lesson')"
        )

    def _migrate_v32(self) -> None:
        """Quarantine ordinary memories until an exact trusted write proves them."""
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS ordinary_memory_provenance (
                memory_id INTEGER PRIMARY KEY,
                recorded_at TEXT NOT NULL,
                origin TEXT NOT NULL,
                eligible INTEGER NOT NULL CHECK(eligible IN (0, 1)),
                content_sha256 TEXT NOT NULL,
                provenance_sha256 TEXT NOT NULL,
                FOREIGN KEY(memory_id) REFERENCES memories(id)
            )"""
        )
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_ordinary_memory_provenance_eligible "
            "ON ordinary_memory_provenance(eligible, memory_id)"
        )
        # There is no safe way to infer who authorized a legacy ordinary row from
        # free-form source text. Preserve those rows for audit, but remove derived
        # neural indexes so they cannot remain retrievable through stale vectors.
        self.db.execute(
            """DELETE FROM memory_embeddings
               WHERE memory_id IN (
                   SELECT id FROM memories
                   WHERE kind NOT IN ('lesson', 'claim')
               )"""
        )
        self.db.execute(
            """DELETE FROM memory_embedding_leases
               WHERE memory_id IN (
                   SELECT id FROM memories
                   WHERE kind NOT IN ('lesson', 'claim')
               )"""
        )

    def _migrate_v33(self) -> None:
        """Persist opt-in Screen Companion controls without storing screen content."""
        statements = (
            """CREATE TABLE IF NOT EXISTS screen_companion_state (
                id INTEGER PRIMARY KEY CHECK(id=1),
                mode TEXT NOT NULL CHECK(mode IN
                    ('disabled', 'observe', 'suggest', 'collaborate')),
                paused INTEGER NOT NULL CHECK(paused IN (0, 1)),
                auto_suggest INTEGER NOT NULL CHECK(auto_suggest IN (0, 1)),
                excluded_apps_json TEXT NOT NULL DEFAULT '[]',
                updated_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS screen_companion_rules (
                id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                trigger_app TEXT NOT NULL,
                title_contains TEXT,
                action_prompt TEXT NOT NULL,
                action_mode TEXT NOT NULL CHECK(action_mode IN
                    ('suggest', 'collaborate')),
                cooldown_seconds INTEGER NOT NULL CHECK(
                    cooldown_seconds BETWEEN 30 AND 86400),
                enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
                last_triggered_at TEXT
            )""",
            """CREATE INDEX IF NOT EXISTS idx_screen_companion_rules_enabled
               ON screen_companion_rules(enabled, trigger_app, id)""",
            """CREATE TABLE IF NOT EXISTS screen_companion_receipts (
                id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL,
                rule_id INTEGER,
                application_sha256 TEXT NOT NULL,
                context_sha256 TEXT NOT NULL,
                action_mode TEXT NOT NULL,
                status TEXT NOT NULL,
                job_id TEXT,
                FOREIGN KEY(rule_id) REFERENCES screen_companion_rules(id)
            )""",
        )
        for statement in statements:
            self.db.execute(statement)
        self.db.execute(
            """INSERT OR IGNORE INTO screen_companion_state(
                   id, mode, paused, auto_suggest, excluded_apps_json, updated_at
               ) VALUES (1, 'disabled', 1, 0, '[]', ?)""",
            (now_iso(),),
        )

    def _migrate_v34(self) -> None:
        """Store privacy-safe Companion feedback without screen or prompt content."""
        presence_columns = {
            str(row["name"])
            for row in self.db.execute("PRAGMA table_info(presence_jobs)")
        }
        if presence_columns and "run_origin" not in presence_columns:
            self.db.execute(
                "ALTER TABLE presence_jobs ADD COLUMN run_origin TEXT NOT NULL "
                "DEFAULT 'interactive' CHECK(run_origin IN "
                "('interactive','companion_suggestion','companion_action'))"
            )
        if presence_columns and "replayable" not in presence_columns:
            self.db.execute(
                "ALTER TABLE presence_jobs ADD COLUMN replayable INTEGER NOT NULL "
                "DEFAULT 1 CHECK(replayable IN (0, 1))"
            )
        digest_check = (
            "length({column})=64 AND "
            "{column} NOT GLOB '*[^0-9a-f]*'"
        )
        statements = (
            f"""CREATE TABLE IF NOT EXISTS screen_companion_feedback (
                id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL,
                suggestion_sha256 TEXT NOT NULL CHECK(
                    {digest_check.format(column='suggestion_sha256')}),
                context_sha256 TEXT NOT NULL CHECK(
                    {digest_check.format(column='context_sha256')}),
                application_sha256 TEXT NOT NULL CHECK(
                    {digest_check.format(column='application_sha256')}),
                category TEXT NOT NULL CHECK(category IN
                    ('coding','general','navigation','organization','research','writing')),
                action_mode TEXT NOT NULL CHECK(action_mode IN
                    ('suggest','collaborate')),
                decision TEXT NOT NULL CHECK(decision IN ('accepted','dismissed')),
                action_job_sha256 TEXT UNIQUE CHECK(
                    action_job_sha256 IS NULL OR
                    ({digest_check.format(column='action_job_sha256')})),
                CHECK(
                    (decision='accepted' AND action_job_sha256 IS NOT NULL) OR
                    (decision='dismissed' AND action_job_sha256 IS NULL)
                ),
                UNIQUE(suggestion_sha256, context_sha256, application_sha256)
            )""",
            """CREATE INDEX IF NOT EXISTS idx_screen_companion_feedback_aggregate
               ON screen_companion_feedback(category, action_mode, decision, id)""",
            """CREATE TABLE IF NOT EXISTS screen_companion_action_outcomes (
                feedback_id INTEGER PRIMARY KEY,
                recorded_at TEXT NOT NULL,
                outcome TEXT NOT NULL CHECK(outcome IN
                    ('complete','failed','incomplete')),
                evidence_kind TEXT NOT NULL CHECK(evidence_kind IN
                    ('cited_sources','failure_observed','process_evidence','tool_success')),
                prediction_id INTEGER NOT NULL UNIQUE,
                reusable INTEGER NOT NULL CHECK(reusable IN (0, 1)),
                CHECK((outcome='complete' AND reusable=1) OR
                      (outcome IN ('failed','incomplete') AND reusable=0)),
                FOREIGN KEY(feedback_id) REFERENCES screen_companion_feedback(id)
                    ON DELETE CASCADE,
                FOREIGN KEY(prediction_id) REFERENCES task_predictions(id)
            )""",
            """CREATE INDEX IF NOT EXISTS idx_screen_companion_outcomes_aggregate
               ON screen_companion_action_outcomes(outcome, evidence_kind, feedback_id)""",
        )
        for statement in statements:
            self.db.execute(statement)

    def _migrate_v35(self) -> None:
        """Bind Companion outcomes to exact runs and purge legacy private transcripts."""
        prediction_columns = {
            str(row["name"])
            for row in self.db.execute("PRAGMA table_info(task_predictions)")
        }
        if prediction_columns and "run_id_sha256" not in prediction_columns:
            self.db.execute(
                "ALTER TABLE task_predictions ADD COLUMN run_id_sha256 TEXT"
            )
        self.db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_predictions_run_id "
            "ON task_predictions(run_id_sha256) WHERE run_id_sha256 IS NOT NULL"
        )
        # v34 outcomes could only prove that a Companion prediction existed; they
        # could not prove it belonged to the exact accepted action job. Preserve
        # the operator feedback, but discard those unprovable positive/negative
        # outcome bindings before exact-run learning becomes authoritative.
        self.db.execute("DELETE FROM screen_companion_action_outcomes")
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS screen_companion_conversations (
                conversation_id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL,
                FOREIGN KEY(conversation_id) REFERENCES conversations(id)
                    ON DELETE CASCADE
            )"""
        )
        # A title is operator-controlled and therefore cannot identify an internal
        # channel. Mark only conversations that contain a known Companion-origin
        # job or the exact legacy prompt envelope used by older builds.
        self.db.execute(
            """INSERT OR IGNORE INTO screen_companion_conversations(
                   conversation_id, created_at
               )
               SELECT DISTINCT p.conversation_id, ?
               FROM presence_jobs AS p
               JOIN conversations AS c ON c.id=p.conversation_id
               WHERE c.project_id=1 AND c.title='Screen Companion'
                 AND (
                     p.run_origin IN ('companion_suggestion','companion_action') OR
                     p.prompt LIKE
                       'Privately analyze this operator-authored Screen Companion routine:%'
                 )""",
            (now_iso(),),
        )
        # Older Companion runs persisted active-window titles and OCR-derived
        # suggestions in their marked internal conversation. They are not training
        # data. Ambiguous title-only conversations are deliberately preserved.
        self.db.execute(
            "DELETE FROM messages WHERE conversation_id IN ("
            "SELECT conversation_id FROM screen_companion_conversations)"
        )
        presence_columns = {
            str(row["name"])
            for row in self.db.execute("PRAGMA table_info(presence_jobs)")
        }
        if {"run_origin", "replayable"}.issubset(presence_columns):
            self.db.execute(
                """UPDATE presence_jobs
                   SET prompt='[ephemeral Screen Companion prompt removed]',
                       attachments_json='[]', run_origin='companion_suggestion',
                       replayable=0
                   WHERE conversation_id IN (
                       SELECT conversation_id FROM screen_companion_conversations
                   )"""
            )

    def _migrate_v36(self) -> None:
        """Persist automatic-suggestion limits and isolate legacy calibration."""
        # Some development snapshots reached user_version 35 before the explicit
        # internal-conversation marker was added to that migration.  Recreate the
        # idempotent privacy boundary here so those databases fail closed too.
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS screen_companion_conversations (
                conversation_id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL,
                FOREIGN KEY(conversation_id) REFERENCES conversations(id)
                    ON DELETE CASCADE
            )"""
        )
        self.db.execute(
            """INSERT OR IGNORE INTO screen_companion_conversations(
                   conversation_id, created_at
               )
               SELECT DISTINCT p.conversation_id, ?
               FROM presence_jobs AS p
               JOIN conversations AS c ON c.id=p.conversation_id
               WHERE c.project_id=1 AND c.title='Screen Companion'
                 AND (
                     p.run_origin IN ('companion_suggestion','companion_action') OR
                     p.prompt LIKE
                       'Privately analyze this operator-authored Screen Companion routine:%'
                 )""",
            (now_iso(),),
        )
        self.db.execute(
            """DELETE FROM messages WHERE conversation_id IN (
                   SELECT conversation_id FROM screen_companion_conversations
               )"""
        )
        self.db.execute(
            """UPDATE presence_jobs
               SET prompt='[ephemeral Screen Companion prompt removed]',
                   attachments_json='[]', run_origin='companion_suggestion',
                   replayable=0
               WHERE conversation_id IN (
                   SELECT conversation_id FROM screen_companion_conversations
               )"""
        )
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS screen_companion_auto_receipts (
                id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL,
                day_key TEXT NOT NULL CHECK(
                    length(day_key)=10 AND
                    day_key GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
                context_sha256 TEXT NOT NULL CHECK(
                    length(context_sha256)=64 AND
                    context_sha256 NOT GLOB '*[^0-9a-f]*'),
                UNIQUE(day_key, context_sha256)
            )"""
        )
        self.db.execute(
            """CREATE INDEX IF NOT EXISTS idx_screen_companion_auto_recent
               ON screen_companion_auto_receipts(created_at DESC, id DESC)"""
        )
        # Older builds accidentally counted the internal suggestion conversation
        # as ordinary operator work.  Only explicitly marked internal channels are
        # reclassified; a user conversation merely named "Screen Companion" is
        # never enough.
        self.db.execute(
            """UPDATE task_predictions
               SET origin='companion_suggestion'
               WHERE conversation_id IN (
                   SELECT conversation_id FROM screen_companion_conversations
               ) AND origin IN ('interactive','worker','proactive')"""
        )

    @staticmethod
    def _project_id(value: int | None) -> int:
        if value is None:
            return 1
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("project_id must be a positive integer")
        if value > 9_223_372_036_854_775_807:
            raise ValueError("project_id is out of range")
        return value

    @staticmethod
    def _project_relative_path(value: str) -> str:
        raw = str(value).strip().replace("\\", "/")
        if not raw or len(raw) > 500 or contains_secret(raw):
            raise ValueError("Project path must be bounded non-secret relative text")
        path = PurePosixPath(raw)
        if path.is_absolute() or any(part in {"", ".."} for part in path.parts):
            raise ValueError("Project path must stay beneath the configured workspace")
        normalized = path.as_posix().rstrip("/") or "."
        if normalized != "." and (
            len(path.parts) != 2
            or path.parts[0] != "@projects"
            or not re.fullmatch(
                r"[a-z0-9](?:[a-z0-9-]{0,58}[a-z0-9])?",
                path.parts[1],
            )
        ):
            raise ValueError("Project path must be '.' or one canonical isolated project path")
        return normalized

    def add_project(self, name: str, relative_path: str) -> int:
        safe_name = redact_secrets(str(name).strip())[:120]
        if not safe_name:
            raise ValueError("Project name must not be empty")
        safe_path = self._project_relative_path(relative_path)
        stamp = now_iso()
        with self._immediate_transaction():
            cursor = self.db.execute(
                """INSERT INTO agent_projects(
                    created_at, updated_at, name, relative_path, enabled
                ) VALUES (?, ?, ?, ?, 1)""",
                (stamp, stamp, safe_name, safe_path),
            )
        return int(cursor.lastrowid)

    def get_project(self, project_id: int | None) -> dict[str, Any] | None:
        normalized = self._project_id(project_id)
        row = self.db.execute(
            """SELECT id, created_at, updated_at, name, relative_path, enabled
               FROM agent_projects WHERE id=?""",
            (normalized,),
        ).fetchone()
        return dict(row) if row is not None else None

    def list_projects(self) -> list[dict[str, Any]]:
        rows = self.db.execute(
            """SELECT p.id, p.created_at, p.updated_at, p.name, p.relative_path,
                      p.enabled, COUNT(DISTINCT c.id) AS conversation_count,
                      COUNT(DISTINCT t.id) AS task_count
               FROM agent_projects AS p
               LEFT JOIN conversations AS c ON c.project_id=p.id
               LEFT JOIN tasks AS t ON t.project_id=p.id
               GROUP BY p.id
               ORDER BY CASE WHEN p.id=1 THEN 0 ELSE 1 END, lower(p.name), p.id"""
        ).fetchall()
        return [dict(row) for row in rows]

    def list_specialist_agents(self) -> list[dict[str, Any]]:
        """Return the operator/Jarvis-visible roster; specialists never call this."""
        rows = self.db.execute(
            """SELECT s.agent_key, s.name, s.purpose, s.model_profile, s.families_json,
                      s.status, s.active_task_id, s.completed_tasks, s.failed_tasks,
                      s.created_at, s.updated_at, s.last_started_at, s.last_reported_at,
                      t.status AS active_task_status,
                      substr(t.prompt, 1, 500) AS active_task_prompt,
                      t.updated_at AS active_task_updated_at,
                      last.id AS last_task_id,
                      last.status AS last_task_status,
                      substr(last.prompt, 1, 500) AS last_task_prompt,
                      last.updated_at AS last_task_updated_at,
                      last.parent_conversation_id AS last_parent_conversation_id
               FROM specialist_agents AS s
               LEFT JOIN tasks AS t ON t.id=s.active_task_id
               LEFT JOIN tasks AS last ON last.id=(
                   SELECT recent.id FROM tasks AS recent
                   WHERE recent.specialist_key=s.agent_key
                     AND recent.delegated_by='jarvis'
                   ORDER BY recent.id DESC LIMIT 1
               )
               ORDER BY s.agent_key"""
        ).fetchall()
        return [dict(row) for row in rows]

    def get_specialist_agent(self, agent_key: str) -> dict[str, Any] | None:
        key = str(agent_key).strip().casefold()
        if key not in SPECIALIST_BY_KEY:
            return None
        row = self.db.execute(
            """SELECT agent_key, name, purpose, model_profile, families_json,
                      status, active_task_id, completed_tasks, failed_tasks,
                      created_at, updated_at, last_started_at, last_reported_at
               FROM specialist_agents WHERE agent_key=?""",
            (key,),
        ).fetchone()
        return dict(row) if row is not None else None

    def task_project(self, task_id: int) -> int | None:
        normalized = self._prediction_optional_id(task_id, "task_id")
        row = self.db.execute(
            "SELECT project_id FROM tasks WHERE id=?", (normalized,)
        ).fetchone()
        return int(row["project_id"] or 1) if row is not None else None

    def task_model_budget_scope(self, task_id: int) -> str | None:
        normalized = self._prediction_optional_id(task_id, "task_id")
        row = self.db.execute(
            "SELECT model_budget_scope FROM tasks WHERE id=?", (normalized,)
        ).fetchone()
        if row is None or not row["model_budget_scope"]:
            return None
        return self._model_budget_scope(str(row["model_budget_scope"]))

    def conversation_project(self, conversation_id: int) -> dict[str, Any] | None:
        if not self.conversation_exists(conversation_id):
            return None
        row = self.db.execute(
            """SELECT p.id, p.created_at, p.updated_at, p.name, p.relative_path, p.enabled
               FROM conversations AS c
               JOIN agent_projects AS p ON p.id=c.project_id
               WHERE c.id=?""",
            (conversation_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    @staticmethod
    def _metric_optional_count(value: int | None, label: str) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{label} must be a non-negative integer or None")
        return value

    @staticmethod
    def _model_budget_scope(value: str) -> str:
        scope = _validated_nonsecret_metadata(value, "Model budget scope")
        if (
            not scope
            or len(scope) > 200
            or re.fullmatch(r"[a-z][a-z0-9_-]{0,31}:[A-Za-z0-9._-]{1,160}", scope)
            is None
        ):
            raise ValueError("Model budget scope is invalid")
        return scope

    @staticmethod
    def _positive_budget(value: int, label: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{label} must be a positive integer")
        return value

    def reserve_model_call(
        self,
        budget_scope: str,
        *,
        estimated_prompt_tokens: int,
        call_limit: int,
        prompt_token_limit: int,
        completion_token_limit: int,
    ) -> int:
        """Atomically reserve one provider call against a shared request lineage."""
        scope = self._model_budget_scope(budget_scope)
        estimate = self._metric_optional_count(
            estimated_prompt_tokens, "estimated_prompt_tokens"
        )
        if estimate is None:
            raise ValueError("estimated_prompt_tokens must be a non-negative integer")
        maximum_calls = self._positive_budget(call_limit, "call_limit")
        maximum_prompt = self._positive_budget(
            prompt_token_limit, "prompt_token_limit"
        )
        maximum_completion = self._positive_budget(
            completion_token_limit, "completion_token_limit"
        )
        with self._immediate_transaction():
            usage = self.db.execute(
                """SELECT COUNT(*) AS calls,
                          COALESCE(SUM(estimated_prompt_tokens), 0) AS prompt_tokens,
                          COALESCE(SUM(completion_tokens), 0) AS completion_tokens
                   FROM model_call_budget_events WHERE budget_scope=?""",
                (scope,),
            ).fetchone()
            calls = int(usage["calls"] or 0)
            prompt_tokens = int(usage["prompt_tokens"] or 0)
            completion_tokens = int(usage["completion_tokens"] or 0)
            if calls >= maximum_calls:
                raise ModelBudgetExceeded(
                    f"request model-call limit reached ({maximum_calls})"
                )
            if prompt_tokens + estimate > maximum_prompt:
                raise ModelBudgetExceeded(
                    f"request prompt-token limit reached ({maximum_prompt})"
                )
            if completion_tokens >= maximum_completion:
                raise ModelBudgetExceeded(
                    f"request completion-token limit reached ({maximum_completion})"
                )
            cursor = self.db.execute(
                """INSERT INTO model_call_budget_events(
                       created_at, budget_scope, state, estimated_prompt_tokens
                   ) VALUES (?, ?, 'reserved', ?)""",
                (now_iso(), scope, estimate),
            )
        return int(cursor.lastrowid)

    def complete_model_call(
        self,
        reservation_id: int,
        *,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        success: bool,
    ) -> None:
        """Finalize one reservation without ever freeing a consumed call slot."""
        normalized = self._prediction_optional_id(reservation_id, "reservation_id")
        if not isinstance(success, bool):
            raise TypeError("success must be a boolean")
        with self._immediate_transaction():
            updated = self.db.execute(
                """UPDATE model_call_budget_events
                   SET completed_at=?, state='completed', prompt_tokens=?,
                       completion_tokens=?, success=?
                   WHERE id=? AND state='reserved'""",
                (
                    now_iso(),
                    self._metric_optional_count(prompt_tokens, "prompt_tokens"),
                    self._metric_optional_count(
                        completion_tokens, "completion_tokens"
                    ),
                    1 if success else 0,
                    normalized,
                ),
            )
            if updated.rowcount != 1:
                raise ValueError("Model call reservation is missing or already completed")

    def record_model_call(
        self,
        *,
        provider: str,
        model: str,
        profile: str,
        latency_ms: int,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        success: bool,
        failure_kind: str | None = None,
        budget_scope: str | None = None,
    ) -> int:
        """Record operational metadata only; prompts and responses are never accepted."""
        if isinstance(latency_ms, bool) or not isinstance(latency_ms, int) or latency_ms < 0:
            raise ValueError("latency_ms must be a non-negative integer")
        if not isinstance(success, bool):
            raise TypeError("success must be a boolean")
        safe_provider = _validated_nonsecret_metadata(provider, "provider")[:40]
        safe_model = _validated_nonsecret_metadata(model, "model")[:200]
        safe_profile = _validated_nonsecret_metadata(profile, "profile")[:40]
        if not safe_provider or not safe_model or not safe_profile:
            raise ValueError("provider, model, and profile must not be empty")
        safe_failure = None
        if failure_kind is not None:
            safe_failure = _validated_nonsecret_metadata(failure_kind, "failure_kind")[:100]
        safe_scope = (
            None if budget_scope is None else self._model_budget_scope(budget_scope)
        )
        cursor = self.db.execute(
            """INSERT INTO model_call_metrics(
                created_at, provider, model, profile, latency_ms,
                prompt_tokens, completion_tokens, success, failure_kind, budget_scope
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                now_iso(), safe_provider, safe_model, safe_profile, latency_ms,
                self._metric_optional_count(prompt_tokens, "prompt_tokens"),
                self._metric_optional_count(completion_tokens, "completion_tokens"),
                1 if success else 0, safe_failure, safe_scope,
            ),
        )
        self.db.commit()
        return int(cursor.lastrowid)

    def model_usage_summary(self, *, hours: int | None = 24) -> dict[str, Any]:
        """Return bounded per-model operational aggregates without conversation content."""
        if hours is not None and (
            isinstance(hours, bool) or not isinstance(hours, int) or not 1 <= hours <= 24 * 365
        ):
            raise ValueError("hours must be between 1 and 8760, or None")
        parameters: tuple[Any, ...] = ()
        where = ""
        since = None
        if hours is not None:
            since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
            where = "WHERE created_at >= ?"
            parameters = (since,)
        rows = self.db.execute(
            f"""SELECT provider, model, profile, latency_ms, prompt_tokens,
                       completion_tokens, success
                FROM model_call_metrics {where}
                ORDER BY id DESC LIMIT 50000""",
            parameters,
        ).fetchall()
        grouped: dict[tuple[str, str, str], list[sqlite3.Row]] = {}
        for row in rows:
            key = (row["provider"], row["model"], row["profile"])
            grouped.setdefault(key, []).append(row)
        groups: list[dict[str, Any]] = []
        for (provider, model, profile), items in sorted(grouped.items()):
            latencies = sorted(int(item["latency_ms"]) for item in items)
            p95_index = max(0, math.ceil(len(latencies) * 0.95) - 1)
            successes = sum(int(item["success"]) for item in items)
            groups.append({
                "provider": provider,
                "model": model,
                "profile": profile,
                "calls": len(items),
                "successful_calls": successes,
                "success_rate": successes / len(items),
                "prompt_tokens": sum(
                    int(item["prompt_tokens"] or 0) for item in items
                ),
                "completion_tokens": sum(
                    int(item["completion_tokens"] or 0) for item in items
                ),
                "mean_latency_ms": round(sum(latencies) / len(latencies)),
                "p95_latency_ms": latencies[p95_index],
            })
        return {
            "hours": hours,
            "since": since,
            "rows_scanned": len(rows),
            "truncated": len(rows) == 50000,
            "groups": groups,
        }

    @staticmethod
    def _prediction_optional_id(value: int | None, label: str) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{label} must be a positive integer or None")
        return value

    @staticmethod
    def _prediction_run_digest(value: str | None) -> str | None:
        if value is None:
            return None
        run_id = str(value).strip()
        if (
            not run_id
            or len(run_id) > 200
            or re.fullmatch(r"[A-Za-z0-9._:-]+", run_id) is None
            or contains_secret(run_id)
        ):
            raise ValueError("Prediction run ID is invalid")
        return hashlib.sha256(
            ("jarvis-presence-prediction-v1\0" + run_id).encode("utf-8")
        ).hexdigest()

    def record_prediction(
        self,
        *,
        family: str,
        profile: str,
        model: str,
        predicted_success: float,
        predicted_steps: int,
        predicted_verification: str,
        basis: str = "prior",
        origin: str = "interactive",
        task_id: int | None = None,
        conversation_id: int | None = None,
        run_id: str | None = None,
    ) -> int:
        """Persist one controlled-vocabulary prediction without prompt or evidence text."""
        if family not in self.PREDICTION_FAMILIES:
            raise ValueError(f"Unknown task family: {family}")
        if isinstance(predicted_success, bool):
            raise ValueError("predicted_success must be a finite number between 0 and 1")
        try:
            confidence = float(predicted_success)
        except (TypeError, ValueError):
            raise ValueError(
                "predicted_success must be a finite number between 0 and 1"
            ) from None
        if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            raise ValueError("predicted_success must be a finite number between 0 and 1")
        if (
            isinstance(predicted_steps, bool)
            or not isinstance(predicted_steps, int)
            or predicted_steps < 0
        ):
            raise ValueError("predicted_steps must be a non-negative integer")
        if predicted_verification not in self.PREDICTION_VERIFICATION:
            raise ValueError(f"Unknown prediction verification: {predicted_verification}")
        if basis not in {"prior", "competence", "model"}:
            raise ValueError("basis must be prior, competence, or model")
        if origin not in self.PREDICTION_ORIGINS:
            raise ValueError(f"Unknown prediction origin: {origin}")
        safe_profile = _validated_nonsecret_metadata(profile, "Prediction profile")
        safe_model = _validated_nonsecret_metadata(model, "Prediction model")
        if not safe_profile or not safe_model:
            raise ValueError("Prediction profile and model must not be empty")
        normalized_task_id = self._prediction_optional_id(task_id, "task_id")
        normalized_conversation_id = self._prediction_optional_id(
            conversation_id, "conversation_id"
        )
        run_id_sha256 = self._prediction_run_digest(run_id)
        with self._immediate_transaction():
            cur = self.db.execute(
                """INSERT INTO task_predictions(
                       created_at, task_id, conversation_id, origin, family, profile,
                       model, predicted_success, predicted_steps,
                       predicted_verification, basis, run_id_sha256
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    now_iso(), normalized_task_id, normalized_conversation_id, origin,
                    family, safe_profile[:40], safe_model[:200], confidence,
                    predicted_steps, predicted_verification, basis, run_id_sha256,
                ),
            )
            return int(cur.lastrowid)

    def resolve_prediction(
        self,
        prediction_id: int,
        *,
        actual_status: str,
        actual_steps: int | None,
        evidence_ok: bool | None,
        failure_class: str | None = None,
    ) -> bool:
        """Resolve one prediction exactly once; evidence may be not applicable."""
        normalized_id = self._prediction_optional_id(prediction_id, "prediction_id")
        if actual_status not in {"complete", "incomplete", "failed"}:
            raise ValueError("actual_status must be complete, incomplete, or failed")
        if actual_steps is not None and (
            isinstance(actual_steps, bool)
            or not isinstance(actual_steps, int)
            or actual_steps < 0
        ):
            raise ValueError("actual_steps must be a non-negative integer or None")
        if evidence_ok is not None and not isinstance(evidence_ok, bool):
            raise ValueError("evidence_ok must be true, false, or None")
        if actual_status == "complete" and evidence_ok is False:
            # Completion without the task's required evidence is not a success.
            # Keeping it as complete would train the competence model to reward
            # confident prose after a failed or missing real-world action.
            actual_status = "incomplete"
            failure_class = failure_class or "verification_absent"
        if actual_status == "complete":
            failure_class = None
        elif failure_class is None:
            failure_class = "unknown"
        if failure_class is not None and failure_class not in self.PREDICTION_FAILURE_CLASSES:
            raise ValueError(f"Unknown failure class: {failure_class}")
        with self._immediate_transaction():
            stamp = now_iso()
            updated = self.db.execute(
                """UPDATE task_predictions
                   SET resolved_at=?, actual_status=?, actual_steps=?, evidence_ok=?,
                       failure_class=?
                   WHERE id=? AND resolved_at IS NULL""",
                (
                    stamp, actual_status, actual_steps,
                    None if evidence_ok is None else int(evidence_ok),
                    failure_class, normalized_id,
                ),
            )
            if updated.rowcount == 1:
                self.db.execute(
                    """UPDATE lesson_applications
                       SET resolved_at=?, successful=?
                       WHERE prediction_id=? AND resolved_at IS NULL""",
                    (stamp, int(actual_status == "complete"), normalized_id),
                )
                memory_ids = [
                    int(row["memory_id"])
                    for row in self.db.execute(
                        """SELECT memory_id FROM memory_retrievals
                           WHERE prediction_id=? AND resolved_at IS NULL""",
                        (normalized_id,),
                    ).fetchall()
                ]
                self.db.execute(
                    """UPDATE memory_retrievals
                       SET resolved_at=?, successful=?
                       WHERE prediction_id=? AND resolved_at IS NULL""",
                    (stamp, int(actual_status == "complete"), normalized_id),
                )
                for memory_id in memory_ids:
                    aggregate = self.db.execute(
                        """SELECT COUNT(*) AS resolved,
                                  COALESCE(SUM(successful), 0) AS successes
                           FROM memory_retrievals
                           WHERE memory_id=? AND resolved_at IS NOT NULL""",
                        (memory_id,),
                    ).fetchone()
                    resolved = int(aggregate["resolved"])
                    successes = int(aggregate["successes"])
                    failures = resolved - successes
                    # Beta(2, 2) prior prevents one lucky or unlucky run from
                    # dominating recall. Evidence strengthens automatically.
                    utility = (successes + 2.0) / (resolved + 4.0)
                    self.db.execute(
                        """UPDATE memory_statistics
                           SET resolved=?, successes=?, failures=?, utility=?,
                               last_resolved_at=?, updated_at=?
                           WHERE memory_id=?""",
                        (
                            resolved, successes, failures, utility,
                            stamp, stamp, memory_id,
                        ),
                    )
        return updated.rowcount == 1

    def calibration_gate(
        self,
        family: str,
        *,
        minimum_attempts: int = 20,
        maximum_brier: float = 0.25,
        maximum_calibration_error: float = 0.15,
        minimum_success_rate: float = 0.70,
        minimum_evidence_rate: float = 0.70,
    ) -> dict[str, Any]:
        """Fail closed unless one family's outcome predictions are trustworthy."""
        if family not in self.PREDICTION_FAMILIES:
            raise ValueError(f"Unknown task family: {family}")
        rows = self.competence(family)
        row = rows[0] if rows else None
        attempts = int(row["attempts"]) if row else 0
        brier = float(row["brier"]) if row and row["brier"] is not None else None
        predicted = (
            float(row["mean_predicted"])
            if row and row["mean_predicted"] is not None else None
        )
        observed = (
            float(row["success_rate"])
            if row and row["success_rate"] is not None else None
        )
        evidence_applicable = int(row["evidence_applicable"]) if row else 0
        evidence_rate = (
            float(row["evidence_rate"])
            if row and row["evidence_rate"] is not None else None
        )
        calibration_error = (
            abs(predicted - observed)
            if predicted is not None and observed is not None else None
        )
        reasons: list[str] = []
        if attempts < int(minimum_attempts):
            reasons.append(f"requires {int(minimum_attempts)} outcomes; has {attempts}")
        if brier is None or brier > float(maximum_brier):
            reasons.append(
                f"Brier score must be <= {float(maximum_brier):.2f}; "
                + ("unknown" if brier is None else f"is {brier:.3f}")
            )
        if calibration_error is None or calibration_error > float(maximum_calibration_error):
            reasons.append(
                f"calibration error must be <= {float(maximum_calibration_error):.2f}; "
                + (
                    "unknown"
                    if calibration_error is None
                    else f"is {calibration_error:.3f}"
                )
            )
        if observed is None or observed < float(minimum_success_rate):
            reasons.append(
                f"observed success must be >= {float(minimum_success_rate):.2f}; "
                + ("unknown" if observed is None else f"is {observed:.3f}")
            )
        if evidence_applicable and (
            evidence_rate is None or evidence_rate < float(minimum_evidence_rate)
        ):
            reasons.append(
                f"verification evidence rate must be >= {float(minimum_evidence_rate):.2f}; "
                + ("unknown" if evidence_rate is None else f"is {evidence_rate:.3f}")
            )
        return {
            "family": family,
            "allowed": not reasons,
            "attempts": attempts,
            "brier": brier,
            "mean_predicted": predicted,
            "observed_success": observed,
            "evidence_applicable": evidence_applicable,
            "evidence_rate": evidence_rate,
            "calibration_error": calibration_error,
            "requirements": {
                "minimum_attempts": int(minimum_attempts),
                "maximum_brier": float(maximum_brier),
                "maximum_calibration_error": float(maximum_calibration_error),
                "minimum_success_rate": float(minimum_success_rate),
                "minimum_evidence_rate": float(minimum_evidence_rate),
            },
            "reasons": reasons,
        }

    def competence(self, family: str | None = None) -> list[dict[str, Any]]:
        """Return resolved non-practice outcomes grouped by the canonical task family."""
        if family is not None and family not in self.PREDICTION_FAMILIES:
            raise ValueError(f"Unknown task family: {family}")
        clause = "AND family=?" if family is not None else ""
        params: tuple[Any, ...] = (family,) if family is not None else ()
        rows = self.db.execute(
            f"""SELECT family,
                       COUNT(*) AS attempts,
                       AVG(predicted_success) AS mean_predicted,
                       AVG(CASE WHEN actual_status='complete' THEN 1.0 ELSE 0.0 END)
                           AS success_rate,
                       AVG((predicted_success -
                           CASE WHEN actual_status='complete' THEN 1.0 ELSE 0.0 END) *
                           (predicted_success -
                           CASE WHEN actual_status='complete' THEN 1.0 ELSE 0.0 END)) AS brier,
                       AVG(actual_steps) AS mean_steps,
                       MAX(actual_steps) AS max_steps,
                       SUM(CASE WHEN evidence_ok IS NOT NULL THEN 1 ELSE 0 END)
                           AS evidence_applicable,
                       AVG(CASE WHEN evidence_ok IS NOT NULL THEN evidence_ok END)
                           AS evidence_rate
                FROM task_predictions
                WHERE resolved_at IS NOT NULL
                  AND origin IN ('interactive','worker','proactive') {clause}
                GROUP BY family ORDER BY family""",
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def calibration(self, bins: int = 10) -> list[dict[str, Any]]:
        """Compare prediction bands with observed completion rates."""
        if isinstance(bins, bool) or not isinstance(bins, int):
            raise ValueError("bins must be an integer")
        bins = max(2, min(bins, 20))
        rows = self.db.execute(
            """SELECT MIN(? - 1, CAST(predicted_success * ? AS INTEGER)) AS bucket,
                      COUNT(*) AS n,
                      AVG(predicted_success) AS mean_predicted,
                      AVG(CASE WHEN actual_status='complete' THEN 1.0 ELSE 0.0 END)
                          AS observed
               FROM task_predictions
               WHERE resolved_at IS NOT NULL
                 AND origin IN ('interactive','worker','proactive')
               GROUP BY bucket ORDER BY bucket""",
            (bins, bins),
        ).fetchall()
        return [dict(row) for row in rows]

    def failure_histogram(
        self,
        family: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        if family is not None and family not in self.PREDICTION_FAMILIES:
            raise ValueError(f"Unknown task family: {family}")
        clause = "AND family=?" if family is not None else ""
        params: tuple[Any, ...] = (family,) if family is not None else ()
        rows = self.db.execute(
            f"""SELECT failure_class, COUNT(*) AS n
                FROM task_predictions
                WHERE resolved_at IS NOT NULL AND failure_class IS NOT NULL
                  AND origin IN ('interactive','worker','proactive') {clause}
                GROUP BY failure_class ORDER BY n DESC, failure_class LIMIT ?""",
            (*params, _bounded_limit(limit, 100)),
        ).fetchall()
        return [dict(row) for row in rows]

    def drift_report(
        self,
        *,
        window: int = 30,
        baseline: int = 90,
        minimum_samples: int = 10,
    ) -> list[dict[str, Any]]:
        """Compare recent outcomes with an earlier per-family baseline."""
        for value, label, maximum in (
            (window, "window", 1_000),
            (baseline, "baseline", 10_000),
            (minimum_samples, "minimum_samples", 1_000),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
                raise ValueError(f"{label} must be an integer from 1 to {maximum}")

        rows = self.db.execute(
            """SELECT id, family, predicted_success, actual_status, actual_steps,
                      evidence_ok, failure_class
               FROM task_predictions
               WHERE resolved_at IS NOT NULL
                 AND origin IN ('interactive','worker','proactive')
               ORDER BY family, resolved_at DESC, id DESC"""
        ).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(str(row["family"]), []).append(dict(row))

        def metrics(items: list[dict[str, Any]]) -> dict[str, Any]:
            evidence = [item for item in items if item["evidence_ok"] is not None]
            steps = [int(item["actual_steps"]) for item in items if item["actual_steps"] is not None]
            failures = Counter(
                str(item["failure_class"])
                for item in items
                if item["failure_class"] is not None
            )
            return {
                "n": len(items),
                "success_rate": sum(item["actual_status"] == "complete" for item in items) / len(items),
                "evidence_n": len(evidence),
                "evidence_rate": (
                    sum(bool(item["evidence_ok"]) for item in evidence) / len(evidence)
                    if evidence else None
                ),
                "brier": sum(
                    (
                        float(item["predicted_success"])
                        - (1.0 if item["actual_status"] == "complete" else 0.0)
                    ) ** 2
                    for item in items
                ) / len(items),
                "mean_steps": sum(steps) / len(steps) if steps else None,
                "failure_classes": dict(sorted(failures.items())),
            }

        findings: list[dict[str, Any]] = []
        for family in sorted(grouped):
            items = grouped[family]
            recent_items = items[:window]
            baseline_items = items[window:window + baseline]
            if len(recent_items) < minimum_samples or len(baseline_items) < minimum_samples:
                continue
            recent = metrics(recent_items)
            prior = metrics(baseline_items)
            signals: list[dict[str, Any]] = []
            if recent["success_rate"] < prior["success_rate"] - 0.15:
                signals.append({
                    "signal": "success_rate_drop",
                    "recent": recent["success_rate"],
                    "baseline": prior["success_rate"],
                    "threshold": 0.15,
                })
            if (
                recent["evidence_n"] >= minimum_samples
                and prior["evidence_n"] >= minimum_samples
                and recent["evidence_rate"] is not None
                and prior["evidence_rate"] is not None
                and recent["evidence_rate"] < prior["evidence_rate"] - 0.15
            ):
                signals.append({
                    "signal": "evidence_rate_drop",
                    "recent": recent["evidence_rate"],
                    "baseline": prior["evidence_rate"],
                    "threshold": 0.15,
                })
            if recent["brier"] > prior["brier"] + 0.10:
                signals.append({
                    "signal": "brier_increase",
                    "recent": recent["brier"],
                    "baseline": prior["brier"],
                    "threshold": 0.10,
                })
            for failure_class, count in recent["failure_classes"].items():
                if count >= 3 and failure_class not in prior["failure_classes"]:
                    signals.append({
                        "signal": "new_failure_class",
                        "failure_class": failure_class,
                        "recent_count": count,
                        "baseline_count": 0,
                        "threshold": 3,
                    })
            if (
                recent["mean_steps"] is not None
                and prior["mean_steps"] is not None
                and recent["mean_steps"] > prior["mean_steps"] * 1.5
            ):
                signals.append({
                    "signal": "mean_steps_increase",
                    "recent": recent["mean_steps"],
                    "baseline": prior["mean_steps"],
                    "threshold_ratio": 1.5,
                })
            if signals:
                findings.append({
                    "family": family,
                    "recent": recent,
                    "baseline": prior,
                    "signals": signals,
                })
        return findings

    def open_prediction_count(self) -> int:
        self._ensure_open()
        return int(
            self.db.execute(
                "SELECT COUNT(*) FROM task_predictions WHERE resolved_at IS NULL"
            ).fetchone()[0]
        )

    def health_indicators(self, *, approval_ttl_hours: int = 24) -> dict[str, int]:
        """Return bounded integrity counters used by deep local diagnostics."""
        if (
            isinstance(approval_ttl_hours, bool)
            or not isinstance(approval_ttl_hours, int)
            or not 1 <= approval_ttl_hours <= 720
        ):
            raise ValueError("approval_ttl_hours must be an integer from 1 to 720")
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=approval_ttl_hours)).isoformat()
        stale_awaiting = int(self.db.execute(
            """SELECT COUNT(*) FROM tasks
               WHERE status='awaiting_approval' AND updated_at<?""",
            (cutoff,),
        ).fetchone()[0])
        return {
            "open_predictions": self.open_prediction_count(),
            "stale_awaiting_approval_tasks": stale_awaiting,
        }

    def prediction_origin_for_task(self, task_id: int) -> str:
        """Distinguish operator-queued work from scheduler-created backlog work."""
        normalized_id = self._prediction_optional_id(task_id, "task_id")
        row = self.db.execute(
            "SELECT backlog_id, initiative_event_id FROM tasks WHERE id=?",
            (normalized_id,),
        ).fetchone()
        return (
            "proactive"
            if row is not None
            and (
                row["backlog_id"] is not None
                or row["initiative_event_id"] is not None
            )
            else "worker"
        )

    def new_conversation(
        self,
        title: str = "Conversation",
        *,
        project_id: int | None = None,
    ) -> int:
        safe_title = redact_secrets(str(title))[:120]
        normalized_project = self._project_id(project_id)
        project = self.get_project(normalized_project)
        if project is None or not bool(project["enabled"]):
            raise ValueError(f"Project #{normalized_project} does not exist or is disabled")
        with self._immediate_transaction():
            cur = self.db.execute(
                "INSERT INTO conversations(created_at, title, project_id) VALUES (?, ?, ?)",
                (now_iso(), safe_title, normalized_project),
            )
            return int(cur.lastrowid)

    def conversation_exists(self, conversation_id: int) -> bool:
        """Return whether one canonical positive conversation ID exists."""
        if isinstance(conversation_id, bool) or not isinstance(conversation_id, int):
            return False
        if conversation_id <= 0 or conversation_id > 9_223_372_036_854_775_807:
            return False
        self._ensure_open()
        row = self.db.execute(
            "SELECT 1 FROM conversations WHERE id=?",
            (conversation_id,),
        ).fetchone()
        return row is not None

    def mark_screen_companion_conversation(self, conversation_id: int) -> None:
        """Mark an exact conversation as Jarvis-internal without trusting its title."""
        normalized_id = self._prediction_optional_id(
            conversation_id, "conversation_id"
        )
        row = self.db.execute(
            "SELECT project_id FROM conversations WHERE id=?", (normalized_id,)
        ).fetchone()
        if row is None or int(row["project_id"]) != 1:
            raise ValueError(
                "Screen Companion internal conversation must exist in the default project"
            )
        with self._immediate_transaction():
            self.db.execute(
                """INSERT OR IGNORE INTO screen_companion_conversations(
                       conversation_id, created_at
                   ) VALUES (?, ?)""",
                (normalized_id, now_iso()),
            )

    def is_screen_companion_conversation(self, conversation_id: int) -> bool:
        if not self.conversation_exists(conversation_id):
            return False
        row = self.db.execute(
            "SELECT 1 FROM screen_companion_conversations WHERE conversation_id=?",
            (int(conversation_id),),
        ).fetchone()
        return row is not None

    def screen_companion_conversation_id(self) -> int | None:
        row = self.db.execute(
            """SELECT conversation_id FROM screen_companion_conversations
               ORDER BY conversation_id DESC LIMIT 1"""
        ).fetchone()
        return None if row is None else int(row["conversation_id"])

    def list_conversations(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return a bounded recent-conversation index for operator interfaces."""
        self._ensure_open()
        limit = _bounded_limit(limit, 200)
        if not limit:
            return []
        rows = self.db.execute(
            """SELECT c.id, c.created_at, c.title, c.project_id,
                      p.name AS project_name,
                      COUNT(m.id) AS message_count,
                      MAX(m.id) AS last_message_id
               FROM conversations AS c
               JOIN agent_projects AS p ON p.id=c.project_id
               LEFT JOIN messages AS m ON m.conversation_id=c.id
               GROUP BY c.id, c.created_at, c.title, c.project_id, p.name
               ORDER BY COALESCE(MAX(m.id), 0) DESC, c.id DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def delete_conversation(self, conversation_id: int) -> dict[str, Any] | None:
        """Delete one operator chat while preserving independent project artifacts.

        Conversation transcripts and durable Presence request copies are removed.
        Project files remain untouched, while calibration records that are useful
        outside the chat lose their conversation link instead of becoming orphans.
        Active requests and internal Screen Companion conversations fail closed.
        """
        if (
            isinstance(conversation_id, bool)
            or not isinstance(conversation_id, int)
            or conversation_id <= 0
            or conversation_id > 9_223_372_036_854_775_807
        ):
            raise ValueError("conversation_id must be a positive integer")
        self._ensure_open()
        with self._immediate_transaction():
            row = self.db.execute(
                """SELECT id, created_at, title, project_id
                   FROM conversations WHERE id=?""",
                (conversation_id,),
            ).fetchone()
            if row is None:
                return None
            internal = self.db.execute(
                """SELECT 1 FROM screen_companion_conversations
                   WHERE conversation_id=?""",
                (conversation_id,),
            ).fetchone()
            if internal is not None:
                raise PermissionError(
                    "Internal Screen Companion conversations cannot be deleted from chat"
                )
            live = self.db.execute(
                """SELECT 1 FROM presence_jobs
                   WHERE conversation_id=? AND status IN ('queued', 'running')
                   LIMIT 1""",
                (conversation_id,),
            ).fetchone()
            if live is not None:
                raise RuntimeError(
                    "Stop the active request before deleting this conversation"
                )

            stamp = now_iso()
            scope = f"conversation:{conversation_id}"
            self.db.execute(
                """UPDATE approvals
                   SET status='denied', updated_at=?, decided_at=?
                   WHERE scope=? AND status='pending'""",
                (stamp, stamp, scope),
            )
            self.db.execute(
                """UPDATE persistent_approval_grants
                   SET revoked_at=?, updated_at=?
                   WHERE scope=? AND revoked_at IS NULL""",
                (stamp, stamp, scope),
            )
            self.db.execute(
                "UPDATE tasks SET parent_conversation_id=NULL WHERE parent_conversation_id=?",
                (conversation_id,),
            )
            for table in ("reflections", "task_predictions", "memory_retrievals"):
                self.db.execute(
                    f"UPDATE {table} SET conversation_id=NULL WHERE conversation_id=?",
                    (conversation_id,),
                )
            for table in (
                "training_examples",
                "conversation_goals",
                "presence_jobs",
                "messages",
            ):
                self.db.execute(
                    f"DELETE FROM {table} WHERE conversation_id=?",
                    (conversation_id,),
                )
            deleted = self.db.execute(
                "DELETE FROM conversations WHERE id=?",
                (conversation_id,),
            )
            if deleted.rowcount != 1:
                raise RuntimeError("Conversation changed while it was being deleted")
        return dict(row)

    def add_training_example(
        self,
        *,
        prompt: str,
        response: str,
        model: str,
        profile: str,
        task_kind: str,
        evidence: dict[str, Any],
        quality_score: float,
        verified: bool,
        conversation_id: int | None = None,
    ) -> int | None:
        fields = {
            "prompt": _bounded_persisted_text(
                redact_secrets(prompt.strip()), 50_000, "training prompt"
            ),
            "response": _bounded_persisted_text(
                redact_secrets(response.strip()), 100_000, "training response"
            ),
            "model": _validated_nonsecret_metadata(model, "Training model")[:200],
            "profile": _validated_nonsecret_metadata(profile, "Training profile")[:40],
            "task_kind": _validated_nonsecret_metadata(task_kind, "Training task kind")[:40],
        }
        if not all(fields.values()):
            raise ValueError("Training examples require non-empty text and metadata")
        score = float(quality_score)
        if not 0.0 <= score <= 1.0:
            raise ValueError("Training quality score must be between 0 and 1")
        evidence_json = _redacted_json_text(evidence)
        digest_source = json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        content_hash = hashlib.sha256(digest_source.encode("utf-8")).hexdigest()
        split = training_prompt_split(fields["prompt"], fields["task_kind"])
        with self._immediate_transaction():
            cursor = self.db.execute(
                """INSERT OR IGNORE INTO training_examples(
                    created_at, conversation_id, prompt, response, model, profile, task_kind,
                    evidence_json, quality_score, verified, split, content_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    now_iso(), conversation_id, fields["prompt"], fields["response"],
                    fields["model"], fields["profile"], fields["task_kind"], evidence_json,
                    score, int(bool(verified)), split, content_hash,
                ),
            )
            return int(cursor.lastrowid) if cursor.rowcount else None

    def list_training_examples(
        self,
        *,
        verified_only: bool = True,
        min_quality: float = 0.0,
        limit: int = 100_000,
    ) -> list[dict[str, Any]]:
        self._ensure_open()
        score = max(0.0, min(float(min_quality), 1.0))
        limit = _bounded_limit(limit, 100_000)
        rows = self.db.execute(
            """SELECT id, created_at, conversation_id, prompt, response, model, profile,
                      task_kind, evidence_json, quality_score, verified, split, content_hash
               FROM training_examples
               WHERE quality_score >= ? AND (? = 0 OR verified = 1)
               ORDER BY id LIMIT ?""",
            (score, int(verified_only), limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def add_evaluation_case(
        self,
        name: str,
        prompt: str,
        expected_contains: list[str],
    ) -> int:
        name = _validated_nonsecret_metadata(name, "Evaluation name")[:200]
        prompt = redact_secrets(prompt.strip())
        expected = [
            redact_secrets(str(item).strip())
            for item in expected_contains
            if str(item).strip()
        ]
        if not name or not prompt or not expected:
            raise ValueError("Evaluation cases require a name, prompt, and expected text")
        encoded = json.dumps(expected, ensure_ascii=False, separators=(",", ":"))
        with self._immediate_transaction():
            self.db.execute(
                """INSERT INTO evaluation_cases(created_at, name, prompt, expected_contains_json)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET
                       prompt=excluded.prompt,
                       expected_contains_json=excluded.expected_contains_json,
                       enabled=1""",
                (now_iso(), name, prompt, encoded),
            )
            row = self.db.execute(
                "SELECT id FROM evaluation_cases WHERE name=?", (name,)
            ).fetchone()
            return int(row[0])

    def list_evaluation_cases(self) -> list[dict[str, Any]]:
        self._ensure_open()
        rows = self.db.execute(
            """SELECT id, created_at, name, prompt, expected_contains_json, enabled
               FROM evaluation_cases ORDER BY id"""
        ).fetchall()
        return [dict(row) for row in rows]

    def add_message(self, conversation_id: int, role: str, content: str) -> None:
        if role not in {"user", "assistant"}:
            raise ValueError("Persisted message role must be user or assistant")
        content = redact_secrets(str(content))
        if len(content) > 100_000:
            content = content[:99_950] + "\n...[message clipped before persistence]"
        with self._immediate_transaction():
            self.db.execute(
                "INSERT INTO messages(conversation_id, created_at, role, content) VALUES (?, ?, ?, ?)",
                (conversation_id, now_iso(), role, content),
            )
            if role == "user":
                postal = _EXPLICIT_USER_POSTAL_CODE.search(content)
                if postal is not None:
                    self._set_preference_locked(
                        "location.postal_code",
                        postal.group(1),
                        source="explicit user profile statement",
                        authority="operator",
                        confidence=1.0,
                        stamp=now_iso(),
                    )

    def recent_messages(self, conversation_id: int, limit: int = 24) -> list[dict[str, str]]:
        self._ensure_open()
        limit = _bounded_limit(limit, 1_000)
        if not limit:
            return []
        rows = self.db.execute(
            "SELECT role, content FROM messages WHERE conversation_id=? ORDER BY id DESC LIMIT ?",
            (conversation_id, limit),
        ).fetchall()
        return [dict(row) for row in reversed(rows)]

    @staticmethod
    def _conversation_contract_json(contract: Any | None) -> str:
        """Serialize one already-validated contract without persisting secrets."""
        if contract is None:
            return "{}"
        payload = contract.to_payload() if hasattr(contract, "to_payload") else contract
        if not isinstance(payload, dict):
            raise ValueError("Conversation goal contract must be an object")
        raw = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        safe = redact_secrets(raw)
        if len(safe) > 12_000:
            raise ValueError("Conversation goal contract exceeds 12,000 characters")
        try:
            decoded = json.loads(safe)
        except json.JSONDecodeError as exc:
            raise ValueError("Conversation goal contract is not valid JSON") from exc
        if not isinstance(decoded, dict):
            raise ValueError("Conversation goal contract must be an object")
        return json.dumps(
            decoded, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )

    @staticmethod
    def _decode_conversation_contract(raw: Any) -> dict[str, Any] | None:
        try:
            decoded = json.loads(str(raw or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return decoded if isinstance(decoded, dict) and decoded else None

    def begin_conversation_goal(
        self,
        conversation_id: int,
        goal_text: str,
        family: str,
        *,
        contract: Any | None = None,
    ) -> int:
        """Start one bounded operator goal and supersede an unrelated pending goal."""
        if not self.conversation_exists(conversation_id):
            raise ValueError("Conversation goal requires an existing conversation")
        normalized_family = str(family).strip().casefold()
        if normalized_family not in self.PREDICTION_FAMILIES:
            raise ValueError(f"Unknown conversation-goal family: {family}")
        safe_goal = redact_secrets(str(goal_text).strip())
        if not safe_goal:
            raise ValueError("Conversation goal must not be empty")
        safe_goal = _bounded_persisted_text(safe_goal, 20_000, "conversation goal")
        contract_json = self._conversation_contract_json(contract)
        stamp = now_iso()
        with self._immediate_transaction():
            self.db.execute(
                """UPDATE conversation_goals
                   SET state='superseded', updated_at=?
                   WHERE conversation_id=? AND state IN ('active', 'incomplete')""",
                (stamp, int(conversation_id)),
            )
            cursor = self.db.execute(
                """INSERT INTO conversation_goals(
                       conversation_id, created_at, updated_at, state, family,
                       goal_text, context_json, contract_json, retryable, resume_count
                   ) VALUES (?, ?, ?, 'active', ?, ?, '[]', ?, 0, 0)""",
                (
                    int(conversation_id), stamp, stamp, normalized_family, safe_goal,
                    contract_json,
                ),
            )
            return int(cursor.lastrowid)

    def pending_conversation_goal(self, conversation_id: int) -> dict[str, Any] | None:
        """Return only a resumable same-conversation goal, never completed history."""
        if not self.conversation_exists(conversation_id):
            return None
        row = self.db.execute(
            """SELECT id, conversation_id, created_at, updated_at, state, family,
                      goal_text, context_json, contract_json, last_result_summary, retryable,
                      resume_count
               FROM conversation_goals
               WHERE conversation_id=? AND (
                   state='active' OR (state='incomplete' AND retryable=1)
               )
               ORDER BY id DESC LIMIT 1""",
            (int(conversation_id),),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        try:
            decoded = json.loads(str(result.get("context_json") or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            decoded = []
        result["context"] = [
            str(item) for item in decoded if isinstance(item, str)
        ][-12:]
        result["contract"] = self._decode_conversation_contract(
            result.get("contract_json")
        )
        result["retryable"] = bool(result.get("retryable"))
        return result

    def resume_conversation_goal(
        self,
        goal_id: int,
        conversation_id: int,
        operator_update: str,
    ) -> dict[str, Any]:
        """Append one redacted operator update and atomically reactivate its goal."""
        safe_update = redact_secrets(str(operator_update).strip())
        if not safe_update:
            raise ValueError("Conversation-goal update must not be empty")
        safe_update = _bounded_persisted_text(
            safe_update, 4_000, "conversation-goal update"
        )
        with self._immediate_transaction():
            row = self.db.execute(
                """SELECT id, conversation_id, context_json
                   FROM conversation_goals
                   WHERE id=? AND conversation_id=? AND (
                       state='active' OR (state='incomplete' AND retryable=1)
                   )""",
                (int(goal_id), int(conversation_id)),
            ).fetchone()
            if row is None:
                raise ValueError("Conversation goal is not resumable in this conversation")
            try:
                decoded = json.loads(str(row["context_json"] or "[]"))
            except (TypeError, ValueError, json.JSONDecodeError):
                decoded = []
            context = [str(item) for item in decoded if isinstance(item, str)][-11:]
            context.append(safe_update)
            self.db.execute(
                """UPDATE conversation_goals
                   SET state='active', updated_at=?, context_json=?, retryable=0,
                       resume_count=resume_count+1
                   WHERE id=?""",
                (
                    now_iso(),
                    json.dumps(context, ensure_ascii=False, separators=(",", ":")),
                    int(goal_id),
                ),
            )
        resumed = self.db.execute(
            """SELECT id, conversation_id, created_at, updated_at, state, family,
                      goal_text, context_json, contract_json, last_result_summary, retryable,
                      resume_count
               FROM conversation_goals WHERE id=?""",
            (int(goal_id),),
        ).fetchone()
        if resumed is None:
            raise RuntimeError("Resumed conversation goal disappeared")
        result = dict(resumed)
        result["context"] = context
        result["contract"] = self._decode_conversation_contract(
            result.get("contract_json")
        )
        result["retryable"] = bool(result.get("retryable"))
        return result

    def update_conversation_goal_contract(
        self,
        goal_id: int,
        conversation_id: int,
        contract: Any,
    ) -> None:
        """Replace only the bounded classification attached to one active goal."""
        contract_json = self._conversation_contract_json(contract)
        with self._immediate_transaction():
            cursor = self.db.execute(
                """UPDATE conversation_goals
                   SET contract_json=?, updated_at=?
                   WHERE id=? AND conversation_id=? AND state='active'""",
                (contract_json, now_iso(), int(goal_id), int(conversation_id)),
            )
            if cursor.rowcount != 1:
                raise ValueError("Conversation goal is not active in this conversation")

    def finish_conversation_goal(
        self,
        goal_id: int,
        *,
        state: str,
        result_summary: str | None = None,
        retryable: bool = False,
    ) -> None:
        """Record the observed goal outcome without granting any new authority."""
        normalized_state = str(state).strip().casefold()
        if normalized_state not in {"active", "incomplete", "complete", "cancelled"}:
            raise ValueError("Unknown conversation-goal state")
        summary = None
        if result_summary is not None:
            summary = _bounded_persisted_text(
                redact_secrets(str(result_summary).strip()),
                4_000,
                "conversation-goal result",
            )
        with self._immediate_transaction():
            cursor = self.db.execute(
                """UPDATE conversation_goals
                   SET state=?, updated_at=?, last_result_summary=?, retryable=?
                   WHERE id=?""",
                (
                    normalized_state, now_iso(), summary,
                    int(bool(retryable and normalized_state == "incomplete")),
                    int(goal_id),
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("Conversation goal does not exist")

    def finish_conversation_goal_if_current(
        self,
        goal_id: int,
        conversation_id: int,
        *,
        expected_updated_at: str,
        state: str,
        result_summary: str | None = None,
    ) -> bool:
        """Optimistically complete/cancel one still-resumable conversation goal."""
        normalized_goal = self._prediction_optional_id(goal_id, "goal_id")
        normalized_conversation = self._prediction_optional_id(
            conversation_id, "conversation_id"
        )
        expected = _validated_nonsecret_metadata(
            expected_updated_at, "Expected conversation-goal timestamp"
        )
        if not expected or len(expected) > 100:
            raise ValueError("Expected conversation-goal timestamp is invalid")
        normalized_state = str(state).strip().casefold()
        if normalized_state not in {"complete", "cancelled"}:
            raise ValueError(
                "Optimistic conversation-goal finish must complete or cancel"
            )
        summary = None
        if result_summary is not None:
            summary = _bounded_persisted_text(
                redact_secrets(str(result_summary).strip()),
                4_000,
                "conversation-goal result",
            )
        with self._immediate_transaction():
            cursor = self.db.execute(
                """UPDATE conversation_goals
                   SET state=?, updated_at=?, last_result_summary=?, retryable=0
                   WHERE id=? AND conversation_id=? AND updated_at=?
                     AND (
                         state='active' OR (state='incomplete' AND retryable=1)
                     )""",
                (
                    normalized_state,
                    now_iso(),
                    summary,
                    normalized_goal,
                    normalized_conversation,
                    expected,
                ),
            )
        return cursor.rowcount == 1

    def cancel_conversation_goal_if_current(
        self,
        goal_id: int,
        conversation_id: int,
        expected_updated_at: str,
    ) -> bool:
        """Cancel an exact resumable goal version without racing newer work."""
        return self.finish_conversation_goal_if_current(
            goal_id,
            conversation_id,
            expected_updated_at=expected_updated_at,
            state="cancelled",
        )

    def list_conversation_goals(
        self,
        conversation_id: int,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Expose bounded goal history for tests, diagnostics, and operator UI."""
        if not self.conversation_exists(conversation_id):
            return []
        bounded = _bounded_limit(limit, 100)
        if not bounded:
            return []
        rows = self.db.execute(
            """SELECT id, conversation_id, created_at, updated_at, state, family,
                      goal_text, context_json, contract_json, last_result_summary, retryable,
                      resume_count
               FROM conversation_goals WHERE conversation_id=?
               ORDER BY id DESC LIMIT ?""",
            (int(conversation_id), bounded),
        ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["contract"] = self._decode_conversation_contract(
                item.get("contract_json")
            )
            results.append(item)
        return results

    def conversation_scoped_memory_messages(
        self,
        conversation_id: int,
        limit: int = 32,
    ) -> list[dict[str, str]]:
        """Return explicit chat/session memory statements independent of recency."""
        self._ensure_open()
        limit = _bounded_limit(limit, 64)
        if not limit:
            return []
        scope = re.compile(
            r"\b(?:for|in|during)\s+(?:this|our|the)\s+"
            r"(?:conversation|chat|session|thread)\b",
            re.I,
        )
        # SQL performs a cheap, broad noun prefilter. Python then applies the
        # exact same scope grammar accepted by the chat fast path, including
        # whitespace/newline variants that a collection of LIKE phrases would
        # miss. Page backward so the method stays memory-bounded even for a
        # very long conversation while retaining facts independent of recency.
        selected: list[dict[str, str]] = []
        before_id = 9_223_372_036_854_775_807
        page_size = 256
        while len(selected) < limit:
            rows = self.db.execute(
                """
                SELECT id, role, content
                FROM messages
                WHERE conversation_id=? AND role='user' AND id<? AND (
                    lower(content) LIKE '%conversation%' OR
                    lower(content) LIKE '%chat%' OR
                    lower(content) LIKE '%session%' OR
                    lower(content) LIKE '%thread%'
                )
                ORDER BY id DESC
                LIMIT ?
                """,
                (conversation_id, before_id, page_size),
            ).fetchall()
            if not rows:
                break
            before_id = min(int(row["id"]) for row in rows)
            for row in rows:
                if scope.search(str(row["content"])) is None:
                    continue
                selected.append({
                    "role": str(row["role"]),
                    "content": str(row["content"]),
                })
                if len(selected) >= limit:
                    break
            if len(rows) < page_size:
                break
        return list(reversed(selected))

    @staticmethod
    def _presence_job_id(value: Any) -> str:
        job_id = str(value).strip().casefold()
        if re.fullmatch(r"[0-9a-f]{32}", job_id) is None:
            raise ValueError("Presence job id must be 32 lowercase hexadecimal characters")
        return job_id

    def create_presence_job(
        self,
        job_id: str,
        *,
        conversation_id: int,
        project_id: int,
        prompt: str,
        model_override: str,
        attachments_json: str = "[]",
        run_origin: str = "interactive",
        replayable: bool = True,
    ) -> dict[str, Any]:
        """Durably accept one Presence turn before it enters the in-memory queue."""
        normalized_id = self._presence_job_id(job_id)
        if not self.conversation_exists(conversation_id):
            raise ValueError("Conversation does not exist")
        normalized_project = self._project_id(project_id)
        conversation_project = self.conversation_project(conversation_id)
        if (
            conversation_project is None
            or int(conversation_project["id"]) != normalized_project
            or not bool(conversation_project.get("enabled"))
        ):
            raise ValueError("Conversation project does not exist or is disabled")
        safe_prompt = _bounded_persisted_text(
            redact_secrets(str(prompt).strip()), 50_000, "presence prompt"
        )
        if not safe_prompt:
            raise ValueError("Presence prompt must not be empty")
        model = str(model_override).strip().casefold()
        if model not in {"auto", "fast", "reasoning", "coding", "deep"}:
            raise ValueError("Invalid Presence model profile")
        normalized_origin = str(run_origin).strip().casefold()
        if normalized_origin not in {
            "interactive", "companion_suggestion", "companion_action",
        }:
            raise ValueError("Invalid Presence run origin")
        if not isinstance(replayable, bool):
            raise TypeError("Presence replayable flag must be boolean")
        if normalized_origin != "interactive" and replayable:
            raise ValueError("Companion Presence jobs must not be replayable")
        if normalized_origin != "interactive":
            # Enforce the privacy boundary at the durable sink, not only at the
            # current Presence caller. Future callers cannot accidentally persist
            # screen-derived prompts or even image descriptors.
            safe_prompt = "[ephemeral Screen Companion request]"
            attachments_json = "[]"
        try:
            descriptors = json.loads(str(attachments_json))
        except (TypeError, ValueError, json.JSONDecodeError):
            raise ValueError("Presence image descriptors are invalid") from None
        if not isinstance(descriptors, list) or len(descriptors) > 4:
            raise ValueError("Presence image descriptors are invalid")
        for descriptor in descriptors:
            if (
                not isinstance(descriptor, dict)
                or set(descriptor) != {"mime", "bytes", "sha256"}
                or descriptor.get("mime") not in {
                    "image/png", "image/jpeg", "image/webp", "image/gif"
                }
                or isinstance(descriptor.get("bytes"), bool)
                or not isinstance(descriptor.get("bytes"), int)
                or not 0 < descriptor["bytes"] <= 5 * 1024 * 1024
                or re.fullmatch(r"[0-9a-f]{64}", str(descriptor.get("sha256") or "")) is None
            ):
                raise ValueError("Presence image descriptors are invalid")
        safe_attachments_json = json.dumps(
            descriptors, ensure_ascii=True, separators=(",", ":"), sort_keys=True
        )
        stamp = now_iso()
        try:
            with self._immediate_transaction():
                self.db.execute(
                    """INSERT INTO presence_jobs(
                           job_id, created_at, updated_at, conversation_id,
                           project_id, prompt, attachments_json, model_override,
                           status, run_origin, replayable
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)""",
                    (
                        normalized_id, stamp, stamp, int(conversation_id),
                        normalized_project, safe_prompt, safe_attachments_json, model,
                        normalized_origin, int(replayable),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            if "presence_jobs.conversation_id" in str(exc):
                raise RuntimeError(
                    "That conversation already has an active or queued request"
                ) from exc
            raise
        return self.get_presence_job(normalized_id) or {}

    def get_presence_job(self, job_id: str) -> dict[str, Any] | None:
        normalized_id = self._presence_job_id(job_id)
        row = self.db.execute(
            """SELECT job_id, created_at, updated_at, conversation_id, project_id,
                      prompt, attachments_json, model_override, status, lease_owner, started_at,
                      finished_at, cancel_requested, last_error, metrics_json,
                      run_origin, replayable
               FROM presence_jobs WHERE job_id=?""",
            (normalized_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def list_presence_jobs(
        self,
        *,
        statuses: tuple[str, ...] = ("queued", "running"),
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        allowed = {
            "queued", "running", "completed", "failed", "cancelled", "interrupted"
        }
        normalized = tuple(dict.fromkeys(str(item).strip().casefold() for item in statuses))
        if not normalized or any(item not in allowed for item in normalized):
            raise ValueError("Invalid Presence job status filter")
        placeholders = ",".join("?" for _ in normalized)
        rows = self.db.execute(
            f"""SELECT job_id, created_at, updated_at, conversation_id, project_id,
                       prompt, attachments_json, model_override, status, lease_owner, started_at,
                       finished_at, cancel_requested, last_error, metrics_json,
                       run_origin, replayable
                FROM presence_jobs WHERE status IN ({placeholders})
                ORDER BY created_at, job_id LIMIT ?""",
            (*normalized, _bounded_limit(limit, 1_000)),
        ).fetchall()
        return [dict(row) for row in rows]

    def recover_presence_jobs(self, runtime_id: str) -> dict[str, Any]:
        """Recover never-started turns and stop uncertain active turns at-most-once."""
        owner = _validated_worker_id(runtime_id)
        stamp = now_iso()
        interrupted_message = (
            "Presence restarted while this request was active. The request was preserved "
            "but was not replayed automatically because its effects may already have occurred. "
            "Review the conversation and explicitly retry if needed."
        )
        with self._immediate_transaction():
            running = self.db.execute(
                """SELECT job_id, conversation_id FROM presence_jobs
                   WHERE status='running' ORDER BY created_at, job_id"""
            ).fetchall()
            for row in running:
                self.db.execute(
                    """UPDATE presence_jobs
                       SET status='interrupted', updated_at=?, finished_at=?,
                           lease_owner=NULL, last_error=?
                       WHERE job_id=? AND status='running'""",
                    (stamp, stamp, interrupted_message, row["job_id"]),
                )
                self.db.execute(
                    """INSERT INTO messages(conversation_id, created_at, role, content)
                       VALUES (?, ?, 'assistant', ?)""",
                    (int(row["conversation_id"]), stamp, interrupted_message),
                )
            queued = self.db.execute(
                """SELECT job_id, created_at, updated_at, conversation_id, project_id,
                          prompt, attachments_json, model_override, status, lease_owner, started_at,
                          finished_at, cancel_requested, last_error, metrics_json,
                          run_origin, replayable
                   FROM presence_jobs WHERE status='queued'
                   ORDER BY created_at, job_id"""
            ).fetchall()
            recoverable: list[sqlite3.Row] = []
            for row in queued:
                if (
                    bool(int(row["replayable"]))
                    and str(row["attachments_json"] or "[]") == "[]"
                ):
                    recoverable.append(row)
                    continue
                interruption_message = (
                    "Presence restarted before this Companion request began. The stale "
                    "observation was not replayed; wait for a fresh observation."
                    if not bool(int(row["replayable"]))
                    else (
                        "Presence restarted before this image request began. The image bytes "
                        "were intentionally not persisted; attach the images again and retry."
                    )
                )
                self.db.execute(
                    """UPDATE presence_jobs
                       SET status='interrupted', updated_at=?, finished_at=?,
                           lease_owner=NULL, last_error=?
                       WHERE job_id=? AND status='queued'""",
                    (stamp, stamp, interruption_message, row["job_id"]),
                )
                self.db.execute(
                    """INSERT INTO messages(conversation_id, created_at, role, content)
                       VALUES (?, ?, 'assistant', ?)""",
                    (int(row["conversation_id"]), stamp, interruption_message),
                )
        return {
            "runtime_id": owner,
            "interrupted": [str(row["job_id"]) for row in running],
            "queued": [dict(row) for row in recoverable],
        }

    def claim_presence_job(self, job_id: str, runtime_id: str) -> bool:
        normalized_id = self._presence_job_id(job_id)
        owner = _validated_worker_id(runtime_id)
        stamp = now_iso()
        with self._immediate_transaction():
            updated = self.db.execute(
                """UPDATE presence_jobs
                   SET status='running', updated_at=?, started_at=?, lease_owner=?
                   WHERE job_id=? AND status='queued' AND cancel_requested=0""",
                (stamp, stamp, owner, normalized_id),
            )
        return updated.rowcount == 1

    def request_presence_job_cancel(
        self,
        job_id: str,
        *,
        persist_confirmation: bool = False,
    ) -> str | None:
        """Request cancellation and durably finish a never-started chat turn.

        Queued jobs cannot rely on a worker to publish their terminal state: once
        cancelled here they are intentionally no longer claimable.  Persist the
        interactive assistant confirmation in the same transaction as that
        state transition so a restart cannot leave a silently cancelled turn.
        """
        normalized_id = self._presence_job_id(job_id)
        stamp = now_iso()
        with self._immediate_transaction():
            row = self.db.execute(
                """SELECT status, conversation_id, run_origin
                   FROM presence_jobs WHERE job_id=?""",
                (normalized_id,),
            ).fetchone()
            if row is None or row["status"] not in {"queued", "running"}:
                return None
            if row["status"] == "queued":
                self.db.execute(
                    """UPDATE presence_jobs
                       SET status='cancelled', updated_at=?, finished_at=?,
                           cancel_requested=1, last_error='Request cancelled before execution'
                       WHERE job_id=? AND status='queued'""",
                    (stamp, stamp, normalized_id),
                )
                if (
                    persist_confirmation
                    and str(row["run_origin"] or "").strip().casefold()
                    == "interactive"
                ):
                    self.db.execute(
                        """INSERT INTO messages(conversation_id, created_at, role, content)
                           VALUES (?, ?, 'assistant', ?)""",
                        (
                            int(row["conversation_id"]),
                            stamp,
                            "Request cancelled before execution.",
                        ),
                    )
                return "cancelled"
            self.db.execute(
                """UPDATE presence_jobs SET updated_at=?, cancel_requested=1
                   WHERE job_id=? AND status='running'""",
                (stamp, normalized_id),
            )
            return "cancelling"

    def abandon_unqueued_companion_action(self, job_id: str) -> bool:
        """Atomically cancel a never-enqueued action and release its feedback bind."""
        normalized_id = self._presence_job_id(job_id)
        action_job_digest = self._screen_companion_action_job_digest(normalized_id)
        stamp = now_iso()
        with self._immediate_transaction():
            updated = self.db.execute(
                """UPDATE presence_jobs
                   SET status='cancelled', updated_at=?, finished_at=?,
                       cancel_requested=1,
                       last_error='Request could not enter the full work queue'
                   WHERE job_id=? AND status='queued'""",
                (stamp, stamp, normalized_id),
            )
            self.db.execute(
                """DELETE FROM screen_companion_feedback
                   WHERE action_job_sha256=? AND decision='accepted'
                     AND id NOT IN (
                         SELECT feedback_id FROM screen_companion_action_outcomes
                     )""",
                (action_job_digest,),
            )
        return updated.rowcount == 1

    def finish_presence_job(
        self,
        job_id: str,
        status: str,
        *,
        runtime_id: str,
        error: str | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> bool:
        normalized_id = self._presence_job_id(job_id)
        owner = _validated_worker_id(runtime_id)
        terminal = str(status).strip().casefold()
        if terminal not in {"completed", "failed", "cancelled", "interrupted"}:
            raise ValueError("Presence job requires a terminal status")
        stamp = now_iso()
        safe_error = (
            None
            if error is None
            else _bounded_persisted_text(
                redact_secrets(str(error)), MAX_TASK_ERROR_CHARS, "presence error"
            )
        )
        allowed_counts = {
            "queue_ms", "total_ms", "agent_total_ms", "time_to_first_token_ms",
            "model_latency_ms", "model_attempts", "retries", "context_chars",
            "estimated_prompt_tokens", "tool_calls",
        }
        allowed_text = {
            "provider", "model", "profile", "task_contract_status",
        }
        safe_metrics: dict[str, Any] = {}
        for key, value in dict(metrics or {}).items():
            if value is None:
                continue
            if key in allowed_counts:
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ValueError(
                        f"Presence metric {key} must be a non-negative integer"
                    )
                safe_metrics[key] = min(value, 2**63 - 1)
            elif key in allowed_text:
                safe_metrics[key] = _validated_nonsecret_metadata(
                    value, f"Presence metric {key}"
                )[:200]
            elif key == "streamed":
                if not isinstance(value, bool):
                    raise TypeError("Presence metric streamed must be a boolean")
                safe_metrics[key] = value
            else:
                raise ValueError(f"Unsupported Presence metric: {key}")
        metrics_json = json.dumps(
            safe_metrics,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        with self._immediate_transaction():
            updated = self.db.execute(
                """UPDATE presence_jobs
                   SET status=?, updated_at=?, finished_at=?, lease_owner=NULL,
                       last_error=?, metrics_json=?
                   WHERE job_id=? AND status='running' AND lease_owner=?""",
                (
                    terminal, stamp, stamp, safe_error, metrics_json,
                    normalized_id, owner,
                ),
            )
        return updated.rowcount == 1

    @staticmethod
    def _presence_pairing_code(value: Any) -> str:
        code = re.sub(r"[\s-]+", "", str(value or "")).upper()
        if len(code) != 12 or any(char not in _PAIRING_ALPHABET for char in code):
            raise ValueError("Pairing code is invalid")
        return code

    @staticmethod
    def _presence_session_id(value: Any) -> str:
        session_id = str(value or "").strip().casefold()
        if re.fullmatch(r"[0-9a-f]{32}", session_id) is None:
            raise ValueError("Presence session id is invalid")
        return session_id

    @staticmethod
    def _pairing_digest(code: str, salt: bytes) -> bytes:
        return hashlib.pbkdf2_hmac(
            "sha256", code.encode("ascii"), salt, _PAIRING_PBKDF2_ROUNDS
        )

    def create_presence_pairing_code(
        self,
        label: str = "remote device",
        *,
        ttl_minutes: int = 10,
    ) -> dict[str, Any]:
        """Create a short-lived code; persist only its salted slow hash."""
        safe_label = redact_secrets(str(label).strip())[:120] or "remote device"
        ttl = max(1, min(int(ttl_minutes), 30))
        created = _as_utc()
        expires = created + timedelta(minutes=ttl)
        code = "".join(secrets.choice(_PAIRING_ALPHABET) for _ in range(12))
        salt = secrets.token_bytes(16)
        digest = self._pairing_digest(code, salt)
        with self._immediate_transaction():
            self.db.execute(
                """UPDATE presence_pairing_codes SET status='revoked'
                   WHERE status='pending'"""
            )
            cursor = self.db.execute(
                """INSERT INTO presence_pairing_codes(
                       created_at, expires_at, label, code_salt, code_digest, status
                   ) VALUES (?, ?, ?, ?, ?, 'pending')""",
                (created.isoformat(), expires.isoformat(), safe_label, salt, digest),
            )
        return {
            "pairing_id": int(cursor.lastrowid),
            "code": f"{code[:4]}-{code[4:8]}-{code[8:]}",
            "label": safe_label,
            "expires_at": expires.isoformat(),
        }

    def consume_presence_pairing_code(
        self,
        code: str,
        *,
        session_ttl_hours: int = 24 * 30,
    ) -> dict[str, Any] | None:
        """Atomically exchange one unexpired code for one high-entropy session."""
        try:
            normalized = self._presence_pairing_code(code)
        except ValueError:
            # Invalid shapes take one slow hash too, reducing the timing oracle.
            self._pairing_digest("2" * 12, b"\0" * 16)
            return None
        current = _as_utc()
        ttl = max(1, min(int(session_ttl_hours), 24 * 90))
        with self._immediate_transaction():
            rows = self.db.execute(
                """SELECT id, label, code_salt, code_digest
                   FROM presence_pairing_codes
                   WHERE status='pending' AND expires_at>? ORDER BY id DESC LIMIT 8""",
                (current.isoformat(),),
            ).fetchall()
            matched = None
            for row in rows:
                candidate = self._pairing_digest(normalized, bytes(row["code_salt"]))
                if secrets.compare_digest(candidate, bytes(row["code_digest"])):
                    matched = row
                    break
            if matched is None:
                return None
            consumed = self.db.execute(
                """UPDATE presence_pairing_codes
                   SET status='consumed', consumed_at=?
                   WHERE id=? AND status='pending' AND expires_at>?""",
                (current.isoformat(), int(matched["id"]), current.isoformat()),
            )
            if consumed.rowcount != 1:
                return None
            token = secrets.token_urlsafe(32)
            session_id = uuid4().hex
            expires = current + timedelta(hours=ttl)
            self.db.execute(
                """INSERT INTO presence_sessions(
                       session_id, session_digest, created_at, expires_at,
                       last_seen_at, label, pairing_code_id
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    hashlib.sha256(token.encode("ascii")).hexdigest(),
                    current.isoformat(),
                    expires.isoformat(),
                    current.isoformat(),
                    str(matched["label"]),
                    int(matched["id"]),
                ),
            )
        return {
            "session_id": session_id,
            "token": token,
            "label": str(matched["label"]),
            "expires_at": expires.isoformat(),
        }

    def authenticate_presence_session(self, token: Any) -> bool:
        raw = str(token or "")
        if len(raw) < 32 or len(raw) > 128 or re.fullmatch(r"[A-Za-z0-9_-]+", raw) is None:
            return False
        digest = hashlib.sha256(raw.encode("ascii")).hexdigest()
        current_dt = _as_utc()
        current = current_dt.isoformat()
        row = self.db.execute(
            """SELECT last_seen_at FROM presence_sessions
               WHERE session_digest=? AND revoked_at IS NULL AND expires_at>?""",
            (digest, current),
        ).fetchone()
        if row is None:
            return False
        # Authentication polling is read-only in the common case. Persist a
        # coarse last-seen heartbeat without turning every event poll into a write.
        heartbeat_cutoff = (current_dt - timedelta(minutes=5)).isoformat()
        if str(row["last_seen_at"]) < heartbeat_cutoff:
            with self._immediate_transaction():
                self.db.execute(
                    """UPDATE presence_sessions SET last_seen_at=?
                       WHERE session_digest=? AND revoked_at IS NULL AND expires_at>?
                         AND last_seen_at<?""",
                    (current, digest, current, heartbeat_cutoff),
                )
        return True

    def list_presence_sessions(self) -> list[dict[str, Any]]:
        rows = self.db.execute(
            """SELECT session_id, created_at, expires_at, last_seen_at,
                      revoked_at, label
               FROM presence_sessions ORDER BY created_at DESC, session_id"""
        ).fetchall()
        return [dict(row) for row in rows]

    def revoke_presence_session(self, session_id: str) -> bool:
        normalized = self._presence_session_id(session_id)
        with self._immediate_transaction():
            updated = self.db.execute(
                """UPDATE presence_sessions SET revoked_at=?
                   WHERE session_id=? AND revoked_at IS NULL""",
                (now_iso(), normalized),
            )
        return updated.rowcount == 1

    def revoke_all_presence_sessions(self) -> int:
        with self._immediate_transaction():
            updated = self.db.execute(
                """UPDATE presence_sessions SET revoked_at=? WHERE revoked_at IS NULL""",
                (now_iso(),),
            )
        return int(updated.rowcount)

    def search_messages(
        self,
        query: str,
        limit: int = 8,
        *,
        project_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Search redacted persisted sessions without exposing the whole transcript."""
        self._ensure_open()
        normalized_project = (
            self._project_id(project_id) if project_id is not None else None
        )
        query = str(query)
        if contains_secret(query):
            raise ValueError("Potential secret detected; session search refused")
        if len(query) > MAX_SEARCH_QUERY_CHARS:
            raise ValueError(
                f"Session search query exceeds {MAX_SEARCH_QUERY_CHARS} characters"
            )
        limit = _bounded_limit(limit, 50)
        query_terms = _memory_query_terms(query)
        like_terms = _memory_like_terms(query, query_terms)
        if not query_terms or not like_terms or not limit:
            return []
        fts_query = _memory_fts_query(query, query_terms)
        candidate_limit = min(500, max(limit * 12, 48))
        if fts_query is not None:
            rows = self.db.execute(
                """SELECT m.id, m.conversation_id, c.title, m.created_at, m.role,
                          m.content
                   FROM message_fts
                   JOIN messages AS m ON m.id=message_fts.rowid
                   JOIN conversations AS c ON c.id=m.conversation_id
                   WHERE message_fts MATCH ?
                     AND (? IS NULL OR c.project_id=?)
                   ORDER BY message_fts.rank, m.id DESC LIMIT ?""",
                (
                    fts_query,
                    normalized_project,
                    normalized_project,
                    candidate_limit,
                ),
            ).fetchall()
        else:
            patterns = [f"%{_escape_like(term)}%" for term in like_terms]
            where = " OR ".join(
                "lower(m.content) LIKE ? ESCAPE '\\'" for _ in patterns
            )
            match_count = " + ".join(
                "CASE WHEN lower(m.content) LIKE ? ESCAPE '\\' THEN 1 ELSE 0 END"
                for _ in patterns
            )
            rows = self.db.execute(
                f"""SELECT m.id, m.conversation_id, c.title, m.created_at, m.role,
                           m.content
                    FROM messages AS m
                    JOIN conversations AS c ON c.id=m.conversation_id
                    WHERE ({where}) AND (? IS NULL OR c.project_id=?)
                    ORDER BY ({match_count}) DESC, m.id DESC LIMIT ?""",
                [
                    *patterns,
                    normalized_project,
                    normalized_project,
                    *patterns,
                    candidate_limit,
                ],
            ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows[:limit]:
            item = dict(row)
            content = str(item.pop("content", ""))
            item["excerpt"] = (
                content
                if len(content) <= 2_000
                else content[:1_950] + "\n...[session excerpt clipped]"
            )
            results.append(item)
        return results

    @staticmethod
    def _ordinary_memory_provenance_digest(material: dict[str, Any]) -> str:
        canonical = json.dumps(
            material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _ordinary_memory_provenance_material(
        self,
        memory_id: int,
        *,
        origin: str,
        eligible: bool,
    ) -> dict[str, Any] | None:
        row = self.db.execute(
            """SELECT id, created_at, kind, content, source
               FROM memories WHERE id=?""",
            (int(memory_id),),
        ).fetchone()
        if row is None or str(row["kind"]) in {"lesson", "claim"}:
            return None
        return {
            "schema": "jarvis.ordinary-memory-provenance.v1",
            "memory": {
                "id": int(row["id"]),
                "created_at": str(row["created_at"]),
                "kind": str(row["kind"]),
                "content": str(row["content"]),
                "source": None if row["source"] is None else str(row["source"]),
            },
            "authorization": {
                "origin": str(origin),
                "eligible": bool(eligible),
            },
        }

    def _set_ordinary_memory_provenance_locked(
        self,
        memory_id: int,
        *,
        origin: str,
        eligible: bool,
    ) -> None:
        normalized_origin = _validated_nonsecret_metadata(
            origin, "Memory provenance origin"
        )[:80]
        if eligible and normalized_origin not in self.ORDINARY_MEMORY_PROVENANCE_ORIGINS:
            raise ValueError("Unknown trusted memory provenance origin")
        if not eligible and normalized_origin != "unverified":
            raise ValueError("Ineligible memory provenance must be unverified")
        material = self._ordinary_memory_provenance_material(
            int(memory_id), origin=normalized_origin, eligible=bool(eligible)
        )
        if material is None:
            raise ValueError("Ordinary memory provenance cannot bind this record")
        content = str(material["memory"]["content"])
        self.db.execute(
            """INSERT INTO ordinary_memory_provenance(
                   memory_id, recorded_at, origin, eligible,
                   content_sha256, provenance_sha256
               ) VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(memory_id) DO UPDATE SET
                   recorded_at=excluded.recorded_at,
                   origin=excluded.origin,
                   eligible=excluded.eligible,
                   content_sha256=excluded.content_sha256,
                   provenance_sha256=excluded.provenance_sha256""",
            (
                int(memory_id),
                now_iso(),
                normalized_origin,
                int(bool(eligible)),
                hashlib.sha256(content.encode("utf-8")).hexdigest(),
                self._ordinary_memory_provenance_digest(material),
            ),
        )

    def _ordinary_memory_provenance_validation(
        self,
        memory_id: int,
    ) -> tuple[bool, bool, bool, bool]:
        """Return (valid, eligible, content mismatch, provenance mismatch)."""
        row = self.db.execute(
            """SELECT memory_id, origin, eligible, content_sha256,
                      provenance_sha256
               FROM ordinary_memory_provenance WHERE memory_id=?""",
            (int(memory_id),),
        ).fetchone()
        memory = self.db.execute(
            "SELECT content, kind FROM memories WHERE id=?",
            (int(memory_id),),
        ).fetchone()
        if memory is None or str(memory["kind"]) in {"lesson", "claim"}:
            return False, False, False, row is not None
        if row is None:
            return False, False, False, True
        eligible = bool(int(row["eligible"]))
        origin = str(row["origin"])
        content_hash = hashlib.sha256(
            str(memory["content"]).encode("utf-8")
        ).hexdigest()
        content_mismatch = str(row["content_sha256"] or "") != content_hash
        material = self._ordinary_memory_provenance_material(
            int(memory_id), origin=origin, eligible=eligible
        )
        expected = (
            None
            if material is None
            else self._ordinary_memory_provenance_digest(material)
        )
        provenance_mismatch = (
            expected is None
            or str(row["provenance_sha256"] or "") != expected
            or (eligible and origin not in self.ORDINARY_MEMORY_PROVENANCE_ORIGINS)
            or (not eligible and origin != "unverified")
        )
        return (
            not content_mismatch and not provenance_mismatch,
            eligible,
            content_mismatch,
            provenance_mismatch,
        )

    def _ordinary_memory_recall_eligible(self, memory_id: int) -> bool:
        valid, eligible, _content_mismatch, _provenance_mismatch = (
            self._ordinary_memory_provenance_validation(int(memory_id))
        )
        return valid and eligible

    def _filter_generic_recall_rows(
        self,
        rows: list[sqlite3.Row],
    ) -> list[sqlite3.Row]:
        """Keep active claims and canonically verified ordinary memories only."""
        return [
            row
            for row in rows
            if str(row["kind"]) == "claim"
            or self._ordinary_memory_recall_eligible(int(row["id"]))
        ]

    def _remember_ordinary(
        self,
        content: str,
        kind: str,
        source: str | None,
        *,
        origin: str,
        eligible: bool,
    ) -> str:
        safe_content = redact_secrets(str(content).strip())[:8_000]
        if not safe_content:
            raise ValueError("Memory content must not be empty")
        safe_kind = _validated_nonsecret_metadata(kind, "Memory kind")[:40]
        if safe_kind in {"lesson", "claim"}:
            raise ValueError("Lessons and claims require their dedicated provenance APIs")
        safe_source = (
            redact_secrets(str(source).strip())[:2_000] if source else None
        )
        with self._immediate_transaction():
            self.db.execute(
                """INSERT OR IGNORE INTO memories(created_at, kind, content, source)
                   VALUES (?, ?, ?, ?)""",
                (now_iso(), safe_kind, safe_content, safe_source),
            )
            row = self.db.execute(
                """SELECT id, source FROM memories
                   WHERE kind=? AND content=?""",
                (safe_kind, safe_content),
            ).fetchone()
            if row is None:
                raise RuntimeError("Memory could not be persisted")
            stored_source = None if row["source"] is None else str(row["source"])
            if eligible and stored_source != safe_source:
                raise ValueError(
                    "Existing memory text has different source provenance"
                )
            existing = self.db.execute(
                "SELECT eligible FROM ordinary_memory_provenance WHERE memory_id=?",
                (int(row["id"]),),
            ).fetchone()
            # A later unverified duplicate must never downgrade a trusted row.
            if eligible or existing is None or not bool(int(existing["eligible"])):
                self._set_ordinary_memory_provenance_locked(
                    int(row["id"]), origin=origin, eligible=eligible
                )
        return "Stored in long-term memory."

    def remember(self, content: str, kind: str = "fact", source: str | None = None) -> str:
        """Store an auditable but recall-ineligible ordinary memory by default."""
        return self._remember_ordinary(
            content, kind, source, origin="unverified", eligible=False
        )

    def remember_verified(
        self,
        content: str,
        kind: str = "fact",
        source: str | None = None,
        *,
        origin: str,
    ) -> str:
        """Store ordinary memory only through an explicit trusted write path."""
        return self._remember_ordinary(
            content, kind, source, origin=origin, eligible=True
        )

    @staticmethod
    def _screen_companion_app(value: Any) -> str:
        app = Path(str(value or "").strip()).name.casefold()
        if not app or len(app) > 120 or re.fullmatch(r"[a-z0-9._ +()-]+", app) is None:
            raise ValueError("Screen Companion application name is invalid")
        if contains_secret(app):
            raise ValueError("Potential secret detected in application name")
        return app

    def screen_companion_state(self) -> dict[str, Any]:
        row = self.db.execute(
            """SELECT mode, paused, auto_suggest, excluded_apps_json, updated_at
               FROM screen_companion_state WHERE id=1"""
        ).fetchone()
        if row is None:
            raise RuntimeError("Screen Companion state is unavailable")
        try:
            raw_excluded = json.loads(str(row["excluded_apps_json"]))
        except json.JSONDecodeError:
            raw_excluded = []
        excluded = [
            str(item) for item in raw_excluded
            if isinstance(item, str)
        ][:64]
        return {
            "mode": str(row["mode"]),
            "paused": bool(int(row["paused"])),
            "auto_suggest": bool(int(row["auto_suggest"])),
            "excluded_apps": excluded,
            "updated_at": str(row["updated_at"]),
        }

    def set_screen_companion_state(
        self,
        *,
        mode: str,
        paused: bool,
        auto_suggest: bool,
        excluded_apps: list[str],
    ) -> dict[str, Any]:
        normalized_mode = str(mode).strip().casefold()
        if normalized_mode not in {"disabled", "observe", "suggest", "collaborate"}:
            raise ValueError("Screen Companion mode is invalid")
        if not isinstance(paused, bool) or not isinstance(auto_suggest, bool):
            raise TypeError("Screen Companion switches must be boolean")
        if not isinstance(excluded_apps, list) or len(excluded_apps) > 64:
            raise ValueError("Screen Companion exclusions must contain at most 64 apps")
        normalized_excluded = sorted({
            self._screen_companion_app(item) for item in excluded_apps
        })
        if normalized_mode == "disabled":
            paused = True
            auto_suggest = False
        elif normalized_mode == "observe":
            auto_suggest = False
        stamp = now_iso()
        with self._immediate_transaction():
            self.db.execute(
                """UPDATE screen_companion_state
                   SET mode=?, paused=?, auto_suggest=?, excluded_apps_json=?,
                       updated_at=? WHERE id=1""",
                (
                    normalized_mode,
                    int(paused),
                    int(auto_suggest),
                    json.dumps(normalized_excluded, separators=(",", ":")),
                    stamp,
                ),
            )
        return self.screen_companion_state()

    def control_screen_companion_state(
        self,
        *,
        action: str,
        mode: str | None = None,
    ) -> dict[str, Any]:
        """Apply one small Companion control change without replacing its settings."""
        normalized_action = str(action or "").strip().casefold()
        if normalized_action not in {"on", "pause", "resume", "off", "mode"}:
            raise ValueError(
                "Screen Companion action must be on, pause, resume, off, or mode"
            )
        normalized_mode = None if mode is None else str(mode).strip().casefold()
        if normalized_action == "mode":
            if normalized_mode not in {"observe", "suggest", "collaborate"}:
                raise ValueError(
                    "Screen Companion mode control must select observe, suggest, or collaborate"
                )
        elif normalized_mode is not None:
            raise ValueError("Screen Companion mode is only valid for the mode action")

        stamp = now_iso()
        with self._immediate_transaction():
            row = self.db.execute(
                """SELECT mode, paused, auto_suggest
                   FROM screen_companion_state WHERE id=1"""
            ).fetchone()
            if row is None:
                raise RuntimeError("Screen Companion state is unavailable")
            current_mode = str(row["mode"])
            paused = bool(int(row["paused"]))
            auto_suggest = bool(int(row["auto_suggest"]))

            if normalized_action in {"on", "resume"}:
                if current_mode == "disabled":
                    current_mode = "observe"
                    auto_suggest = False
                paused = False
            elif normalized_action == "pause":
                paused = True
            elif normalized_action == "off":
                current_mode = "disabled"
                paused = True
                auto_suggest = False
            else:
                current_mode = str(normalized_mode)
                paused = False
                if current_mode == "observe":
                    auto_suggest = False

            self.db.execute(
                """UPDATE screen_companion_state
                   SET mode=?, paused=?, auto_suggest=?, updated_at=? WHERE id=1""",
                (current_mode, int(paused), int(auto_suggest), stamp),
            )
        return self.screen_companion_state()

    def add_screen_companion_rule(
        self,
        *,
        trigger_app: str,
        action_prompt: str,
        action_mode: str = "suggest",
        title_contains: str | None = None,
        cooldown_seconds: int = 300,
    ) -> int:
        app = self._screen_companion_app(trigger_app)
        prompt = redact_secrets(str(action_prompt).strip())
        if not prompt or len(prompt) > 4_000:
            raise ValueError("Screen Companion rule prompt must contain 1-4000 characters")
        if contains_secret(str(action_prompt)):
            raise ValueError("Potential secret detected; Screen Companion rule refused")
        mode = str(action_mode).strip().casefold()
        if mode not in {"suggest", "collaborate"}:
            raise ValueError("Screen Companion rule mode is invalid")
        title = None
        if title_contains is not None and str(title_contains).strip():
            raw_title = str(title_contains).strip()
            if contains_secret(raw_title):
                raise ValueError("Potential secret detected in title matcher")
            title = redact_secrets(raw_title)[:200].casefold()
        if (
            isinstance(cooldown_seconds, bool)
            or not isinstance(cooldown_seconds, int)
            or not 30 <= cooldown_seconds <= 86_400
        ):
            raise ValueError("Screen Companion cooldown must be 30-86400 seconds")
        stamp = now_iso()
        with self._immediate_transaction():
            cursor = self.db.execute(
                """INSERT INTO screen_companion_rules(
                       created_at, updated_at, trigger_app, title_contains,
                       action_prompt, action_mode, cooldown_seconds, enabled
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, 1)""",
                (stamp, stamp, app, title, prompt, mode, cooldown_seconds),
            )
        return int(cursor.lastrowid)

    def list_screen_companion_rules(self) -> list[dict[str, Any]]:
        return [
            {
                **dict(row),
                "enabled": bool(int(row["enabled"])),
            }
            for row in self.db.execute(
                """SELECT id, created_at, updated_at, trigger_app,
                          title_contains, action_prompt, action_mode,
                          cooldown_seconds, enabled, last_triggered_at
                   FROM screen_companion_rules ORDER BY id"""
            ).fetchall()
        ]

    def set_screen_companion_rule_enabled(self, rule_id: int, enabled: bool) -> bool:
        normalized = self._prediction_optional_id(rule_id, "rule_id")
        if normalized is None:
            raise ValueError("rule_id is required")
        if not isinstance(enabled, bool):
            raise TypeError("enabled must be boolean")
        with self._immediate_transaction():
            cursor = self.db.execute(
                """UPDATE screen_companion_rules
                   SET enabled=?, updated_at=? WHERE id=?""",
                (int(enabled), now_iso(), normalized),
            )
        return cursor.rowcount == 1

    def delete_screen_companion_rule(self, rule_id: int) -> bool:
        normalized = self._prediction_optional_id(rule_id, "rule_id")
        if normalized is None:
            raise ValueError("rule_id is required")
        with self._immediate_transaction():
            self.db.execute(
                "DELETE FROM screen_companion_receipts WHERE rule_id=?",
                (normalized,),
            )
            cursor = self.db.execute(
                "DELETE FROM screen_companion_rules WHERE id=?", (normalized,)
            )
        return cursor.rowcount == 1

    def claim_screen_companion_rule(
        self,
        rule_id: int,
        *,
        application: str,
        context_sha256: str,
        now: datetime | None = None,
    ) -> int | None:
        normalized = self._prediction_optional_id(rule_id, "rule_id")
        app = self._screen_companion_app(application)
        digest = str(context_sha256).strip().casefold()
        if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValueError("Screen Companion context digest is invalid")
        current = _as_utc(now)
        stamp = current.isoformat()
        with self._immediate_transaction():
            row = self.db.execute(
                """SELECT action_mode, cooldown_seconds, enabled,
                          last_triggered_at
                   FROM screen_companion_rules WHERE id=?""",
                (normalized,),
            ).fetchone()
            if row is None or not bool(int(row["enabled"])):
                return None
            last = row["last_triggered_at"]
            if last is not None:
                try:
                    last_at = _as_utc(datetime.fromisoformat(str(last)))
                except ValueError:
                    return None
                if current < last_at + timedelta(seconds=int(row["cooldown_seconds"])):
                    return None
            self.db.execute(
                """UPDATE screen_companion_rules
                   SET last_triggered_at=?, updated_at=? WHERE id=?""",
                (stamp, stamp, normalized),
            )
            cursor = self.db.execute(
                """INSERT INTO screen_companion_receipts(
                       created_at, rule_id, application_sha256, context_sha256,
                       action_mode, status
                   ) VALUES (?, ?, ?, ?, ?, 'claimed')""",
                (
                    stamp,
                    normalized,
                    hashlib.sha256(app.encode("utf-8")).hexdigest(),
                    digest,
                    str(row["action_mode"]),
                ),
            )
        return int(cursor.lastrowid)

    def finish_screen_companion_receipt(
        self,
        receipt_id: int,
        *,
        status: str,
        job_id: str | None = None,
    ) -> bool:
        normalized = self._prediction_optional_id(receipt_id, "receipt_id")
        safe_status = str(status).strip().casefold()
        if safe_status not in {"suggested", "queued", "skipped", "failed"}:
            raise ValueError("Screen Companion receipt status is invalid")
        safe_job = None
        if job_id is not None:
            safe_job = _validated_nonsecret_metadata(job_id, "Screen Companion job ID")[:100]
        with self._immediate_transaction():
            cursor = self.db.execute(
                """UPDATE screen_companion_receipts SET status=?, job_id=?
                   WHERE id=? AND status='claimed'""",
                (safe_status, safe_job, normalized),
            )
        return cursor.rowcount == 1

    def claim_screen_companion_auto(
        self,
        *,
        context_sha256: str,
        cooldown_seconds: int,
        daily_limit: int = 6,
        now: datetime | None = None,
    ) -> int | None:
        """Atomically reserve one automatic suggestion across process restarts.

        Only a context digest and timestamps are retained.  This receipt grants no
        tool or action authority; it solely makes the privacy/rate limit durable.
        """
        digest = self._screen_companion_learning_digest(
            context_sha256, "Screen Companion context digest"
        )
        if (
            isinstance(cooldown_seconds, bool)
            or not isinstance(cooldown_seconds, int)
            or not 30 <= cooldown_seconds <= 86_400
        ):
            raise ValueError("Screen Companion automatic cooldown is invalid")
        if (
            isinstance(daily_limit, bool)
            or not isinstance(daily_limit, int)
            or not 1 <= daily_limit <= 100
        ):
            raise ValueError("Screen Companion automatic daily limit is invalid")
        current = _as_utc(now or datetime.now(timezone.utc))
        stamp = current.isoformat()
        day_key = current.date().isoformat()
        with self._immediate_transaction():
            # Keep the privacy receipt store bounded while retaining enough history
            # for restart-safe daily and cooldown enforcement.
            self.db.execute(
                "DELETE FROM screen_companion_auto_receipts WHERE created_at < ?",
                ((current - timedelta(days=31)).isoformat(),),
            )
            if self.db.execute(
                """SELECT 1 FROM screen_companion_auto_receipts
                   WHERE day_key=? AND context_sha256=?""",
                (day_key, digest),
            ).fetchone() is not None:
                return None
            count = int(self.db.execute(
                """SELECT COUNT(*) FROM screen_companion_auto_receipts
                   WHERE day_key=?""",
                (day_key,),
            ).fetchone()[0])
            if count >= daily_limit:
                return None
            latest = self.db.execute(
                """SELECT created_at FROM screen_companion_auto_receipts
                   ORDER BY created_at DESC, id DESC LIMIT 1"""
            ).fetchone()
            if latest is not None:
                try:
                    latest_at = _as_utc(datetime.fromisoformat(str(latest[0])))
                except ValueError:
                    return None
                if current < latest_at + timedelta(seconds=cooldown_seconds):
                    return None
            cursor = self.db.execute(
                """INSERT INTO screen_companion_auto_receipts(
                       created_at, day_key, context_sha256
                   ) VALUES (?, ?, ?)""",
                (stamp, day_key, digest),
            )
        return int(cursor.lastrowid)

    @staticmethod
    def _screen_companion_learning_digest(value: Any, label: str) -> str:
        if not isinstance(value, str):
            raise TypeError(f"{label} must be a SHA-256 string")
        digest = value.strip().casefold()
        if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValueError(f"{label} must be a lowercase SHA-256 digest")
        return digest

    @staticmethod
    def _screen_companion_action_job_digest(action_job_id: Any) -> str:
        if not isinstance(action_job_id, str):
            raise TypeError("Screen Companion action job ID must be a string")
        normalized = action_job_id.strip()
        if (
            not normalized
            or len(normalized) > 200
            or re.fullmatch(r"[A-Za-z0-9._:-]+", normalized) is None
            or contains_secret(normalized)
        ):
            raise ValueError("Screen Companion action job ID is invalid")
        return hashlib.sha256(
            ("jarvis-screen-companion-action-v1\0" + normalized).encode("utf-8")
        ).hexdigest()

    def record_screen_companion_feedback(
        self,
        *,
        suggestion_sha256: str,
        context_sha256: str,
        application_sha256: str,
        decision: str,
        category: str = "general",
        action_mode: str = "suggest",
        action_job_id: str | None = None,
    ) -> int:
        """Record content-free operator feedback exactly once.

        The three caller-provided digests are opaque identifiers. The optional
        action job identifier is hashed before persistence and is required only
        for an accepted suggestion. No screen, title, prompt, or suggestion text
        enters this table.
        """
        suggestion_digest = self._screen_companion_learning_digest(
            suggestion_sha256, "Screen Companion suggestion digest"
        )
        context_digest = self._screen_companion_learning_digest(
            context_sha256, "Screen Companion context digest"
        )
        application_digest = self._screen_companion_learning_digest(
            application_sha256, "Screen Companion application digest"
        )
        normalized_decision = str(decision).strip().casefold()
        normalized_category = str(category).strip().casefold()
        normalized_mode = str(action_mode).strip().casefold()
        if normalized_decision not in self.SCREEN_COMPANION_LEARNING_DECISIONS:
            raise ValueError("Screen Companion feedback decision is invalid")
        if normalized_category not in self.SCREEN_COMPANION_LEARNING_CATEGORIES:
            raise ValueError("Screen Companion feedback category is invalid")
        if normalized_mode not in {"suggest", "collaborate"}:
            raise ValueError("Screen Companion feedback action mode is invalid")
        action_job_digest = None
        if normalized_decision == "accepted":
            if action_job_id is None:
                raise ValueError("Accepted Companion feedback requires an action job ID")
            action_job_digest = self._screen_companion_action_job_digest(action_job_id)
        elif action_job_id is not None:
            raise ValueError("Dismissed Companion feedback must not bind an action job")

        exact = (
            suggestion_digest,
            context_digest,
            application_digest,
            normalized_category,
            normalized_mode,
            normalized_decision,
            action_job_digest,
        )
        try:
            with self._immediate_transaction():
                existing = self.db.execute(
                    """SELECT id, suggestion_sha256, context_sha256,
                              application_sha256, category, action_mode,
                              decision, action_job_sha256
                       FROM screen_companion_feedback
                       WHERE suggestion_sha256=? AND context_sha256=?
                         AND application_sha256=?""",
                    exact[:3],
                ).fetchone()
                if existing is not None:
                    observed = tuple(existing[key] for key in (
                        "suggestion_sha256", "context_sha256", "application_sha256",
                        "category", "action_mode", "decision", "action_job_sha256",
                    ))
                    if observed != exact:
                        raise ValueError(
                            "Companion feedback identifier is already bound differently"
                        )
                    return int(existing["id"])
                cursor = self.db.execute(
                    """INSERT INTO screen_companion_feedback(
                           created_at, suggestion_sha256, context_sha256,
                           application_sha256, category, action_mode, decision,
                           action_job_sha256
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (now_iso(), *exact),
                )
                return int(cursor.lastrowid)
        except sqlite3.IntegrityError:
            raise ValueError(
                "Companion feedback conflicts with an existing protected binding"
            ) from None

    def screen_companion_feedback_for_action_job(
        self, action_job_id: str
    ) -> dict[str, Any] | None:
        """Find content-free feedback after restart using a hashed action job ID."""
        action_job_digest = self._screen_companion_action_job_digest(action_job_id)
        row = self.db.execute(
            """SELECT f.id AS feedback_id, f.created_at,
                      f.suggestion_sha256, f.context_sha256,
                      f.application_sha256, f.category, f.action_mode,
                      f.decision, o.recorded_at AS outcome_recorded_at,
                      o.outcome, o.evidence_kind, o.prediction_id, o.reusable
               FROM screen_companion_feedback AS f
               LEFT JOIN screen_companion_action_outcomes AS o
                 ON o.feedback_id=f.id
               WHERE f.action_job_sha256=?""",
            (action_job_digest,),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        if result["reusable"] is not None:
            result["reusable"] = bool(int(result["reusable"]))
        return result

    def discard_screen_companion_feedback_for_action_job(
        self, action_job_id: str
    ) -> bool:
        """Remove an accepted binding whose action never entered the work queue."""
        action_job_digest = self._screen_companion_action_job_digest(action_job_id)
        with self._immediate_transaction():
            cursor = self.db.execute(
                """DELETE FROM screen_companion_feedback
                   WHERE action_job_sha256=? AND decision='accepted'
                     AND id NOT IN (
                         SELECT feedback_id FROM screen_companion_action_outcomes
                     )""",
                (action_job_digest,),
            )
        return cursor.rowcount == 1

    def bind_screen_companion_outcome(
        self,
        *,
        action_job_id: str,
        prediction_id: int,
    ) -> bool:
        """Bind one exact resolved Companion prediction to accepted feedback.

        A positive reusable outcome requires a resolved ``companion_action``
        prediction, ``actual_status='complete'``, and ``evidence_ok=1``. Failed
        or incomplete predictions remain useful negative signals but are marked
        permanently non-reusable.
        """
        action_job_digest = self._screen_companion_action_job_digest(action_job_id)
        normalized_prediction = self._prediction_optional_id(
            prediction_id, "prediction_id"
        )
        with self._immediate_transaction():
            feedback = self.db.execute(
                """SELECT id, decision FROM screen_companion_feedback
                   WHERE action_job_sha256=?""",
                (action_job_digest,),
            ).fetchone()
            if feedback is None or str(feedback["decision"]) != "accepted":
                raise ValueError(
                    "Companion outcome requires exact accepted feedback"
                )
            prediction = self.db.execute(
                """SELECT origin, predicted_verification, resolved_at,
                          actual_status, evidence_ok, run_id_sha256
                   FROM task_predictions WHERE id=?""",
                (normalized_prediction,),
            ).fetchone()
            if prediction is None or prediction["resolved_at"] is None:
                raise ValueError("Companion outcome requires a resolved prediction")
            if str(prediction["origin"]) != "companion_action":
                raise ValueError("Prediction is not a Companion action outcome")
            if str(prediction["run_id_sha256"] or "") != str(
                self._prediction_run_digest(action_job_id) or ""
            ):
                raise ValueError(
                    "Companion prediction is not bound to this exact action job"
                )
            outcome = str(prediction["actual_status"] or "")
            if outcome not in self.SCREEN_COMPANION_LEARNING_OUTCOMES:
                raise ValueError("Companion prediction outcome is invalid")
            if outcome == "complete":
                evidence_kind = str(prediction["predicted_verification"] or "")
                if (
                    int(prediction["evidence_ok"] or 0) != 1
                    or evidence_kind not in {
                        "cited_sources", "process_evidence", "tool_success",
                    }
                ):
                    raise ValueError(
                        "Completed Companion outcome lacks verified evidence"
                    )
                reusable = 1
            else:
                evidence_kind = "failure_observed"
                reusable = 0
            existing = self.db.execute(
                """SELECT feedback_id, outcome, evidence_kind, prediction_id,
                          reusable
                   FROM screen_companion_action_outcomes WHERE feedback_id=?""",
                (int(feedback["id"]),),
            ).fetchone()
            expected = (
                int(feedback["id"]), outcome, evidence_kind,
                int(normalized_prediction), reusable,
            )
            if existing is not None:
                observed = tuple(existing[key] for key in (
                    "feedback_id", "outcome", "evidence_kind", "prediction_id",
                    "reusable",
                ))
                if observed != expected:
                    raise ValueError(
                        "Companion feedback already has a different outcome binding"
                    )
                return True
            try:
                self.db.execute(
                    """INSERT INTO screen_companion_action_outcomes(
                           feedback_id, recorded_at, outcome, evidence_kind,
                           prediction_id, reusable
                       ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        int(feedback["id"]), now_iso(), outcome, evidence_kind,
                        int(normalized_prediction), reusable,
                    ),
                )
            except sqlite3.IntegrityError:
                raise ValueError(
                    "Companion prediction is already bound to different feedback"
                ) from None
        return True

    def screen_companion_learning_policy(
        self,
        *,
        suggestion_sha256: str,
        application_sha256: str,
        category: str,
    ) -> dict[str, Any]:
        """Return a suppression-only policy signal from content-free feedback."""
        suggestion_digest = self._screen_companion_learning_digest(
            suggestion_sha256, "Screen Companion suggestion digest"
        )
        application_digest = self._screen_companion_learning_digest(
            application_sha256, "Screen Companion application digest"
        )
        normalized_category = str(category).strip().casefold()
        if normalized_category not in self.SCREEN_COMPANION_LEARNING_CATEGORIES:
            raise ValueError("Screen Companion feedback category is invalid")
        row = self.db.execute(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN decision='accepted' THEN 1 ELSE 0 END) AS accepted,
                      SUM(CASE WHEN decision='dismissed' THEN 1 ELSE 0 END) AS dismissed
               FROM screen_companion_feedback
               WHERE suggestion_sha256=? AND application_sha256=? AND category=?""",
            (suggestion_digest, application_digest, normalized_category),
        ).fetchone()
        total = int(row["total"] or 0)
        accepted = int(row["accepted"] or 0)
        dismissed = int(row["dismissed"] or 0)
        category_row = self.db.execute(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN f.decision='accepted' THEN 1 ELSE 0 END)
                          AS accepted,
                      SUM(CASE WHEN f.decision='dismissed' THEN 1 ELSE 0 END)
                          AS dismissed,
                      SUM(CASE WHEN o.reusable=1 THEN 1 ELSE 0 END)
                          AS reusable
               FROM screen_companion_feedback AS f
               LEFT JOIN screen_companion_action_outcomes AS o
                 ON o.feedback_id=f.id
               WHERE f.application_sha256=? AND f.category=?""",
            (application_digest, normalized_category),
        ).fetchone()
        category_accepted = int(category_row["accepted"] or 0)
        category_dismissed = int(category_row["dismissed"] or 0)
        category_reusable = int(category_row["reusable"] or 0)
        return {
            "total": total,
            "accepted": accepted,
            "dismissed": dismissed,
            "acceptance_rate": accepted / total if total else None,
            "category_accepted": category_accepted,
            "category_dismissed": category_dismissed,
            "category_reusable": category_reusable,
            # Feedback can only remove an automatic suggestion. It never grants
            # authority or bypasses existing mode, approval, or policy gates.
            "suppress_auto": (
                category_reusable == 0
                and (
                    (dismissed >= 3 and accepted == 0)
                    or (category_dismissed >= 3 and category_accepted == 0)
                )
            ),
        }

    def screen_companion_learning_ranking(
        self, *, application_sha256: str
    ) -> dict[str, Any]:
        """Return content-free, app-scoped category ranking signals.

        Only independently verified outcomes receive a positive score. Merely
        accepting a suggestion is never enough to teach a successful pattern.
        Scores may rank or suppress suggestions but confer no new authority.
        """
        application_digest = self._screen_companion_learning_digest(
            application_sha256, "Screen Companion application digest"
        )
        rows = self.db.execute(
            """SELECT f.category,
                      SUM(CASE WHEN f.decision='dismissed' THEN 1 ELSE 0 END)
                          AS dismissed,
                      SUM(CASE WHEN o.reusable=1 THEN 1 ELSE 0 END)
                          AS reusable,
                      SUM(CASE WHEN o.reusable=0 THEN 1 ELSE 0 END)
                          AS failed
               FROM screen_companion_feedback AS f
               LEFT JOIN screen_companion_action_outcomes AS o
                 ON o.feedback_id=f.id
               WHERE f.application_sha256=?
               GROUP BY f.category""",
            (application_digest,),
        ).fetchall()
        categories: dict[str, dict[str, int]] = {}
        for row in rows:
            reusable = int(row["reusable"] or 0)
            failed = int(row["failed"] or 0)
            dismissed = int(row["dismissed"] or 0)
            categories[str(row["category"])] = {
                "reusable": reusable,
                "failed": failed,
                "dismissed": dismissed,
                "score": reusable * 4 - failed * 3 - dismissed,
            }
        preferred = [
            category for category, values in sorted(
                categories.items(),
                key=lambda item: (-item[1]["score"], item[0]),
            )
            if values["reusable"] > 0 and values["score"] > 0
        ][:3]
        avoided = [
            category for category, values in sorted(categories.items())
            if values["reusable"] == 0
            and (values["dismissed"] >= 3 or values["score"] < 0)
        ][:3]
        return {
            "preferred": preferred,
            "avoided": avoided,
            "categories": categories,
        }

    def screen_companion_learning_stats(self) -> dict[str, Any]:
        """Return bounded aggregate feedback/outcome statistics, never content."""
        overall = self.db.execute(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN decision='accepted' THEN 1 ELSE 0 END) AS accepted,
                      SUM(CASE WHEN decision='dismissed' THEN 1 ELSE 0 END) AS dismissed
               FROM screen_companion_feedback"""
        ).fetchone()
        outcome = self.db.execute(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN reusable=1 THEN 1 ELSE 0 END) AS reusable,
                      SUM(CASE WHEN reusable=0 THEN 1 ELSE 0 END) AS non_reusable
               FROM screen_companion_action_outcomes"""
        ).fetchone()
        category_rows = self.db.execute(
            """SELECT f.category, COUNT(*) AS total,
                      SUM(CASE WHEN f.decision='accepted' THEN 1 ELSE 0 END) AS accepted,
                      SUM(CASE WHEN f.decision='dismissed' THEN 1 ELSE 0 END) AS dismissed,
                      COUNT(o.feedback_id) AS verified_outcomes,
                      SUM(CASE WHEN o.reusable=1 THEN 1 ELSE 0 END) AS reusable_outcomes
               FROM screen_companion_feedback AS f
               LEFT JOIN screen_companion_action_outcomes AS o
                 ON o.feedback_id=f.id
               GROUP BY f.category ORDER BY f.category"""
        ).fetchall()
        total = int(overall["total"] or 0)
        accepted = int(overall["accepted"] or 0)
        verified_outcomes = int(outcome["total"] or 0)
        reusable_outcomes = int(outcome["reusable"] or 0)
        return {
            "feedback": total,
            "accepted": accepted,
            "dismissed": int(overall["dismissed"] or 0),
            "acceptance_rate": accepted / total if total else None,
            "verified_outcomes": verified_outcomes,
            "reusable_outcomes": reusable_outcomes,
            "non_reusable_outcomes": int(outcome["non_reusable"] or 0),
            "verified_success_rate": (
                reusable_outcomes / verified_outcomes if verified_outcomes else None
            ),
            "by_category": {
                str(row["category"]): {
                    "total": int(row["total"]),
                    "accepted": int(row["accepted"] or 0),
                    "dismissed": int(row["dismissed"] or 0),
                    "verified_outcomes": int(row["verified_outcomes"] or 0),
                    "reusable_outcomes": int(row["reusable_outcomes"] or 0),
                }
                for row in category_rows
            },
        }

    def forget_screen_companion_receipts(self) -> int:
        with self._immediate_transaction():
            outcome_count = int(self.db.execute(
                "SELECT COUNT(*) FROM screen_companion_action_outcomes"
            ).fetchone()[0])
            feedback_cursor = self.db.execute(
                "DELETE FROM screen_companion_feedback"
            )
            receipt_cursor = self.db.execute("DELETE FROM screen_companion_receipts")
            auto_cursor = self.db.execute(
                "DELETE FROM screen_companion_auto_receipts"
            )
            companion_selector = (
                "SELECT id FROM task_predictions WHERE origin IN "
                "('companion_suggestion','companion_action')"
            )
            self.db.execute(
                f"DELETE FROM lesson_provenance WHERE prediction_id IN ({companion_selector})"
            )
            self.db.execute(
                f"DELETE FROM lesson_applications WHERE prediction_id IN ({companion_selector})"
            )
            self.db.execute(
                f"DELETE FROM memory_retrievals WHERE prediction_id IN ({companion_selector})"
            )
            reflection_cursor = self.db.execute(
                f"DELETE FROM reflections WHERE prediction_id IN ({companion_selector})"
            )
            prediction_cursor = self.db.execute(
                """DELETE FROM task_predictions
                   WHERE origin IN ('companion_suggestion','companion_action')"""
            )
        return (
            outcome_count
            + int(feedback_cursor.rowcount)
            + int(receipt_cursor.rowcount)
            + int(auto_cursor.rowcount)
            + int(reflection_cursor.rowcount)
            + int(prediction_cursor.rowcount)
        )

    @staticmethod
    def _claim_identity(subject: str, predicate: str) -> str:
        canonical = json.dumps(
            {
                "subject": " ".join(subject.casefold().split()),
                "predicate": " ".join(predicate.casefold().split()),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(("jarvis-claim-v1\0" + canonical).encode("utf-8")).hexdigest()

    @staticmethod
    def _claim_clock_predicate(predicate: str) -> str:
        return " ".join(str(predicate).casefold().split())

    def _refit_claim_volatility_locked(self, predicate: str, stamp: str) -> None:
        normalized = self._claim_clock_predicate(predicate)
        rows = self.db.execute(
            """SELECT claim_key, observed_at, value_sha256, source_key, confidence
               FROM (
                   SELECT id, claim_key, observed_at, value_sha256,
                          source_key, confidence
                   FROM memory_claim_observations
                   WHERE predicate=? ORDER BY id DESC LIMIT 4000
               )
               ORDER BY claim_key, observed_at, id""",
            (normalized,),
        ).fetchall()
        previous: dict[str, sqlite3.Row] = {}
        pairs: list[tuple[float, bool, float, float]] = []
        values: set[str] = set()
        for row in rows:
            claim_key = str(row["claim_key"])
            values.add(str(row["value_sha256"]))
            prior = previous.get(claim_key)
            if prior is not None and str(prior["source_key"]) != str(row["source_key"]):
                delta = claim_age_days(
                    str(prior["observed_at"]), str(row["observed_at"])
                )
                if delta > 0:
                    pairs.append(
                        (
                            delta,
                            str(prior["value_sha256"]) == str(row["value_sha256"]),
                            float(prior["confidence"]),
                            float(row["confidence"]),
                        )
                    )
            previous[claim_key] = row
        vocabulary_size = max(2, len(values))
        hazard, pair_count = estimate_claim_hazard(
            pairs[-900:], vocabulary_size=vocabulary_size
        )
        self.db.execute(
            """INSERT INTO memory_claim_volatility(
                   predicate, hazard_per_day, pair_count, vocabulary_size, fitted_at
               ) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(predicate) DO UPDATE SET
                   hazard_per_day=excluded.hazard_per_day,
                   pair_count=excluded.pair_count,
                   vocabulary_size=excluded.vocabulary_size,
                   fitted_at=excluded.fitted_at""",
            (normalized, hazard, pair_count, vocabulary_size, stamp),
        )

    def _record_claim_observation_locked(
        self,
        claim_id: int,
        *,
        claim_key: str,
        predicate: str,
        value_sha256: str,
        source_identity: str | None,
        authority: str,
        confidence: float,
        stamp: str,
    ) -> None:
        if not self._claim_clock_ready:
            return
        normalized = self._claim_clock_predicate(predicate)
        self.db.execute(
            """INSERT INTO memory_claim_observations(
                   claim_id, claim_key, predicate, observed_at, value_sha256,
                   source_key, authority, confidence
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                claim_id, claim_key, normalized, stamp, value_sha256,
                claim_source_key(authority, source_identity), authority, confidence,
            ),
        )
        observation_count = int(
            self.db.execute(
                "SELECT COUNT(*) FROM memory_claim_observations WHERE predicate=?",
                (normalized,),
            ).fetchone()[0]
        )
        if (
            observation_count <= MIN_HAZARD_PAIRS + 2
            or observation_count % 8 == 0
        ):
            self._refit_claim_volatility_locked(normalized, stamp)

    def _set_claim_status_locked(
        self,
        claim_id: int,
        status: str,
        *,
        stamp: str,
        reason: str,
        related_claim_id: int | None = None,
    ) -> None:
        row = self.db.execute(
            "SELECT status FROM memory_claims WHERE id=?", (claim_id,)
        ).fetchone()
        if row is None or str(row["status"]) == status:
            return
        valid_until = stamp if status == "superseded" else None
        self.db.execute(
            """UPDATE memory_claims
               SET status=?, valid_until=?, updated_at=? WHERE id=?""",
            (status, valid_until, stamp, claim_id),
        )
        self.db.execute(
            """INSERT INTO memory_claim_events(
                   claim_id, created_at, status, reason, related_claim_id
               ) VALUES (?, ?, ?, ?, ?)""",
            (claim_id, stamp, status, reason[:200], related_claim_id),
        )

    def _remember_claim_locked(
        self,
        subject: str,
        predicate: str,
        value: str,
        *,
        source: str,
        authority: str,
        confidence: float,
        stamp: str,
        source_identity: str | None = None,
    ) -> int:
        claim_key = self._claim_identity(subject, predicate)
        latest_event = self.db.execute(
            """SELECT MAX(e.created_at)
               FROM memory_claim_events AS e
               JOIN memory_claims AS c ON c.id=e.claim_id
               WHERE c.claim_key=?""",
            (claim_key,),
        ).fetchone()[0]
        if latest_event:
            requested_at = _as_utc(
                datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
            )
            latest_at = _as_utc(
                datetime.fromisoformat(str(latest_event).replace("Z", "+00:00"))
            )
            if requested_at <= latest_at:
                stamp = (latest_at + timedelta(microseconds=1)).isoformat()
        normalized_value = " ".join(value.casefold().split())
        value_sha256 = hashlib.sha256(normalized_value.encode("utf-8")).hexdigest()
        evidence_sha256 = hashlib.sha256(
            json.dumps(
                {
                    "authority": authority,
                    "confidence": round(confidence, 6),
                    "source": source,
                    "value": value_sha256,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        all_claims = self.db.execute(
            """SELECT id, memory_id, value_sha256, authority, confidence, status
               FROM memory_claims
               WHERE claim_key=?
               ORDER BY id""",
            (claim_key,),
        ).fetchall()
        live = [
            row for row in all_claims
            if str(row["status"]) in {"active", "disputed"}
        ]
        matching = [
            row for row in all_claims if row["value_sha256"] == value_sha256
        ]
        new_weight = _CLAIM_AUTHORITY_WEIGHT[authority]
        strongest_weight = max(
            (_CLAIM_AUTHORITY_WEIGHT[str(row["authority"])] for row in live),
            default=-1,
        )

        if matching:
            selected = max(
                matching,
                key=lambda row: (
                    _CLAIM_AUTHORITY_WEIGHT[str(row["authority"])], int(row["id"])
                ),
            )
            claim_id = int(selected["id"])
            existing_weight = _CLAIM_AUTHORITY_WEIGHT[str(selected["authority"])]
            evidence_exists = self.db.execute(
                """SELECT 1 FROM memory_claim_evidence
                   WHERE claim_id=? AND evidence_sha256=?""",
                (claim_id, evidence_sha256),
            ).fetchone() is not None
            combined_confidence = float(selected["confidence"])
            if not evidence_exists:
                combined_confidence = min(
                    0.999,
                    1.0
                    - (1.0 - combined_confidence) * (1.0 - confidence * 0.5),
                )
            promoted_authority = authority if new_weight > existing_weight else str(selected["authority"])
            self.db.execute(
                """UPDATE memory_claims
                   SET updated_at=?, confidence=?, authority=?, source=? WHERE id=?""",
                (stamp, combined_confidence, promoted_authority, source, claim_id),
            )
            self.db.execute(
                "UPDATE memories SET source=? WHERE id=?",
                (f"{promoted_authority}:{source}"[:2_000], int(selected["memory_id"])),
            )
            if new_weight > strongest_weight or (
                authority == "operator" and new_weight >= strongest_weight
            ):
                self._set_claim_status_locked(
                    claim_id, "active", stamp=stamp,
                    reason="matching claim promoted by stronger evidence",
                )
                for row in live:
                    other_id = int(row["id"])
                    if other_id != claim_id:
                        self._set_claim_status_locked(
                            other_id, "superseded", stamp=stamp,
                            reason="superseded by stronger matching claim",
                            related_claim_id=claim_id,
                        )
        else:
            content = _bounded_persisted_text(
                f"{subject} {predicate}: {value}", 8_000, "temporal claim"
            )
            self.db.execute(
                """INSERT OR IGNORE INTO memories(created_at, kind, content, source)
                   VALUES (?, 'claim', ?, ?)""",
                (stamp, content, f"{authority}:{source}"[:2_000]),
            )
            memory_row = self.db.execute(
                "SELECT id FROM memories WHERE kind='claim' AND content=?", (content,)
            ).fetchone()
            if memory_row is None:
                raise RuntimeError("Temporal claim memory could not be persisted")
            if not live or new_weight > strongest_weight or (
                authority == "operator" and new_weight == strongest_weight
            ):
                status = "active"
            else:
                status = "disputed"
            supersedes_id = None
            if status == "active" and live:
                strongest = max(
                    live,
                    key=lambda row: (
                        _CLAIM_AUTHORITY_WEIGHT[str(row["authority"])], int(row["id"])
                    ),
                )
                supersedes_id = int(strongest["id"])
            cursor = self.db.execute(
                """INSERT INTO memory_claims(
                       memory_id, created_at, updated_at, claim_key, subject,
                       predicate, value, value_sha256, source, authority,
                       confidence, status, valid_from, valid_until, supersedes_id
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)""",
                (
                    int(memory_row["id"]), stamp, stamp, claim_key, subject,
                    predicate, value, value_sha256, source, authority,
                    confidence, status, stamp, supersedes_id,
                ),
            )
            claim_id = int(cursor.lastrowid)
            self.db.execute(
                """INSERT INTO memory_claim_events(
                       claim_id, created_at, status, reason, related_claim_id
                   ) VALUES (?, ?, ?, ?, ?)""",
                (
                    claim_id, stamp, status,
                    "new strongest claim" if status == "active" else "conflicts with stronger claim",
                    supersedes_id,
                ),
            )
            if status == "active":
                for row in live:
                    self._set_claim_status_locked(
                        int(row["id"]), "superseded", stamp=stamp,
                        reason="replaced by newer authoritative claim",
                        related_claim_id=claim_id,
                    )
            elif new_weight == strongest_weight:
                for row in live:
                    if _CLAIM_AUTHORITY_WEIGHT[str(row["authority"])] == new_weight:
                        self._set_claim_status_locked(
                            int(row["id"]), "disputed", stamp=stamp,
                            reason="equal-authority values conflict",
                            related_claim_id=claim_id,
                        )

        self.db.execute(
            """INSERT OR IGNORE INTO memory_claim_evidence(
                   claim_id, created_at, source, authority, confidence, evidence_sha256
               ) VALUES (?, ?, ?, ?, ?, ?)""",
            (claim_id, stamp, source, authority, confidence, evidence_sha256),
        )
        self._record_claim_observation_locked(
            claim_id,
            claim_key=claim_key,
            predicate=predicate,
            value_sha256=value_sha256,
            source_identity=source_identity,
            authority=authority,
            confidence=confidence,
            stamp=stamp,
        )
        return claim_id

    def remember_claim(
        self,
        subject: str,
        predicate: str,
        value: str,
        *,
        source: str,
        authority: str,
        confidence: float = 1.0,
        source_identity: str | None = None,
    ) -> int:
        """Record a versioned fact; authority is a runtime-controlled enum."""
        subject = _validated_nonsecret_metadata(subject, "Claim subject")
        predicate = _validated_nonsecret_metadata(predicate, "Claim predicate")
        value = redact_secrets(str(value).strip())
        source = _validated_nonsecret_metadata(source, "Claim source")
        authority = str(authority).strip().casefold()
        if authority not in CLAIM_AUTHORITIES:
            raise ValueError("Unknown claim authority")
        if not subject or len(subject) > 500:
            raise ValueError("Claim subject must contain 1-500 characters")
        if not predicate or len(predicate) > 200:
            raise ValueError("Claim predicate must contain 1-200 characters")
        if not value or len(value) > 4_000:
            raise ValueError("Claim value must contain 1-4,000 characters")
        if not source or len(source) > 500:
            raise ValueError("Claim source must contain 1-500 characters")
        if source_identity is not None:
            source_identity = _validated_nonsecret_metadata(
                source_identity, "Claim source identity"
            )
            if not source_identity or len(source_identity) > 500:
                raise ValueError("Claim source identity must contain 1-500 characters")
        confidence = float(confidence)
        if not math.isfinite(confidence):
            raise ValueError("Claim confidence must be finite")
        confidence = max(0.0, min(confidence, 1.0))
        with self._immediate_transaction():
            return self._remember_claim_locked(
                subject, predicate, value, source=source, authority=authority,
                confidence=confidence, stamp=now_iso(),
                source_identity=source_identity,
            )

    def current_claims(
        self,
        query: str = "",
        limit: int = 8,
        *,
        clock_mode: str = "disabled",
        stale_threshold: float = 0.70,
        as_of: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return current claims with optional shadow/enforced confidence aging."""
        self._ensure_open()
        if contains_secret(str(query)):
            raise ValueError("Potential secret detected; claim search refused")
        clock_mode = str(clock_mode).strip().casefold()
        if clock_mode not in {"disabled", "shadow", "enforce"}:
            raise ValueError("Claim clock mode must be disabled, shadow, or enforce")
        stale_threshold = float(stale_threshold)
        if not math.isfinite(stale_threshold) or not 0.5 <= stale_threshold <= 0.99:
            raise ValueError("Claim stale threshold must be between 0.5 and 0.99")
        limit = _bounded_limit(limit, 50)
        if not limit:
            return []
        query_terms = _memory_query_terms(str(query))
        terms = sorted(query_terms, key=lambda item: (-len(item), item))[:16]
        relevance_sql = ""
        parameters: list[Any] = []
        if terms:
            relevance_sql = " AND (" + " OR ".join(
                "instr(lower(subject || ' ' || predicate || ' ' || value), ?) > 0"
                for _term in terms
            ) + ")"
            parameters.extend(terms)
        parameters.append(max(200, limit * 20))
        rows = self.db.execute(
            f"""SELECT id AS claim_id, memory_id, created_at, updated_at,
                       subject, predicate, value, source, authority, confidence, status
                FROM memory_claims
                WHERE status IN ('active', 'disputed'){relevance_sql}
                ORDER BY CASE authority
                             WHEN 'operator' THEN 4 WHEN 'verified' THEN 3
                             WHEN 'learned' THEN 2 ELSE 1 END DESC,
                         updated_at DESC, id DESC
                LIMIT ?""",
            parameters,
        ).fetchall()
        items = [dict(row) for row in rows]
        if query_terms:
            def relevance(item: dict[str, Any]) -> tuple[int, int, str, int]:
                tokens = set(_memory_tokens(
                    f"{item['subject']} {item['predicate']} {item['value']}",
                    meaningful_only=True,
                ))
                return (
                    len(tokens.intersection(query_terms)),
                    _CLAIM_AUTHORITY_WEIGHT[str(item["authority"])],
                    str(item["updated_at"]),
                    int(item["claim_id"]),
                )
            items.sort(key=relevance, reverse=True)
        items = items[:limit]
        if clock_mode == "disabled":
            return items
        for item in items:
            normalized = self._claim_clock_predicate(str(item["predicate"]))
            fit = self.db.execute(
                """SELECT hazard_per_day, pair_count, vocabulary_size, fitted_at
                   FROM memory_claim_volatility WHERE predicate=?""",
                (normalized,),
            ).fetchone()
            support = self.db.execute(
                """SELECT MAX(observed_at) AS supported_at
                   FROM memory_claim_observations WHERE claim_id=?""",
                (int(item["claim_id"]),),
            ).fetchone()
            hazard = (
                float(fit["hazard_per_day"])
                if fit is not None else DEFAULT_HAZARD_PER_DAY
            )
            pair_count = int(fit["pair_count"]) if fit is not None else 0
            vocabulary_size = int(fit["vocabulary_size"]) if fit is not None else 2
            supported_at = str(
                support["supported_at"]
                if support is not None and support["supported_at"]
                else item["updated_at"]
            )
            elapsed = claim_age_days(supported_at, as_of)
            immutable = protected_predicate(normalized)
            stored_confidence = float(item["confidence"])
            effective = claim_effective_confidence(
                stored_confidence,
                hazard_per_day=hazard,
                elapsed_days=elapsed,
                vocabulary_size=vocabulary_size,
                immutable=immutable,
            )
            if immutable:
                clock_status = "protected"
            elif pair_count < 6:
                clock_status = "cold_start"
            elif effective < stale_threshold:
                clock_status = "stale"
            else:
                clock_status = "fresh"
            item.update(
                {
                    "stored_confidence": stored_confidence,
                    "effective_confidence": effective,
                    "hazard_per_day": hazard,
                    "clock_pair_count": pair_count,
                    "clock_status": clock_status,
                    "supported_at": supported_at,
                    "age_days": elapsed,
                }
            )
            if clock_mode == "enforce":
                item["confidence"] = effective
                if str(item["status"]) == "active" and effective < stale_threshold:
                    item["stored_status"] = "active"
                    item["status"] = "stale"
            stale_read = int(not immutable and effective < stale_threshold)
            self.db.execute(
                """INSERT INTO memory_claim_clock_statistics(
                       claim_id, reads, stale_reads, last_effective_confidence,
                       last_clock_status, last_read_at
                   ) VALUES (?, 1, ?, ?, ?, ?)
                   ON CONFLICT(claim_id) DO UPDATE SET
                       reads=reads+1,
                       stale_reads=stale_reads+excluded.stale_reads,
                       last_effective_confidence=excluded.last_effective_confidence,
                       last_clock_status=excluded.last_clock_status,
                       last_read_at=excluded.last_read_at""",
                (
                    int(item["claim_id"]), stale_read, effective, clock_status,
                    str(as_of or now_iso()),
                ),
            )
        return items

    def claim_history(
        self,
        subject: str,
        predicate: str,
        *,
        as_of: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return every version, or the effective active/disputed set at a past time."""
        subject = _validated_nonsecret_metadata(subject, "Claim subject")
        predicate = _validated_nonsecret_metadata(predicate, "Claim predicate")
        claim_key = self._claim_identity(subject, predicate)
        if as_of is None:
            rows = self.db.execute(
                """SELECT id AS claim_id, memory_id, created_at, updated_at,
                          subject, predicate, value, source, authority, confidence,
                          status, valid_from, valid_until, supersedes_id
                   FROM memory_claims WHERE claim_key=? ORDER BY id""",
                (claim_key,),
            ).fetchall()
            return [dict(row) for row in rows]
        try:
            parsed = datetime.fromisoformat(str(as_of).replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("as_of must be an ISO-8601 timestamp") from None
        stamp = _as_utc(parsed).isoformat()
        rows = self.db.execute(
            """SELECT c.id AS claim_id, c.memory_id, c.created_at, c.updated_at,
                      c.subject, c.predicate, c.value, c.source, c.authority,
                      c.confidence,
                      (SELECT e.status FROM memory_claim_events AS e
                       WHERE e.claim_id=c.id AND e.created_at<=?
                       ORDER BY e.created_at DESC, e.id DESC LIMIT 1) AS status,
                      c.valid_from, c.valid_until, c.supersedes_id
               FROM memory_claims AS c
               WHERE c.claim_key=? AND c.created_at<=?
                 AND EXISTS (
                     SELECT 1 FROM memory_claim_events AS e
                     WHERE e.claim_id=c.id AND e.created_at<=?
                 )
               ORDER BY c.id""",
            (stamp, claim_key, stamp, stamp),
        ).fetchall()
        return [
            dict(row) for row in rows
            if str(row["status"]) in {"active", "disputed"}
        ]

    @staticmethod
    def _embedding_vector(value: Any, *, dimensions: int | None = None) -> list[float]:
        if not isinstance(value, list) or not value or len(value) > 4096:
            raise ValueError("Memory embedding must be a non-empty bounded vector")
        if dimensions is not None and len(value) != dimensions:
            raise ValueError("Memory embedding dimensions do not match")
        vector: list[float] = []
        for component in value:
            if isinstance(component, bool):
                raise ValueError("Memory embedding contains a non-number")
            try:
                number = float(component)
            except (TypeError, ValueError):
                raise ValueError("Memory embedding contains a non-number") from None
            if not math.isfinite(number):
                raise ValueError("Memory embedding contains a non-finite number")
            vector.append(number)
        if not any(vector):
            raise ValueError("Memory embedding must not be the zero vector")
        return vector

    @classmethod
    def _embedding_blob(cls, value: Any) -> tuple[list[float], bytes, float]:
        vector = cls._embedding_vector(value)
        try:
            blob = struct.pack(f"<{len(vector)}f", *vector)
            stored = list(struct.unpack(f"<{len(vector)}f", blob))
        except (OverflowError, struct.error):
            raise ValueError("Memory embedding cannot be represented as float32") from None
        norm = math.sqrt(sum(component * component for component in stored))
        if not math.isfinite(norm) or norm <= 0:
            raise ValueError("Memory embedding has an invalid float32 norm")
        return stored, blob, norm

    @classmethod
    def _embedding_from_storage(
        cls,
        blob: Any,
        legacy_json: Any,
        dimensions: int,
    ) -> tuple[list[float], float]:
        raw = bytes(blob) if blob is not None else b""
        if len(raw) == dimensions * 4:
            try:
                vector = list(struct.unpack(f"<{dimensions}f", raw))
            except struct.error:
                vector = []
            if vector and all(math.isfinite(value) for value in vector):
                norm = math.sqrt(sum(value * value for value in vector))
                if norm > 0:
                    return vector, norm
        try:
            vector = cls._embedding_vector(
                json.loads(str(legacy_json)), dimensions=dimensions
            )
        except (ValueError, json.JSONDecodeError):
            raise ValueError("Stored memory embedding is invalid") from None
        norm = math.sqrt(sum(value * value for value in vector))
        if not norm:
            raise ValueError("Stored memory embedding is zero")
        return vector, norm

    @staticmethod
    def _query_embedding_sha256(query: str) -> str:
        normalized = str(query).strip()
        return hashlib.sha256(
            ("jarvis-query-embedding-v1\0" + normalized).encode("utf-8")
        ).hexdigest()

    def cached_query_embedding(
        self,
        query: str,
        model: str,
        *,
        dimensions: int | None = None,
    ) -> list[float] | None:
        """Return an exact cached query vector without storing the raw query."""
        normalized = str(query).strip()
        if not normalized or len(normalized) > MAX_SEARCH_QUERY_CHARS:
            return None
        if contains_secret(normalized):
            raise ValueError("Potential secret detected; query embedding cache refused")
        safe_model = _validated_nonsecret_metadata(model, "Embedding model")[:200]
        if dimensions is not None:
            if (
                isinstance(dimensions, bool)
                or not isinstance(dimensions, int)
                or not 1 <= dimensions <= 4096
            ):
                raise ValueError("Embedding dimensions are invalid")
            row = self.db.execute(
                """SELECT dimensions, embedding_blob
                   FROM memory_query_embeddings
                   WHERE query_sha256=? AND model=? AND dimensions=?""",
                (self._query_embedding_sha256(normalized), safe_model, dimensions),
            ).fetchone()
        else:
            row = self.db.execute(
                """SELECT dimensions, embedding_blob
                   FROM memory_query_embeddings
                   WHERE query_sha256=? AND model=?
                   ORDER BY last_used_at DESC LIMIT 1""",
                (self._query_embedding_sha256(normalized), safe_model),
            ).fetchone()
        if row is None:
            return None
        try:
            vector, _norm = self._embedding_from_storage(
                row["embedding_blob"], "[]", int(row["dimensions"])
            )
        except ValueError:
            return None
        self.db.execute(
            """UPDATE memory_query_embeddings
               SET hit_count=hit_count+1, last_used_at=?
               WHERE query_sha256=? AND model=? AND dimensions=?""",
            (
                now_iso(), self._query_embedding_sha256(normalized), safe_model,
                int(row["dimensions"]),
            ),
        )
        return vector

    def cache_query_embedding(
        self,
        query: str,
        model: str,
        vector: list[float],
    ) -> None:
        """Persist a bounded semantic-query cache keyed only by a one-way digest."""
        normalized = str(query).strip()
        if not normalized or len(normalized) > MAX_SEARCH_QUERY_CHARS:
            raise ValueError("Query embedding cache input is empty or too long")
        if contains_secret(normalized):
            raise ValueError("Potential secret detected; query embedding cache refused")
        safe_model = _validated_nonsecret_metadata(model, "Embedding model")[:200]
        stored, blob, norm = self._embedding_blob(vector)
        digest = self._query_embedding_sha256(normalized)
        stamp = now_iso()
        with self._immediate_transaction():
            self.db.execute(
                """INSERT INTO memory_query_embeddings(
                       query_sha256, model, dimensions, embedding_blob,
                       vector_norm, created_at, last_used_at, hit_count
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                   ON CONFLICT(query_sha256, model, dimensions) DO UPDATE SET
                       embedding_blob=excluded.embedding_blob,
                       vector_norm=excluded.vector_norm,
                       last_used_at=excluded.last_used_at""",
                (
                    digest, safe_model, len(stored), blob, norm, stamp, stamp,
                ),
            )
            self.db.execute(
                """DELETE FROM memory_query_embeddings
                   WHERE rowid IN (
                       SELECT rowid FROM memory_query_embeddings
                       ORDER BY last_used_at DESC, rowid DESC
                       LIMIT -1 OFFSET ?
                   )""",
                (MAX_QUERY_EMBEDDING_CACHE,),
            )

    def pending_memory_embeddings(
        self,
        model: str,
        *,
        limit: int = 32,
    ) -> list[dict[str, Any]]:
        """Return a bounded batch whose raw text is already redacted at persistence."""
        safe_model = _validated_nonsecret_metadata(model, "Embedding model")[:200]
        limit = _bounded_limit(limit, 64)
        if not limit:
            return []
        rows = self.db.execute(
            """SELECT m.id, m.kind, m.content, e.content_sha256, e.embedding_blob
               FROM memories AS m
               LEFT JOIN memory_embeddings AS e
                 ON e.memory_id=m.id AND e.model=?
               LEFT JOIN memory_claims AS c ON c.memory_id=m.id
               WHERE m.kind<>'lesson'
                 AND (m.kind<>'claim' OR c.status IN ('active', 'disputed'))
                 AND (m.kind='claim' OR EXISTS (
                     SELECT 1 FROM ordinary_memory_provenance AS omp
                     WHERE omp.memory_id=m.id AND omp.eligible=1
                 ))
               ORDER BY CASE
                            WHEN e.memory_id IS NULL OR e.embedding_blob IS NULL THEN 0
                            ELSE 1
                        END, m.id
               LIMIT ?""",
            (safe_model, min(MAX_MEMORY_SEARCH_CANDIDATES, max(limit * 8, 64))),
        ).fetchall()
        pending: list[dict[str, Any]] = []
        for row in rows:
            if (
                str(row["kind"]) != "claim"
                and not self._ordinary_memory_recall_eligible(int(row["id"]))
            ):
                continue
            content = str(row["content"])
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            if row["content_sha256"] == digest and row["embedding_blob"] is not None:
                continue
            pending.append({
                "memory_id": int(row["id"]),
                "content": content,
                "content_sha256": digest,
            })
            if len(pending) >= limit:
                break
        return pending

    def claim_pending_memory_embeddings(
        self,
        model: str,
        lease_owner: str,
        *,
        limit: int = 32,
        lease_seconds: int = 300,
    ) -> list[dict[str, Any]]:
        """Atomically lease unindexed records so agents never duplicate API work."""
        safe_model = _validated_nonsecret_metadata(model, "Embedding model")[:200]
        owner = _validated_worker_id(lease_owner)
        limit = _bounded_limit(limit, 64)
        duration = max(30, min(int(lease_seconds), 3_600))
        if not limit:
            return []
        current = _as_utc()
        expires = (current + timedelta(seconds=duration)).isoformat()
        selected: list[dict[str, Any]] = []
        with self._immediate_transaction():
            rows = self.db.execute(
                """SELECT m.id, m.kind, m.content, e.content_sha256 AS embedded_sha256,
                          e.embedding_blob,
                          l.content_sha256 AS leased_sha256,
                          l.lease_owner, l.lease_expires_at
                   FROM memories AS m
                   LEFT JOIN memory_embeddings AS e
                     ON e.memory_id=m.id AND e.model=?
                   LEFT JOIN memory_embedding_leases AS l
                     ON l.memory_id=m.id AND l.model=?
                   LEFT JOIN memory_claims AS c ON c.memory_id=m.id
                   WHERE m.kind<>'lesson'
                     AND (m.kind<>'claim' OR c.status IN ('active', 'disputed'))
                     AND (m.kind='claim' OR EXISTS (
                         SELECT 1 FROM ordinary_memory_provenance AS omp
                         WHERE omp.memory_id=m.id AND omp.eligible=1
                     ))
                   ORDER BY CASE
                                WHEN e.memory_id IS NULL OR e.embedding_blob IS NULL THEN 0
                                ELSE 1
                            END, m.id
                   LIMIT ?""",
                (
                    safe_model,
                    safe_model,
                    min(MAX_MEMORY_SEARCH_CANDIDATES, max(limit * 16, 128)),
                ),
            ).fetchall()
            for row in rows:
                if (
                    str(row["kind"]) != "claim"
                    and not self._ordinary_memory_recall_eligible(int(row["id"]))
                ):
                    continue
                content = str(row["content"])
                digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
                if (
                    str(row["embedded_sha256"] or "") == digest
                    and row["embedding_blob"] is not None
                ):
                    continue
                lease_live = (
                    str(row["leased_sha256"] or "") == digest
                    and str(row["lease_expires_at"] or "") > current.isoformat()
                )
                if lease_live:
                    continue
                self.db.execute(
                    """INSERT INTO memory_embedding_leases(
                           memory_id, model, content_sha256, lease_owner,
                           lease_expires_at, attempt_count, last_error, updated_at
                       ) VALUES (?, ?, ?, ?, ?, 1, NULL, ?)
                       ON CONFLICT(memory_id, model) DO UPDATE SET
                           content_sha256=excluded.content_sha256,
                           lease_owner=excluded.lease_owner,
                           lease_expires_at=excluded.lease_expires_at,
                           attempt_count=CASE
                               WHEN memory_embedding_leases.content_sha256=excluded.content_sha256
                               THEN memory_embedding_leases.attempt_count+1 ELSE 1 END,
                           last_error=NULL, updated_at=excluded.updated_at""",
                    (
                        int(row["id"]), safe_model, digest, owner,
                        expires, current.isoformat(),
                    ),
                )
                selected.append({
                    "memory_id": int(row["id"]),
                    "content": content,
                    "content_sha256": digest,
                })
                if len(selected) >= limit:
                    break
        return selected

    def store_memory_embeddings(
        self,
        model: str,
        records: list[dict[str, Any]],
        vectors: list[list[float]],
        *,
        lease_owner: str | None = None,
    ) -> int:
        """Cache neural indexes only when they still match immutable persisted text."""
        safe_model = _validated_nonsecret_metadata(model, "Embedding model")[:200]
        owner = None if lease_owner is None else _validated_worker_id(lease_owner)
        if len(records) != len(vectors) or len(records) > 64:
            raise ValueError("Embedding records and vectors must be equally bounded")
        stored = 0
        stamp = now_iso()
        with self._immediate_transaction():
            for record, raw_vector in zip(records, vectors, strict=True):
                memory_id = self._prediction_optional_id(
                    record.get("memory_id"), "memory_id"
                )
                row = self.db.execute(
                    "SELECT content, kind FROM memories WHERE id=?", (memory_id,)
                ).fetchone()
                if row is None or str(row["kind"]) == "lesson":
                    continue
                if (
                    str(row["kind"]) != "claim"
                    and not self._ordinary_memory_recall_eligible(memory_id)
                ):
                    continue
                content = str(row["content"])
                digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
                if digest != str(record.get("content_sha256") or ""):
                    continue
                if owner is not None:
                    lease = self.db.execute(
                        """SELECT 1 FROM memory_embedding_leases
                           WHERE memory_id=? AND model=? AND content_sha256=?
                             AND lease_owner=? AND lease_expires_at>?""",
                        (memory_id, safe_model, digest, owner, stamp),
                    ).fetchone()
                    if lease is None:
                        continue
                vector, blob, norm = self._embedding_blob(raw_vector)
                self.db.execute(
                    """INSERT INTO memory_embeddings(
                           memory_id, model, dimensions, content_sha256,
                           embedding_json, embedding_blob, vector_norm,
                           created_at, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(memory_id, model) DO UPDATE SET
                           dimensions=excluded.dimensions,
                           content_sha256=excluded.content_sha256,
                           embedding_json=excluded.embedding_json,
                           embedding_blob=excluded.embedding_blob,
                           vector_norm=excluded.vector_norm,
                           updated_at=excluded.updated_at""",
                    (
                        memory_id, safe_model, len(vector), digest,
                        "[]", blob, norm, stamp, stamp,
                    ),
                )
                self.db.execute(
                    """DELETE FROM memory_embedding_leases
                       WHERE memory_id=? AND model=? AND content_sha256=?
                         AND ? IS NOT NULL AND lease_owner=?""",
                    (memory_id, safe_model, digest, owner, owner),
                )
                stored += 1
        return stored

    def fail_memory_embedding_batch(
        self,
        model: str,
        records: list[dict[str, Any]],
        lease_owner: str,
        error: Any,
        *,
        retry_seconds: int = 60,
    ) -> int:
        safe_model = _validated_nonsecret_metadata(model, "Embedding model")[:200]
        owner = _validated_worker_id(lease_owner)
        current = _as_utc()
        retry_at = (
            current + timedelta(seconds=max(15, min(int(retry_seconds), 3_600)))
        ).isoformat()
        safe_error = redact_secrets(str(error))[:1_000]
        changed = 0
        with self._immediate_transaction():
            for record in records[:64]:
                memory_id = self._prediction_optional_id(
                    record.get("memory_id"), "memory_id"
                )
                updated = self.db.execute(
                    """UPDATE memory_embedding_leases
                       SET lease_owner=NULL, lease_expires_at=?, last_error=?, updated_at=?
                       WHERE memory_id=? AND model=? AND content_sha256=?
                         AND lease_owner=?""",
                    (
                        retry_at, safe_error, current.isoformat(), memory_id,
                        safe_model, str(record.get("content_sha256") or ""), owner,
                    ),
                )
                changed += int(updated.rowcount)
        return changed

    @staticmethod
    def _learned_memory_utility(row: dict[str, Any]) -> float:
        resolved = max(0, int(row.get("utility_resolved") or 0))
        observed = float(row.get("utility") or 0.5)
        confidence = min(1.0, resolved / 10.0)
        return 0.5 + (observed - 0.5) * confidence

    def semantic_memory_search(
        self,
        query_vector: list[float],
        model: str,
        *,
        limit: int = 12,
    ) -> list[dict[str, Any]]:
        safe_model = _validated_nonsecret_metadata(model, "Embedding model")[:200]
        query = self._embedding_vector(query_vector)
        limit = _bounded_limit(limit, 100)
        if not limit:
            return []
        query_norm = math.sqrt(sum(value * value for value in query))
        rows = self.db.execute(
            """SELECT m.id, m.created_at, m.kind, m.content, m.source,
                      c.status AS claim_status, c.authority AS claim_authority,
                      e.dimensions, e.embedding_json, e.embedding_blob, e.vector_norm,
                      COALESCE(s.resolved, 0) AS utility_resolved,
                      COALESCE(s.utility, 0.5) AS utility
               FROM memory_embeddings AS e
               JOIN memories AS m ON m.id=e.memory_id
               LEFT JOIN memory_claims AS c ON c.memory_id=m.id
               LEFT JOIN memory_statistics AS s ON s.memory_id=m.id
               WHERE e.model=? AND e.dimensions=?
                 AND m.kind<>'lesson'
                 AND (m.kind<>'claim' OR c.status IN ('active', 'disputed'))
                 AND (m.kind='claim' OR EXISTS (
                     SELECT 1 FROM ordinary_memory_provenance AS omp
                     WHERE omp.memory_id=m.id AND omp.eligible=1
                 ))
               ORDER BY m.id DESC LIMIT ?""",
            (safe_model, len(query), MAX_MEMORY_SEARCH_CANDIDATES),
        ).fetchall()
        scored: list[tuple[float, int, dict[str, Any]]] = []
        for raw in rows:
            row = dict(raw)
            if (
                str(row["kind"]) != "claim"
                and not self._ordinary_memory_recall_eligible(int(row["id"]))
            ):
                continue
            try:
                vector, stored_norm = self._embedding_from_storage(
                    row.pop("embedding_blob", None),
                    row.pop("embedding_json", "[]"),
                    len(query),
                )
            except ValueError:
                continue
            row.pop("vector_norm", None)
            norm = stored_norm
            if not norm:
                continue
            dot_product = sum(a * b for a, b in zip(query, vector, strict=True))
            # OpenAI's v3 embeddings are L2-normalized. Preserve correctness for
            # custom/test embedders while avoiding a division on the common path.
            if abs(query_norm - 1.0) <= 1e-3 and abs(norm - 1.0) <= 1e-3:
                similarity = dot_product
            else:
                similarity = dot_product / (query_norm * norm)
            if similarity <= 0:
                continue
            utility = self._learned_memory_utility(row)
            adjusted = similarity * (0.9 + 0.2 * utility)
            memory_id = int(row.pop("id"))
            row.pop("dimensions", None)
            row.pop("utility_resolved", None)
            row.pop("utility", None)
            row["memory_id"] = memory_id
            row["semantic_score"] = similarity
            scored.append((adjusted, memory_id, row))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [row for _, _, row in scored[:limit]]

    def hybrid_memory_search(
        self,
        query: str,
        query_vector: list[float],
        model: str,
        *,
        limit: int = 12,
    ) -> list[dict[str, Any]]:
        """Fuse sparse and neural recall; learned utility only adjusts close matches."""
        query = str(query)
        if len(query) > MAX_SEARCH_QUERY_CHARS:
            raise ValueError(f"Memory search query exceeds {MAX_SEARCH_QUERY_CHARS} characters")
        limit = _bounded_limit(limit, 100)
        if not limit:
            return []
        query_terms = _memory_query_terms(query)
        like_terms = _memory_like_terms(query, query_terms)
        lexical: list[dict[str, Any]] = []
        if query_terms and like_terms:
            candidate_limit = min(MAX_MEMORY_SEARCH_CANDIDATES, max(64, limit * 32))
            fts_query = _memory_fts_query(query, query_terms)
            if fts_query is not None:
                rows = self.db.execute(
                    """SELECT m.id, m.created_at, m.kind, m.content, m.source,
                              c.status AS claim_status, c.authority AS claim_authority
                       FROM memory_fts
                       JOIN memories AS m ON m.id=memory_fts.rowid
                       LEFT JOIN memory_claims AS c ON c.memory_id=m.id
                       WHERE memory_fts MATCH ?
                         AND m.kind<>'lesson'
                         AND (m.kind<>'claim' OR c.status IN ('active', 'disputed'))
                         AND (m.kind='claim' OR EXISTS (
                             SELECT 1 FROM ordinary_memory_provenance AS omp
                             WHERE omp.memory_id=m.id AND omp.eligible=1
                         ))
                       ORDER BY memory_fts.rank, m.id DESC LIMIT ?""",
                    (fts_query, candidate_limit),
                ).fetchall()
            else:
                patterns = [f"%{_escape_like(term)}%" for term in like_terms]
                where = " OR ".join(
                    "lower(m.content) LIKE ? ESCAPE '\\'" for _ in patterns
                )
                match_count = " + ".join(
                    "CASE WHEN lower(m.content) LIKE ? ESCAPE '\\' THEN 1 ELSE 0 END"
                    for _ in patterns
                )
                rows = self.db.execute(
                    f"""SELECT m.id, m.created_at, m.kind, m.content, m.source,
                               c.status AS claim_status, c.authority AS claim_authority
                        FROM memories AS m
                        LEFT JOIN memory_claims AS c ON c.memory_id=m.id
                        WHERE ({where})
                          AND m.kind<>'lesson'
                          AND (m.kind<>'claim' OR c.status IN ('active', 'disputed'))
                          AND (m.kind='claim' OR EXISTS (
                              SELECT 1 FROM ordinary_memory_provenance AS omp
                              WHERE omp.memory_id=m.id AND omp.eligible=1
                          ))
                        ORDER BY ({match_count}) DESC, m.id DESC LIMIT ?""",
                    [*patterns, *patterns, candidate_limit],
                ).fetchall()
            rows = self._filter_generic_recall_rows(list(rows))
            lexical = _rank_memory_rows(rows, query_terms, keep_id=True)[: max(limit * 4, 24)]
        semantic = self.semantic_memory_search(
            query_vector, model, limit=max(limit * 4, 24)
        )
        fused: dict[int, dict[str, Any]] = {}
        for channel, items in (("lexical", lexical), ("semantic", semantic)):
            for rank, item in enumerate(items, 1):
                memory_id = int(item["memory_id"])
                entry = fused.setdefault(memory_id, {
                    "item": item,
                    "score": 0.0,
                    "channels": set(),
                })
                entry["score"] += 1.0 / (60.0 + rank)
                entry["channels"].add(channel)
                if channel == "lexical":
                    entry["item"] = item
        if fused:
            memory_ids = sorted(fused)
            placeholders = ",".join("?" for _ in memory_ids)
            statistics = self.db.execute(
                f"""SELECT memory_id, resolved AS utility_resolved, utility
                    FROM memory_statistics WHERE memory_id IN ({placeholders})""",
                memory_ids,
            ).fetchall()
            for raw in statistics:
                row = dict(raw)
                memory_id = int(row["memory_id"])
                # Learned utility can reorder close results, but it is too
                # weak to overpower strong lexical/semantic relevance.
                fused[memory_id]["score"] += (
                    self._learned_memory_utility(row) - 0.5
                ) * 0.004
        ranked = sorted(
            fused.values(),
            key=lambda entry: (entry["score"], int(entry["item"]["memory_id"])),
            reverse=True,
        )
        results: list[dict[str, Any]] = []
        for entry in ranked[:limit]:
            item = dict(entry["item"])
            item.pop("semantic_score", None)
            channels = entry["channels"]
            item["retrieval_channel"] = "hybrid" if len(channels) > 1 else next(iter(channels))
            results.append(item)
        return results

    def record_memory_retrievals(
        self,
        prediction_id: int,
        family: str,
        query: str,
        memories: list[dict[str, Any]],
        *,
        conversation_id: int | None = None,
    ) -> int:
        """Persist outcome-linkable retrieval evidence without persisting the raw query."""
        normalized_prediction = self._prediction_optional_id(
            prediction_id, "prediction_id"
        )
        if family not in self.PREDICTION_FAMILIES:
            raise ValueError(f"Unknown memory retrieval family: {family}")
        normalized_conversation = self._prediction_optional_id(
            conversation_id, "conversation_id"
        )
        fingerprint = hashlib.sha256(
            ("jarvis-memory-query-v1\0" + str(query)).encode("utf-8")
        ).hexdigest()
        stamp = now_iso()
        inserted = 0
        with self._immediate_transaction():
            prediction = self.db.execute(
                "SELECT family, resolved_at FROM task_predictions WHERE id=?",
                (normalized_prediction,),
            ).fetchone()
            if (
                prediction is None
                or prediction["family"] != family
                or prediction["resolved_at"] is not None
            ):
                raise ValueError("Memory retrieval must bind to the active matching prediction")
            for rank, item in enumerate(memories[:10], 1):
                memory_id = self._prediction_optional_id(
                    item.get("memory_id"), "memory_id"
                )
                channel = str(item.get("retrieval_channel") or "lexical")
                if channel not in {"lexical", "semantic", "hybrid"}:
                    raise ValueError("Unknown memory retrieval channel")
                valid = self.db.execute(
                    "SELECT kind FROM memories WHERE id=?", (memory_id,)
                ).fetchone()
                if valid is None:
                    raise ValueError("Memory retrieval references a missing memory")
                if str(valid["kind"]) == "lesson":
                    raise ValueError(
                        "Verified lessons require the dedicated provenance retrieval path"
                    )
                if (
                    str(valid["kind"]) != "claim"
                    and not self._ordinary_memory_recall_eligible(memory_id)
                ):
                    raise ValueError(
                        "Memory retrieval references an ineligible ordinary memory"
                    )
                cursor = self.db.execute(
                    """INSERT OR IGNORE INTO memory_retrievals(
                           created_at, prediction_id, conversation_id, family,
                           query_sha256, memory_id, rank, channel
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        stamp, normalized_prediction, normalized_conversation,
                        family, fingerprint, memory_id, rank, channel,
                    ),
                )
                if cursor.rowcount == 1:
                    self.db.execute(
                        """INSERT INTO memory_statistics(
                               memory_id, retrievals, updated_at, last_retrieved_at
                           ) VALUES (?, 1, ?, ?)
                           ON CONFLICT(memory_id) DO UPDATE SET
                               retrievals=retrievals+1,
                               updated_at=excluded.updated_at,
                               last_retrieved_at=excluded.last_retrieved_at""",
                        (memory_id, stamp, stamp),
                    )
                    inserted += 1
        return inserted

    def memory_quality(self, limit: int = 20) -> dict[str, Any]:
        """Expose measured recall coverage and utility without model-authored claims."""
        limit = _bounded_limit(limit, 100)
        totals = self.db.execute(
            """SELECT COUNT(*) AS memories,
                      (SELECT COUNT(*) FROM memories AS em
                       LEFT JOIN memory_claims AS ec ON ec.memory_id=em.id
                       WHERE em.kind<>'lesson'
                         AND (em.kind<>'claim' OR ec.status IN ('active','disputed')))
                          AS embedding_eligible,
                      (SELECT COUNT(*) FROM memory_embeddings) AS embeddings,
                      (SELECT COUNT(*) FROM memory_embeddings
                       WHERE embedding_blob IS NOT NULL) AS binary_embeddings,
                      (SELECT COUNT(*) FROM memory_embedding_leases
                       WHERE lease_owner IS NOT NULL
                         AND lease_expires_at>strftime('%Y-%m-%dT%H:%M:%f+00:00','now'))
                          AS active_embedding_leases,
                      (SELECT COUNT(*) FROM memory_embedding_leases
                       WHERE last_error IS NOT NULL) AS embedding_failures,
                      (SELECT COUNT(*) FROM memory_query_embeddings)
                          AS cached_query_embeddings,
                      (SELECT COALESCE(SUM(hit_count), 0)
                       FROM memory_query_embeddings) AS query_embedding_cache_hits,
                      (SELECT COUNT(*) FROM memory_retrievals) AS retrievals,
                      (SELECT COUNT(*) FROM memory_retrievals WHERE resolved_at IS NOT NULL)
                          AS resolved_retrievals,
                      (SELECT AVG(successful) FROM memory_retrievals
                       WHERE resolved_at IS NOT NULL) AS observed_utility,
                      (SELECT COUNT(*) FROM memory_claims
                       WHERE status='active') AS active_claims,
                      (SELECT COUNT(*) FROM memory_claims
                       WHERE status='disputed') AS disputed_claims,
                      (SELECT COUNT(*) FROM memory_claims
                       WHERE status='superseded') AS superseded_claims,
                      (SELECT COUNT(*) FROM memory_claim_events) AS claim_events
                      ,(SELECT COUNT(*) FROM memory_claim_observations)
                          AS claim_observations
                      ,(SELECT COUNT(*) FROM memory_claim_volatility)
                          AS claim_clock_predicates
                      ,(SELECT COUNT(*) FROM memory_claim_volatility
                        WHERE pair_count>=6) AS claim_clock_mature_predicates
                      ,(SELECT COALESCE(SUM(reads), 0)
                        FROM memory_claim_clock_statistics) AS claim_clock_reads
                      ,(SELECT COALESCE(SUM(stale_reads), 0)
                        FROM memory_claim_clock_statistics) AS claim_clock_stale_reads
               FROM memories"""
        ).fetchone()
        top = self.db.execute(
            """SELECT s.memory_id, m.kind,
                      s.retrievals, s.resolved, s.successes, s.failures, s.utility,
                      s.last_retrieved_at, s.last_resolved_at
               FROM memory_statistics AS s
               JOIN memories AS m ON m.id=s.memory_id
               ORDER BY s.resolved DESC, s.utility DESC, s.memory_id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        lesson_ids = {
            int(row["id"])
            for row in self.db.execute(
                "SELECT id FROM memories WHERE kind='lesson' ORDER BY id"
            ).fetchall()
        }
        valid_lesson_ids: set[int] = set()
        hash_mismatch_ids: set[int] = set()
        digest_mismatch_ids: set[int] = set()
        for memory_id in lesson_ids:
            valid, content_mismatch, digest_mismatch = (
                self._lesson_provenance_validation(memory_id)
            )
            if valid:
                valid_lesson_ids.add(memory_id)
            if content_mismatch:
                hash_mismatch_ids.add(memory_id)
            if digest_mismatch:
                digest_mismatch_ids.add(memory_id)
        ordinary_ids = {
            int(row["id"])
            for row in self.db.execute(
                """SELECT id FROM memories
                   WHERE kind NOT IN ('lesson', 'claim') ORDER BY id"""
            ).fetchall()
        }
        valid_ordinary_ids: set[int] = set()
        eligible_ordinary_ids: set[int] = set()
        ordinary_hash_mismatch_ids: set[int] = set()
        ordinary_digest_mismatch_ids: set[int] = set()
        for memory_id in ordinary_ids:
            valid, eligible, content_mismatch, provenance_mismatch = (
                self._ordinary_memory_provenance_validation(memory_id)
            )
            if valid:
                valid_ordinary_ids.add(memory_id)
            if valid and eligible:
                eligible_ordinary_ids.add(memory_id)
            if content_mismatch:
                ordinary_hash_mismatch_ids.add(memory_id)
            if provenance_mismatch:
                ordinary_digest_mismatch_ids.add(memory_id)
        active_claim_count = int(totals["active_claims"] or 0) + int(
            totals["disputed_claims"] or 0
        )
        measured_totals = dict(totals)
        measured_totals.update({
            "embedding_eligible": len(eligible_ordinary_ids) + active_claim_count,
            "ordinary_memory_records": len(ordinary_ids),
            "ordinary_memory_provenance_valid": len(valid_ordinary_ids),
            "ordinary_memory_recall_eligible": len(eligible_ordinary_ids),
            "ordinary_memory_quarantined": len(ordinary_ids - eligible_ordinary_ids),
            "ordinary_memory_hash_mismatches": len(ordinary_hash_mismatch_ids),
            "ordinary_memory_digest_mismatches": len(ordinary_digest_mismatch_ids),
            "structured_lessons": len(lesson_ids),
            "lesson_records": len(lesson_ids),
            "provenance_valid_lessons": len(valid_lesson_ids),
            "provenance_quarantined_lessons": len(lesson_ids - valid_lesson_ids),
            "provenance_hash_mismatches": len(hash_mismatch_ids),
            "provenance_digest_mismatches": len(digest_mismatch_ids),
        })
        return {
            "totals": measured_totals,
            "measured_memories": [
                dict(row)
                for row in top
                if str(row["kind"]) == "claim"
                or int(row["memory_id"]) in eligible_ordinary_ids
            ],
        }

    def _prediction_family_for_context(
        self,
        task_id: int | None,
        conversation_id: int | None,
    ) -> str | None:
        clauses: list[str] = []
        parameters: list[int] = []
        if task_id is not None:
            clauses.append("task_id=?")
            parameters.append(int(task_id))
        if conversation_id is not None:
            clauses.append("conversation_id=?")
            parameters.append(int(conversation_id))
        if not clauses:
            return None
        row = self.db.execute(
            "SELECT family FROM task_predictions WHERE resolved_at IS NOT NULL "
            "AND origin NOT IN ('companion_action','companion_suggestion') AND ("
            + " OR ".join(clauses)
            + ") ORDER BY id DESC LIMIT 1",
            parameters,
        ).fetchone()
        family = str(row["family"]) if row is not None else None
        return family if family in self.PREDICTION_FAMILIES else None

    @staticmethod
    def _canonical_reflection_lesson_content(
        *,
        family: str,
        outcome_status: str,
        summary: str,
        mistakes: str,
        improvements: str,
    ) -> str | None:
        """Reconstruct the sole lesson text emitted by the legacy runtime."""
        reusable = str(improvements).strip()
        if not reusable:
            return None
        parts = [
            f"Task family: {family}.",
            f"Observed outcome: {outcome_status}.",
        ]
        if str(summary):
            parts.append(f"Observed result: {summary}")
        if str(mistakes):
            parts.append(f"Observed blocker: {mistakes}")
        parts.append(f"Reusable lesson: {reusable}")
        return "\n".join(parts)

    def _lesson_provenance_material(
        self,
        memory_id: int,
        prediction_id: int,
        reflection_id: int,
    ) -> dict[str, Any] | None:
        """Return canonical source material only for one internally exact chain."""
        row = self.db.execute(
            """SELECT
                   m.id AS memory_id, m.kind AS lesson_kind,
                   m.content AS lesson_content, m.source AS lesson_source,
                   m.family AS lesson_family,
                   m.outcome_status AS lesson_outcome_status,
                   m.reflection_id AS lesson_reflection_id,
                   r.id AS reflection_id, r.created_at AS reflection_created_at,
                   r.task_id AS reflection_task_id,
                   r.conversation_id AS reflection_conversation_id,
                   r.prediction_id AS reflection_prediction_id,
                   r.status AS reflection_status, r.summary AS reflection_summary,
                   r.mistakes AS reflection_mistakes,
                   r.improvements AS reflection_improvements,
                   r.tool_calls AS reflection_tool_calls,
                   p.id AS prediction_id, p.created_at AS prediction_created_at,
                   p.task_id AS prediction_task_id,
                   p.conversation_id AS prediction_conversation_id,
                   p.origin AS prediction_origin, p.family AS prediction_family,
                   p.profile AS prediction_profile, p.model AS prediction_model,
                   p.predicted_success, p.predicted_steps,
                   p.predicted_verification, p.basis AS prediction_basis,
                   p.resolved_at AS prediction_resolved_at,
                   p.actual_status AS prediction_actual_status,
                   p.actual_steps AS prediction_actual_steps,
                   p.evidence_ok AS prediction_evidence_ok,
                   p.failure_class AS prediction_failure_class
               FROM memories AS m
               JOIN reflections AS r ON r.id=?
               JOIN task_predictions AS p ON p.id=?
               WHERE m.id=?""",
            (int(reflection_id), int(prediction_id), int(memory_id)),
        ).fetchone()
        if row is None or str(row["lesson_kind"]) != "lesson":
            return None
        if (
            row["lesson_reflection_id"] is None
            or int(row["lesson_reflection_id"]) != int(reflection_id)
            or row["reflection_prediction_id"] is None
            or int(row["reflection_prediction_id"]) != int(prediction_id)
            or str(row["lesson_family"] or "")
            != str(row["prediction_family"] or "")
            or str(row["lesson_outcome_status"] or "")
            != str(row["reflection_status"] or "")
            or str(row["reflection_status"] or "")
            != str(row["prediction_actual_status"] or "")
            or row["prediction_resolved_at"] is None
            or str(row["prediction_resolved_at"]) > str(row["reflection_created_at"])
            or row["prediction_actual_steps"] is None
            or int(row["prediction_actual_steps"])
            != int(row["reflection_tool_calls"])
            or str(row["prediction_origin"])
            in self.SCREEN_COMPANION_PREDICTION_ORIGINS
        ):
            return None
        if row["reflection_task_id"] is not None:
            if (
                row["prediction_task_id"] is None
                or int(row["prediction_task_id"]) != int(row["reflection_task_id"])
            ):
                return None
        elif row["reflection_conversation_id"] is not None:
            if (
                row["prediction_task_id"] is not None
                or row["prediction_conversation_id"] is None
                or int(row["prediction_conversation_id"])
                != int(row["reflection_conversation_id"])
            ):
                return None
        else:
            return None
        if str(row["lesson_outcome_status"]) == "complete" and (
            str(row["predicted_verification"]) != "not_applicable"
            and int(row["prediction_evidence_ok"] or 0) != 1
        ):
            return None
        return {
            "schema": "jarvis.lesson-provenance.v1",
            "lesson": {
                "memory_id": int(row["memory_id"]),
                "content": str(row["lesson_content"]),
                "source": row["lesson_source"],
                "family": str(row["lesson_family"]),
                "outcome_status": str(row["lesson_outcome_status"]),
                "reflection_id": int(row["lesson_reflection_id"]),
            },
            "reflection": {
                "id": int(row["reflection_id"]),
                "created_at": str(row["reflection_created_at"]),
                "task_id": row["reflection_task_id"],
                "conversation_id": row["reflection_conversation_id"],
                "prediction_id": int(row["reflection_prediction_id"]),
                "status": str(row["reflection_status"]),
                "summary": str(row["reflection_summary"]),
                "mistakes": str(row["reflection_mistakes"]),
                "improvements": str(row["reflection_improvements"]),
                "tool_calls": int(row["reflection_tool_calls"]),
            },
            "prediction": {
                "id": int(row["prediction_id"]),
                "created_at": str(row["prediction_created_at"]),
                "task_id": row["prediction_task_id"],
                "conversation_id": row["prediction_conversation_id"],
                "origin": str(row["prediction_origin"]),
                "family": str(row["prediction_family"]),
                "profile": str(row["prediction_profile"]),
                "model": str(row["prediction_model"]),
                "predicted_success": float(row["predicted_success"]),
                "predicted_steps": int(row["predicted_steps"]),
                "predicted_verification": str(row["predicted_verification"]),
                "basis": str(row["prediction_basis"]),
                "resolved_at": str(row["prediction_resolved_at"]),
                "actual_status": str(row["prediction_actual_status"]),
                "actual_steps": int(row["prediction_actual_steps"]),
                "evidence_ok": (
                    None if row["prediction_evidence_ok"] is None
                    else int(row["prediction_evidence_ok"])
                ),
                "failure_class": row["prediction_failure_class"],
            },
        }

    @staticmethod
    def _lesson_provenance_digest(material: dict[str, Any]) -> str:
        canonical = json.dumps(
            material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _lesson_provenance_validation(
        self,
        memory_id: int,
    ) -> tuple[bool, bool, bool]:
        """Return (valid, content mismatch, chain/digest mismatch)."""
        rows = self.db.execute(
            """SELECT prediction_id, memory_id, reflection_id, content_sha256,
                      provenance_sha256
               FROM lesson_provenance WHERE memory_id=?
               ORDER BY verified_at DESC, prediction_id DESC""",
            (int(memory_id),),
        ).fetchall()
        content_row = self.db.execute(
            "SELECT content FROM memories WHERE id=? AND kind='lesson'",
            (int(memory_id),),
        ).fetchone()
        if content_row is None:
            return False, False, bool(rows)
        observed_content_hash = hashlib.sha256(
            str(content_row["content"]).encode("utf-8")
        ).hexdigest()
        content_mismatch = False
        chain_mismatch = not bool(rows)
        for row in rows:
            if str(row["content_sha256"] or "") != observed_content_hash:
                content_mismatch = True
                continue
            material = self._lesson_provenance_material(
                int(row["memory_id"]),
                int(row["prediction_id"]),
                int(row["reflection_id"]),
            )
            if material is None:
                chain_mismatch = True
                continue
            expected = self._lesson_provenance_digest(material)
            if str(row["provenance_sha256"] or "") != expected:
                chain_mismatch = True
                continue
            return True, content_mismatch, chain_mismatch
        return False, content_mismatch, True

    def _lesson_prediction_for_reflection(
        self,
        reflection_id: int,
        *,
        family: str,
        outcome_status: str,
        allow_legacy_inference: bool = False,
        bind_legacy_inference: bool = True,
    ) -> sqlite3.Row | None:
        """Return the one exact resolved prediction that can support a lesson."""
        reflection = self.db.execute(
            """SELECT id, created_at, task_id, conversation_id, prediction_id,
                      status, tool_calls
               FROM reflections WHERE id=?""",
            (int(reflection_id),),
        ).fetchone()
        if reflection is None:
            return None
        if str(reflection["status"]) != outcome_status:
            return None
        task_id = reflection["task_id"]
        conversation_id = reflection["conversation_id"]
        prediction_id = reflection["prediction_id"]
        if prediction_id is None and allow_legacy_inference:
            if task_id is not None:
                candidates = self.db.execute(
                    """SELECT id, task_id, conversation_id, origin, family,
                              predicted_verification, actual_status, actual_steps,
                              evidence_ok, resolved_at
                       FROM task_predictions
                       WHERE task_id=? AND resolved_at IS NOT NULL
                         AND resolved_at<=?
                       ORDER BY resolved_at DESC, id DESC LIMIT 25""",
                    (int(task_id), str(reflection["created_at"])),
                ).fetchall()
            elif conversation_id is not None:
                candidates = self.db.execute(
                    """SELECT id, task_id, conversation_id, origin, family,
                              predicted_verification, actual_status, actual_steps,
                              evidence_ok, resolved_at
                       FROM task_predictions
                       WHERE conversation_id=? AND task_id IS NULL
                         AND resolved_at IS NOT NULL AND resolved_at<=?
                       ORDER BY resolved_at DESC, id DESC LIMIT 25""",
                    (int(conversation_id), str(reflection["created_at"])),
                ).fetchall()
            else:
                candidates = []
            exact_candidates = [
                candidate for candidate in candidates
                if str(candidate["origin"])
                not in self.SCREEN_COMPANION_PREDICTION_ORIGINS
                and str(candidate["family"]) == family
                and str(candidate["actual_status"]) == outcome_status
                and candidate["actual_steps"] is not None
                and int(candidate["actual_steps"]) == int(reflection["tool_calls"])
                and (
                    outcome_status != "complete"
                    or str(candidate["predicted_verification"]) == "not_applicable"
                    or int(candidate["evidence_ok"] or 0) == 1
                )
            ]
            if len(exact_candidates) != 1:
                return None
            prediction_id = int(exact_candidates[0]["id"])
            if bind_legacy_inference:
                cursor = self.db.execute(
                    """UPDATE reflections SET prediction_id=?
                       WHERE id=? AND prediction_id IS NULL
                         AND NOT EXISTS (
                             SELECT 1 FROM reflections AS bound
                             WHERE bound.prediction_id=? AND bound.id<>?
                         )""",
                    (
                        prediction_id, int(reflection_id), prediction_id,
                        int(reflection_id),
                    ),
                )
                if cursor.rowcount != 1:
                    return None
        if prediction_id is None:
            return None
        prediction = self.db.execute(
            """SELECT id, task_id, conversation_id, origin, family,
                      predicted_verification, actual_status, actual_steps,
                      evidence_ok, resolved_at
               FROM task_predictions WHERE id=? AND resolved_at IS NOT NULL""",
            (int(prediction_id),),
        ).fetchone()
        if prediction is None:
            return None
        if str(prediction["origin"]) in self.SCREEN_COMPANION_PREDICTION_ORIGINS:
            return None
        if str(prediction["resolved_at"]) > str(reflection["created_at"]):
            return None
        if task_id is not None:
            if prediction["task_id"] is None or int(prediction["task_id"]) != int(task_id):
                return None
        elif conversation_id is not None:
            if (
                prediction["task_id"] is not None
                or prediction["conversation_id"] is None
                or int(prediction["conversation_id"]) != int(conversation_id)
            ):
                return None
        else:
            return None
        if str(prediction["family"]) != family:
            return None
        if str(prediction["actual_status"]) != outcome_status:
            return None
        actual_steps = prediction["actual_steps"]
        if actual_steps is None or int(actual_steps) != int(reflection["tool_calls"]):
            return None
        if outcome_status == "complete" and (
            str(prediction["predicted_verification"]) != "not_applicable"
            and int(prediction["evidence_ok"] or 0) != 1
        ):
            return None
        return prediction

    def remember_verified_lesson(
        self,
        content: str,
        *,
        family: str,
        outcome_status: str,
        reflection_id: int,
    ) -> int:
        """Persist a reflection-derived lesson with controlled provenance."""
        if family not in self.PREDICTION_FAMILIES:
            raise ValueError(f"Unknown lesson family: {family}")
        if outcome_status not in {"complete", "incomplete", "failed"}:
            raise ValueError("Unknown lesson outcome status")
        safe = _bounded_persisted_text(
            redact_secrets(str(content).strip()), 8_000, "verified lesson"
        )
        if not safe:
            raise ValueError("Verified lesson must not be empty")
        normalized_reflection = self._prediction_optional_id(
            reflection_id, "reflection_id"
        )
        prediction = self._lesson_prediction_for_reflection(
            int(normalized_reflection),
            family=family,
            outcome_status=outcome_status,
        )
        if prediction is None:
            raise ValueError(
                "Verified lesson requires an exact resolved reflection/prediction outcome"
            )
        source = (
            f"verified reflection:{normalized_reflection};"
            f"prediction:{int(prediction['id'])}"
        )
        content_sha256 = hashlib.sha256(safe.encode("utf-8")).hexdigest()
        with self._immediate_transaction():
            self.db.execute(
                """INSERT OR IGNORE INTO memories(
                       created_at, kind, content, source, family,
                       outcome_status, reflection_id
                   ) VALUES (?, 'lesson', ?, ?, ?, ?, ?)""",
                (
                    now_iso(), safe, source, family, outcome_status,
                    normalized_reflection,
                ),
            )
            row = self.db.execute(
                """SELECT id, source, family, outcome_status, reflection_id
                   FROM memories
                   WHERE kind='lesson' AND content=?""",
                (safe,),
            ).fetchone()
            if row is not None and (
                str(row["family"] or "") != family
                or str(row["outcome_status"] or "") != outcome_status
                or row["reflection_id"] is None
                or int(row["reflection_id"]) != int(normalized_reflection)
            ):
                raise ValueError(
                    "Existing lesson text has different family, outcome, or reflection provenance"
                )
            if row is not None:
                material = self._lesson_provenance_material(
                    int(row["id"]),
                    int(prediction["id"]),
                    int(normalized_reflection),
                )
                if material is None:
                    raise ValueError("Verified lesson provenance chain is inconsistent")
                provenance_sha256 = self._lesson_provenance_digest(material)
                existing = self.db.execute(
                    """SELECT memory_id, reflection_id, content_sha256,
                              provenance_sha256
                       FROM lesson_provenance WHERE prediction_id=?""",
                    (int(prediction["id"]),),
                ).fetchone()
                if existing is None:
                    self.db.execute(
                        """INSERT INTO lesson_provenance(
                               prediction_id, memory_id, reflection_id, verified_at,
                               content_sha256, provenance_sha256
                           ) VALUES (?, ?, ?, ?, ?, ?)""",
                        (
                            int(prediction["id"]), int(row["id"]),
                            int(normalized_reflection), now_iso(), content_sha256,
                            provenance_sha256,
                        ),
                    )
                elif (
                    int(existing["memory_id"]) != int(row["id"])
                    or int(existing["reflection_id"]) != int(normalized_reflection)
                    or str(existing["content_sha256"]) != content_sha256
                    or str(existing["provenance_sha256"] or "")
                    != provenance_sha256
                ):
                    raise ValueError("Prediction is already bound to different lesson provenance")
        if row is None:
            raise RuntimeError("Verified lesson could not be persisted")
        lesson_id = int(row["id"])
        self._mirror_vault_note(
            "lessons",
            f"{family.replace('_', ' ').title()} lesson — Reflection {normalized_reflection}",
            safe,
            tags=("jarvis", "verified-lesson", family, outcome_status),
            links=(f"Reflection {normalized_reflection}",),
            source=source,
        )
        return lesson_id

    def match_lessons(
        self,
        query: str,
        family: str,
        *,
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        """Match only successful, reflection-derived lessons in the same family."""
        if family not in self.PREDICTION_FAMILIES:
            raise ValueError(f"Unknown lesson family: {family}")
        if contains_secret(str(query)):
            raise ValueError("Potential secret detected; lesson matching refused")
        limit = _bounded_limit(limit, 10)
        query_terms = _memory_query_terms(str(query))
        like_terms = _memory_like_terms(str(query), query_terms)
        if not limit or not query_terms or not like_terms:
            return []
        patterns = [f"%{_escape_like(term)}%" for term in like_terms]
        where = " OR ".join("lower(m.content) LIKE ? ESCAPE '\\'" for _ in patterns)
        match_count = " + ".join(
            "CASE WHEN lower(m.content) LIKE ? ESCAPE '\\' THEN 1 ELSE 0 END"
            for _ in patterns
        )
        rows = self.db.execute(
            f"""SELECT m.id, m.created_at, m.kind, m.content, m.source, m.family,
                       m.outcome_status, m.reflection_id,
                       COALESCE(a.resolved, 0) AS utility_resolved,
                       COALESCE(a.successes, 0) AS utility_successes
                FROM memories AS m
                LEFT JOIN (
                    SELECT memory_id,
                           SUM(CASE WHEN resolved_at IS NOT NULL THEN 1 ELSE 0 END)
                               AS resolved,
                           COALESCE(SUM(CASE WHEN resolved_at IS NOT NULL
                                             THEN successful ELSE 0 END), 0)
                               AS successes
                    FROM lesson_applications GROUP BY memory_id
                ) AS a ON a.memory_id=m.id
                WHERE m.kind='lesson' AND m.family=? AND m.outcome_status='complete'
                  AND EXISTS (
                      SELECT 1 FROM lesson_provenance AS lp WHERE lp.memory_id=m.id
                  )
                  AND ({where})
                ORDER BY ({match_count}) DESC, m.id DESC LIMIT ?""",
            [family, *patterns, *patterns, min(320, max(32, limit * 24))],
        ).fetchall()
        verified_rows = [
            row for row in rows
            if self._lesson_provenance_validation(int(row["id"]))[0]
        ]
        return _rank_memory_rows(verified_rows, query_terms, keep_id=True)[:limit]

    def record_lesson_applications(
        self,
        prediction_id: int,
        family: str,
        memory_ids: list[int],
    ) -> None:
        normalized_prediction = self._prediction_optional_id(
            prediction_id, "prediction_id"
        )
        if family not in self.PREDICTION_FAMILIES:
            raise ValueError(f"Unknown lesson family: {family}")
        bounded_ids = []
        for raw in memory_ids[:10]:
            normalized = self._prediction_optional_id(raw, "memory_id")
            if normalized not in bounded_ids:
                bounded_ids.append(normalized)
        stamp = now_iso()
        with self._immediate_transaction():
            prediction = self.db.execute(
                "SELECT family, resolved_at FROM task_predictions WHERE id=?",
                (normalized_prediction,),
            ).fetchone()
            if (
                prediction is None
                or prediction["family"] != family
                or prediction["resolved_at"] is not None
            ):
                raise ValueError("Lesson application must bind to the active matching prediction")
            for rank, memory_id in enumerate(bounded_ids, 1):
                valid = self.db.execute(
                    """SELECT 1 FROM memories
                       WHERE id=? AND kind='lesson' AND family=?
                         AND outcome_status='complete'""",
                    (memory_id, family),
                ).fetchone()
                if valid is None or not self._lesson_provenance_validation(memory_id)[0]:
                    raise ValueError("Lesson application references an ineligible lesson")
                self.db.execute(
                    """INSERT OR IGNORE INTO lesson_applications(
                           created_at, prediction_id, memory_id, family, rank
                       ) VALUES (?, ?, ?, ?, ?)""",
                    (stamp, normalized_prediction, memory_id, family, rank),
                )

    def lesson_effectiveness(self, family: str | None = None) -> list[dict[str, Any]]:
        if family is not None and family not in self.PREDICTION_FAMILIES:
            raise ValueError(f"Unknown lesson family: {family}")
        clause = "AND a.family=?" if family else ""
        parameters: tuple[Any, ...] = (family,) if family else ()
        rows = self.db.execute(
            f"""SELECT a.family, COUNT(*) AS applications,
                       SUM(CASE WHEN a.resolved_at IS NOT NULL THEN 1 ELSE 0 END) AS resolved,
                       AVG(CASE WHEN a.resolved_at IS NOT NULL THEN a.successful END)
                           AS success_rate
                FROM lesson_applications a WHERE 1=1 {clause}
                GROUP BY a.family ORDER BY a.family""",
            parameters,
        ).fetchall()
        return [dict(row) for row in rows]

    def search(
        self,
        query: str,
        limit: int = 8,
        *,
        include_id: bool = False,
    ) -> list[dict[str, Any]]:
        self._ensure_open()
        query = str(query)
        if len(query) > MAX_SEARCH_QUERY_CHARS:
            raise ValueError(f"Memory search query exceeds {MAX_SEARCH_QUERY_CHARS} characters")
        limit = _bounded_limit(limit, 100)
        query_terms = _memory_query_terms(query)
        like_terms = _memory_like_terms(query, query_terms)
        if not query_terms or not like_terms or not limit:
            return []
        patterns = [f"%{_escape_like(term)}%" for term in like_terms]
        where = " OR ".join("lower(m.content) LIKE ? ESCAPE '\\'" for _ in patterns)
        match_count = " + ".join(
            "CASE WHEN lower(m.content) LIKE ? ESCAPE '\\' THEN 1 ELSE 0 END"
            for _ in patterns
        )
        candidate_limit = min(
            MAX_MEMORY_SEARCH_CANDIDATES,
            max(64, limit * 32),
        )
        fts_query = _memory_fts_query(query, query_terms)
        if fts_query is not None:
            rows = self.db.execute(
                """SELECT m.id, m.created_at, m.kind, m.content, m.source,
                          c.status AS claim_status, c.authority AS claim_authority
                   FROM memory_fts
                   JOIN memories AS m ON m.id=memory_fts.rowid
                   LEFT JOIN memory_claims AS c ON c.memory_id=m.id
                   WHERE memory_fts MATCH ?
                     AND m.kind<>'lesson'
                     AND (m.kind<>'claim' OR c.status IN ('active', 'disputed'))
                     AND (m.kind='claim' OR EXISTS (
                         SELECT 1 FROM ordinary_memory_provenance AS omp
                         WHERE omp.memory_id=m.id AND omp.eligible=1
                     ))
                   ORDER BY memory_fts.rank, m.id DESC LIMIT ?""",
                (fts_query, candidate_limit),
            ).fetchall()
        else:
            rows = self.db.execute(
                f"""SELECT m.id, m.created_at, m.kind, m.content, m.source,
                           c.status AS claim_status, c.authority AS claim_authority
                    FROM memories AS m
                    LEFT JOIN memory_claims AS c ON c.memory_id=m.id
                    WHERE ({where})
                      AND m.kind<>'lesson'
                      AND (m.kind<>'claim' OR c.status IN ('active', 'disputed'))
                      AND (m.kind='claim' OR EXISTS (
                          SELECT 1 FROM ordinary_memory_provenance AS omp
                          WHERE omp.memory_id=m.id AND omp.eligible=1
                      ))
                    ORDER BY ({match_count}) DESC, m.id DESC LIMIT ?""",
                [*patterns, *patterns, candidate_limit],
            ).fetchall()
        rows = self._filter_generic_recall_rows(list(rows))
        return _rank_memory_rows(rows, query_terms, keep_id=bool(include_id))[:limit]

    def list_memories(self, limit: int = 20) -> list[dict[str, Any]]:
        self._ensure_open()
        limit = _bounded_limit(limit, 1_000)
        if not limit:
            return []
        rows = self.db.execute(
            "SELECT created_at, kind, content, source FROM memories ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]

    def _insert_task_locked(
        self,
        prompt: str,
        *,
        stamp: str,
        available_at: str,
        max_attempts: int,
        idempotency_key: str | None,
        project_id: int = 1,
        requested_model: str | None = None,
        specialist_key: str | None = None,
        delegated_by: str | None = None,
        parent_conversation_id: int | None = None,
        model_budget_scope: str | None = None,
    ) -> tuple[int, bool]:
        prompt = redact_secrets(str(prompt))
        if model_budget_scope is not None:
            model_budget_scope = self._model_budget_scope(model_budget_scope)
        if idempotency_key is not None:
            idempotency_key = _validated_nonsecret_metadata(
                idempotency_key, "Task idempotency key"
            )
        cur = self.db.execute(
            """INSERT OR IGNORE INTO tasks(
                created_at, updated_at, status, prompt, available_at,
                attempt_count, max_attempts, idempotency_key, project_id, requested_model,
                specialist_key, delegated_by, parent_conversation_id,
                model_budget_scope
            ) VALUES (?, ?, 'queued', ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                stamp, stamp, prompt, available_at, max_attempts, idempotency_key,
                project_id, requested_model, specialist_key, delegated_by,
                parent_conversation_id, model_budget_scope,
            ),
        )
        if cur.rowcount:
            return int(cur.lastrowid), True
        if not idempotency_key:
            raise RuntimeError("Task insert was ignored without an idempotency key")
        row = self.db.execute(
            """SELECT id, project_id, requested_model, specialist_key,
                      delegated_by, parent_conversation_id, model_budget_scope
               FROM tasks WHERE idempotency_key=?""",
            (idempotency_key,),
        ).fetchone()
        if row is None:
            raise RuntimeError("Could not resolve idempotent task insert")
        if (
            int(row["project_id"] or 1) != int(project_id)
            or (row["requested_model"] or None) != requested_model
            or (row["specialist_key"] or None) != specialist_key
            or (row["delegated_by"] or None) != delegated_by
            or row["parent_conversation_id"] != parent_conversation_id
            or (row["model_budget_scope"] or None) != model_budget_scope
        ):
            raise ValueError(
                "Task idempotency key is already bound to a different project or model "
                "or specialist delegation context"
            )
        return int(row["id"]), False

    def add_task(
        self,
        prompt: str,
        *,
        max_attempts: int = 3,
        idempotency_key: str | None = None,
        available_at: datetime | None = None,
        goal_id: int | None = None,
        backlog_id: int | None = None,
        project_id: int | None = None,
        requested_model: str | None = None,
    ) -> int:
        prompt = prompt.strip()
        if not prompt:
            raise ValueError("Task prompt must not be empty")
        if len(prompt) > 50_000:
            raise ValueError("Task prompt exceeds the 50,000 character limit")
        max_attempts = max(1, min(int(max_attempts), 100))
        key = idempotency_key.strip() if idempotency_key else None
        if key and len(key) > 500:
            raise ValueError("Task idempotency key is too long")
        if key:
            _validated_nonsecret_metadata(key, "Task idempotency key")
        normalized_project = self._project_id(project_id)
        project = self.get_project(normalized_project)
        if project is None or not bool(project["enabled"]):
            raise ValueError(f"Project #{normalized_project} does not exist or is disabled")
        safe_model = None
        if requested_model is not None:
            safe_model = _validated_nonsecret_metadata(
                str(requested_model).strip(), "Task requested model"
            )[:200] or None
        stamp = now_iso()
        available_text = _as_utc(available_at).isoformat() if available_at else stamp
        with self._immediate_transaction():
            task_id, _ = self._insert_task_locked(
                prompt,
                stamp=stamp,
                available_at=available_text,
                max_attempts=max_attempts,
                idempotency_key=key,
                project_id=normalized_project,
                requested_model=safe_model,
            )
            if goal_id is not None or backlog_id is not None:
                self.db.execute(
                    "UPDATE tasks SET goal_id=?, backlog_id=? WHERE id=?",
                    (goal_id, backlog_id, task_id),
                )
        return task_id

    def delegate_specialist_task(
        self,
        prompt: str,
        *,
        specialist_key: str,
        project_id: int,
        parent_conversation_id: int | None = None,
        max_attempts: int = 3,
        model_budget_scope: str | None = None,
        max_delegations: int = 4,
    ) -> int:
        """Queue one purpose-bound assignment from Jarvis to one isolated specialist."""
        text = str(prompt).strip()
        if not text or len(text) > 50_000:
            raise ValueError("Specialist assignment must contain 1-50,000 characters")
        key = str(specialist_key).strip().casefold()
        specialist = SPECIALIST_BY_KEY.get(key)
        if specialist is None or self.get_specialist_agent(key) is None:
            raise ValueError("Unknown specialist")
        declared = specialist_for_consultation_prompt(text)
        if declared is not None and declared.key != key:
            raise ValueError(
                "Specialist assignment does not match the declared consultation family"
            )
        normalized_project = self._project_id(project_id)
        project = self.get_project(normalized_project)
        if project is None or not bool(project["enabled"]):
            raise ValueError("Specialist assignments require an enabled project")
        parent = None
        if parent_conversation_id is not None:
            parent = self._prediction_optional_id(
                parent_conversation_id, "parent_conversation_id"
            )
            conversation_project = self.conversation_project(parent)
            if (
                conversation_project is None
                or int(conversation_project["id"]) != normalized_project
            ):
                raise ValueError("Delegation conversation must belong to the same project")
        maximum = max(1, min(int(max_attempts), 5))
        delegation_limit = max(0, min(int(max_delegations), 32))
        scope = (
            self._model_budget_scope(model_budget_scope)
            if model_budget_scope is not None
            else self._model_budget_scope(
                f"conversation:{parent}" if parent is not None else f"request:{uuid4().hex}"
            )
        )
        stamp = now_iso()
        idempotency_key = None
        if parent is not None:
            digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:24]
            scope_digest = hashlib.sha256(scope.encode("utf-8")).hexdigest()[:12]
            idempotency_key = f"delegation:{parent}:{scope_digest}:{key}:{digest}"
        with self._immediate_transaction():
            task_id, created = self._insert_task_locked(
                text,
                stamp=stamp,
                available_at=stamp,
                max_attempts=maximum,
                idempotency_key=idempotency_key,
                project_id=normalized_project,
                requested_model=specialist.model_profile,
                specialist_key=key,
                delegated_by="jarvis",
                parent_conversation_id=parent,
                model_budget_scope=scope,
            )
            if created:
                delegated = int(self.db.execute(
                    """SELECT COUNT(*) FROM tasks
                       WHERE delegated_by='jarvis' AND model_budget_scope=?""",
                    (scope,),
                ).fetchone()[0])
                if delegated > delegation_limit:
                    raise ModelBudgetExceeded(
                        "request specialist-delegation limit reached "
                        f"({delegation_limit})"
                    )
                self.db.execute(
                    """INSERT INTO activity_log(
                           created_at, category, action, status, task_id, details_json
                       ) VALUES (?, 'specialist', 'delegate', 'queued', ?, ?)""",
                    (
                        stamp,
                        task_id,
                        _redacted_json_text({
                            "specialist_key": key,
                            "project_id": normalized_project,
                            "parent_conversation_id": parent,
                            "model_profile": specialist.model_profile,
                        }),
                    ),
                )
        return task_id

    def specialist_task_reports(
        self,
        *,
        project_id: int,
        task_id: int | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return specialist assignments only to the owning Jarvis project context."""
        normalized_project = self._project_id(project_id)
        bounded = _bounded_limit(limit, 500)
        clause = "AND t.id=?" if task_id is not None else ""
        parameters: list[Any] = [normalized_project]
        if task_id is not None:
            parameters.append(self._prediction_optional_id(task_id, "task_id"))
        parameters.append(bounded)
        rows = self.db.execute(
            f"""SELECT t.id, t.created_at, t.updated_at, t.status, t.prompt,
                       t.result, t.last_error, t.attempt_count, t.max_attempts,
                       t.project_id, t.requested_model, t.specialist_key,
                       t.delegated_by, t.parent_conversation_id,
                       s.name AS specialist_name,
                       s.purpose AS specialist_purpose
                FROM tasks t
                JOIN specialist_agents s ON s.agent_key=t.specialist_key
                WHERE t.project_id=? AND t.delegated_by='jarvis' {clause}
                ORDER BY t.id DESC LIMIT ?""",
            parameters,
        ).fetchall()
        return [dict(row) for row in rows]

    def _recover_stale_locked(self, current: datetime) -> dict[str, int]:
        current_text = current.isoformat()
        rows = self.db.execute(
            """SELECT id, attempt_count, max_attempts, specialist_key
               FROM tasks
               WHERE status='running'
                 AND lease_expires_at IS NOT NULL
                 AND lease_expires_at<=?
               ORDER BY id""",
            (current_text,),
        ).fetchall()
        recovered = {"requeued": 0, "failed": 0}
        for row in rows:
            reason = f"Worker lease expired at or before {current_text}"
            if int(row["attempt_count"]) < int(row["max_attempts"]):
                self.db.execute(
                    """UPDATE tasks
                       SET status='queued', updated_at=?, available_at=?,
                           lease_owner=NULL, lease_expires_at=NULL, last_error=?
                       WHERE id=? AND status='running' AND lease_expires_at<=?""",
                    (current_text, current_text, reason, row["id"], current_text),
                )
                recovered["requeued"] += 1
            else:
                self.db.execute(
                    """UPDATE tasks
                       SET status='failed', updated_at=?, result=COALESCE(result, ?),
                           lease_owner=NULL, lease_expires_at=NULL, last_error=?
                       WHERE id=? AND status='running' AND lease_expires_at<=?""",
                    (current_text, reason, reason, row["id"], current_text),
                )
                recovered["failed"] += 1
            if row["specialist_key"] is not None:
                self.db.execute(
                    """UPDATE specialist_agents
                       SET status='ready', active_task_id=NULL,
                           failed_tasks=failed_tasks+?,
                           last_reported_at=CASE WHEN ?=1 THEN ? ELSE last_reported_at END,
                           updated_at=?
                       WHERE agent_key=? AND active_task_id=?""",
                    (
                        int(int(row["attempt_count"]) >= int(row["max_attempts"])),
                        int(int(row["attempt_count"]) >= int(row["max_attempts"])),
                        current_text, current_text, str(row["specialist_key"]),
                        int(row["id"]),
                    ),
                )
        return recovered

    def recover_stale_tasks(self, *, now: datetime | None = None) -> dict[str, int]:
        current = _as_utc(now)
        with self._immediate_transaction():
            return self._recover_stale_locked(current)

    def claim_task(
        self,
        worker_id: str | None = None,
        *,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        owner = _validated_worker_id(self.worker_id if worker_id is None else worker_id)
        lease_seconds = max(1, min(int(lease_seconds), 24 * 60 * 60))
        current = _as_utc(now)
        current_text = current.isoformat()
        lease_expires = (current + timedelta(seconds=lease_seconds)).isoformat()
        with self._immediate_transaction():
            self._recover_stale_locked(current)
            row = self.db.execute(
                """SELECT id FROM tasks
                   WHERE status='queued'
                     AND attempt_count < max_attempts
                     AND (available_at IS NULL OR available_at<=?)
                     AND (
                         specialist_key IS NULL OR EXISTS (
                             SELECT 1 FROM specialist_agents s
                             WHERE s.agent_key=tasks.specialist_key
                               AND s.status='ready'
                         )
                     )
                   ORDER BY id LIMIT 1""",
                (current_text,),
            ).fetchone()
            if row is None:
                return None
            updated = self.db.execute(
                """UPDATE tasks
                   SET status='running', updated_at=?, lease_owner=?, lease_expires_at=?,
                       attempt_count=attempt_count+1
                   WHERE id=? AND status='queued'""",
                (current_text, owner, lease_expires, row["id"]),
            )
            if updated.rowcount != 1:
                return None
            claimed = self.db.execute(
                """SELECT id, created_at, updated_at, status, prompt, result,
                          available_at, lease_owner, lease_expires_at,
                          attempt_count, max_attempts, last_error, idempotency_key,
                          goal_id, backlog_id, project_id, requested_model,
                          initiative_event_id, specialist_key, delegated_by,
                          parent_conversation_id
                   FROM tasks WHERE id=?""",
                (row["id"],),
            ).fetchone()
            if claimed is not None and claimed["specialist_key"] is not None:
                self.db.execute(
                    """UPDATE specialist_agents
                       SET status='working', active_task_id=?, last_started_at=?, updated_at=?
                       WHERE agent_key=?""",
                    (
                        int(claimed["id"]), current_text, current_text,
                        str(claimed["specialist_key"]),
                    ),
                )
            if claimed is not None and claimed["initiative_event_id"] is not None:
                self.db.execute(
                    """UPDATE initiative_events SET status='running'
                       WHERE id=? AND task_id=? AND status='queued'""",
                    (int(claimed["initiative_event_id"]), int(claimed["id"])),
                )
        return dict(claimed) if claimed else None

    def renew_task_lease(
        self,
        task_id: int,
        worker_id: str | None = None,
        *,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        now: datetime | None = None,
    ) -> bool:
        owner = _validated_worker_id(self.worker_id if worker_id is None else worker_id)
        lease_seconds = max(1, min(int(lease_seconds), 24 * 60 * 60))
        current = _as_utc(now)
        with self._immediate_transaction():
            updated = self.db.execute(
                """UPDATE tasks SET updated_at=?, lease_expires_at=?
                   WHERE id=? AND status='running' AND lease_owner=?""",
                (
                    current.isoformat(),
                    (current + timedelta(seconds=lease_seconds)).isoformat(),
                    task_id,
                    owner,
                ),
            )
        return updated.rowcount == 1

    def finish_task(
        self,
        task_id: int,
        result: str,
        status: str = "done",
        *,
        worker_id: str | None = None,
    ) -> bool:
        if status not in TERMINAL_TASK_STATUSES:
            raise ValueError(f"Terminal task status required, got {status!r}")
        safe_result = redact_secrets(str(result))
        result_text = _bounded_persisted_text(safe_result, MAX_TASK_RESULT_CHARS, "task result")
        error_text = _bounded_persisted_text(safe_result, MAX_TASK_ERROR_CHARS, "task error") if status == "failed" else None
        owner = _validated_worker_id(self.worker_id if worker_id is None else worker_id)
        stamp = now_iso()
        with self._immediate_transaction():
            row = self.db.execute(
                "SELECT status, lease_owner, specialist_key, backlog_id FROM tasks WHERE id=?",
                (task_id,),
            ).fetchone()
            if row is None:
                return False
            if row["status"] != "running":
                return False
            if row["lease_owner"] not in {None, owner}:
                return False
            updated = self.db.execute(
                """UPDATE tasks
                   SET status=?, updated_at=?, result=?, last_error=?,
                       lease_owner=NULL, lease_expires_at=NULL,
                       awaiting_approval_id=NULL
                   WHERE id=?""",
                (status, stamp, result_text, error_text, task_id),
            )
            if updated.rowcount == 1 and row["specialist_key"] is not None:
                self.db.execute(
                    """UPDATE specialist_agents
                       SET status='ready', active_task_id=NULL,
                           completed_tasks=completed_tasks+?,
                           failed_tasks=failed_tasks+?,
                           last_reported_at=?, updated_at=?
                       WHERE agent_key=? AND active_task_id=?""",
                    (
                        int(status == "done"), int(status == "failed"), stamp, stamp,
                        str(row["specialist_key"]), int(task_id),
                    ),
                )
        completed = updated.rowcount == 1
        if completed and row["backlog_id"] is not None:
            proactive = self.db.execute(
                """SELECT b.id, b.kind, s.subject
                   FROM proactive_backlog AS b
                   JOIN approved_subjects AS s ON s.id=b.subject_id
                   WHERE b.id=?""",
                (int(row["backlog_id"]),),
            ).fetchone()
            if proactive is not None and str(proactive["kind"]) in {"research", "ideas"}:
                backlog_kind = str(proactive["kind"])
                subject = str(proactive["subject"])
                label = "Research brief" if backlog_kind == "research" else "Ideas brief"
                self._mirror_vault_note(
                    "research",
                    f"{subject} — {label} — Task {int(task_id)}",
                    result_text,
                    tags=("jarvis", "proactive", backlog_kind, subject),
                    links=(subject,),
                    source=f"proactive-backlog:{int(proactive['id'])}/task:{int(task_id)}",
                )
        return completed

    def fail_task(
        self,
        task_id: int,
        error: str,
        *,
        worker_id: str | None = None,
        retry: bool = True,
        retry_delay_seconds: int = 0,
        now: datetime | None = None,
    ) -> str | None:
        error_text = _bounded_persisted_text(
            redact_secrets(str(error)), MAX_TASK_ERROR_CHARS, "task error"
        )
        owner = _validated_worker_id(self.worker_id if worker_id is None else worker_id)
        current = _as_utc(now)
        current_text = current.isoformat()
        delay = max(0, min(int(retry_delay_seconds), 7 * 24 * 60 * 60))
        with self._immediate_transaction():
            row = self.db.execute(
                """SELECT status, lease_owner, attempt_count, max_attempts,
                          specialist_key FROM tasks WHERE id=?""",
                (task_id,),
            ).fetchone()
            if row is None:
                return None
            if row["status"] != "running":
                return None
            if row["lease_owner"] not in {None, owner}:
                return None
            should_retry = retry and int(row["attempt_count"]) < int(row["max_attempts"])
            next_status = "queued" if should_retry else "failed"
            available_at = (current + timedelta(seconds=delay)).isoformat()
            self.db.execute(
                """UPDATE tasks
                   SET status=?, updated_at=?, result=?, last_error=?, available_at=?,
                       lease_owner=NULL, lease_expires_at=NULL,
                       awaiting_approval_id=NULL
                   WHERE id=?""",
                (next_status, current_text, error_text, error_text, available_at, task_id),
            )
            if row["specialist_key"] is not None:
                self.db.execute(
                    """UPDATE specialist_agents
                       SET status='ready', active_task_id=NULL,
                           failed_tasks=failed_tasks+?,
                           last_reported_at=CASE WHEN ?=1 THEN ? ELSE last_reported_at END,
                           updated_at=?
                       WHERE agent_key=? AND active_task_id=?""",
                    (
                        int(next_status == "failed"), int(next_status == "failed"),
                        current_text, current_text, str(row["specialist_key"]),
                        int(task_id),
                    ),
                )
        return next_status

    def await_task_approval(
        self,
        task_id: int,
        approval_id: int,
        *,
        worker_id: str | None = None,
    ) -> str | None:
        """Park a leased task until its exact pending approval is decided."""
        owner = _validated_worker_id(self.worker_id if worker_id is None else worker_id)
        stamp = now_iso()
        waiting_text = f"Awaiting approval #{int(approval_id)}"
        with self._immediate_transaction():
            approval = self.db.execute(
                "SELECT task_id, status FROM approvals WHERE id=?",
                (int(approval_id),),
            ).fetchone()
            task = self.db.execute(
                """SELECT status, lease_owner, result, awaiting_approval_id,
                          specialist_key
                   FROM tasks WHERE id=?""",
                (int(task_id),),
            ).fetchone()
            if (
                approval is None
                or approval["status"] not in {"pending", "approved", "denied"}
                or approval["task_id"] != int(task_id)
                or task is None
            ):
                return None
            denial = f"Approval #{int(approval_id)} was denied"
            if (
                approval["status"] == "denied"
                and task["status"] == "failed"
                and task["result"] == denial
            ):
                return "failed"
            if (
                task["status"] != "running"
                or task["lease_owner"] not in {None, owner}
                or task["awaiting_approval_id"] != int(approval_id)
            ):
                return None
            next_status = (
                "awaiting_approval"
                if approval["status"] == "pending"
                else "queued"
                if approval["status"] == "approved"
                else "failed"
            )
            task_result = (
                waiting_text
                if next_status == "awaiting_approval"
                else None
                if next_status == "queued"
                else denial
            )
            available_at = stamp if next_status == "queued" else None
            updated = self.db.execute(
                """UPDATE tasks
                   SET status=?, updated_at=?, result=?, last_error=?,
                       available_at=?, lease_owner=NULL, lease_expires_at=NULL,
                       awaiting_approval_id=?,
                       attempt_count=CASE WHEN attempt_count>0 THEN attempt_count-1 ELSE 0 END
                   WHERE id=? AND status='running'""",
                (
                    next_status,
                    stamp,
                    task_result,
                    task_result,
                    available_at,
                    int(approval_id) if next_status == "awaiting_approval" else None,
                    int(task_id),
                ),
            )
            if updated.rowcount == 1 and next_status == "failed":
                self._record_reflection_locked(
                    stamp=stamp,
                    status="failed",
                    summary=str(task_result),
                    mistakes="The required sensitive action was denied by the operator.",
                    improvements="",
                    task_id=int(task_id),
                    conversation_id=None,
                    prediction_id=None,
                    tool_calls=0,
                )
            if updated.rowcount == 1 and task["specialist_key"] is not None:
                self.db.execute(
                    """UPDATE specialist_agents
                       SET status='ready', active_task_id=NULL,
                           failed_tasks=failed_tasks+?,
                           last_reported_at=CASE WHEN ?=1 THEN ? ELSE last_reported_at END,
                           updated_at=?
                       WHERE agent_key=? AND active_task_id=?""",
                    (
                        int(next_status == "failed"), int(next_status == "failed"),
                        stamp, stamp, str(task["specialist_key"]), int(task_id),
                    ),
                )
        return next_status if updated.rowcount == 1 else None

    def list_tasks(self, limit: int = 20) -> list[dict[str, Any]]:
        self._ensure_open()
        limit = _bounded_limit(limit, 10_000)
        if not limit:
            return []
        rows = self.db.execute(
            """SELECT id, created_at, updated_at, status, prompt, result,
                      available_at, lease_owner, lease_expires_at,
                      attempt_count, max_attempts, last_error, idempotency_key,
                      goal_id, backlog_id, awaiting_approval_id, project_id,
                      requested_model, initiative_event_id, specialist_key,
                      delegated_by, parent_conversation_id
               FROM tasks ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def add_scheduled_job(
        self,
        name: str,
        prompt: str,
        interval_minutes: int,
        *,
        project_id: int | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Create one durable recurring task without running it immediately."""
        safe_name = redact_secrets(str(name).strip())[:120]
        safe_prompt = redact_secrets(str(prompt).strip())
        if not safe_name:
            raise ValueError("Scheduled job name must not be empty")
        if not safe_prompt:
            raise ValueError("Scheduled job prompt must not be empty")
        if len(safe_prompt) > 20_000:
            raise ValueError("Scheduled job prompt exceeds the 20,000 character limit")
        interval = int(interval_minutes)
        if interval < 1 or interval > 525_600:
            raise ValueError("Scheduled interval must be between 1 minute and 1 year")
        normalized_project = self._project_id(project_id)
        project = self.get_project(normalized_project)
        if project is None or not bool(project["enabled"]):
            raise ValueError(f"Project #{normalized_project} does not exist or is disabled")
        current = _as_utc(now)
        stamp = current.isoformat()
        next_run = (current + timedelta(minutes=interval)).isoformat()
        with self._immediate_transaction():
            cursor = self.db.execute(
                """INSERT INTO scheduled_jobs(
                       created_at, updated_at, project_id, name, prompt,
                       interval_minutes, next_run_at, enabled
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, 1)""",
                (
                    stamp,
                    stamp,
                    normalized_project,
                    safe_name,
                    safe_prompt,
                    interval,
                    next_run,
                ),
            )
            job_id = int(cursor.lastrowid)
        return {
            "id": job_id,
            "name": safe_name,
            "prompt": safe_prompt,
            "project_id": normalized_project,
            "interval_minutes": interval,
            "next_run_at": next_run,
            "enabled": 1,
        }

    def list_scheduled_jobs(
        self,
        *,
        project_id: int | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        normalized_project = (
            self._project_id(project_id) if project_id is not None else None
        )
        bounded = _bounded_limit(limit, 200)
        if not bounded:
            return []
        rows = self.db.execute(
            """SELECT id, created_at, updated_at, project_id, name, prompt,
                      interval_minutes, next_run_at, enabled, last_run_at, last_task_id
               FROM scheduled_jobs
               WHERE (? IS NULL OR project_id=?)
               ORDER BY id DESC LIMIT ?""",
            (normalized_project, normalized_project, bounded),
        ).fetchall()
        return [dict(row) for row in rows]

    def set_scheduled_job_enabled(
        self,
        job_id: int,
        enabled: bool,
        *,
        project_id: int | None = None,
        now: datetime | None = None,
    ) -> bool:
        normalized_job = self._prediction_optional_id(job_id, "scheduled_job_id")
        if normalized_job is None:
            raise ValueError("scheduled_job_id is required")
        if not isinstance(enabled, bool):
            raise TypeError("enabled must be boolean")
        normalized_project = self._project_id(project_id)
        current = _as_utc(now)
        stamp = current.isoformat()
        with self._immediate_transaction():
            updated = self.db.execute(
                """UPDATE scheduled_jobs
                   SET enabled=?, updated_at=?,
                       next_run_at=CASE WHEN ?=1 AND enabled=0
                           THEN ? ELSE next_run_at END
                   WHERE id=? AND project_id=?""",
                (
                    int(bool(enabled)),
                    stamp,
                    int(bool(enabled)),
                    (current + timedelta(minutes=1)).isoformat(),
                    normalized_job,
                    normalized_project,
                ),
            )
        return updated.rowcount == 1

    def delete_scheduled_job(
        self,
        job_id: int,
        *,
        project_id: int | None = None,
    ) -> bool:
        normalized_job = self._prediction_optional_id(job_id, "scheduled_job_id")
        if normalized_job is None:
            raise ValueError("scheduled_job_id is required")
        normalized_project = self._project_id(project_id)
        with self._immediate_transaction():
            deleted = self.db.execute(
                "DELETE FROM scheduled_jobs WHERE id=? AND project_id=?",
                (normalized_job, normalized_project),
            )
        return deleted.rowcount == 1

    def queue_due_scheduled_jobs(self, *, now: datetime | None = None) -> int:
        """Atomically materialize every due recurrence as one idempotent worker task."""
        current = _as_utc(now)
        current_text = current.isoformat()
        queued = 0
        with self._immediate_transaction():
            rows = self.db.execute(
                """SELECT id, project_id, name, prompt, interval_minutes, next_run_at
                   FROM scheduled_jobs
                   WHERE enabled=1 AND next_run_at<=?
                   ORDER BY next_run_at, id LIMIT 100""",
                (current_text,),
            ).fetchall()
            for row in rows:
                scheduled_for = str(row["next_run_at"])
                job_id = int(row["id"])
                prompt = (
                    f"Scheduled job #{job_id} ({row['name']}): {row['prompt']}\n"
                    "Complete the bounded task in its assigned project and report the result."
                )
                specialist = specialist_for_scheduled_prompt(prompt)
                task_id, created = self._insert_task_locked(
                    prompt,
                    stamp=current_text,
                    available_at=current_text,
                    max_attempts=3,
                    idempotency_key=f"schedule:{job_id}:{scheduled_for}",
                    project_id=int(row["project_id"]),
                    requested_model=(specialist.model_profile if specialist else None),
                    specialist_key=(specialist.key if specialist else None),
                    delegated_by="schedule",
                )
                queued += int(created)
                interval = timedelta(minutes=int(row["interval_minutes"]))
                next_run = datetime.fromisoformat(scheduled_for)
                if next_run.tzinfo is None:
                    next_run = next_run.replace(tzinfo=timezone.utc)
                next_run = next_run.astimezone(timezone.utc)
                while next_run <= current:
                    next_run += interval
                self.db.execute(
                    """UPDATE scheduled_jobs
                       SET updated_at=?, last_run_at=?, last_task_id=?, next_run_at=?
                       WHERE id=? AND next_run_at=?""",
                    (
                        current_text,
                        scheduled_for,
                        task_id,
                        next_run.isoformat(),
                        job_id,
                        scheduled_for,
                    ),
                )
        return queued

    def add_learning_topic(self, topic: str, interval_hours: int = 24) -> int:
        topic = _validated_learning_topic(topic)
        interval_hours = max(1, min(int(interval_hours), 24 * 365))
        stamp = now_iso()
        with self._immediate_transaction():
            cur = self.db.execute(
                "INSERT INTO learning_topics(created_at, topic, interval_hours, next_run) "
                "VALUES (?, ?, ?, ?)",
                (stamp, topic, interval_hours, stamp),
            )
        return int(cur.lastrowid)

    def ensure_learning_topic(
        self,
        topic: str,
        interval_hours: int = 24,
    ) -> tuple[int, bool]:
        """Create or re-enable a recurring topic without duplicating the current run."""
        topic = _validated_learning_topic(topic)
        interval_hours = max(1, min(int(interval_hours), 24 * 365))
        current = datetime.now(timezone.utc)
        stamp = current.isoformat()
        next_run = (current + timedelta(hours=interval_hours)).isoformat()
        with self._immediate_transaction():
            existing = self.db.execute(
                "SELECT id FROM learning_topics WHERE topic=?",
                (topic,),
            ).fetchone()
            if existing is None:
                cur = self.db.execute(
                    "INSERT INTO learning_topics(created_at, topic, interval_hours, next_run, enabled) "
                    "VALUES (?, ?, ?, ?, 1)",
                    (stamp, topic, interval_hours, next_run),
                )
                return int(cur.lastrowid), True
            topic_id = int(existing["id"])
            self.db.execute(
                "UPDATE learning_topics SET interval_hours=?, next_run=?, enabled=1 WHERE id=?",
                (interval_hours, next_run, topic_id),
            )
            return topic_id, False

    def queue_due_learning(self, *, now: datetime | None = None) -> int:
        current = _as_utc(now)
        current_text = current.isoformat()
        queued = 0
        with self._immediate_transaction():
            rows = self.db.execute(
                """SELECT id, topic, interval_hours, next_run
                   FROM learning_topics
                   WHERE enabled=1 AND next_run<=?
                   ORDER BY id""",
                (current_text,),
            ).fetchall()
            for row in rows:
                try:
                    topic = _validated_learning_topic(row["topic"])
                except ValueError:
                    # Older databases may contain command-shaped topics created
                    # before validation existed. Disable them rather than
                    # repeatedly executing an unresolved instruction.
                    self.db.execute(
                        "UPDATE learning_topics SET enabled=0 WHERE id=?",
                        (row["id"],),
                    )
                    continue
                scheduled_for = row["next_run"]
                key = f"learning:{row['id']}:{scheduled_for}"
                prompt = (
                    f"Continuously learn about this topic: {topic}. "
                    "Research current, authoritative sources; compare the evidence; "
                    "and return a concise dated brief with exact source URLs."
                )
                existing_run = self.db.execute(
                    "SELECT task_id FROM learning_runs WHERE topic_id=? AND scheduled_for=?",
                    (row["id"], scheduled_for),
                ).fetchone()
                if existing_run is None:
                    specialist = (
                        specialist_for_prompt(prompt)
                        or SPECIALIST_BY_KEY["research"]
                    )
                    task_id, created = self._insert_task_locked(
                        prompt,
                        stamp=current_text,
                        available_at=current_text,
                        max_attempts=3,
                        idempotency_key=key,
                        requested_model=specialist.model_profile,
                        specialist_key=specialist.key,
                        delegated_by="jarvis",
                    )
                    self.db.execute(
                        """INSERT OR IGNORE INTO learning_runs(
                            topic_id, scheduled_for, task_id, created_at
                        ) VALUES (?, ?, ?, ?)""",
                        (row["id"], scheduled_for, task_id, current_text),
                    )
                    queued += int(created)
                next_run = current + timedelta(hours=int(row["interval_hours"]))
                self.db.execute(
                    "UPDATE learning_topics SET next_run=? WHERE id=? AND next_run=?",
                    (next_run.isoformat(), row["id"], scheduled_for),
                )
        return queued

    def list_learning_topics(self) -> list[dict[str, Any]]:
        self._ensure_open()
        rows = self.db.execute(
            "SELECT id, topic, interval_hours, next_run, enabled FROM learning_topics ORDER BY id"
        ).fetchall()
        return [dict(row) for row in rows]

    def set_learning_topic_enabled(self, topic_id: int, enabled: bool) -> bool:
        self._ensure_open()
        if isinstance(topic_id, bool) or not isinstance(topic_id, int) or topic_id <= 0:
            return False
        stamp = now_iso()
        with self._immediate_transaction():
            if enabled:
                updated = self.db.execute(
                    "UPDATE learning_topics SET enabled=1, next_run=? WHERE id=?",
                    (stamp, topic_id),
                )
            else:
                updated = self.db.execute(
                    "UPDATE learning_topics SET enabled=0 WHERE id=?",
                    (topic_id,),
                )
        return updated.rowcount == 1

    def list_learning_runs(self, limit: int = 100) -> list[dict[str, Any]]:
        self._ensure_open()
        limit = _bounded_limit(limit, 10_000)
        if not limit:
            return []
        rows = self.db.execute(
            """SELECT id, topic_id, scheduled_for, task_id, created_at
               FROM learning_runs ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    # Proactive-assistant control plane ---------------------------------

    def control_state(self) -> dict[str, Any]:
        self._ensure_open()
        row = self.db.execute(
            "SELECT state, updated_at, reason FROM runtime_control WHERE id=1"
        ).fetchone()
        return dict(row) if row else {"state": "stopped", "updated_at": now_iso(), "reason": "missing control row"}

    def set_control_state(self, state: str, reason: str | None = None) -> None:
        state = str(state).strip().casefold()
        if state not in {"running", "paused", "stopped"}:
            raise ValueError("Control state must be running, paused, or stopped")
        reason_text = redact_secrets(str(reason).strip())[:1000] if reason else None
        stamp = now_iso()
        with self._immediate_transaction():
            self.db.execute(
                "UPDATE runtime_control SET state=?, updated_at=?, reason=? WHERE id=1",
                (state, stamp, reason_text),
            )
            self.db.execute(
                "INSERT INTO activity_log(created_at, category, action, status, details_json) "
                "VALUES (?, 'control', ?, 'complete', ?)",
                (stamp, state, json.dumps({"reason": reason_text}, ensure_ascii=False)),
            )

    def log_activity(
        self,
        category: str,
        action: str,
        status: str,
        *,
        task_id: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> int:
        category = _validated_nonsecret_metadata(category, "Activity category")
        action = _validated_nonsecret_metadata(action, "Activity action")
        status = _validated_nonsecret_metadata(status, "Activity status")
        payload = _redacted_json_text(details or {})
        payload = _bounded_persisted_text(payload, 8_000, "activity details")
        with self._immediate_transaction():
            cur = self.db.execute(
                """INSERT INTO activity_log(
                       created_at, category, action, status, task_id, details_json
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    now_iso(), category[:40], action[:100], status[:30], task_id, payload,
                ),
            )
        return int(cur.lastrowid)

    def list_activity(self, limit: int = 100) -> list[dict[str, Any]]:
        self._ensure_open()
        limit = _bounded_limit(limit, 10_000)
        if not limit:
            return []
        rows = self.db.execute(
            """SELECT id, created_at, category, action, status, task_id, details_json
               FROM activity_log ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def activity_count_since(
        self,
        category: str,
        since: datetime,
        *,
        task_scoped: bool = False,
    ) -> int:
        self._ensure_open()
        task_filter = " AND task_id IS NOT NULL" if task_scoped else ""
        row = self.db.execute(
            "SELECT COUNT(*) FROM activity_log WHERE category=? AND created_at>=?"
            + task_filter,
            (category, _as_utc(since).isoformat()),
        ).fetchone()
        return int(row[0]) if row else 0

    def add_goal(
        self,
        title: str,
        description: str = "",
        *,
        kind: str = "goal",
        priority: int = 50,
    ) -> int:
        title = redact_secrets(str(title).strip())
        kind = str(kind).strip().casefold()
        if not title or len(title) > 300:
            raise ValueError("Goal title must contain 1-300 characters")
        if kind not in {"goal", "project"}:
            raise ValueError("Goal kind must be goal or project")
        description = redact_secrets(str(description).strip())
        if len(description) > 8_000:
            raise ValueError("Goal description exceeds 8,000 characters")
        priority = max(0, min(int(priority), 100))
        stamp = now_iso()
        with self._immediate_transaction():
            cur = self.db.execute(
                """INSERT INTO goals(
                       created_at, updated_at, kind, title, description, status, priority
                   ) VALUES (?, ?, ?, ?, ?, 'active', ?)""",
                (stamp, stamp, kind, title, description, priority),
            )
        return int(cur.lastrowid)

    def update_goal_status(self, goal_id: int, status: str) -> bool:
        status = str(status).strip().casefold()
        if status not in {"active", "paused", "completed", "cancelled"}:
            raise ValueError("Goal status must be active, paused, completed, or cancelled")
        normalized_goal = self._prediction_optional_id(goal_id, "goal_id")
        if normalized_goal is None:
            raise ValueError("goal_id is required")
        with self._immediate_transaction():
            updated = self.db.execute(
                "UPDATE goals SET status=?, updated_at=? WHERE id=?",
                (status, now_iso(), normalized_goal),
            )
        return updated.rowcount == 1

    def list_goals(self, limit: int = 100) -> list[dict[str, Any]]:
        self._ensure_open()
        limit = _bounded_limit(limit, 1_000)
        rows = self.db.execute(
            """SELECT id, created_at, updated_at, kind, title, description, status, priority
               FROM goals ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'paused' THEN 1 ELSE 2 END,
                    priority DESC, id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def add_journal_entry(
        self,
        goal_id: int,
        content: str,
        *,
        kind: str = "note",
        task_id: int | None = None,
    ) -> int:
        content = redact_secrets(str(content).strip())
        if not content or len(content) > 20_000:
            raise ValueError("Journal entry must contain 1-20,000 characters")
        kind = _validated_nonsecret_metadata(kind, "Journal kind")[:40] or "note"
        with self._immediate_transaction():
            goal = self.db.execute(
                "SELECT title FROM goals WHERE id=?", (int(goal_id),)
            ).fetchone()
            if goal is None:
                raise ValueError(f"Goal #{goal_id} does not exist")
            cur = self.db.execute(
                """INSERT INTO journal_entries(goal_id, created_at, kind, content, task_id)
                   VALUES (?, ?, ?, ?, ?)""",
                (int(goal_id), now_iso(), kind, content, task_id),
            )
            self.db.execute("UPDATE goals SET updated_at=? WHERE id=?", (now_iso(), int(goal_id)))
        entry_id = int(cur.lastrowid)
        goal_title = str(goal["title"])
        self._mirror_vault_note(
            "journal",
            f"{goal_title} — {kind} — Entry {entry_id}",
            content,
            tags=("jarvis", "journal", kind),
            links=(goal_title,),
            source=f"goal:{int(goal_id)}/journal:{entry_id}",
        )
        return entry_id

    def list_journal(self, goal_id: int, limit: int = 100) -> list[dict[str, Any]]:
        self._ensure_open()
        limit = _bounded_limit(limit, 1_000)
        rows = self.db.execute(
            """SELECT id, goal_id, created_at, kind, content, task_id
               FROM journal_entries WHERE goal_id=? ORDER BY id DESC LIMIT ?""",
            (int(goal_id), limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def _set_preference_locked(
        self,
        name: str,
        value: str,
        *,
        source: str,
        authority: str,
        confidence: float,
        stamp: str,
    ) -> int:
        self.db.execute(
            """INSERT INTO preferences(
                   created_at, updated_at, name, value, source, confidence, active
               ) VALUES (?, ?, ?, ?, ?, ?, 1)
               ON CONFLICT(name) DO UPDATE SET
                   updated_at=excluded.updated_at, value=excluded.value,
                   source=excluded.source, confidence=excluded.confidence, active=1""",
            (stamp, stamp, name, value, source, confidence),
        )
        row = self.db.execute(
            "SELECT id FROM preferences WHERE name=?", (name,)
        ).fetchone()
        if row is None:
            raise RuntimeError("Preference could not be persisted")
        self._remember_claim_locked(
            "user", f"preference:{name}", value,
            source=source, authority=authority,
            confidence=confidence, stamp=stamp,
        )
        return int(row["id"])

    def set_preference(
        self,
        name: str,
        value: str,
        *,
        source: str = "user",
        confidence: float = 1.0,
    ) -> int:
        name = _validated_nonsecret_metadata(name, "Preference name").casefold()
        value = redact_secrets(str(value).strip())
        if not name or len(name) > 100 or not value or len(value) > 2_000:
            raise ValueError("Preference name/value is empty or too long")
        confidence = float(confidence)
        if not math.isfinite(confidence):
            raise ValueError("Preference confidence must be finite")
        confidence = max(0.0, min(confidence, 1.0))
        safe_source = _validated_nonsecret_metadata(source, "Preference source")[:100]
        authority = (
            "operator"
            if safe_source.casefold() in {
                "user", "explicit user preference", "explicit user feedback",
                "explicit user profile statement",
            }
            else "verified" if safe_source.casefold().startswith("verified")
            else "learned"
        )
        stamp = now_iso()
        with self._immediate_transaction():
            return self._set_preference_locked(
                name,
                value,
                source=safe_source,
                authority=authority,
                confidence=confidence,
                stamp=stamp,
            )

    def list_preferences(self) -> list[dict[str, Any]]:
        self._ensure_open()
        rows = self.db.execute(
            """SELECT id, updated_at, name, value, source, confidence
               FROM preferences WHERE active=1 ORDER BY updated_at DESC, id DESC LIMIT 100"""
        ).fetchall()
        return [dict(row) for row in rows]

    def approve_subject(self, subject: str, notes: str = "") -> int:
        subject = _validated_nonsecret_metadata(subject, "Approved subject")
        notes = redact_secrets(str(notes).strip())
        if not subject or len(subject) > 500:
            raise ValueError("Subject must contain 1-500 characters")
        with self._immediate_transaction():
            self.db.execute(
                """INSERT INTO approved_subjects(created_at, subject, notes, enabled)
                   VALUES (?, ?, ?, 1)
                   ON CONFLICT(subject) DO UPDATE SET notes=excluded.notes, enabled=1""",
                (now_iso(), subject, notes[:2_000]),
            )
            row = self.db.execute(
                "SELECT id FROM approved_subjects WHERE subject=?", (subject,)
            ).fetchone()
        return int(row["id"])

    def list_subjects(self) -> list[dict[str, Any]]:
        self._ensure_open()
        return [dict(row) for row in self.db.execute(
            "SELECT id, created_at, subject, notes, enabled FROM approved_subjects ORDER BY id"
        ).fetchall()]

    def add_backlog_item(
        self,
        kind: str,
        subject_id: int,
        instructions: str = "",
        *,
        priority: int = 50,
        interval_hours: int = 168,
        goal_id: int | None = None,
    ) -> int:
        kind = str(kind).strip().casefold()
        if kind not in {"research", "ideas", "prototype"}:
            raise ValueError("Backlog kind must be research, ideas, or prototype")
        instructions = redact_secrets(str(instructions).strip())
        if len(instructions) > 8_000:
            raise ValueError("Backlog instructions exceed 8,000 characters")
        priority = max(0, min(int(priority), 100))
        interval_hours = max(1, min(int(interval_hours), 24 * 365))
        stamp = now_iso()
        with self._immediate_transaction():
            subject = self.db.execute(
                "SELECT enabled FROM approved_subjects WHERE id=?", (int(subject_id),)
            ).fetchone()
            if subject is None or not subject["enabled"]:
                raise ValueError("Backlog items require an enabled, explicitly approved subject")
            if goal_id is not None and self.db.execute(
                "SELECT 1 FROM goals WHERE id=? AND status IN ('active', 'paused')", (int(goal_id),)
            ).fetchone() is None:
                raise ValueError("Linked goal does not exist or is closed")
            cur = self.db.execute(
                """INSERT INTO proactive_backlog(
                       created_at, updated_at, kind, subject_id, goal_id, instructions,
                       priority, interval_hours, next_run, enabled
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
                (stamp, stamp, kind, int(subject_id), goal_id, instructions,
                 priority, interval_hours, stamp),
            )
        return int(cur.lastrowid)

    def list_backlog(self) -> list[dict[str, Any]]:
        self._ensure_open()
        rows = self.db.execute(
            """SELECT b.id, b.kind, b.subject_id, s.subject, b.goal_id, b.instructions,
                      b.priority, b.interval_hours, b.next_run, b.enabled, b.updated_at
               FROM proactive_backlog b JOIN approved_subjects s ON s.id=b.subject_id
               ORDER BY b.enabled DESC, b.priority DESC, b.id"""
        ).fetchall()
        return [dict(row) for row in rows]

    def set_backlog_enabled(self, backlog_id: int, enabled: bool) -> bool:
        normalized_backlog = self._prediction_optional_id(
            backlog_id, "backlog_id"
        )
        if normalized_backlog is None:
            raise ValueError("backlog_id is required")
        if not isinstance(enabled, bool):
            raise TypeError("enabled must be boolean")
        with self._immediate_transaction():
            updated = self.db.execute(
                "UPDATE proactive_backlog SET enabled=?, updated_at=? WHERE id=?",
                (int(enabled), now_iso(), normalized_backlog),
            )
        return updated.rowcount == 1

    @staticmethod
    def _proactive_prompt(row: sqlite3.Row) -> str:
        subject = str(row["subject"])
        extra = str(row["instructions"] or "").strip()
        if row["kind"] == "research":
            base = (
                f"Proactive approved-subject research: {subject}. Research current authoritative "
                "sources, compare evidence, identify useful implications, and present a concise dated "
                "brief with exact source URLs."
            )
        elif row["kind"] == "ideas":
            base = (
                f"Research current authoritative public sources for this explicitly approved "
                f"subject and generate grounded practical project ideas: {subject}. Rank the "
                "ideas by usefulness, effort, and testability, cite exact fetched URLs, and "
                "recommend one next step."
            )
        else:
            base = (
                f"Build and test a small reversible prototype inside the JARVIS workspace for this "
                f"explicitly approved subject: {subject}. Inspect existing workspace files, keep the "
                "prototype self-contained, run relevant tests, and present the paths and observed results."
            )
        return f"{base}\nOperator backlog instructions: {extra}" if extra else base

    def schedule_idle_activity(
        self,
        *,
        daily_limit: int,
        now: datetime | None = None,
    ) -> int | None:
        current = _as_utc(now)
        current_text = current.isoformat()
        day_start = current.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        daily_limit = max(0, min(int(daily_limit), 100))
        if daily_limit == 0:
            return None
        with self._immediate_transaction():
            control = self.db.execute("SELECT state FROM runtime_control WHERE id=1").fetchone()
            if control is None or control["state"] != "running":
                return None
            if self.db.execute(
                "SELECT 1 FROM tasks WHERE status IN ('queued', 'running') LIMIT 1"
            ).fetchone() is not None:
                return None
            used = self.db.execute(
                "SELECT COUNT(*) FROM proactive_runs WHERE created_at>=?", (day_start,)
            ).fetchone()[0]
            if int(used) >= daily_limit:
                return None
            row = self.db.execute(
                """SELECT b.*, s.subject
                   FROM proactive_backlog b
                   JOIN approved_subjects s ON s.id=b.subject_id
                   LEFT JOIN goals g ON g.id=b.goal_id
                   WHERE b.enabled=1 AND s.enabled=1 AND b.next_run<=?
                     AND (b.goal_id IS NULL OR g.status='active')
                   ORDER BY b.priority DESC, b.next_run, b.id LIMIT 1""",
                (current_text,),
            ).fetchone()
            if row is None:
                return None
            key = f"proactive:{row['id']}:{row['next_run']}"
            proactive_prompt = self._proactive_prompt(row)
            specialist = specialist_for_prompt(proactive_prompt) or SPECIALIST_BY_KEY[
                "coding" if row["kind"] == "prototype" else "research"
            ]
            task_id, created = self._insert_task_locked(
                proactive_prompt, stamp=current_text, available_at=current_text,
                max_attempts=2, idempotency_key=key,
                requested_model=specialist.model_profile,
                specialist_key=specialist.key,
                delegated_by="jarvis",
            )
            self.db.execute(
                "UPDATE tasks SET goal_id=?, backlog_id=? WHERE id=?",
                (row["goal_id"], row["id"], task_id),
            )
            self.db.execute(
                """INSERT OR IGNORE INTO proactive_runs(
                       backlog_id, task_id, created_at, status
                   ) VALUES (?, ?, ?, 'queued')""",
                (row["id"], task_id, current_text),
            )
            self.db.execute(
                "UPDATE proactive_backlog SET next_run=?, updated_at=? WHERE id=?",
                ((current + timedelta(hours=int(row["interval_hours"]))).isoformat(),
                 current_text, row["id"]),
            )
            if created:
                self.db.execute(
                    """INSERT INTO activity_log(
                           created_at, category, action, status, task_id, details_json
                       ) VALUES (?, 'scheduler', 'idle_select', 'queued', ?, ?)""",
                    (current_text, task_id, json.dumps({
                        "backlog_id": row["id"], "kind": row["kind"],
                        "subject_id": row["subject_id"],
                    }, sort_keys=True)),
                )
            return task_id if created else None

    def _record_reflection_locked(
        self,
        *,
        stamp: str,
        status: str,
        summary: str,
        mistakes: str,
        improvements: str,
        task_id: int | None,
        conversation_id: int | None,
        prediction_id: int | None,
        tool_calls: int,
    ) -> int:
        """Persist reflection-linked terminal bookkeeping inside an active transaction."""
        status_text = _validated_nonsecret_metadata(status, "Reflection status")[:30]
        summary = redact_secrets(str(summary))
        mistakes = redact_secrets(str(mistakes))
        improvements = redact_secrets(str(improvements))
        tool_call_count = max(0, int(tool_calls))
        cur = self.db.execute(
            """INSERT INTO reflections(
                   created_at, task_id, conversation_id, prediction_id, status,
                   summary, mistakes, improvements, tool_calls
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                stamp, task_id, conversation_id, prediction_id, status_text, summary,
                mistakes, improvements, tool_call_count,
            ),
        )
        reflection_id = int(cur.lastrowid)
        if task_id is not None:
            task = self.db.execute(
                "SELECT goal_id, backlog_id, initiative_event_id FROM tasks WHERE id=?",
                (task_id,),
            ).fetchone()
            if task and task["backlog_id"] is not None:
                self.db.execute(
                    """UPDATE proactive_runs SET status=?, completed_at=?, result_summary=?
                       WHERE task_id=?""",
                    ("done" if status == "complete" else "failed", stamp, summary, task_id),
                )
            if task and task["initiative_event_id"] is not None:
                self.db.execute(
                    """UPDATE initiative_events
                       SET status=?, completed_at=?, result_summary=?
                       WHERE id=? AND task_id=?""",
                    (
                        "done" if status == "complete" else "failed",
                        stamp,
                        summary,
                        int(task["initiative_event_id"]),
                        int(task_id),
                    ),
                )
            goal_exists = (
                task is not None
                and task["goal_id"] is not None
                and self.db.execute(
                    "SELECT 1 FROM goals WHERE id=?", (task["goal_id"],)
                ).fetchone() is not None
            )
            if goal_exists:
                journal_text = summary
                if mistakes:
                    journal_text += f"\nMistakes/blockers: {mistakes}"
                if improvements:
                    journal_text += f"\nNext improvement: {improvements}"
                self.db.execute(
                    """INSERT INTO journal_entries(goal_id, created_at, kind, content, task_id)
                       VALUES (?, ?, 'reflection', ?, ?)""",
                    (task["goal_id"], stamp, journal_text[:20_000], task_id),
                )
                self.db.execute(
                    "UPDATE goals SET updated_at=? WHERE id=?",
                    (stamp, task["goal_id"]),
                )
        self.db.execute(
            """INSERT INTO activity_log(
                   created_at, category, action, status, task_id, details_json
               ) VALUES (?, 'reflection', 'review', ?, ?, ?)""",
            (
                stamp, status_text, task_id,
                json.dumps(
                    {"reflection_id": reflection_id, "tool_calls": tool_call_count},
                    sort_keys=True,
                ),
            ),
        )
        return reflection_id

    def record_reflection(
        self,
        *,
        status: str,
        summary: str,
        mistakes: str = "",
        improvements: str = "",
        task_id: int | None = None,
        conversation_id: int | None = None,
        prediction_id: int | None = None,
        tool_calls: int = 0,
    ) -> int:
        summary = _bounded_persisted_text(
            redact_secrets(str(summary).strip()), 4_000, "reflection summary"
        )
        mistakes = _bounded_persisted_text(
            redact_secrets(str(mistakes).strip()), 4_000, "reflection mistakes"
        )
        improvements = _bounded_persisted_text(
            redact_secrets(str(improvements).strip()), 4_000, "reflection improvements"
        )
        if not summary:
            raise ValueError("Reflection summary must not be empty")
        normalized_prediction: int | None = None
        if prediction_id is not None:
            normalized_prediction = self._prediction_optional_id(
                prediction_id, "prediction_id"
            )
            prediction = self.db.execute(
                """SELECT task_id, conversation_id, predicted_verification,
                          actual_status, actual_steps, evidence_ok, resolved_at
                   FROM task_predictions WHERE id=?""",
                (normalized_prediction,),
            ).fetchone()
            if prediction is None or prediction["resolved_at"] is None:
                raise ValueError("Reflection requires an already resolved prediction")
            if task_id is not None:
                if (
                    prediction["task_id"] is None
                    or int(prediction["task_id"]) != int(task_id)
                ):
                    raise ValueError("Reflection prediction task does not match")
            elif conversation_id is not None:
                if (
                    prediction["task_id"] is not None
                    or prediction["conversation_id"] is None
                    or int(prediction["conversation_id"]) != int(conversation_id)
                ):
                    raise ValueError("Reflection prediction conversation does not match")
            else:
                raise ValueError("Reflection prediction requires task or conversation context")
            if str(prediction["actual_status"]) != str(status):
                raise ValueError("Reflection status does not match its prediction")
            if (
                prediction["actual_steps"] is None
                or int(prediction["actual_steps"]) != max(0, int(tool_calls))
            ):
                raise ValueError("Reflection steps do not match its prediction")
            if str(status) == "complete" and (
                str(prediction["predicted_verification"]) != "not_applicable"
                and int(prediction["evidence_ok"] or 0) != 1
            ):
                raise ValueError("Complete reflection lacks required prediction evidence")
        stamp = now_iso()
        with self._immediate_transaction():
            reflection_id = self._record_reflection_locked(
                stamp=stamp,
                status=status,
                summary=summary,
                mistakes=mistakes,
                improvements=improvements,
                task_id=task_id,
                conversation_id=conversation_id,
                prediction_id=normalized_prediction,
                tool_calls=tool_calls,
            )
        family = self._prediction_family_for_context(task_id, conversation_id)
        if improvements and family is not None:
            lesson_content = self._canonical_reflection_lesson_content(
                family=family,
                outcome_status=status,
                summary=summary,
                mistakes=mistakes,
                improvements=improvements,
            )
            try:
                self.remember_verified_lesson(
                    str(lesson_content),
                    family=family,
                    outcome_status=status,
                    reflection_id=reflection_id,
                )
            except (RuntimeError, ValueError):
                # A reflection remains useful audit evidence, but a reusable
                # lesson must fail closed when its exact resolved prediction
                # cannot be proven. Never fall back to an unbound lesson row.
                pass
        return reflection_id

    def list_reflections(self, limit: int = 50) -> list[dict[str, Any]]:
        self._ensure_open()
        limit = _bounded_limit(limit, 1_000)
        rows = self.db.execute(
            """SELECT id, created_at, task_id, conversation_id, status, summary,
                      prediction_id, mistakes, improvements, tool_calls
               FROM reflections ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def record_repair_proposal(
        self,
        *,
        trigger: str,
        failing_tests: list[str],
        diff_text: str,
        verification: dict[str, Any],
        status: str,
        candidate_path: str,
        void_reason: str | None = None,
    ) -> int:
        if status not in {"proposed", "voided"}:
            raise ValueError("Repair drafts may only be proposed or voided")
        safe_trigger = _bounded_persisted_text(
            redact_secrets(str(trigger).strip()), 4_000, "repair trigger"
        )
        safe_diff = _bounded_persisted_text(
            redact_secrets(str(diff_text)), 200_000, "repair diff"
        )
        safe_reason = (
            _bounded_persisted_text(
                redact_secrets(str(void_reason)), 4_000, "repair void reason"
            )
            if void_reason else None
        )
        digest = hashlib.sha256(safe_diff.encode("utf-8")).hexdigest()
        with self._immediate_transaction():
            cur = self.db.execute(
                """INSERT INTO self_repair_proposals(
                       created_at, trigger_text, failing_tests_json, diff_text,
                       diff_sha256, verification_json, status, void_reason,
                       candidate_path
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    now_iso(), safe_trigger,
                    _redacted_json_text([str(item)[:1_000] for item in failing_tests[:100]]),
                    safe_diff, digest, _redacted_json_text(verification), status,
                    safe_reason, redact_secrets(str(candidate_path))[:4_000],
                ),
            )
            proposal_id = int(cur.lastrowid)
            self.db.execute(
                """INSERT INTO activity_log(
                       created_at, category, action, status, details_json
                   ) VALUES (?, 'self_repair', 'draft', ?, ?)""",
                (
                    now_iso(), status,
                    _redacted_json_text({"proposal_id": proposal_id, "diff_sha256": digest}),
                ),
            )
        return proposal_id

    def list_repair_proposals(self, limit: int = 50) -> list[dict[str, Any]]:
        limit = _bounded_limit(limit, 500)
        rows = self.db.execute(
            """SELECT id, created_at, trigger_text, failing_tests_json,
                      diff_sha256, verification_json, status, void_reason,
                      candidate_path
               FROM self_repair_proposals ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_repair_proposal(self, proposal_id: int) -> dict[str, Any] | None:
        normalized = self._prediction_optional_id(proposal_id, "proposal_id")
        row = self.db.execute(
            "SELECT * FROM self_repair_proposals WHERE id=?", (normalized,)
        ).fetchone()
        return dict(row) if row is not None else None

    def record_recovery_attestation(
        self,
        *,
        runtime_sha256: str,
        passed: bool,
        evidence: dict[str, Any],
    ) -> int:
        if re.fullmatch(r"[0-9a-f]{64}", str(runtime_sha256)) is None:
            raise ValueError("Recovery runtime hash must be lowercase SHA-256")
        with self._immediate_transaction():
            cur = self.db.execute(
                """INSERT INTO recovery_attestations(
                       created_at, runtime_sha256, schema_version, passed, evidence_json
                   ) VALUES (?, ?, ?, ?, ?)""",
                (
                    now_iso(), runtime_sha256, SCHEMA_VERSION, int(bool(passed)),
                    _redacted_json_text(evidence),
                ),
            )
        return int(cur.lastrowid)

    def latest_recovery_attestation(self) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT * FROM recovery_attestations ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row is not None else None

    def approve_work_domain(
        self,
        name: str,
        *,
        kind: str,
        project_id: int,
        max_tasks_per_day: int = 2,
    ) -> int:
        safe_name = _validated_nonsecret_metadata(name, "Domain name")
        if not safe_name or len(safe_name) > 200:
            raise ValueError("Domain name must contain 1-200 characters")
        if kind not in {"research", "workspace_project", "maintenance"}:
            raise ValueError("Domain kind must be research, workspace_project, or maintenance")
        normalized_project = self._project_id(project_id)
        project = self.get_project(normalized_project)
        if project is None or not bool(project["enabled"]):
            raise ValueError("Work domains require an enabled project")
        maximum = int(max_tasks_per_day)
        if isinstance(max_tasks_per_day, bool) or not 1 <= maximum <= 20:
            raise ValueError("max_tasks_per_day must be from 1 to 20")
        stamp = now_iso()
        with self._immediate_transaction():
            self.db.execute(
                """INSERT INTO work_domains(
                       created_at, updated_at, name, kind, project_id,
                       max_tasks_per_day, standing_authorization, enabled
                   ) VALUES (?, ?, ?, ?, ?, ?, 1, 1)
                   ON CONFLICT(name) DO UPDATE SET
                       updated_at=excluded.updated_at, kind=excluded.kind,
                       project_id=excluded.project_id,
                       max_tasks_per_day=excluded.max_tasks_per_day,
                       standing_authorization=1, enabled=1""",
                (stamp, stamp, safe_name, kind, normalized_project, maximum),
            )
            row = self.db.execute(
                "SELECT id FROM work_domains WHERE name=?", (safe_name,)
            ).fetchone()
        return int(row["id"])

    def list_work_domains(self) -> list[dict[str, Any]]:
        rows = self.db.execute(
            """SELECT d.id, d.created_at, d.updated_at, d.name, d.kind,
                      d.project_id, p.name AS project_name, d.max_tasks_per_day,
                      d.standing_authorization, d.enabled
               FROM work_domains d JOIN agent_projects p ON p.id=d.project_id
               ORDER BY d.enabled DESC, d.id"""
        ).fetchall()
        return [dict(row) for row in rows]

    def revoke_work_domain(self, domain_id: int) -> bool:
        normalized = self._prediction_optional_id(domain_id, "domain_id")
        if normalized is None:
            raise ValueError("domain_id is required")
        with self._immediate_transaction():
            updated = self.db.execute(
                """UPDATE work_domains SET enabled=0, standing_authorization=0,
                          updated_at=? WHERE id=?""",
                (now_iso(), normalized),
            )
        return updated.rowcount == 1

    def record_initiative_observation(
        self,
        *,
        signal_key: str,
        signal_kind: str,
        summary: str,
        evidence: dict[str, Any],
        project_id: int = 1,
    ) -> int | None:
        safe_key = _validated_nonsecret_metadata(signal_key, "Initiative signal key")
        safe_kind = _validated_nonsecret_metadata(signal_kind, "Initiative signal kind")
        if not safe_key or len(safe_key) > 500 or not safe_kind or len(safe_kind) > 100:
            raise ValueError("Initiative signal metadata is invalid")
        normalized_project = self._project_id(project_id)
        safe_summary = _bounded_persisted_text(
            redact_secrets(str(summary).strip()), 4_000, "initiative summary"
        )
        with self._immediate_transaction():
            cur = self.db.execute(
                """INSERT OR IGNORE INTO initiative_events(
                       created_at, signal_key, signal_kind, tier, domain_id,
                       project_id, summary, evidence_json, status
                   ) VALUES (?, ?, ?, 0, NULL, ?, ?, ?, 'observed')""",
                (
                    now_iso(), safe_key, safe_kind, normalized_project,
                    safe_summary, _redacted_json_text(evidence),
                ),
            )
        return int(cur.lastrowid) if cur.rowcount else None

    def schedule_domain_recovery(
        self,
        allowed_families: set[str],
        *,
        now: datetime | None = None,
    ) -> int | None:
        """Queue one traceable retry from a failed task in an approved project domain."""
        allowed = sorted(set(allowed_families) & self.PREDICTION_FAMILIES)
        if not allowed:
            return None
        current = _as_utc(now)
        stamp = current.isoformat()
        day_start = current.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        placeholders = ",".join("?" for _ in allowed)
        with self._immediate_transaction():
            control = self.db.execute(
                "SELECT state FROM runtime_control WHERE id=1"
            ).fetchone()
            if control is None or control["state"] != "running":
                return None
            if self.db.execute(
                "SELECT 1 FROM tasks WHERE status IN ('queued', 'running') LIMIT 1"
            ).fetchone() is not None:
                return None
            candidates = self.db.execute(
                f"""SELECT t.id AS source_task_id, t.prompt, t.last_error,
                           t.project_id, p.family, d.id AS domain_id,
                           d.name AS domain_name, d.max_tasks_per_day
                    FROM tasks t
                    JOIN work_domains d ON d.project_id=t.project_id
                    JOIN task_predictions p ON p.id=(
                        SELECT MAX(p2.id) FROM task_predictions p2
                        WHERE p2.task_id=t.id AND p2.resolved_at IS NOT NULL
                    )
                    WHERE t.status='failed' AND t.initiative_event_id IS NULL
                      AND d.enabled=1 AND d.standing_authorization=1
                      AND p.family IN ({placeholders})
                      AND NOT EXISTS (
                          SELECT 1 FROM initiative_events i
                          WHERE i.signal_key='failed_task:' || t.id
                      )
                    ORDER BY t.updated_at DESC, t.id DESC LIMIT 50""",
                allowed,
            ).fetchall()
            selected = None
            for candidate in candidates:
                used = self.db.execute(
                    """SELECT COUNT(*) FROM initiative_events
                       WHERE tier=1 AND domain_id=? AND created_at>=?""",
                    (candidate["domain_id"], day_start),
                ).fetchone()[0]
                if int(used) < int(candidate["max_tasks_per_day"]):
                    selected = candidate
                    break
            if selected is None:
                return None
            specialist = specialist_for_family(
                str(selected["family"]), str(selected["prompt"])
            )
            if specialist is None:
                return None
            signal_key = f"failed_task:{int(selected['source_task_id'])}"
            summary = (
                f"Approved domain {selected['domain_name']} has failed task "
                f"#{int(selected['source_task_id'])} in family {selected['family']}."
            )
            event_cursor = self.db.execute(
                """INSERT INTO initiative_events(
                       created_at, signal_key, signal_kind, tier, domain_id,
                       project_id, summary, evidence_json, status
                   ) VALUES (?, ?, 'failed_domain_task', 1, ?, ?, ?, ?, 'queued')""",
                (
                    stamp, signal_key, int(selected["domain_id"]),
                    int(selected["project_id"]), summary,
                    _redacted_json_text({
                        "source_task_id": int(selected["source_task_id"]),
                        "family": selected["family"],
                        "specialist_key": specialist.key,
                        "last_error": str(selected["last_error"] or "")[:2_000],
                    }),
                ),
            )
            event_id = int(event_cursor.lastrowid)
            prompt = (
                f"Bounded self-initiated recovery inside the explicitly approved domain "
                f"'{selected['domain_name']}'. A prior task failed. Inspect the current project, "
                "make only reversible in-project changes needed to address the observed failure, "
                "and verify with real test evidence. Do not publish, deploy, alter credentials, "
                "or touch the Jarvis runtime.\n"
                f"Prior task: {str(selected['prompt'])[:20_000]}\n"
                f"Observed failure: {str(selected['last_error'] or 'unspecified')[:4_000]}"
            )
            task_id, created = self._insert_task_locked(
                prompt,
                stamp=stamp,
                available_at=stamp,
                max_attempts=2,
                idempotency_key=f"initiative:{event_id}",
                project_id=int(selected["project_id"]),
                requested_model=specialist.model_profile,
                specialist_key=specialist.key,
                delegated_by="jarvis",
            )
            if not created:
                raise RuntimeError("Initiative task idempotency collision")
            self.db.execute(
                "UPDATE tasks SET initiative_event_id=? WHERE id=?",
                (event_id, task_id),
            )
            self.db.execute(
                "UPDATE initiative_events SET task_id=? WHERE id=?",
                (task_id, event_id),
            )
            self.db.execute(
                """INSERT INTO activity_log(
                       created_at, category, action, status, task_id, details_json
                   ) VALUES (?, 'initiative', 'domain_recovery', 'queued', ?, ?)""",
                (
                    stamp, task_id,
                    _redacted_json_text({
                        "event_id": event_id,
                        "domain_id": int(selected["domain_id"]),
                        "signal_key": signal_key,
                    }),
                ),
            )
        return task_id

    def list_initiative_events(
        self,
        *,
        since: datetime | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        bounded = _bounded_limit(limit, 2_000)
        where = "WHERE i.created_at>=?" if since is not None else ""
        parameters: tuple[Any, ...] = (_as_utc(since).isoformat(),) if since else ()
        rows = self.db.execute(
            f"""SELECT i.id, i.created_at, i.signal_key, i.signal_kind, i.tier,
                       i.domain_id, d.name AS domain_name, i.project_id,
                       i.summary, i.evidence_json, i.status, i.task_id,
                       i.completed_at, i.result_summary
                FROM initiative_events i
                LEFT JOIN work_domains d ON d.id=i.domain_id
                {where} ORDER BY i.id DESC LIMIT ?""",
            (*parameters, bounded),
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def approval_fingerprint(action: str, resource: str, scope: str) -> str:
        normalized = json.dumps(
            {"action": str(action), "resource": str(resource), "scope": str(scope)},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    @staticmethod
    def approval_effect_fingerprint(action: str, resource: str) -> str:
        """Bind a standing grant to one canonical action/resource effect."""
        raw_resource = str(resource).strip()
        try:
            parsed = json.loads(raw_resource)
        except (TypeError, ValueError, json.JSONDecodeError):
            canonical_resource = raw_resource
        else:
            canonical_resource = json.dumps(
                parsed,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        normalized = json.dumps(
            {"action": str(action), "resource": canonical_resource},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    @classmethod
    def session_approval_fingerprint(
        cls,
        action: str,
        resource: str,
        scope: str,
    ) -> str:
        normalized = json.dumps(
            {
                "effect": cls.approval_effect_fingerprint(action, resource),
                "grant_kind": "session",
                "scope": str(scope),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    @staticmethod
    def persistent_approval_eligible(
        action: str,
        resource: str,
        *,
        task_id: int | None = None,
    ) -> bool:
        """Allow standing grants only for exact foreground private-read effects."""
        if task_id is not None or str(action) != "access_private_files":
            return False
        try:
            parsed = json.loads(str(resource))
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
        if not isinstance(parsed, dict):
            return False
        tool = parsed.get("tool")
        arguments = parsed.get("arguments")
        digest = parsed.get("arguments_sha256")
        expected_keys = {
            "computer_list_files": {"path", "recursive", "resolved_path"},
            "computer_read_file": {
                "path", "start_line", "end_line", "resolved_path",
            },
            "computer_search_files": {"pattern", "path", "resolved_path"},
            "computer_storage_report": {"path", "limit", "resolved_path"},
        }
        return (
            tool in _PERSISTENT_READ_APPROVAL_TOOLS
            and isinstance(arguments, dict)
            and set(arguments) == expected_keys.get(tool)
            and isinstance(digest, str)
            and re.fullmatch(r"[0-9a-f]{64}", digest) is not None
        )

    @staticmethod
    def _validated_approval_scope(scope: str, task_id: int | None) -> str:
        normalized = str(scope).strip()
        if normalized == "foreground":
            if task_id is not None:
                raise ValueError("Foreground approval scope cannot carry a task id")
            return normalized
        request_match = re.fullmatch(r"request:[0-9a-f]{24}", normalized)
        if request_match is not None:
            if task_id is not None:
                raise ValueError("Request approval scope cannot carry a task id")
            return normalized
        match = re.fullmatch(r"(task|conversation):([1-9][0-9]*)", normalized)
        if match is None:
            raise ValueError(
                "Approval scope must be foreground, request:<digest>, task:<id>, or conversation:<id>"
            )
        if match.group(1) == "task":
            scoped_task_id = int(match.group(2))
            if task_id != scoped_task_id:
                raise ValueError("Task approval scope must exactly match task_id")
        elif task_id is not None:
            raise ValueError("Conversation approval scope cannot carry a task id")
        return normalized

    def _link_pending_approval_locked(
        self,
        task_id: int | None,
        approval_id: int,
        stamp: str,
    ) -> None:
        """Bind a pending request to its currently running background task."""
        if task_id is None:
            return
        self.db.execute(
            """UPDATE tasks SET awaiting_approval_id=?, updated_at=?
               WHERE id=? AND status='running'""",
            (int(approval_id), stamp, int(task_id)),
        )

    def authorize_or_request(
        self,
        action: str,
        resource: str,
        reason: str,
        *,
        approval_scope: str,
        task_id: int | None = None,
        display_resource: str | None = None,
    ) -> tuple[bool, int]:
        action = _validated_nonsecret_metadata(action, "Approval action")[:100]
        exact_resource = str(resource).strip()
        presented_resource = (
            exact_resource if display_resource is None else str(display_resource).strip()
        )
        if display_resource is not None and presented_resource != exact_resource:
            try:
                exact_payload = json.loads(exact_resource)
                display_payload = json.loads(presented_resource)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError(
                    "Custom approval display resource must be valid JSON"
                ) from exc
            if (
                not isinstance(exact_payload, dict)
                or not isinstance(display_payload, dict)
                or exact_payload.get("tool") != "install_project_dependencies"
                or display_payload.get("tool") != "install_project_dependencies"
                or exact_payload.get("arguments_sha256")
                != display_payload.get("arguments_sha256")
            ):
                raise ValueError(
                    "Custom approval display is not bound to the exact dependency resource"
                )
            if len(presented_resource) > 1_900:
                raise ValueError("Dependency approval display resource is too large")
        approval_tool = ""
        try:
            parsed_resource = json.loads(presented_resource)
        except (TypeError, ValueError, json.JSONDecodeError):
            persisted_resource = redact_secrets(presented_resource)
        else:
            if isinstance(parsed_resource, dict):
                approval_tool = str(parsed_resource.get("tool") or "").casefold()
            persisted_resource = _redacted_json_text(parsed_resource)
        if action == "control_desktop_application" and approval_tool == "desktop_interact":
            if len(persisted_resource) > 32_000:
                raise ValueError("Desktop interaction approval resource is too large")
            resource = persisted_resource
        else:
            resource = _bounded_persisted_text(
                persisted_resource, 2_000, "approval resource"
            )
        reason = _bounded_persisted_text(
            redact_secrets(str(reason).strip()), 1_000, "approval reason"
        )
        if task_id is not None:
            task_id = int(task_id)
            if task_id <= 0:
                raise ValueError("task_id must be positive")
        scope = self._validated_approval_scope(approval_scope, task_id)
        fingerprint = self.approval_fingerprint(action, exact_resource, scope)
        effect_fingerprint = self.approval_effect_fingerprint(action, resource)
        session_fingerprint = self.session_approval_fingerprint(
            action, resource, scope
        )
        stamp = now_iso()
        with self._immediate_transaction():
            if self.persistent_approval_eligible(
                action, resource, task_id=task_id
            ):
                grant = self.db.execute(
                    """SELECT id FROM persistent_approval_grants
                       WHERE effect_fingerprint=? AND action=?
                         AND grant_kind='always' AND revoked_at IS NULL
                       ORDER BY id DESC LIMIT 1""",
                    (effect_fingerprint, action),
                ).fetchone()
                if grant is not None:
                    grant_id = int(grant["id"])
                    self.db.execute(
                        """INSERT INTO activity_log(
                               created_at, category, action, status, task_id, details_json
                           ) VALUES (?, 'approval', ?, 'persistent_authorized', NULL, ?)""",
                        (
                            stamp,
                            action,
                            json.dumps(
                                {"grant_id": grant_id, "scope": scope},
                                sort_keys=True,
                            ),
                        ),
                    )
                    return True, grant_id
                session_grant = self.db.execute(
                    """SELECT id FROM persistent_approval_grants
                       WHERE effect_fingerprint=? AND action=?
                         AND grant_kind='session' AND scope=?
                         AND revoked_at IS NULL AND expires_at>?
                       ORDER BY id DESC LIMIT 1""",
                    (session_fingerprint, action, scope, stamp),
                ).fetchone()
                if session_grant is not None:
                    grant_id = int(session_grant["id"])
                    self.db.execute(
                        """INSERT INTO activity_log(
                               created_at, category, action, status, task_id, details_json
                           ) VALUES (?, 'approval', ?, 'session_authorized', NULL, ?)""",
                        (
                            stamp,
                            action,
                            json.dumps(
                                {"grant_id": grant_id, "scope": scope},
                                sort_keys=True,
                            ),
                        ),
                    )
                    return True, grant_id
            rows = self.db.execute(
                """SELECT id, status, expires_at, task_id, scope FROM approvals
                   WHERE fingerprint=? AND scope=?
                     AND ((task_id=? ) OR (task_id IS NULL AND ? IS NULL))
                   ORDER BY id DESC LIMIT 5""",
                (fingerprint, scope, task_id, task_id),
            ).fetchall()
            for row in rows:
                if row["status"] == "approved" and (
                    row["expires_at"] is None or row["expires_at"] > stamp
                ):
                    self.db.execute(
                        "UPDATE approvals SET status='consumed', updated_at=? WHERE id=?",
                        (stamp, row["id"]),
                    )
                    self.db.execute(
                        """INSERT INTO activity_log(
                               created_at, category, action, status, task_id, details_json
                           ) VALUES (?, 'approval', ?, 'consumed', ?, ?)""",
                        (
                            stamp,
                            action,
                            task_id,
                            json.dumps(
                                {"approval_id": int(row["id"]), "scope": scope},
                                sort_keys=True,
                            ),
                        ),
                    )
                    if task_id is not None:
                        self.db.execute(
                            """UPDATE tasks SET awaiting_approval_id=NULL, updated_at=?
                               WHERE id=? AND awaiting_approval_id=?""",
                            (stamp, task_id, int(row["id"])),
                        )
                    return True, int(row["id"])
                if row["status"] == "approved" and row["expires_at"] and row["expires_at"] <= stamp:
                    self.db.execute(
                        "UPDATE approvals SET status='expired', updated_at=? WHERE id=?",
                        (stamp, row["id"]),
                    )
                if row["status"] == "pending":
                    self._link_pending_approval_locked(
                        task_id, int(row["id"]), stamp
                    )
                    return False, int(row["id"])
            cur = self.db.execute(
                """INSERT INTO approvals(
                       created_at, updated_at, fingerprint, action, resource, reason,
                       status, task_id, scope
                   ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
                (stamp, stamp, fingerprint, action, resource, reason, task_id, scope),
            )
            approval_id = int(cur.lastrowid)
            self._link_pending_approval_locked(task_id, approval_id, stamp)
            self.db.execute(
                """INSERT INTO activity_log(
                       created_at, category, action, status, task_id, details_json
                   ) VALUES (?, 'approval', ?, 'pending', ?, ?)""",
                (
                    stamp,
                    action,
                    task_id,
                    json.dumps({"approval_id": approval_id, "scope": scope}, sort_keys=True),
                ),
            )
        return False, approval_id

    def decide_approval_always(self, approval_id: int) -> int | None:
        """Create or reactivate one reversible exact-effect read-only grant."""
        stamp_dt = _as_utc()
        stamp = stamp_dt.isoformat()
        with self._immediate_transaction():
            row = self.db.execute(
                """SELECT id, action, resource, reason, task_id, scope
                   FROM approvals WHERE id=? AND status='pending'""",
                (int(approval_id),),
            ).fetchone()
            if row is None or not self.persistent_approval_eligible(
                str(row["action"]), str(row["resource"]), task_id=row["task_id"]
            ):
                return None
            effect_fingerprint = self.approval_effect_fingerprint(
                str(row["action"]), str(row["resource"])
            )
            self.db.execute(
                """INSERT INTO persistent_approval_grants(
                       created_at, updated_at, effect_fingerprint, action, resource,
                       reason, source_approval_id, revoked_at, grant_kind, scope,
                       expires_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 'always', NULL, NULL)
                   ON CONFLICT(effect_fingerprint) DO UPDATE SET
                       updated_at=excluded.updated_at,
                       action=excluded.action,
                       resource=excluded.resource,
                       reason=excluded.reason,
                       source_approval_id=excluded.source_approval_id,
                       grant_kind='always',
                       scope=NULL,
                       expires_at=NULL,
                       revoked_at=NULL""",
                (
                    stamp, stamp, effect_fingerprint, str(row["action"]),
                    str(row["resource"]), str(row["reason"]), int(approval_id),
                ),
            )
            grant = self.db.execute(
                """SELECT id FROM persistent_approval_grants
                   WHERE effect_fingerprint=? AND revoked_at IS NULL""",
                (effect_fingerprint,),
            ).fetchone()
            if grant is None:
                raise RuntimeError("Persistent approval grant was not created")
            grant_id = int(grant["id"])
            updated = self.db.execute(
                """UPDATE approvals
                   SET status='consumed', updated_at=?, decided_at=?, expires_at=NULL
                   WHERE id=? AND status='pending'""",
                (stamp, stamp, int(approval_id)),
            )
            if updated.rowcount != 1:
                raise RuntimeError("Approval changed while creating persistent grant")
            self.db.execute(
                """INSERT INTO activity_log(
                       created_at, category, action, status, task_id, details_json
                   ) VALUES (?, 'approval', 'decision', 'approved_always', NULL, ?)""",
                (
                    stamp,
                    json.dumps(
                        {
                            "approval_id": int(approval_id),
                            "grant_id": grant_id,
                            "scope": str(row["scope"]),
                        },
                        sort_keys=True,
                    ),
                ),
            )
        return grant_id

    def decide_approval_for_session(
        self,
        approval_id: int,
        *,
        ttl_hours: int = 24,
    ) -> int | None:
        """Allow one exact read-only effect within one conversation for a bounded TTL."""
        stamp_dt = _as_utc()
        stamp = stamp_dt.isoformat()
        ttl_hours = max(1, min(int(ttl_hours), 168))
        expires = (stamp_dt + timedelta(hours=ttl_hours)).isoformat()
        with self._immediate_transaction():
            row = self.db.execute(
                """SELECT id, action, resource, reason, task_id, scope
                   FROM approvals WHERE id=? AND status='pending'""",
                (int(approval_id),),
            ).fetchone()
            if row is None or not self.persistent_approval_eligible(
                str(row["action"]), str(row["resource"]), task_id=row["task_id"]
            ):
                return None
            scope = str(row["scope"])
            if re.fullmatch(r"conversation:[1-9][0-9]{0,18}", scope) is None:
                return None
            effect_fingerprint = self.session_approval_fingerprint(
                str(row["action"]), str(row["resource"]), scope
            )
            self.db.execute(
                """INSERT INTO persistent_approval_grants(
                       created_at, updated_at, effect_fingerprint, action, resource,
                       reason, source_approval_id, revoked_at, grant_kind, scope,
                       expires_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 'session', ?, ?)
                   ON CONFLICT(effect_fingerprint) DO UPDATE SET
                       updated_at=excluded.updated_at,
                       action=excluded.action,
                       resource=excluded.resource,
                       reason=excluded.reason,
                       source_approval_id=excluded.source_approval_id,
                       grant_kind='session',
                       scope=excluded.scope,
                       expires_at=excluded.expires_at,
                       revoked_at=NULL""",
                (
                    stamp, stamp, effect_fingerprint, str(row["action"]),
                    str(row["resource"]), str(row["reason"]), int(approval_id),
                    scope, expires,
                ),
            )
            grant = self.db.execute(
                """SELECT id FROM persistent_approval_grants
                   WHERE effect_fingerprint=? AND grant_kind='session'
                     AND scope=? AND revoked_at IS NULL""",
                (effect_fingerprint, scope),
            ).fetchone()
            if grant is None:
                raise RuntimeError("Session approval grant was not created")
            grant_id = int(grant["id"])
            updated = self.db.execute(
                """UPDATE approvals
                   SET status='consumed', updated_at=?, decided_at=?, expires_at=NULL
                   WHERE id=? AND status='pending'""",
                (stamp, stamp, int(approval_id)),
            )
            if updated.rowcount != 1:
                raise RuntimeError("Approval changed while creating session grant")
            self.db.execute(
                """INSERT INTO activity_log(
                       created_at, category, action, status, task_id, details_json
                   ) VALUES (?, 'approval', 'decision', 'approved_session', NULL, ?)""",
                (
                    stamp,
                    json.dumps(
                        {
                            "approval_id": int(approval_id),
                            "expires_at": expires,
                            "grant_id": grant_id,
                            "scope": scope,
                        },
                        sort_keys=True,
                    ),
                ),
            )
        return grant_id

    def list_persistent_approvals(
        self,
        limit: int = 100,
        *,
        include_revoked: bool = True,
    ) -> list[dict[str, Any]]:
        self._ensure_open()
        limit = _bounded_limit(limit, 1_000)
        if include_revoked:
            rows = self.db.execute(
                """SELECT id, created_at, updated_at, action, resource, reason,
                          source_approval_id, revoked_at, grant_kind, scope, expires_at
                   FROM persistent_approval_grants
                   ORDER BY id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        else:
            rows = self.db.execute(
                """SELECT id, created_at, updated_at, action, resource, reason,
                          source_approval_id, revoked_at, grant_kind, scope, expires_at
                   FROM persistent_approval_grants
                   WHERE revoked_at IS NULL
                     AND (expires_at IS NULL OR expires_at>?)
                   ORDER BY id DESC LIMIT ?""",
                (now_iso(), limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def revoke_persistent_approval(self, grant_id: int) -> bool:
        stamp = now_iso()
        with self._immediate_transaction():
            updated = self.db.execute(
                """UPDATE persistent_approval_grants
                   SET revoked_at=?, updated_at=?
                   WHERE id=? AND revoked_at IS NULL""",
                (stamp, stamp, int(grant_id)),
            )
            if updated.rowcount == 1:
                self.db.execute(
                    """INSERT INTO activity_log(
                           created_at, category, action, status, task_id, details_json
                       ) VALUES (?, 'approval', 'persistent_grant', 'revoked', NULL, ?)""",
                    (
                        stamp,
                        json.dumps({"grant_id": int(grant_id)}, sort_keys=True),
                    ),
                )
        return updated.rowcount == 1

    def decide_approval(self, approval_id: int, approve: bool, *, ttl_hours: int = 24) -> bool:
        stamp_dt = _as_utc()
        stamp = stamp_dt.isoformat()
        ttl_hours = max(1, min(int(ttl_hours), 720))
        status = "approved" if approve else "denied"
        expires = (stamp_dt + timedelta(hours=ttl_hours)).isoformat() if approve else None
        with self._immediate_transaction():
            row = self.db.execute(
                "SELECT task_id, scope FROM approvals WHERE id=? AND status='pending'",
                (int(approval_id),),
            ).fetchone()
            if row is None:
                return False
            updated = self.db.execute(
                """UPDATE approvals SET status=?, updated_at=?, decided_at=?, expires_at=?
                   WHERE id=? AND status='pending'""",
                (status, stamp, stamp, expires, int(approval_id)),
            )
            if updated.rowcount:
                if row["task_id"] is not None:
                    if approve:
                        self.db.execute(
                            """UPDATE tasks
                               SET status='queued', updated_at=?, result=NULL, last_error=NULL,
                                   available_at=?, lease_owner=NULL, lease_expires_at=NULL,
                                   awaiting_approval_id=NULL
                               WHERE id=? AND status='awaiting_approval'
                                 AND awaiting_approval_id=?""",
                            (stamp, stamp, row["task_id"], int(approval_id)),
                        )
                    else:
                        denial = f"Approval #{int(approval_id)} was denied"
                        terminalized = self.db.execute(
                            """UPDATE tasks
                               SET status='failed', updated_at=?, result=?, last_error=?,
                                   lease_owner=NULL, lease_expires_at=NULL,
                                   awaiting_approval_id=NULL
                               WHERE id=?
                                 AND status IN ('running', 'queued', 'awaiting_approval')
                                 AND awaiting_approval_id=?""",
                            (stamp, denial, denial, row["task_id"], int(approval_id)),
                        )
                        if terminalized.rowcount == 1:
                            specialist_row = self.db.execute(
                                "SELECT specialist_key FROM tasks WHERE id=?",
                                (int(row["task_id"]),),
                            ).fetchone()
                            if (
                                specialist_row is not None
                                and specialist_row["specialist_key"] is not None
                            ):
                                self.db.execute(
                                    """UPDATE specialist_agents
                                       SET status='ready', active_task_id=NULL,
                                           failed_tasks=failed_tasks+1,
                                           last_reported_at=?, updated_at=?
                                       WHERE agent_key=? AND active_task_id=?""",
                                    (
                                        stamp, stamp,
                                        str(specialist_row["specialist_key"]),
                                        int(row["task_id"]),
                                    ),
                                )
                            self._record_reflection_locked(
                                stamp=stamp,
                                status="failed",
                                summary=denial,
                                mistakes="The required sensitive action was denied by the operator.",
                                improvements="",
                                task_id=int(row["task_id"]),
                                conversation_id=None,
                                prediction_id=None,
                                tool_calls=0,
                            )
                self.db.execute(
                    """INSERT INTO activity_log(
                           created_at, category, action, status, task_id, details_json
                       ) VALUES (?, 'approval', 'decision', ?, ?, ?)""",
                    (
                        stamp,
                        status,
                        row["task_id"],
                        json.dumps(
                            {"approval_id": int(approval_id), "scope": row["scope"]},
                            sort_keys=True,
                        ),
                    ),
                )
        return updated.rowcount == 1

    def list_approvals(self, limit: int = 100) -> list[dict[str, Any]]:
        self._ensure_open()
        limit = _bounded_limit(limit, 1_000)
        rows = self.db.execute(
            """SELECT id, created_at, updated_at, action, resource, reason, status,
                       expires_at, decided_at, task_id, scope
               FROM approvals ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        items = [dict(row) for row in rows]
        for item in items:
            item["persistent_eligible"] = self.persistent_approval_eligible(
                str(item["action"]), str(item["resource"]), task_id=item["task_id"]
            )
        return items

    def get_approval(self, approval_id: Any) -> dict[str, Any] | None:
        """Return one exact approval row without relying on a bounded list scan."""
        self._ensure_open()
        if isinstance(approval_id, bool):
            return None
        if isinstance(approval_id, int):
            normalized_id = approval_id
        elif isinstance(approval_id, str) and re.fullmatch(
            r"[1-9][0-9]{0,18}", approval_id
        ):
            normalized_id = int(approval_id)
        else:
            return None
        if normalized_id <= 0 or normalized_id > 9_223_372_036_854_775_807:
            return None
        row = self.db.execute(
            """SELECT id, created_at, updated_at, action, resource, reason, status,
                      expires_at, decided_at, task_id, scope
               FROM approvals WHERE id=?""",
            (normalized_id,),
        ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["persistent_eligible"] = self.persistent_approval_eligible(
            str(item["action"]), str(item["resource"]), task_id=item["task_id"]
        )
        return item

    def save_self_snapshot(self, snapshot: dict[str, Any]) -> int:
        payload = _redacted_json_text(snapshot)
        payload = _bounded_persisted_text(payload, 100_000, "self snapshot")
        with self._immediate_transaction():
            cur = self.db.execute(
                "INSERT INTO self_snapshots(created_at, snapshot_json) VALUES (?, ?)",
                (now_iso(), payload),
            )
            # Keep snapshots useful but bounded; activity history remains separate.
            self.db.execute(
                "DELETE FROM self_snapshots WHERE id NOT IN "
                "(SELECT id FROM self_snapshots ORDER BY id DESC LIMIT 100)"
            )
        return int(cur.lastrowid)

    def operational_summary(self) -> dict[str, Any]:
        self._ensure_open()
        task_counts = {
            str(row["status"]): int(row["count"])
            for row in self.db.execute(
                "SELECT status, COUNT(*) AS count FROM tasks GROUP BY status"
            ).fetchall()
        }
        errors = [dict(row) for row in self.db.execute(
            """SELECT id, updated_at, last_error FROM tasks
               WHERE last_error IS NOT NULL ORDER BY updated_at DESC LIMIT 10"""
        ).fetchall()]
        return {
            "control": self.control_state(),
            "task_counts": task_counts,
            "active_tasks": [dict(row) for row in self.db.execute(
                """SELECT id, status, updated_at, goal_id, backlog_id, specialist_key
                   FROM tasks WHERE status IN ('queued', 'running') ORDER BY id LIMIT 100"""
            ).fetchall()],
            "specialists": self.list_specialist_agents(),
            "recent_errors": errors,
            "goals": self.list_goals(limit=100),
            "preferences": self.list_preferences(),
            "backlog": self.list_backlog(),
            "pending_approvals": [item for item in self.list_approvals(limit=100) if item["status"] == "pending"],
            "reflection_count": int(self.db.execute("SELECT COUNT(*) FROM reflections").fetchone()[0]),
            "memory_count": int(self.db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]),
            "competence": self.competence(),
            "calibration": self.calibration(),
            "open_prediction_count": self.open_prediction_count(),
        }
