"""Current Memory domain extracted without changing method control flow.

Global dependencies resolve through jarvis.memory at call time. The facade keeps
public imports, patch seams, constants and authorization helpers authoritative.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any
import sqlite3
from .memory_runtime import (_memory, _with_read_snapshot)


class ConversationsMemoryMixin:
    """Mechanically extracted current Memory methods."""

    def new_conversation(
        self,
        title: str = "Conversation",
        *,
        project_id: int | None = None,
    ) -> int:
        safe_title = _memory().redact_secrets(str(title))[:120]
        normalized_project = self._project_id(project_id)
        project = self.get_project(normalized_project)
        if project is None or not bool(project["enabled"]):
            raise ValueError(f"Project #{normalized_project} does not exist or is disabled")
        with self._immediate_transaction():
            cur = self.db.execute(
                "INSERT INTO conversations(created_at, title, project_id) VALUES (?, ?, ?)",
                (_memory().now_iso(), safe_title, normalized_project),
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
                (normalized_id, _memory().now_iso()),
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
        limit = _memory()._bounded_limit(limit, 200)
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
        self._recall_cache.clear()
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

            stamp = _memory().now_iso()
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
            messages_removed = int(
                self.db.execute(
                    "SELECT COUNT(*) FROM messages WHERE conversation_id=?",
                    (conversation_id,),
                ).fetchone()[0]
            )
            _memory().memory_spine.fts_secure_delete(self.db, "message_fts")
            # The compaction records go with the transcript they replaced, and
            # they go BEFORE it: the span table is the only copy of those rows,
            # so leaving it behind would turn a deleted conversation into an
            # unreachable one rather than an erased one (design 2.10 item 1).
            # Child first -- ``memory_compacted_spans`` holds the foreign key.
            milestones_removed = 0
            spans_removed = 0
            if self._compaction_ready:
                spans_removed = int(self.db.execute(
                    "DELETE FROM memory_compacted_spans WHERE conversation_id=?",
                    (conversation_id,),
                ).rowcount or 0)
                milestones_removed = int(self.db.execute(
                    "DELETE FROM memory_milestones WHERE conversation_id=?",
                    (conversation_id,),
                ).rowcount or 0)
            for table in (
                "training_examples",
                "conversation_goals",
                "presence_jobs",
                "memory_fact_proposals",
                "messages",
            ):
                self.db.execute(
                    f"DELETE FROM {table} WHERE conversation_id=?",
                    (conversation_id,),
                )
            if self._spine_ready:
                _memory().memory_spine.append_event(
                    self.db,
                    self._spine_key,
                    kind="conversation.deleted",
                    actor="operator",
                    source="conversation deletion",
                    scope=f"project:{int(row['project_id'])}" if row["project_id"] else "global",
                    permission="operator",
                    outcome="applied",
                    payload={
                        "messages_removed": messages_removed,
                        "milestones_removed": milestones_removed,
                        "spans_removed": spans_removed,
                    },
                    now=stamp,
                    conversation_id=int(conversation_id),
                    subject_kind="conversation",
                    subject_id=int(conversation_id),
                )
            deleted = self.db.execute(
                "DELETE FROM conversations WHERE id=?",
                (conversation_id,),
            )
            if deleted.rowcount != 1:
                raise RuntimeError("Conversation changed while it was being deleted")
        return dict(row)

    def add_message(self, conversation_id: int, role: str, content: str) -> int:
        """Persist one transcript row and return its id."""
        if role not in {"user", "assistant"}:
            raise ValueError("Persisted message role must be user or assistant")
        content = _memory().redact_secrets(str(content))
        if len(content) > 100_000:
            content = content[:99_950] + "\n...[message clipped before persistence]"
        with self._immediate_transaction():
            cursor = self.db.execute(
                "INSERT INTO messages(conversation_id, created_at, role, content) VALUES (?, ?, ?, ?)",
                (conversation_id, _memory().now_iso(), role, content),
            )
            message_id = int(cursor.lastrowid)
            if role == "user":
                postal = _memory()._EXPLICIT_USER_POSTAL_CODE.search(content)
                if postal is not None:
                    self._set_preference_locked(
                        "location.postal_code",
                        postal.group(1),
                        source="explicit user profile statement",
                        authority="operator",
                        confidence=1.0,
                        stamp=_memory().now_iso(),
                        actor="runtime",
                        conversation_id=int(conversation_id),
                        permission="runtime:transcript",
                    )
        return message_id

    def recent_messages(self, conversation_id: int, limit: int = 24) -> list[dict[str, str]]:
        self._ensure_open()
        limit = _memory()._bounded_limit(limit, 1_000)
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
        raw = _memory().json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        safe = _memory().redact_secrets(raw)
        if len(safe) > 12_000:
            raise ValueError("Conversation goal contract exceeds 12,000 characters")
        try:
            decoded = _memory().json.loads(safe)
        except _memory().json.JSONDecodeError as exc:
            raise ValueError("Conversation goal contract is not valid JSON") from exc
        if not isinstance(decoded, dict):
            raise ValueError("Conversation goal contract must be an object")
        return _memory().json.dumps(
            decoded, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )

    @staticmethod
    def _decode_conversation_contract(raw: Any) -> dict[str, Any] | None:
        try:
            decoded = _memory().json.loads(str(raw or "{}"))
        except (TypeError, ValueError, _memory().json.JSONDecodeError):
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
        safe_goal = _memory().redact_secrets(str(goal_text).strip())
        if not safe_goal:
            raise ValueError("Conversation goal must not be empty")
        safe_goal = _memory()._bounded_persisted_text(safe_goal, 20_000, "conversation goal")
        contract_json = self._conversation_contract_json(contract)
        stamp = _memory().now_iso()
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
            decoded = _memory().json.loads(str(result.get("context_json") or "[]"))
        except (TypeError, ValueError, _memory().json.JSONDecodeError):
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
        safe_update = _memory().redact_secrets(str(operator_update).strip())
        if not safe_update:
            raise ValueError("Conversation-goal update must not be empty")
        safe_update = _memory()._bounded_persisted_text(
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
                decoded = _memory().json.loads(str(row["context_json"] or "[]"))
            except (TypeError, ValueError, _memory().json.JSONDecodeError):
                decoded = []
            context = [str(item) for item in decoded if isinstance(item, str)][-11:]
            context.append(safe_update)
            self.db.execute(
                """UPDATE conversation_goals
                   SET state='active', updated_at=?, context_json=?, retryable=0,
                       resume_count=resume_count+1
                   WHERE id=?""",
                (
                    _memory().now_iso(),
                    _memory().json.dumps(context, ensure_ascii=False, separators=(",", ":")),
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
                (contract_json, _memory().now_iso(), int(goal_id), int(conversation_id)),
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
        if normalized_state not in {"incomplete", "complete", "cancelled"}:
            raise ValueError("Unknown conversation-goal state")
        summary = None
        if result_summary is not None:
            summary = _memory()._bounded_persisted_text(
                _memory().redact_secrets(str(result_summary).strip()),
                4_000,
                "conversation-goal result",
            )
        with self._immediate_transaction():
            current = self.db.execute(
                "SELECT state, retryable FROM conversation_goals WHERE id=?",
                (int(goal_id),),
            ).fetchone()
            if current is None:
                raise ValueError("Conversation goal does not exist")
            current_state = str(current["state"])
            resumable = current_state == "active" or (
                current_state == "incomplete" and bool(current["retryable"])
            )
            if not resumable:
                # Repeating the exact terminal outcome is harmless and must not
                # rewrite its evidence; every other terminal transition fails.
                if current_state == normalized_state:
                    return
                raise ValueError("Conversation goal is already terminal")
            cursor = self.db.execute(
                """UPDATE conversation_goals
                   SET state=?, updated_at=?, last_result_summary=?, retryable=?
                   WHERE id=? AND (
                       state='active' OR (state='incomplete' AND retryable=1)
                   )""",
                (
                    normalized_state, _memory().now_iso(), summary,
                    int(bool(retryable and normalized_state == "incomplete")),
                    int(goal_id),
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("Conversation goal changed before it was finished")

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
        expected = _memory()._validated_nonsecret_metadata(
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
            summary = _memory()._bounded_persisted_text(
                _memory().redact_secrets(str(result_summary).strip()),
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
                    _memory().now_iso(),
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
        bounded = _memory()._bounded_limit(limit, 100)
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

    @_with_read_snapshot
    def conversation_scoped_memory_messages(
        self,
        conversation_id: int,
        limit: int = 32,
    ) -> list[dict[str, str]]:
        """Return explicit chat/session memory statements independent of recency."""
        self._ensure_open()
        limit = _memory()._bounded_limit(limit, 64)
        if not limit:
            return []
        scope = _memory().re.compile(
            r"\b(?:for|in|during)\s+(?:this|our|the)\s+"
            r"(?:conversation|chat|session|thread)\b",
            _memory().re.I,
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
        if _memory().contains_secret(query):
            raise ValueError("Potential secret detected; session search refused")
        if len(query) > _memory().MAX_SEARCH_QUERY_CHARS:
            raise ValueError(
                f"Session search query exceeds {_memory().MAX_SEARCH_QUERY_CHARS} characters"
            )
        limit = _memory()._bounded_limit(limit, 50)
        query_terms = _memory()._memory_query_terms(query)
        like_terms = _memory()._memory_like_terms(query, query_terms)
        if not query_terms or not like_terms or not limit:
            return []
        fts_query = _memory()._memory_fts_query(query, query_terms)
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
            patterns = [f"%{_memory()._escape_like(term)}%" for term in like_terms]
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

    @_with_read_snapshot
    def prior_conversation_excerpts(
        self,
        query: str,
        *,
        exclude_conversation_id: int | None = None,
        project_id: int | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Bounded excerpts from OTHER conversations' transcript rows.

        The transcript recall channel of the automatic read path.  Everything
        is deterministic SQL and Python in one deferred snapshot: no model
        call, no write lock.  The query screens are the claims lane's; the
        discovery is the staged narrowing of ``_lexical_recall_candidates``
        (OR of every term, then only the terms that discriminate inside the
        visible scope, then every term required), counted over the same
        visible scope and with the unknown-identity floor for structured
        identifiers; the order within the pool is FTS5's rank; the whole call
        shares one deadline (``TRANSCRIPT_RECALL_TIME_BUDGET_MS`` warm,
        ``TRANSCRIPT_RECALL_COLD_TIME_BUDGET_MS`` on the first read of a store
        object), checked between stages and after every screened row, and on
        expiry what was screened so far is returned with mode
        ``budget-exceeded``.  Every excerpt passes the widened private-
        identifier and secret screen before it is returned.

        ``exclude_conversation_id`` is the current conversation and is never
        read.  A governed command or receipt row is skipped: those facts are
        the claims lane's, which outranks this channel.  Returns at most
        ``TRANSCRIPT_RECALL_EXCERPT_CAP`` rows, oldest first, each carrying
        ``message_id``, ``conversation_id``, ``title``, ``created_at``,
        ``role`` and ``excerpt``; the diagnostic record is
        ``transcript_recall_report()``.
        """
        started = _memory().time.monotonic()
        budget_ms = (
            float(_memory().TRANSCRIPT_RECALL_TIME_BUDGET_MS)
            if self._transcript_recall_warm
            else float(_memory().TRANSCRIPT_RECALL_COLD_TIME_BUDGET_MS)
        )
        self._transcript_recall_warm = True
        deadline = started + budget_ms / 1000.0
        report = _memory()._blank_transcript_recall_report("idle", budget_ms=budget_ms)
        self._last_transcript_recall_report = report

        def finish(
            mode: str,
            *,
            reason: str | None = None,
            budget: str | None = None,
            returned: int = 0,
        ) -> None:
            report["mode"] = str(mode)
            report["reason"] = reason
            report["budget"] = budget
            report["returned"] = int(returned)
            report["abstained"] = str(mode) in _memory()._TRANSCRIPT_RECALL_ABSTAINING_MODES
            report["elapsed_ms"] = round((_memory().time.monotonic() - started) * 1000.0, 3)

        def past_deadline() -> bool:
            return _memory().time.monotonic() >= deadline

        query = str(query)
        if len(query) > _memory().MAX_SEARCH_QUERY_CHARS:
            raise ValueError(
                f"Transcript recall query exceeds {_memory().MAX_SEARCH_QUERY_CHARS} characters"
            )
        if (
            _memory().contains_secret(query)
            or _memory().contains_private_identifier(query)
            or _memory()._memory_query_targets_authority_evasion(query)
        ):
            finish("screened", reason="query refused by the claims-lane screens")
            return []
        cap = _memory()._bounded_limit(
            int(_memory().TRANSCRIPT_RECALL_EXCERPT_CAP if limit is None else limit),
            int(_memory().TRANSCRIPT_RECALL_EXCERPT_CAP),
        )
        if not cap:
            finish("empty", reason="zero limit")
            return []
        excluded = (
            None if exclude_conversation_id is None else int(exclude_conversation_id)
        )
        project: int | None = None
        if project_id is not None:
            project = self._project_id(project_id)
            enabled = self.db.execute(
                "SELECT enabled FROM agent_projects WHERE id=?", (project,)
            ).fetchone()
            if enabled is None or not bool(enabled["enabled"]):
                finish("project-unavailable", reason="project missing or disabled")
                return []
        query_terms = _memory()._memory_query_terms(query)
        like_terms = _memory()._memory_like_terms(query, query_terms)
        if not query_terms or not like_terms:
            finish("empty", reason="no searchable terms")
            return []
        candidate_limit = int(_memory().MAX_MEMORY_SEARCH_CANDIDATES)
        scope_sql = """FROM message_fts
               JOIN messages AS m ON m.id=message_fts.rowid
               JOIN conversations AS c ON c.id=m.conversation_id
               WHERE message_fts MATCH ?
                 AND (? IS NULL OR m.conversation_id<>?)
                 AND (? IS NULL OR c.project_id=?)"""
        scope_parameters: tuple[Any, ...] = (excluded, excluded, project, project)

        def pool(match: str) -> list[sqlite3.Row] | None:
            try:
                return list(self.db.execute(
                    f"""SELECT m.id AS id, m.conversation_id AS conversation_id,
                               m.created_at AS created_at, m.role AS role
                        {scope_sql}
                        ORDER BY message_fts.rank, m.id DESC LIMIT ?""",
                    (match, *scope_parameters, candidate_limit + 1),
                ).fetchall())
            except _memory().sqlite3.DatabaseError:
                return None

        def visible_frequencies(matches: Sequence[str]) -> list[int] | None:
            """Count a bounded group of FTS expressions over the visible scope."""
            if not matches:
                return []
            ctes: list[str] = []
            selects: list[str] = []
            parameters: list[Any] = []
            for index, match in enumerate(matches):
                ctes.append(f"q{index} AS (SELECT m.id {scope_sql} LIMIT ?)")
                selects.append(
                    f"SELECT {index} AS term_index, COUNT(*) AS frequency "
                    f"FROM q{index}"
                )
                parameters.extend((match, *scope_parameters, candidate_limit + 1))
            try:
                counted = self.db.execute(
                    f"WITH {', '.join(ctes)} {' UNION ALL '.join(selects)} "
                    "ORDER BY term_index",
                    parameters,
                ).fetchall()
            except _memory().sqlite3.DatabaseError:
                return None
            frequencies = [0] * len(matches)
            for row in counted:
                frequencies[int(row["term_index"])] = int(row["frequency"])
            return frequencies

        mode = "or"
        fts_query = _memory()._memory_fts_query(query, query_terms)
        rows: list[_memory().sqlite3.Row] | None
        if fts_query is None:
            # Wildcard input keeps the bounded LIKE path, as every other lane.
            patterns = [f"%{_memory()._escape_like(term)}%" for term in like_terms]
            where = " OR ".join(
                "lower(m.content) LIKE ? ESCAPE '\\'" for _ in patterns
            )
            match_count = " + ".join(
                "CASE WHEN lower(m.content) LIKE ? ESCAPE '\\' THEN 1 ELSE 0 END"
                for _ in patterns
            )
            try:
                rows = list(self.db.execute(
                    f"""SELECT m.id AS id, m.conversation_id AS conversation_id,
                               m.created_at AS created_at, m.role AS role
                        FROM messages AS m
                        JOIN conversations AS c ON c.id=m.conversation_id
                        WHERE ({where})
                          AND (? IS NULL OR m.conversation_id<>?)
                          AND (? IS NULL OR c.project_id=?)
                        ORDER BY ({match_count}) DESC, m.id DESC LIMIT ?""",
                    [*patterns, *scope_parameters, *patterns, candidate_limit + 1],
                ).fetchall())
            except _memory().sqlite3.DatabaseError:
                rows = None
            if rows is None:
                finish("error", reason="database error during LIKE discovery")
                return []
            if len(rows) > candidate_limit:
                finish("overflow", reason="LIKE pool exceeds the candidate limit")
                return []
            mode = "like"
        else:
            # The unknown-identity floor at every corpus size: a structured
            # identifier (letters and digits) the visible scope has never seen
            # abstains rather than returning look-alike rows.
            structured = list(dict.fromkeys(
                _memory()._normalize_memory_token(term)
                for term in query_terms
                if _memory()._memory_identity_capable_term(term)
            ))
            if structured:
                counts = visible_frequencies(
                    [_memory()._memory_fts_literal(term) for term in structured]
                )
                if counts is None:
                    finish("error", reason="database error during identity check")
                    return []
                unknown = [
                    term for term, count in zip(structured, counts, strict=True)
                    if count == 0
                ]
                if unknown:
                    report["unknown_terms"].extend(unknown)
                    finish(
                        "unknown-identity",
                        reason="the visible transcripts never mention the identifier",
                    )
                    return []
                if past_deadline():
                    finish("budget-exceeded", budget="time", reason="identity check")
                    return []
            rows = pool(fts_query)
            if rows is None:
                finish("error", reason="database error during discovery")
                return []
            if len(rows) > candidate_limit:
                if past_deadline():
                    finish("budget-exceeded", budget="time", reason="OR pool")
                    return []
                # Stage 2: keep only the terms that discriminate on their own,
                # counted over the visible scope.
                groups = _memory()._memory_fts_term_groups(query, query_terms)
                counts = visible_frequencies(
                    [_memory()._memory_fts_group_query(spellings) for _term, spellings in groups]
                )
                if counts is None:
                    finish("error", reason="database error during term counts")
                    return []
                discriminating: list[str] = []
                unknown_identities: list[str] = []
                for (term, _spellings), count in zip(groups, counts, strict=True):
                    if count > candidate_limit:
                        report["dropped_terms"].append(term)
                    elif count == 0 and _memory()._memory_identity_capable_term(term):
                        unknown_identities.append(term)
                    else:
                        discriminating.append(term)
                if unknown_identities:
                    report["unknown_terms"].extend(unknown_identities)
                    finish(
                        "unknown-identity",
                        reason="the visible transcripts never mention the identifier",
                    )
                    return []
                rows = None
                if discriminating and len(discriminating) < len(groups):
                    narrowed = _memory()._memory_fts_query(query, discriminating)
                    if narrowed is not None:
                        rows = pool(narrowed)
                        if rows is None:
                            finish("error", reason="database error during narrowing")
                            return []
                        if len(rows) > candidate_limit:
                            rows = None
                        else:
                            mode = "narrowed"
                if rows is None:
                    # Stage 3: no term discriminates alone; require every term.
                    if past_deadline():
                        finish("budget-exceeded", budget="time", reason="narrowing")
                        return []
                    all_terms = _memory()._memory_fts_query(query, query_terms, require_all=True)
                    rows = None if all_terms is None else pool(all_terms)
                    if rows is None:
                        finish("error", reason="database error during intersection")
                        return []
                    if len(rows) > candidate_limit:
                        finish("overflow", reason="every stage exceeds the candidate limit")
                        return []
                    mode = "all-terms"
        report["candidates"] = len(rows)
        selected: list[dict[str, Any]] = []
        budget: str | None = None
        # A bounded over-fetch so a row the screen drops can be replaced; the
        # cap on returned excerpts is hard either way.
        for row in rows[: cap * 2]:
            if past_deadline():
                budget = "time"
                break
            detail = self.db.execute(
                """SELECT m.content AS content, c.title AS title
                   FROM messages AS m
                   JOIN conversations AS c ON c.id=m.conversation_id
                   WHERE m.id=?""",
                (int(row["id"]),),
            ).fetchone()
            if detail is None:
                continue
            content = str(detail["content"] or "")
            if _memory()._TRANSCRIPT_GOVERNED_ROW.match(content):
                report["excluded_governed"] += 1
                continue
            excerpt = _memory()._transcript_excerpt_window(content, like_terms)
            if _memory()._transcript_excerpt_screen_reason(excerpt) is not None:
                report["excluded_by_screen"] += 1
                continue
            title = " ".join(str(detail["title"] or "").split())[:80]
            if title and (_memory().contains_secret(title) or _memory().screen_endpoint(title)[0]):
                title = ""
            selected.append({
                "message_id": int(row["id"]),
                "conversation_id": int(row["conversation_id"]),
                "title": title,
                "created_at": str(row["created_at"] or ""),
                "role": str(row["role"] or ""),
                "excerpt": excerpt,
            })
            if len(selected) >= cap:
                break
        # Oldest first, so a later conversation reads as the later word.
        selected.sort(key=lambda item: (item["created_at"], item["message_id"]))
        if budget is not None:
            finish(
                "budget-exceeded",
                budget=budget,
                reason="time budget exceeded during the screen phase",
                returned=len(selected),
            )
        else:
            finish(mode, returned=len(selected))
        return selected
