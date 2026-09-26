"""Current Memory domain extracted without changing method control flow.

Global dependencies resolve through jarvis.memory at call time. The facade keeps
public imports, patch seams, constants and authorization helpers authoritative.
"""
from __future__ import annotations

from collections.abc import Mapping
from collections.abc import Sequence
from typing import Any
from .memory_runtime import (_memory, _with_read_snapshot, _with_recall_cache)


class GovernanceMemoryMixin:
    """Mechanically extracted current Memory methods."""

    def _graph_claim_row_locked(self, claim_id: int) -> Any:
        """The one claim-row shape ``memory_graph.project_claim`` consumes.

        The write hook and the migration-48 backfill read the same columns in
        the same order, so a claim projected by a live write and the same
        claim projected by a rebuild cannot differ.
        """
        columns = ", ".join(_memory().memory_graph.CLAIM_ROW_COLUMNS)
        return self.db.execute(
            f"SELECT {columns} FROM memory_claims WHERE id=?",
            (int(claim_id),),
        ).fetchone()

    @staticmethod
    def _claim_scope_filter(
        visible_scopes: Sequence[str],
        project_scope: str | None,
        alias: str = "c",
        *,
        index_scope: bool = True,
    ) -> tuple[str, list[Any]]:
        """The visible-scope filter with project shadowing, for a table
        aliased ``alias``.

        Project facts override a global claim identity before lexical
        relevance, ranking, or candidate caps are applied. Otherwise a query
        that matches only the old global value could omit the project row and
        leak a contradicted global fact into its prompt.

        The claims lane and the graph channel share this one predicate so the
        two can never diverge: ``alias`` is ``c`` for ``memory_claims`` and
        ``e`` for ``memory_graph_edges``, which copies the same ``scope`` and
        ``claim_key`` columns for exactly this reason.

        ``index_scope=False`` prefixes the scope test with SQLite's ``+``,
        which changes no semantics but stops the planner choosing the scope
        index for it.  A caller that already selects a handful of rows by
        primary key wants the rowid path: with the scope index driving, the
        chain-row load searched the whole scope and filtered twelve rows out
        of 20,005 (2.86 ms); on the rowid it is 0.025 ms for identical rows.
        The lane's own read leaves it True -- there the scope index *is* the
        right driver, because nothing narrower is available.
        """
        scope_placeholders = ",".join("?" for _scope in visible_scopes)
        prefix = "" if index_scope else "+"
        scope_filter_sql = f"{prefix}{alias}.scope IN ({scope_placeholders})"
        scope_filter_parameters: list[Any] = [*visible_scopes]
        if project_scope is not None:
            scope_filter_sql += f"""
              AND (
                  {alias}.scope=?
                  OR NOT EXISTS (
                      SELECT 1 FROM memory_claims AS project_claim
                      WHERE project_claim.scope=?
                        AND project_claim.claim_key={alias}.claim_key
                        AND project_claim.status IN ('active', 'disputed')
                  )
              )"""
            scope_filter_parameters.extend((project_scope, project_scope))
        return scope_filter_sql, scope_filter_parameters

    def _spine_context(
        self,
        actor: str,
        conversation_id: int | None,
        permission: str,
    ) -> dict[str, Any]:
        return {
            "actor": actor if actor in _memory().memory_spine.SPINE_ACTORS else "runtime",
            "conversation_id": (
                int(conversation_id)
                if isinstance(conversation_id, int) and not isinstance(conversation_id, bool)
                and 0 < conversation_id <= 9_223_372_036_854_775_807
                else None
            ),
            "permission": str(permission or "runtime")[:80],
        }

    def _append_memory_event(
        self,
        kind: str,
        *,
        memory_id: int,
        payload: Mapping[str, Any],
        stamp: str,
        source: str | None,
        context: Mapping[str, Any],
        outcome: str = "applied",
    ) -> int:
        """Append one ``memory.*`` / ``lesson.*`` event inside the caller's
        write transaction and return its id.

        ``memories`` has no scope column, so every memory event is global; a
        claim's backing row carries the claim's own event instead and never
        receives one of these.  The actor is a receipt only: recall
        eligibility is decided by ``ordinary_memory_provenance``.
        """
        return _memory().memory_spine.append_event(
            self.db,
            self._spine_key,
            kind=kind,
            actor=str(context["actor"]),
            source=str(source or ""),
            scope="global",
            permission=str(context["permission"]),
            outcome=outcome,
            payload=payload,
            now=stamp,
            conversation_id=context["conversation_id"],
            subject_kind="memory",
            subject_id=int(memory_id),
        )

    def _append_claim_after_image_event(
        self,
        claim_id: int,
        *,
        kind: str,
        stamp: str,
        reason: str,
        related_claim_id: int | None,
        actor: str,
        conversation_id: int | None,
        permission: str,
    ) -> int | None:
        """Append a status/reassert event carrying the claim's after-image so the
        projection can be rebuilt from the spine alone.  Only when the spine
        exists (legacy migrations write claims before it does)."""
        if not self._spine_ready:
            return None
        row = self.db.execute(
            """SELECT id, scope, claim_key, status, valid_until, confidence, authority, source
               FROM memory_claims WHERE id=?""",
            (claim_id,),
        ).fetchone()
        if row is None:
            return None
        context = self._spine_context(actor, conversation_id, permission)
        return _memory().memory_spine.append_event(
            self.db,
            self._spine_key,
            kind=kind,
            actor=context["actor"],
            source=str(row["source"]),
            scope=str(row["scope"]),
            permission=context["permission"],
            outcome="applied",
            payload=_memory().memory_spine.claim_status_payload(
                row, at=stamp, reason=reason, related_claim_id=related_claim_id
            ),
            now=stamp,
            conversation_id=context["conversation_id"],
            subject_kind="claim",
            subject_id=int(claim_id),
        )

    def retract_explicit_project_claim(
        self,
        conversation_id: int,
        project_id: int,
        operator_prompt: str,
        *,
        permission: str = "operator:interactive",
    ) -> dict[str, Any]:
        """Atomically retire one exact operator-named project fact.

        The live claim for the named subject and predicate becomes
        ``superseded`` with no successor, so it is no longer current but its
        version history is kept.  The operator command and the fixed receipt
        commit or roll back with the status change.
        """
        parsed = _memory().parse_explicit_project_fact_retraction(operator_prompt)
        if parsed is None:
            raise ValueError("Prompt is not an explicit project fact retraction")
        normalized_project = self._project_id(project_id)
        scope = _memory().project_claim_scope(normalized_project)
        if (
            isinstance(conversation_id, bool)
            or not isinstance(conversation_id, int)
            or conversation_id <= 0
        ):
            raise ValueError("conversation_id must be a positive integer")
        subject = parsed["subject"]
        predicate = parsed["predicate"]
        claim_key = self._claim_identity(subject, predicate)
        stamp = _memory().now_iso()
        with self._immediate_transaction():
            project = self.db.execute(
                """SELECT p.id, p.enabled
                   FROM conversations AS c
                   JOIN agent_projects AS p ON p.id=c.project_id
                   WHERE c.id=?
                     AND NOT EXISTS (
                         SELECT 1 FROM screen_companion_conversations AS companion
                         WHERE companion.conversation_id=c.id
                     )""",
                (conversation_id,),
            ).fetchone()
            if (
                project is None
                or int(project["id"]) != normalized_project
                or not bool(project["enabled"])
            ):
                raise ValueError(
                    "Conversation project does not exist, is disabled, or mismatched"
                )
            latest_event = self.db.execute(
                """SELECT MAX(e.created_at)
                   FROM memory_claim_events AS e
                   JOIN memory_claims AS c ON c.id=e.claim_id
                   WHERE c.scope=? AND c.claim_key=?""",
                (scope, claim_key),
            ).fetchone()[0]
            if latest_event:
                requested_at = _memory()._as_utc(
                    _memory().datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
                )
                latest_at = _memory()._as_utc(
                    _memory().datetime.fromisoformat(str(latest_event).replace("Z", "+00:00"))
                )
                if requested_at <= latest_at:
                    stamp = (latest_at + _memory().timedelta(microseconds=1)).isoformat()
            live = self.db.execute(
                """SELECT id FROM memory_claims
                   WHERE scope=? AND claim_key=?
                     AND status IN ('active', 'disputed')
                   ORDER BY id""",
                (scope, claim_key),
            ).fetchall()
            self.db.execute(
                """INSERT INTO messages(conversation_id, created_at, role, content)
                   VALUES (?, ?, 'user', ?)""",
                (conversation_id, stamp, str(operator_prompt).strip()),
            )
            live_ids = [int(row["id"]) for row in live]
            if not live_ids:
                action = "missing"
                assistant_message = (
                    "No active project fact matches that subject and predicate; "
                    "nothing changed."
                )
            else:
                for claim_id in live_ids:
                    self._set_claim_status_locked(
                        claim_id,
                        "superseded",
                        stamp=stamp,
                        reason="retracted by operator",
                        actor="operator",
                        conversation_id=conversation_id,
                        permission=permission,
                        spine_kind="claim.retracted",
                    )
                remaining = self.db.execute(
                    """SELECT COUNT(*) FROM memory_claims
                       WHERE scope=? AND claim_key=?
                         AND status IN ('active', 'disputed')""",
                    (scope, claim_key),
                ).fetchone()[0]
                if int(remaining) != 0:
                    raise RuntimeError("Project claim retraction did not resolve")
                action = "retracted"
                assistant_message = (
                    f"Retracted project fact (claim record #{live_ids[-1]}). "
                    "It is no longer current; the version history is kept."
                )
            assistant_cursor = self.db.execute(
                """INSERT INTO messages(conversation_id, created_at, role, content)
                   VALUES (?, ?, 'assistant', ?)""",
                (conversation_id, stamp, assistant_message),
            )
            return {
                "project_id": normalized_project,
                "scope": scope,
                "claim_ids": live_ids,
                "action": action,
                "assistant_message_id": int(assistant_cursor.lastrowid),
                "assistant_message": assistant_message,
                "subject": subject,
                "predicate": predicate,
            }

    def erase_explicit_project_claim(
        self,
        conversation_id: int,
        project_id: int,
        operator_prompt: str,
        *,
        permission: str = "operator:interactive",
    ) -> dict[str, Any]:
        """Atomically erase every version of one exact operator-named project fact.

        The claim rows, their evidence, clock statistics, observations, and
        backing memory rows are deleted (foreign-key order), a
        ``claim.tombstoned`` spine event names the removed ids, and every
        earlier spine payload about the key is redacted.  The operator's own
        transcript copies survive until their conversations are deleted; the
        receipt says how many.  No value is echoed.
        """
        parsed = _memory().parse_explicit_project_fact_erasure(operator_prompt)
        if parsed is None:
            raise ValueError("Prompt is not an explicit project fact erasure")
        if not self._spine_ready:
            raise RuntimeError("The memory spine is unavailable")
        normalized_project = self._project_id(project_id)
        scope = _memory().project_claim_scope(normalized_project)
        if (
            isinstance(conversation_id, bool)
            or not isinstance(conversation_id, int)
            or conversation_id <= 0
        ):
            raise ValueError("conversation_id must be a positive integer")
        subject = parsed["subject"]
        predicate = parsed["predicate"]
        claim_key = self._claim_identity(subject, predicate)
        stamp = _memory().now_iso()
        self._recall_cache.clear()
        self.db.execute("PRAGMA secure_delete=ON")
        try:
            with self._immediate_transaction():
                project = self.db.execute(
                    """SELECT p.id, p.enabled
                       FROM conversations AS c
                       JOIN agent_projects AS p ON p.id=c.project_id
                       WHERE c.id=?
                         AND NOT EXISTS (
                             SELECT 1 FROM screen_companion_conversations AS companion
                             WHERE companion.conversation_id=c.id
                         )""",
                    (conversation_id,),
                ).fetchone()
                if (
                    project is None
                    or int(project["id"]) != normalized_project
                    or not bool(project["enabled"])
                ):
                    raise ValueError(
                        "Conversation project does not exist, is disabled, or mismatched"
                    )
                rows = self.db.execute(
                    """SELECT id, memory_id, value FROM memory_claims
                       WHERE scope=? AND claim_key=? ORDER BY id""",
                    (scope, claim_key),
                ).fetchall()
                claim_ids = [int(row["id"]) for row in rows]
                memory_ids = [int(row["memory_id"]) for row in rows]
                values = {str(row["value"]) for row in rows}
                self.db.execute(
                    """INSERT INTO messages(conversation_id, created_at, role, content)
                       VALUES (?, ?, 'user', ?)""",
                    (conversation_id, stamp, str(operator_prompt).strip()),
                )
                if not claim_ids:
                    assistant_message = (
                        "No project fact matches that subject and predicate; "
                        "nothing changed."
                    )
                    assistant_cursor = self.db.execute(
                        """INSERT INTO messages(conversation_id, created_at, role, content)
                           VALUES (?, ?, 'assistant', ?)""",
                        (conversation_id, stamp, assistant_message),
                    )
                    return {
                        "project_id": normalized_project,
                        "scope": scope,
                        "claim_ids": [],
                        "action": "missing",
                        "assistant_message_id": int(assistant_cursor.lastrowid),
                        "assistant_message": assistant_message,
                        "subject": subject,
                        "predicate": predicate,
                    }
                placeholders = ",".join("?" for _ in claim_ids)
                memory_placeholders = ",".join("?" for _ in memory_ids)
                # Proposal records reference claims and carry the command text
                # (the value included): unlink, expire, and blank them first.
                proposal_rows = self.db.execute(
                    "SELECT id, command FROM memory_fact_proposals WHERE project_id=?",
                    (normalized_project,),
                ).fetchall()
                proposal_ids: list[int] = []
                for proposal in proposal_rows:
                    try:
                        parsed_command = _memory().parse_explicit_project_fact(str(proposal["command"]))
                    except _memory().GovernedMemoryCommandError:
                        parsed_command = None
                    if parsed_command is None:
                        continue
                    if self._claim_identity(
                        parsed_command["subject"], parsed_command["predicate"]
                    ) == claim_key:
                        proposal_ids.append(int(proposal["id"]))
                if proposal_ids:
                    proposal_placeholders = ",".join("?" for _ in proposal_ids)
                    self.db.execute(
                        f"""UPDATE memory_fact_proposals
                           SET claim_id=NULL, command='[erased project fact]',
                               command_sha256=?, command_salt=NULL,
                               status=CASE WHEN status='shown' THEN 'expired' ELSE status END,
                               resolved_at=COALESCE(resolved_at, ?)
                           WHERE id IN ({proposal_placeholders})""",
                        ["0" * 64, stamp, *proposal_ids],
                    )
                self.db.execute(
                    f"UPDATE memory_fact_proposals SET claim_id=NULL WHERE claim_id IN ({placeholders})",
                    claim_ids,
                )
                fts_scrubbed = _memory().memory_spine.fts_secure_delete(self.db, "memory_fts")
                for table in (
                    "memory_claim_clock_statistics",
                    "memory_claim_observations",
                    "memory_claim_evidence",
                    "memory_claim_events",
                ):
                    self.db.execute(
                        f"DELETE FROM {table} WHERE claim_id IN ({placeholders})",
                        claim_ids,
                    )
                removed_entity_ids: list[int] = []
                if self._graph_ready:
                    # Edges go before the claim rows they reference (the
                    # foreign key requires it), and an entity left with no edge
                    # in any status is swept with them, so no label of an
                    # erased fact survives the transaction.
                    removed_entity_ids = [
                        int(entity_id)
                        for entity_id in _memory().memory_graph.delete_edges(
                            self.db, claim_ids
                        )
                    ]
                self.db.execute(
                    f"UPDATE memory_claims SET supersedes_id=NULL "
                    f"WHERE supersedes_id IN ({placeholders})",
                    claim_ids,
                )
                self.db.execute(
                    f"DELETE FROM memory_claims WHERE id IN ({placeholders})", claim_ids
                )
                for table in (
                    "memory_retrievals",
                    "memory_statistics",
                    "memory_embeddings",
                    "memory_embedding_leases",
                    "ordinary_memory_provenance",
                ):
                    self.db.execute(
                        f"DELETE FROM {table} WHERE memory_id IN ({memory_placeholders})",
                        memory_ids,
                    )
                self.db.execute(
                    f"DELETE FROM memories WHERE id IN ({memory_placeholders})", memory_ids
                )
                transcript_copies = 0
                for value in values:
                    if len(value) >= 3:
                        transcript_copies += int(
                            self.db.execute(
                                "SELECT COUNT(*) FROM messages WHERE instr(content, ?) > 0",
                                (value,),
                            ).fetchone()[0]
                        )
                        transcript_copies += int(
                            self.db.execute(
                                """SELECT COUNT(*) FROM conversation_goals
                                   WHERE instr(goal_text, ?) > 0
                                      OR instr(COALESCE(last_result_summary, ''), ?) > 0""",
                                (value, value),
                            ).fetchone()[0]
                        )
                removed_milestone_ids, removed_span_handles = (
                    self._erase_milestones_naming_claim_key_locked(claim_key)
                )
                # Red team H-3.  ``transcript_copies`` counted ``messages`` and
                # ``conversation_goals`` only, so an entire storage class was
                # invisible to it: an operator was told "48 copies remain"
                # while sixteen copies of the erased value sat inside another
                # conversation's surviving compacted span.  That number is what
                # a privacy decision is made on, so it must not omit a place
                # the value actually is.  Counted AFTER the milestone erase, so
                # only SURVIVING spans are counted -- a span this erase just
                # destroyed is not a copy that remains.
                transcript_copies += self._compacted_span_copies_locked(values)
                redaction_targets = _memory().memory_spine.events_to_redact(self.db, scope, claim_key)
                tombstone_payload: dict[str, Any] = {
                    "at": stamp,
                    "claim_key": claim_key,
                    "removed_claim_ids": claim_ids,
                    "removed_memory_ids": memory_ids,
                    "redacted_event_ids": redaction_targets,
                    "transcript_copies": transcript_copies,
                }
                # Chunked, NOT truncated.  `[:cap]` deleted milestones past
                # the 128th while naming them in no receipt at all -- a hole
                # in the audit trail wearing a cap's clothing, and the exact
                # thing MEMORY_DELETED_MAX_IDS' own docstring ("writers chunk
                # larger deletes") and this key set's comment both forbid.
                # The first cap-sized slice rides on this tombstone; the rest
                # follow it as additional claim.tombstoned events below, the
                # same shape the memory.deleted path uses.  validate_payload
                # now enforces the cap, so a future truncation cannot pass.
                cap = _memory().memory_spine.MILESTONE_TOMBSTONE_MAX_IDS
                milestone_overflow: list[tuple[list[int], list[str]]] = []
                if removed_milestone_ids:
                    tombstone_payload["removed_milestone_ids"] = (
                        removed_milestone_ids[:cap]
                    )
                    tombstone_payload["removed_span_handles"] = (
                        removed_span_handles[:cap]
                    )
                    for offset in range(cap, len(removed_milestone_ids), cap):
                        milestone_overflow.append((
                            removed_milestone_ids[offset:offset + cap],
                            removed_span_handles[offset:offset + cap],
                        ))
                if removed_entity_ids:
                    tombstone_payload["removed_entity_ids"] = removed_entity_ids
                tombstone_id = _memory().memory_spine.append_event(
                    self.db,
                    self._spine_key,
                    kind="claim.tombstoned",
                    actor="operator",
                    source="explicit operator project fact erasure",
                    scope=scope,
                    permission=str(permission)[:80],
                    outcome="applied",
                    payload=tombstone_payload,
                    now=stamp,
                    conversation_id=int(conversation_id),
                    subject_kind="claim",
                    subject_id=claim_ids[-1],
                )
                # Every milestone past the first chunk gets a receipt of its
                # own, parented to the tombstone that opened the erase so the
                # set can be reassembled from the chain.
                overflow_event_ids: list[int] = []
                for chunk_ids, chunk_handles in milestone_overflow:
                    overflow_event_ids.append(int(_memory().memory_spine.append_event(
                        self.db,
                        self._spine_key,
                        kind="claim.tombstoned",
                        actor="operator",
                        source="explicit operator project fact erasure (continued)",
                        scope=scope,
                        permission=str(permission)[:80],
                        outcome="applied",
                        payload={
                            "at": stamp,
                            "claim_key": claim_key,
                            # The claim ids rode on the first event; repeating
                            # them here would double-count them on replay.
                            "removed_claim_ids": [],
                            "removed_milestone_ids": chunk_ids,
                            "removed_span_handles": chunk_handles,
                        },
                        now=stamp,
                        conversation_id=int(conversation_id),
                        subject_kind="claim",
                        subject_id=claim_ids[-1],
                        parent_event_id=int(tombstone_id),
                    )))
                redacted = _memory().memory_spine.redact_claim_key_events(
                    self.db, scope, claim_key, tombstone_id
                )
                # Red team H-3, second half.  A destroyed span is the ONLY
                # copy of the turns it held, so an erase that silently takes
                # one is the largest thing this sentence can fail to mention.
                destroyed = len(removed_span_handles)
                compacted_note = (
                    ""
                    if not destroyed
                    else (
                        f" {destroyed} compacted span"
                        f"{'s' if destroyed != 1 else ''} covering those turns "
                        f"{'were' if destroyed != 1 else 'was'} deleted with it "
                        "and cannot be rehydrated."
                    )
                )
                assistant_message = (
                    f"Erased project fact ({len(claim_ids)} version"
                    f"{'s' if len(claim_ids) != 1 else ''} removed; tombstone #{tombstone_id})."
                    f"{compacted_note} "
                    f"{transcript_copies} transcript cop"
                    f"{'ies' if transcript_copies != 1 else 'y'} remain until "
                    "their conversations are deleted."
                )
                assistant_cursor = self.db.execute(
                    """INSERT INTO messages(conversation_id, created_at, role, content)
                       VALUES (?, ?, 'assistant', ?)""",
                    (conversation_id, stamp, assistant_message),
                )
                result = {
                    "project_id": normalized_project,
                    "scope": scope,
                    "claim_ids": claim_ids,
                    "action": "erased",
                    "tombstone_event_id": int(tombstone_id),
                    "tombstone_overflow_event_ids": overflow_event_ids,
                    "redacted_event_ids": redacted,
                    "removed_entity_ids": removed_entity_ids,
                    "transcript_copies": transcript_copies,
                    "fts_scrubbed": fts_scrubbed,
                    "proposal_records_blanked": len(proposal_ids),
                    "assistant_message_id": int(assistant_cursor.lastrowid),
                    "assistant_message": assistant_message,
                    "subject": subject,
                    "predicate": predicate,
                }
        finally:
            # secure_delete stays on for the whole connection (set at open).
            pass
        self._recall_cache.clear()
        if str(self.path) != ":memory:":
            try:
                self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            except _memory().sqlite3.OperationalError:
                pass
        return result

    def _compacted_span_copies_locked(self, values: Sequence[str]) -> int:
        """How many surviving compacted spans still contain an erased value.

        A span is compressed, so no SQL predicate can see inside one: each
        blob is decompressed and searched.  That is affordable here because
        this runs on the operator's explicit erase, never on a turn, and the
        alternative is a receipt that under-reports by an entire storage
        class (red team H-3).

        Counted per SPAN rather than per occurrence, matching what the
        sentence says -- "copies remain" is about places the value still is,
        and a span is one place an operator can act on with one command.
        """
        if not self._compaction_ready:
            return 0
        needles = [str(value) for value in values if len(str(value)) >= 3]
        if not needles:
            return 0
        copies = 0
        for row in self.db.execute(
            "SELECT handle, body FROM memory_compacted_spans ORDER BY handle"
        ).fetchall():
            try:
                text = _memory().memory_compaction.decompress_span(row["body"])
            except (_memory().memory_compaction.CompactionError, ValueError, TypeError):
                # An unreadable span is a verify problem, not a silent zero:
                # it is reported there, and here it is counted as a place the
                # value may still be rather than assumed clean.
                copies += 1
                continue
            if any(needle in text for needle in needles):
                copies += 1
        return copies

    def _erase_milestones_naming_claim_key_locked(
        self, claim_key: str
    ) -> tuple[list[int], list[str]]:
        """Delete every milestone whose ``claim_keys`` names an erased key.

        The row is DELETED and never edited, because no ``UPDATE`` is permitted
        on either table -- a milestone is written once and a recompaction gets
        a new ``seq`` (design 2.3, M-1/M-2).  Its span goes with it: the span
        is the only copy of the transcript rows behind the erased fact, so
        leaving it would defeat the erase exactly as leaving the claim key in
        the milestone would.  ``seq`` gaps are legal and expected afterwards.

        Milestones are NOT reachable from ``_memory_dependent_tables``, and
        deliberately so (N-5): that list is derived from tables carrying a
        ``memory_id`` column, neither M5 table has one, and hard-coding into
        the one list M3 made derived is how a table gets silently missed.  A
        milestone is not a dependent of a ``memories`` row -- it records a
        historical statement -- so the erase count there stays ten.
        """
        if not self._compaction_ready:
            return [], []
        target = str(claim_key)
        removed_ids: list[int] = []
        removed_handles: list[str] = []
        for row in self.db.execute(
            "SELECT id, handle, invariants_json FROM memory_milestones ORDER BY id"
        ).fetchall():
            try:
                derived = _memory().json.loads(str(row["invariants_json"]))["derived"]
                keys = derived.get("claim_keys") or []
            except (TypeError, ValueError, KeyError):
                continue
            if target not in {str(item) for item in keys}:
                continue
            removed_ids.append(int(row["id"]))
            removed_handles.append(str(row["handle"]))
        if not removed_ids:
            return [], []
        placeholders = ", ".join("?" for _ in removed_ids)
        # Child first: the spans table holds the foreign key.
        self.db.execute(
            f"DELETE FROM memory_compacted_spans WHERE milestone_id IN ({placeholders})",
            removed_ids,
        )
        self.db.execute(
            f"DELETE FROM memory_milestones WHERE id IN ({placeholders})",
            removed_ids,
        )
        return removed_ids, removed_handles

    def _memory_dependent_tables(self) -> list[str]:
        """Every live table carrying a ``memory_id`` column, minus
        ``memory_claims``.

        Derived from the live schema rather than typed out (M3 design 6.1,
        review R10): a table added later joins the erase order automatically
        instead of silently keeping a row that points at an erased memory.
        ``memory_claims`` is excluded because a claim's backing row is refused
        by ``erase_memory`` before any delete runs.  Migration scratch tables
        never appear because the derivation reads the live schema, and they
        are dropped before their migration commits.
        """
        names = [
            str(row[0]) for row in self.db.execute(
                """SELECT name FROM sqlite_master
                   WHERE type='table' AND name NOT LIKE 'sqlite_%'
                   ORDER BY name"""
            ).fetchall()
        ]
        tables: list[str] = []
        for name in names:
            if name == "memory_claims" or '"' in name:
                continue
            columns = {
                str(row[1]) for row in self.db.execute(
                    f'PRAGMA table_info("{name}")'
                ).fetchall()
            }
            if "memory_id" in columns:
                tables.append(name)
        return sorted(tables)

    @_with_read_snapshot
    def describe_memory(self, memory_id: int) -> dict[str, Any] | None:
        """What ``Erase memory #<id>`` would act on, looked up by id alone.

        A confirmation prompt must never say "no such memory" about a row that
        exists.  ``list_memories`` is a listing — it hides claim backing rows
        and stops at its limit — so a surface that confirms through it is
        wrong for exactly the rows the erase has fixed refusals for.  This
        reads the one row by primary key and reports what the operator needs
        to decide, and what ``erase_memory`` will do:
        ``is_claim_backing`` and ``is_vault_note`` are the two refusals it
        would hit.  ``None`` means no such row, which is the third.

        No content is returned, only its length: a confirmation prompt is a
        place to name a row, not to echo it back.
        """
        self._ensure_open()
        if (
            isinstance(memory_id, bool)
            or not isinstance(memory_id, int)
            or not 0 < memory_id <= 9_223_372_036_854_775_807
        ):
            return None
        row = self.db.execute(
            """SELECT m.id, m.created_at, m.kind, length(m.content) AS content_length,
                      omp.origin AS origin, omp.eligible AS eligible,
                      (SELECT 1 FROM memory_claims AS c WHERE c.memory_id=m.id)
                          AS claim_backed
               FROM memories AS m
               LEFT JOIN ordinary_memory_provenance AS omp ON omp.memory_id=m.id
               WHERE m.id=?""",
            (int(memory_id),),
        ).fetchone()
        if row is None:
            return None
        kind = str(row["kind"] or "")
        return {
            "id": int(row["id"]),
            "kind": kind,
            "created_at": str(row["created_at"] or ""),
            "origin": None if row["origin"] is None else str(row["origin"]),
            "eligible": None if row["eligible"] is None else bool(row["eligible"]),
            "is_claim_backing": kind == "claim" or row["claim_backed"] is not None,
            "is_vault_note": str(row["origin"] or "") == "verified_vault_note",
            "content_length": int(row["content_length"] or 0),
        }

    def erase_memory(
        self,
        conversation_id: int | None,
        memory_id: int,
        *,
        operator_prompt: str | None = None,
        permission: str = "operator:interactive",
    ) -> dict[str, Any]:
        """Erase one ordinary memory row by its explicit id, with a receipt.

        ``memories.id`` is explicit and never reused since schema 47, so it is
        the operator-facing identity (``Erase memory #<id>``).  Three cases
        refuse and change nothing, each with a fixed reason: no such row
        (``missing``); a row that backs a project fact (``claim_backing`` —
        erasing it alone would leave the claim projection inconsistent, so the
        operator is pointed at ``Erase this project fact:``); and a row that
        mirrors a vault note (``vault_note`` — the indexer would re-create it
        on the next pass).

        Otherwise, in one ``BEGIN IMMEDIATE`` under ``secure_delete``: the FTS
        index is scrubbed, every table carrying a ``memory_id`` column is
        cleared for that id (the list is derived from the live schema, never
        typed), the row is deleted, a digest-only ``memory.deleted`` event
        records the removal with the keyed content digest and how many
        transcript copies remain, and the fixed receipt says so.  No content
        is echoed.  ``conversation_id`` may be ``None`` for the CLI path, in
        which case no transcript rows are written and the receipt text is
        unchanged; ``operator_prompt`` is recorded as the operator's turn when
        both it and a conversation are given.
        """
        self._ensure_open()
        if not self._spine_ready:
            raise RuntimeError("The memory spine is unavailable")
        if (
            isinstance(memory_id, bool)
            or not isinstance(memory_id, int)
            or not 0 < memory_id <= 9_223_372_036_854_775_807
        ):
            raise ValueError("memory_id must be a positive integer")
        if conversation_id is not None and (
            isinstance(conversation_id, bool)
            or not isinstance(conversation_id, int)
            or not 0 < conversation_id <= 9_223_372_036_854_775_807
        ):
            raise ValueError("conversation_id must be a positive integer")
        stamp = _memory().now_iso()
        self._recall_cache.clear()
        self.db.execute("PRAGMA secure_delete=ON")
        with self._immediate_transaction():
            if conversation_id is not None and operator_prompt:
                self.db.execute(
                    """INSERT INTO messages(conversation_id, created_at, role, content)
                       VALUES (?, ?, 'user', ?)""",
                    (conversation_id, stamp, str(operator_prompt).strip()),
                )
            row = self.db.execute(
                """SELECT m.id, m.created_at, m.kind, m.content,
                          (SELECT omp.origin FROM ordinary_memory_provenance AS omp
                            WHERE omp.memory_id=m.id) AS origin,
                          (SELECT 1 FROM memory_claims AS c
                            WHERE c.memory_id=m.id) AS claim_backed
                   FROM memories AS m WHERE m.id=?""",
                (memory_id,),
            ).fetchone()
            refusal: str | None = None
            if row is None:
                refusal = "missing"
                message = f"No memory #{memory_id} exists; nothing changed."
            elif str(row["kind"] or "") == "claim" or row["claim_backed"] is not None:
                refusal = "claim_backing"
                message = (
                    f"Memory #{memory_id} backs a project fact; use "
                    "Erase this project fact: {\u2026} (see /facts) instead."
                )
            elif str(row["origin"] or "") == "verified_vault_note":
                refusal = "vault_note"
                message = (
                    f"Memory #{memory_id} mirrors a vault note; delete the note "
                    "in the vault and reindex."
                )
            if refusal is not None:
                assistant_message_id = None
                if conversation_id is not None:
                    assistant_message_id = int(self.db.execute(
                        """INSERT INTO messages(conversation_id, created_at, role, content)
                           VALUES (?, ?, 'assistant', ?)""",
                        (conversation_id, stamp, message),
                    ).lastrowid)
                return {
                    "memory_id": int(memory_id),
                    "action": refusal,
                    "kind": None if row is None else str(row["kind"] or ""),
                    "created_at": None if row is None else str(row["created_at"] or ""),
                    "transcript_copies": 0,
                    "deleted_event_id": None,
                    "fts_scrubbed": False,
                    "dependent_rows_deleted": {},
                    "assistant_message_id": assistant_message_id,
                    "assistant_message": message,
                }
            kind = str(row["kind"] or "")
            created_at = str(row["created_at"] or "")
            content = str(row["content"] or "")
            transcript_copies = 0
            if len(content) >= 3:
                transcript_copies += int(self.db.execute(
                    "SELECT COUNT(*) FROM messages WHERE instr(content, ?) > 0",
                    (content,),
                ).fetchone()[0])
                transcript_copies += int(self.db.execute(
                    """SELECT COUNT(*) FROM conversation_goals
                       WHERE instr(goal_text, ?) > 0
                          OR instr(COALESCE(last_result_summary, ''), ?) > 0""",
                    (content, content),
                ).fetchone()[0])
            fts_scrubbed = _memory().memory_spine.fts_secure_delete(self.db, "memory_fts")
            dependent_rows_deleted: dict[str, int] = {}
            for table in self._memory_dependent_tables():
                cursor = self.db.execute(
                    f'DELETE FROM "{table}" WHERE memory_id=?', (memory_id,)
                )
                removed = int(cursor.rowcount or 0)
                if removed > 0:
                    dependent_rows_deleted[table] = removed
            self.db.execute("DELETE FROM memories WHERE id=?", (memory_id,))
            payload = _memory().memory_spine.memory_deleted_payload(
                self._spine_key,
                [(int(memory_id), content)],
                reason="explicit operator memory erasure",
                at=stamp,
            )
            payload["kind"] = kind
            payload["transcript_copies"] = transcript_copies
            deleted_event_id = self._append_memory_event(
                "memory.deleted",
                memory_id=int(memory_id),
                payload=payload,
                stamp=stamp,
                source="explicit operator memory erasure",
                context=self._spine_context("operator", conversation_id, permission),
            )
            message = (
                f"Erased memory #{memory_id} (kind: {kind or 'unknown'}, "
                f"created {created_at[:10]}). {transcript_copies} transcript "
                f"cop{'ies' if transcript_copies != 1 else 'y'} remain until "
                "their conversations are deleted."
            )
            assistant_message_id = None
            if conversation_id is not None:
                assistant_message_id = int(self.db.execute(
                    """INSERT INTO messages(conversation_id, created_at, role, content)
                       VALUES (?, ?, 'assistant', ?)""",
                    (conversation_id, stamp, message),
                ).lastrowid)
            result = {
                "memory_id": int(memory_id),
                "action": "erased",
                "kind": kind,
                "created_at": created_at,
                "transcript_copies": transcript_copies,
                "deleted_event_id": (
                    None if deleted_event_id is None else int(deleted_event_id)
                ),
                "fts_scrubbed": bool(fts_scrubbed),
                "dependent_rows_deleted": dependent_rows_deleted,
                "assistant_message_id": assistant_message_id,
                "assistant_message": message,
            }
        self._recall_cache.clear()
        if str(self.path) != ":memory:":
            try:
                self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            except _memory().sqlite3.OperationalError:
                pass
        return result

    def append_spine_event(
        self,
        kind: str,
        *,
        actor: str,
        source: str,
        scope: str,
        permission: str,
        outcome: str,
        payload: Mapping[str, Any],
        conversation_id: int | None = None,
        subject_kind: str | None = None,
        subject_id: int | None = None,
        parent_event_id: int | None = None,
    ) -> int | None:
        """Best-effort receipt outside any claim write (proposal events).

        Like clock telemetry: never inside a caller's snapshot, a short lock
        timeout, and a dropped event is counted, never raised.
        """
        self._ensure_open()
        if not self._spine_ready or self.db.in_transaction:
            self._dropped_spine_events += 1
            return None
        restore_timeout = int(getattr(self, "_busy_timeout_ms", _memory().DEFAULT_BUSY_TIMEOUT_MS))
        context = self._spine_context(actor, conversation_id, permission)
        try:
            self.db.execute(f"PRAGMA busy_timeout={_memory()._CLAIM_CLOCK_WRITE_TIMEOUT_MS}")
            try:
                self.db.execute("BEGIN IMMEDIATE")
                event_id = _memory().memory_spine.append_event(
                    self.db,
                    self._spine_key,
                    kind=kind,
                    actor=context["actor"],
                    source=source,
                    scope=scope,
                    permission=context["permission"],
                    outcome=outcome,
                    payload=payload,
                    now=_memory().now_iso(),
                    conversation_id=context["conversation_id"],
                    subject_kind=subject_kind,
                    subject_id=subject_id,
                    parent_event_id=parent_event_id,
                )
                self.db.commit()
                return int(event_id)
            except (_memory().sqlite3.OperationalError, _memory().memory_spine.SpineError, ValueError):
                if self.db.in_transaction:
                    self.db.rollback()
                self._dropped_spine_events += 1
                return None
        finally:
            self.db.execute(f"PRAGMA busy_timeout={restore_timeout}")

    @_with_read_snapshot
    def verify_spine(self) -> dict[str, Any]:
        """Recompute the keyed chain and the claim lineage; never repairs."""
        self._ensure_open()
        if not self._spine_ready:
            return {"ok": False, "events": 0, "problems": ["spine is unavailable"]}
        report = _memory().memory_spine.verify_spine(self.db, self._spine_key)
        report["dropped_best_effort_events_this_process"] = int(self._dropped_spine_events)
        # Informational only: the spine is authentic whether or not a
        # projection drifted, so ``ok`` and the CLI exit code are unchanged by
        # graph state.  Projection drift is a rebuild matter, exactly as for
        # claims (M3 design 4.6).
        counts = (
            _memory().memory_graph.graph_counts(self.db) if self._graph_ready
            else {"edges": 0, "entities": 0}
        )
        report["graph_edges"] = int(counts.get("edges") or 0)
        report["graph_entities"] = int(counts.get("entities") or 0)
        report["graph_ok"] = bool(self.verify_graph().get("ok"))
        return report

    @_with_read_snapshot
    def latest_spine_event_id(
        self, *, kind: str, subject_kind: str, subject_id: int
    ) -> int | None:
        """Newest event of one kind about one subject (backward lineage)."""
        self._ensure_open()
        if not self._spine_ready:
            return None
        return _memory().memory_spine.latest_event_id(
            self.db, kind=kind, subject_kind=subject_kind, subject_id=int(subject_id)
        )

    def rebuild_claim_projection(
        self,
        *,
        apply: bool = False,
        plan: Mapping[str, Any] | None = None,
        actor: str = "operator",
        permission: str = "operator:cli",
    ) -> dict[str, Any]:
        """Replay the spine into a shadow claim projection and compare it with
        the live rows.  ``apply=False`` is a dry run that changes nothing; its
        report carries ``head_event_id`` and ``plan_token`` (twelve hex
        characters over the head event id and the divergence set), which
        bind the report to the store it described.

        ``apply=True`` reconciles the live projection in place under the write
        lock, never by table swap: rows without spine history are deleted with
        their dependents and backing rows, field divergences are rewritten
        from the spine, and rows the spine knows but the store lost are
        recreated with their backing rows and replayed status events (evidence
        is reported as lost, never invented).  ``plan`` is the dry-run report
        the operator confirmed; without one, a dry run is taken in its own
        snapshot before the lock.  Inside the transaction the dry run is taken
        again and must match the plan (token, or divergence set for a plan
        without a token), otherwise ``stale_plan`` refuses.  It also refuses,
        rolling back with ``applied`` False and a ``refusal`` code, when the
        chain does not verify, when the history itself is inconsistent, or
        when a divergence survives the rebuild; on success
        ``projection.rebuilt`` is the last event of the transaction.
        """
        if not apply:
            return self._rebuild_claim_projection_dry_run()
        return self._apply_claim_projection(
            plan=plan, actor=actor, permission=permission
        )

    @staticmethod
    def _claim_backing_content_builder() -> Any:
        """Both legal backing contents for a spine after-image, canonical
        first.  ``memory_spine`` accepts either when verifying and picks the
        keyed one exactly when the canonical content is already bound to a
        different claim, which is the same test the writer makes."""

        def backing_content(
            payload: Mapping[str, Any], scope: str
        ) -> tuple[str, str]:
            return _memory().backing_content_variants(
                str(payload.get("subject", "")),
                str(payload.get("predicate", "")),
                str(payload.get("value", "")),
                str(scope),
                str(payload.get("original_created_at") or payload.get("valid_from") or ""),
                str(payload.get("claim_key", "")),
            )

        return backing_content

    @staticmethod
    def _claim_divergence_signature(
        divergences: Sequence[Mapping[str, Any]],
    ) -> list[list[Any]]:
        signature: set[tuple[int, str]] = set()
        for item in divergences:
            claim_id = item.get("claim_id")
            try:
                normalized = -1 if claim_id is None else int(claim_id)
            except (TypeError, ValueError):
                normalized = -1
            signature.add((normalized, str(item.get("kind"))))
        return [list(pair) for pair in sorted(signature)]

    @classmethod
    def _claim_plan_token(
        cls,
        head_event_id: int | None,
        divergences: Sequence[Mapping[str, Any]],
    ) -> str:
        """Twelve hex characters binding a dry-run report to the store it
        described: the head event id and the sorted (claim_id, kind) set."""
        return _memory().memory_spine.sha256_hex(
            _memory().memory_spine.canonical([
                None if head_event_id is None else int(head_event_id),
                cls._claim_divergence_signature(divergences),
            ])
        )[:12]

    @classmethod
    def _plan_token_of(cls, plan: Mapping[str, Any]) -> str | None:
        token = plan.get("plan_token")
        if isinstance(token, str) and token:
            return token
        if plan.get("head_event_id") is not None:
            return cls._claim_plan_token(
                int(plan["head_event_id"]), plan.get("divergences") or []
            )
        return None

    def _head_event_id_locked(self) -> int | None:
        row = self.db.execute(
            "SELECT last_event_id FROM memory_spine_head WHERE id=1"
        ).fetchone()
        return None if row is None else int(row[0])

    @_with_read_snapshot
    def _rebuild_claim_projection_dry_run(self) -> dict[str, Any]:
        self._ensure_open()
        if not self._spine_ready:
            return {"ok": False, "rows_live": 0, "rows_rebuilt": 0,
                    "divergences": [{"claim_id": None, "kind": "verify",
                                     "detail": "spine is unavailable"}],
                    "head_event_id": None, "plan_token": None}
        report = _memory().memory_spine.rebuild_claim_projection(
            self.db, self._spine_key,
            content_builder=self._claim_backing_content_builder(),
        )
        report["head_event_id"] = self._head_event_id_locked()
        report["plan_token"] = self._claim_plan_token(
            report["head_event_id"], report["divergences"]
        )
        return report

    def _apply_claim_projection(
        self, *, plan: Mapping[str, Any] | None, actor: str, permission: str
    ) -> dict[str, Any]:
        self._ensure_open()
        report: dict[str, Any] = {
            "ok": False, "applied": False, "refusal": None, "plan_token": None,
            "rows_before": 0, "rows_after": 0, "divergences_fixed": 0,
            "removed_ids": [], "removed_memory_ids": [], "recreated_ids": [],
            "updated_ids": [], "lost_evidence_claim_ids": [], "event_id": None,
            "divergences": [], "before": None, "after": None,
            "verification": None,
        }
        if not self._spine_ready:
            report["refusal"] = "spine_unavailable"
            return report
        if self.db.in_transaction:
            # A caller's open snapshot would be committed with the rebuild.
            report["refusal"] = "transaction_already_open"
            return report
        if plan is None:
            # The plan is what the operator saw: a dry run in its own
            # snapshot, before the write lock, never the in-lock re-run.
            plan = self._rebuild_claim_projection_dry_run()
        plan_token = self._plan_token_of(plan)
        report["plan_token"] = plan_token
        report["before"] = dict(plan)
        report["rows_before"] = int(plan.get("rows_live") or 0)
        report["divergences"] = list(plan.get("divergences") or [])
        builder = self._claim_backing_content_builder()
        context = self._spine_context(actor, None, permission)
        self._recall_cache.clear()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            verification = _memory().memory_spine.verify_spine(self.db, self._spine_key)
            report["verification"] = verification
            # The chain must be authentic; lineage problems are what apply
            # reconciles (an out-of-band insert is a row without an event).
            if not bool(verification.get("chain_ok", verification.get("ok", False))):
                report["refusal"] = "verify_failed"
                return report
            current = _memory().memory_spine.rebuild_claim_projection(
                self.db, self._spine_key, content_builder=builder
            )
            current_token = self._claim_plan_token(
                self._head_event_id_locked(), current["divergences"]
            )
            if plan_token is not None:
                stale = plan_token != current_token
            else:
                stale = (
                    self._claim_divergence_signature(plan.get("divergences") or [])
                    != self._claim_divergence_signature(current["divergences"])
                )
            if stale:
                # The store changed after the operator saw the plan.
                report["refusal"] = "stale_plan"
                report["divergences"] = list(current["divergences"])
                return report
            report["plan_token"] = plan_token or current_token
            report["rows_before"] = int(current["rows_live"])
            if not current["divergences"]:
                report["ok"] = True
                report["rows_after"] = int(current["rows_live"])
                return report
            def _reproject_graph() -> dict[str, Any]:
                """Rebuild the graph from the reconciled claim rows, inside
                this transaction and before the receipt is appended, so the
                claim receipt can say the graph followed (design 4.5)."""
                if not self._graph_ready:
                    return {}
                _memory().memory_graph.reproject(self.db, now=_memory().now_iso())
                return {"graph_reprojected": True}

            applied = _memory().memory_spine.apply_claim_projection(
                self.db,
                self._spine_key,
                plan,
                content_builder=builder,
                now=_memory().now_iso(),
                actor=context["actor"],
                permission=context["permission"],
                post_apply=_reproject_graph,
            )
            after = _memory().memory_spine.rebuild_claim_projection(
                self.db, self._spine_key, content_builder=builder
            )
            report["after"] = after
            if after["divergences"]:
                report["refusal"] = "residual_divergence"
                report["divergences"] = list(after["divergences"])
                return report
            self.db.commit()
        except _memory().memory_spine.SpineError as exc:
            code = str(getattr(exc, "code", "") or "").strip()
            report["refusal"] = code or str(exc).split(":", 1)[0].strip() or "spine_error"
            return report
        except _memory().sqlite3.Error:
            report["refusal"] = "write_conflict"
            return report
        finally:
            if self.db.in_transaction:
                self.db.rollback()
        report["ok"] = True
        report["applied"] = True
        report["divergences"] = []
        report["rows_after"] = int(applied.get("rows_after", after["rows_live"]))
        for key in (
            "divergences_fixed", "removed_ids", "removed_memory_ids",
            "recreated_ids", "updated_ids", "lost_evidence_claim_ids", "event_id",
        ):
            if key in applied:
                report[key] = applied[key]
        self._recall_cache.clear()
        if str(self.path) != ":memory:":
            try:
                self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            except _memory().sqlite3.OperationalError:
                pass
        return report

    @_with_read_snapshot
    def rebuild_memory_projection(self) -> dict[str, Any]:
        """Replay ``memory.*`` / ``lesson.*`` events into a shadow keyed by
        memory id and compare digests, provenance, and lesson provenance with
        the live rows (dry run; details name digests and fields, never
        content).  Claim backing rows are lineage-checked only."""
        self._ensure_open()
        if not self._spine_ready:
            return {"ok": False, "rows_live": 0, "rows_rebuilt": 0,
                    "divergences": [{"memory_id": None, "kind": "verify",
                                     "detail": "spine is unavailable"}],
                    "verification": None}
        return _memory().memory_spine.rebuild_memory_projection(self.db, self._spine_key)

    @_with_read_snapshot
    def verify_graph(self) -> dict[str, Any]:
        """Check the temporal graph against the claim rows it projects; never
        repairs.  Problem details name fields, never values."""
        self._ensure_open()
        # Delegate unconditionally: the module already reports a store with no
        # graph as ``ready`` False with no problems, and it reads the live
        # tables rather than the cached open-time flag, so a table dropped out
        # of band after this store was opened is reported honestly.  Inventing
        # a problem of kind "graph" here would put a kind outside
        # ``VERIFY_PROBLEM_KINDS`` into a caller's report.
        return _memory().memory_graph.verify_graph(self.db)

    def rebuild_graph_projection(
        self,
        *,
        apply: bool = False,
        plan: Mapping[str, Any] | None = None,
        actor: str = "operator",
        permission: str = "operator:cli",
    ) -> dict[str, Any]:
        """Compare the graph with the projection it is derived from.

        ``apply=False`` is a dry run that changes nothing; its report carries
        ``head_event_id`` and ``plan_token``, which bind it to the store it
        described.  ``apply=True`` reconciles in place under the write lock —
        extra edges deleted, missing and field-divergent ones re-projected,
        orphan entities swept, new entity ids allocated only for entities that
        do not exist — re-runs the dry run inside the transaction, rolls back
        on residue, and appends ``projection.rebuilt {projection: "graph"}``.
        ``plan`` is the dry-run report the operator confirmed; a mismatch
        refuses with ``stale_plan``.
        """
        if not apply:
            return self._rebuild_graph_projection_dry_run()
        return self._apply_graph_projection(
            plan=plan, actor=actor, permission=permission
        )

    @staticmethod
    def _graph_divergence_signature(
        report: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    ) -> list[list[Any]]:
        """The module's own (claim_id, entity_id, kind) set, so a plan token
        can never disagree with what ``verify_graph`` calls a divergence."""
        source = (
            report if isinstance(report, _memory().Mapping) else {"divergences": list(report)}
        )
        return [list(item) for item in _memory().memory_graph.divergence_signature(source)]

    @classmethod
    def _graph_plan_token(
        cls,
        head_event_id: int | None,
        report: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    ) -> str:
        """Twelve hex characters binding a graph dry run to the store it
        described: the head event id and the sorted divergence set."""
        return _memory().memory_spine.sha256_hex(
            _memory().memory_spine.canonical([
                "graph",
                None if head_event_id is None else int(head_event_id),
                cls._graph_divergence_signature(report),
            ])
        )[:12]

    @_with_read_snapshot
    def _rebuild_graph_projection_dry_run(self) -> dict[str, Any]:
        self._ensure_open()
        # Same rule as ``verify_graph``: the module owns the shape, including
        # the ``ready`` False report for a store with no graph.
        report = _memory().memory_graph.rebuild_graph_projection(self.db)
        report["head_event_id"] = self._head_event_id_locked()
        report["plan_token"] = self._graph_plan_token(
            report["head_event_id"], report
        )
        return report

    def _apply_graph_projection(
        self, *, plan: Mapping[str, Any] | None, actor: str, permission: str
    ) -> dict[str, Any]:
        self._ensure_open()
        report: dict[str, Any] = {
            "ok": False, "applied": False, "refusal": None, "plan_token": None,
            "edges_before": 0, "edges_after": 0, "divergences_fixed": 0,
            "removed_ids": [], "removed_entity_ids": [], "recreated_ids": [],
            "updated_ids": [], "event_id": None, "divergences": [],
            "before": None, "after": None,
        }
        if not self._graph_ready:
            report["refusal"] = "graph_unavailable"
            return report
        if self.db.in_transaction:
            # A caller's open snapshot would be committed with the rebuild.
            report["refusal"] = "transaction_already_open"
            return report
        if plan is None:
            # The plan is what the operator saw: a dry run in its own
            # snapshot, before the write lock, never the in-lock re-run.
            plan = self._rebuild_graph_projection_dry_run()
        plan_token = plan.get("plan_token")
        if not isinstance(plan_token, str) or not plan_token:
            plan_token = (
                None if plan.get("head_event_id") is None
                else self._graph_plan_token(int(plan["head_event_id"]), plan)
            )
        report["plan_token"] = plan_token
        report["before"] = dict(plan)
        report["divergences"] = list(plan.get("divergences") or [])
        context = self._spine_context(actor, None, permission)
        self._recall_cache.clear()
        self.db.execute("BEGIN IMMEDIATE")
        applied: dict[str, Any] = {}
        try:
            current = _memory().memory_graph.rebuild_graph_projection(self.db)
            current_token = self._graph_plan_token(
                self._head_event_id_locked(), current
            )
            if plan_token is not None:
                stale = plan_token != current_token
            else:
                stale = (
                    self._graph_divergence_signature(plan.get("divergences") or [])
                    != self._graph_divergence_signature(current)
                )
            if stale:
                # The store changed after the operator saw the plan.
                report["refusal"] = "stale_plan"
                report["divergences"] = list(current["divergences"])
                return report
            report["plan_token"] = plan_token or current_token
            report["edges_before"] = int(current.get("edges_live") or 0)
            if not current["divergences"]:
                report["ok"] = True
                report["edges_after"] = int(current.get("edges_live") or 0)
                return report
            applied = _memory().memory_graph.apply_graph_projection(
                self.db,
                self._spine_key,
                plan,
                now=_memory().now_iso(),
                actor=context["actor"],
                permission=context["permission"],
            )
            after = _memory().memory_graph.rebuild_graph_projection(self.db)
            report["after"] = after
            if after["divergences"]:
                report["refusal"] = "residual_divergence"
                report["divergences"] = list(after["divergences"])
                return report
            self.db.commit()
        except _memory().memory_spine.SpineError as exc:
            code = str(getattr(exc, "code", "") or "").strip()
            report["refusal"] = code or str(exc).split(":", 1)[0].strip() or "spine_error"
            return report
        except _memory().sqlite3.Error:
            report["refusal"] = "write_conflict"
            return report
        finally:
            if self.db.in_transaction:
                self.db.rollback()
        report["ok"] = True
        report["applied"] = True
        report["divergences"] = []
        for key in (
            "edges_after", "divergences_fixed", "removed_ids",
            "removed_entity_ids", "recreated_ids", "updated_ids", "event_id",
        ):
            if key in applied:
                report[key] = applied[key]
        self._recall_cache.clear()
        if str(self.path) != ":memory:":
            try:
                self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            except _memory().sqlite3.OperationalError:
                pass
        return report

    @_with_recall_cache
    @_with_read_snapshot
    def subject_claim_history(
        self,
        subject: str,
        *,
        project_id: int | None = None,
        limit: int = 6,
    ) -> list[dict[str, Any]]:
        """Superseded versions of every claim key about one subject, newest
        first, through the screened read path (design 12.6 item 6).

        A secret in the subject refuses (``ValueError``); a private
        identifier, a missing or disabled project, or a candidate overflow
        abstains with ``[]``.  Project keys shadow the global key, look-alike
        subjects are excluded by the identity rule, values that carry a secret
        or private identifier are dropped, and every row must still pass the
        recall-material check with ``superseded`` admitted.  Ordering is
        ``valid_until DESC, id DESC``; at most ``limit`` (six) entries and
        three per key.  Entries carry ``status`` superseded, ``superseded_at``,
        and ``retracted`` (True when the key has no current value).  Only
        ``Erase`` removes a value from this history; ``Forget`` keeps it.
        """
        self._ensure_open()
        raw_subject = str(subject)
        if len(raw_subject) > _memory().MAX_SEARCH_QUERY_CHARS:
            raise ValueError(
                f"Claim subject exceeds {_memory().MAX_SEARCH_QUERY_CHARS} characters"
            )
        if _memory().contains_secret(raw_subject):
            raise ValueError("Potential secret detected; claim history refused")
        if _memory().contains_private_identifier(raw_subject):
            return []
        limit = _memory()._bounded_limit(limit, 6)
        subject_fold = " ".join(raw_subject.casefold().split())
        subject_terms = _memory()._memory_tokens(
            raw_subject, meaningful_only=True, cache_allowed=False
        )
        if not limit or not subject_fold or not subject_terms:
            return []
        project_scope = None
        if project_id is not None:
            normalized_project = self._project_id(project_id)
            project = self.db.execute(
                "SELECT enabled FROM agent_projects WHERE id=?",
                (normalized_project,),
            ).fetchone()
            if project is None or not bool(project["enabled"]):
                return []
            project_scope = _memory().project_claim_scope(normalized_project)
        visible_scopes = (
            ("global",) if project_scope is None else ("global", project_scope)
        )
        scope_placeholders = ",".join("?" for _scope in visible_scopes)
        parameters: list[Any] = [*visible_scopes]
        subject_sql = ""
        if subject_fold.isascii():
            # SQLite's lower() folds ASCII only; other subjects are screened
            # in Python below under the same candidate bound.
            subject_sql = " AND instr(lower(c.subject), ?) > 0"
            parameters.append(subject_fold)
        parameters.append(_memory().MAX_MEMORY_SEARCH_CANDIDATES + 1)
        rows = self.db.execute(
            f"""SELECT c.id AS claim_id, c.memory_id, c.scope, c.claim_key,
                       c.created_at, c.updated_at, c.subject, c.predicate, c.value,
                       c.value_sha256, c.source, c.authority, c.confidence, c.status,
                       c.valid_from, c.valid_until, c.supersedes_id
                FROM memory_claims AS c
                WHERE c.scope IN ({scope_placeholders})
                  AND c.status='superseded'{subject_sql}
                ORDER BY c.valid_until DESC, c.id DESC
                LIMIT ?""",
            parameters,
        ).fetchall()
        if len(rows) > _memory().MAX_MEMORY_SEARCH_CANDIDATES:
            return []
        requested_terms = set(subject_terms)
        candidates: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            row_subject = str(item["subject"])
            if subject_fold not in " ".join(row_subject.casefold().split()):
                continue
            row_terms = _memory()._memory_tokens(
                row_subject, meaningful_only=True, cache_allowed=False
            )
            if not row_terms or not requested_terms.issubset(set(row_terms)):
                continue
            head = row_terms[0]
            if head not in requested_terms and _memory()._claim_subject_identity_conflict(
                head, requested_terms
            ):
                continue
            value = str(item["value"])
            if _memory().contains_secret(value) or _memory().contains_private_identifier(value):
                continue
            candidates.append(item)
        if not candidates:
            return []
        if project_scope is not None:
            # A project key shadows the global key's whole history, whatever
            # the status of the project rows.
            global_keys = sorted({
                str(item["claim_key"]) for item in candidates
                if str(item["scope"]) != project_scope
            })
            shadowed: set[str] = set()
            for offset in range(0, len(global_keys), 400):
                chunk = global_keys[offset:offset + 400]
                placeholders = ",".join("?" for _key in chunk)
                shadowed.update(
                    str(found[0]) for found in self.db.execute(
                        f"""SELECT DISTINCT claim_key FROM memory_claims
                            WHERE scope=? AND claim_key IN ({placeholders})""",
                        [project_scope, *chunk],
                    ).fetchall()
                )
            candidates = [
                item for item in candidates
                if str(item["scope"]) == project_scope
                or str(item["claim_key"]) not in shadowed
            ]
        eligible_ids = self._claim_rows_recall_eligible(
            candidates, project_id=project_id, allow_superseded=True
        )
        candidates = [
            item for item in candidates if int(item["claim_id"]) in eligible_ids
        ]
        keys = list(dict.fromkeys(
            (str(item["scope"]), str(item["claim_key"])) for item in candidates
        ))
        current_keys: set[tuple[str, str]] = set()
        for offset in range(0, len(keys), 400):
            chunk = keys[offset:offset + 400]
            placeholders = ",".join("(?, ?)" for _pair in chunk)
            current_keys.update(
                (str(found["scope"]), str(found["claim_key"]))
                for found in self.db.execute(
                    f"""SELECT scope, claim_key FROM memory_claims
                        WHERE status IN ('active', 'disputed')
                          AND (scope, claim_key) IN ({placeholders})""",
                    [value for pair in chunk for value in pair],
                ).fetchall()
            )
        results: list[dict[str, Any]] = []
        per_key: dict[tuple[str, str], int] = {}
        for item in candidates:
            key = (str(item["scope"]), str(item["claim_key"]))
            if per_key.get(key, 0) >= 3:
                continue
            per_key[key] = per_key.get(key, 0) + 1
            item.pop("claim_key", None)
            item.pop("value_sha256", None)
            item["status"] = "superseded"
            item["superseded_at"] = str(
                item.get("valid_until") or item.get("updated_at") or ""
            )
            item["retracted"] = key not in current_keys
            results.append(item)
            if len(results) >= limit:
                break
        return results

    def lesson_recall_report(self) -> dict[str, Any]:
        """A copy of the diagnostic record for the most recent lesson read.

        ``mode`` is the sixteen-value closed set of
        ``learning_ladder.LESSON_RECALL_MODES`` (M4 design 5.4), ``exit`` names
        the row of ``learning_ladder.LESSON_EXITS`` the lane took, and
        ``abstained`` is true for the twelve modes that mean the lane refused,
        so an empty list is never silent.  The record is written
        **before** both raises in ``match_lessons``, so a caller that catches
        the ``ValueError`` still learns why: ``screened`` for a secret in the
        query, ``family-unsupported`` for an unknown family.

        ``superseded_shadowed`` counts rows that were in the candidate pool
        and failed their reuse controls for a lifecycle reason -- superseded,
        contradicted, quarantined or expired.  It is the operator-visible
        answer to "why did my lesson go quiet", and it is never shown to the
        model.
        """
        return dict(self._last_lesson_recall_report)

    def _lesson_exit(
        self, report: Mapping[str, Any], exit_key: str, started: float
    ) -> dict[str, Any]:
        """Publish the diagnostic record for one exit of ``match_lessons``.

        ``exit_key`` names a row of ``learning_ladder.LESSON_EXITS``, which
        owns the mode, the reason sub-code and whether the exit cues.  The
        store supplies only the counters it accumulated, so there is exactly
        one place in the tree where an exit means something.
        """
        record = _memory().learning_ladder.lesson_recall_record(
            exit_key,
            family=report["family"],
            project_id=report["project_id"],
            candidates=int(report["candidates"]),
            anchored=int(report["anchored"]),
            in_project=int(report["in_project"]),
            eligible=int(report["eligible"]),
            returned=int(report["returned"]),
            superseded_shadowed=int(report["superseded_shadowed"]),
            elapsed_ms=round((_memory().time.monotonic() - started) * 1000.0, 3),
        )
        self._last_lesson_recall_report = record
        return record

    def _lesson_abstain(
        self, report: Mapping[str, Any], exit_key: str, started: float
    ) -> list[dict[str, Any]]:
        """Record one refusal and return the empty list the lane already
        returned.  Purely additive: the caller's behaviour is unchanged."""
        self._lesson_exit(report, exit_key, started)
        return []

    def graph_recall_report(self) -> dict[str, Any]:
        """A copy of the diagnostic record for the most recent graph read.

        ``mode`` is the closed set of design 5.6 and ``abstained`` is true
        whenever the channel refused, so an empty chain list is never silent.
        """
        return dict(self._last_graph_recall_report)

    def _graph_abstain(
        self,
        report: dict[str, Any],
        mode: str,
        reason: str,
        started: float,
    ) -> dict[str, Any]:
        report["mode"] = str(mode)
        report["abstained"] = True
        report["reason"] = str(reason)
        report["elapsed_ms"] = round((_memory().time.monotonic() - started) * 1000.0, 3)
        self._last_graph_recall_report = report
        return {"rows": [], "overflow": [], "report": dict(report)}

    @_with_recall_cache
    @_with_read_snapshot
    def graph_chains(
        self,
        query: str = "",
        *,
        project_id: int | None = None,
        subjects: Sequence[str] = (),
        seed_claims: Sequence[Mapping[str, Any]] = (),
        temporal: bool = False,
        as_of: str | None = None,
        lane_mode: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Channel 3: bounded chains of stored facts, both directions, as of
        now or as of a past version.

        Everything here is deterministic Python over indexed reads in one
        deferred snapshot: no model call, no write lock.  The query screens are
        the claims lane's; the whole call shares one deadline
        (``memory_graph.TIME_BUDGET_MS``), taken once here and checked in the
        traversal loop, before the screen phase, and after every screened row;
        every returned row passes ``_claim_rows_recall_eligible`` and the
        widened endpoint screen on **both** its subject and its value.

        ``lane_mode`` is ``claim_recall_report()["mode"]``.  A lane that
        abstained for a security reason silences the channel; ``identity-
        overflow`` and ``identity-conflict`` are identity floors, so the graph
        keeps running but with non-exact resolution disabled and answers only
        from exactly spelled names (design 2.3d).

        Returns ``{"rows", "overflow", "report"}``; a locked database or any
        ``sqlite3.Error`` degrades to no rows with ``report.mode == "error"``.
        """
        self._ensure_open()
        started = _memory().time.monotonic()
        deadline = started + float(_memory().memory_graph.TIME_BUDGET_MS) / 1000.0
        report = _memory()._blank_graph_recall_report("idle")
        report["lane_mode"] = None if lane_mode is None else str(lane_mode)
        self._last_graph_recall_report = report
        if not self._graph_ready:
            return self._graph_abstain(
                report, "error", "graph projection is unavailable", started
            )
        raw_query = str(query)
        if len(raw_query) > _memory().MAX_SEARCH_QUERY_CHARS:
            raise ValueError(
                f"Graph chain query exceeds {_memory().MAX_SEARCH_QUERY_CHARS} characters"
            )
        if _memory().contains_secret(raw_query):
            raise ValueError("Potential secret detected; graph chain search refused")
        if _memory().contains_private_identifier(raw_query):
            return self._graph_abstain(
                report, "screened", "private identifier in query", started
            )
        if _memory()._memory_query_targets_authority_evasion(raw_query):
            return self._graph_abstain(
                report, "screened", "authority evasion in query", started
            )
        lane = str(lane_mode or "")
        if lane in _memory()._LANE_SILENCING_MODES:
            # Design 10.7 item 5: a lane-silenced call reports ``screened``,
            # except ``project-unavailable``, which reports itself -- it is in
            # the closed mode set of 5.6 and it says something different to an
            # operator ("this project is gone") than a screen does.
            return self._graph_abstain(
                report,
                "project-unavailable" if lane == "project-unavailable" else "screened",
                f"claims lane abstained: {lane}",
                started,
            )
        project_scope = None
        if project_id is not None:
            normalized_project = self._project_id(project_id)
            project = self.db.execute(
                "SELECT enabled FROM agent_projects WHERE id=?",
                (normalized_project,),
            ).fetchone()
            if project is None or not bool(project["enabled"]):
                return self._graph_abstain(
                    report, "project-unavailable",
                    "project missing or disabled", started,
                )
            project_scope = _memory().project_claim_scope(normalized_project)
        visible_scopes = (
            ("global",) if project_scope is None else ("global", project_scope)
        )
        # Design 10.3 item 1: no lane mode disables non-exact resolution any
        # more.  The rule lives in one place with its own test; the security
        # abstentions above still silence the channel outright.
        exact_only = _memory().memory_graph.lane_forces_exact_only(lane)
        row_cap = _memory()._bounded_limit(
            int(_memory().memory_graph.CHAIN_ROW_CAP if limit is None else limit),
            int(_memory().memory_graph.CHAIN_ROW_CAP),
        )
        named_subjects = [
            str(item) for item in (subjects or ()) if str(item).strip()
        ]
        # A question names a handful of subjects; a crafted one can name many,
        # and every extra start is another frontier root, another look-alike
        # floor and another traversal.  Bound it here rather than trusting the
        # caller, and say in the report how many were dropped so a truncated
        # read is never silent.
        subjects_dropped = max(0, len(named_subjects) - _memory().MAX_GRAPH_START_SUBJECTS)
        if subjects_dropped:
            named_subjects = named_subjects[:_memory().MAX_GRAPH_START_SUBJECTS]
        seeds = [item for item in (seed_claims or ()) if isinstance(item, _memory().Mapping)]
        # A named subject the main lane found nothing for keeps the existing
        # "say the asked fact is not recorded instead of substituting" rule:
        # its hop-1 rows are tagged ``match: subject`` (design 5.8).
        seed_subject_keys = {
            _memory().memory_graph.entity_key(str(item.get("subject") or "")) for item in seeds
        }
        match_subject_keys = [
            key for key in (
                _memory().memory_graph.entity_key(name) for name in named_subjects
            )
            if key and key not in seed_subject_keys
        ]
        try:
            # The graph reads edges through the claims lane's own scope and
            # shadowing predicate, aliased to the edge table, so the two lanes
            # can never disagree about what a project shadows (design 5.2).
            edge_scope_sql, edge_scope_parameters = self._claim_scope_filter(
                visible_scopes, project_scope, alias="e"
            )
            walk = _memory().memory_graph.graph_walk(
                self.db,
                visible_scopes=visible_scopes,
                query=raw_query,
                scope_sql=edge_scope_sql,
                scope_params=edge_scope_parameters,
                project_scope=project_scope,
                subjects=named_subjects,
                seed_claims=seeds,
                temporal=bool(temporal),
                as_of=None if as_of is None else str(as_of),
                exact_only=exact_only,
                deadline=deadline,
            )
            claim_ids = [
                int(item) for item in (walk.get("claim_ids") or ())
            ][:int(_memory().memory_graph.SCREENED_ROW_CAP)]
            claim_rows: dict[int, dict[str, Any]] = {}
            if claim_ids:
                # The id list is the narrow term here (at most
                # SCREENED_ROW_CAP of them), so the rowid path is the right
                # one -- see _claim_scope_filter's note on ``index_scope``.
                scope_filter_sql, scope_filter_parameters = self._claim_scope_filter(
                    visible_scopes, project_scope, index_scope=False
                )
                placeholders = ",".join("?" for _claim_id in claim_ids)
                candidates = [
                    dict(row) for row in self.db.execute(
                        f"""SELECT c.id AS claim_id, c.memory_id, c.scope,
                                   c.claim_key, c.created_at, c.updated_at,
                                   c.subject, c.predicate, c.value,
                                   c.value_sha256, c.source, c.authority,
                                   c.confidence, c.status, c.valid_from,
                                   c.valid_until
                            FROM memory_claims AS c
                            WHERE c.id IN ({placeholders})
                              AND {scope_filter_sql}""",
                        [*claim_ids, *scope_filter_parameters],
                    ).fetchall()
                ]
                eligible = self._claim_rows_recall_eligible(
                    candidates,
                    project_id=project_id,
                    allow_superseded=bool(temporal) or as_of is not None,
                )
                claim_rows = {
                    int(item["claim_id"]): item
                    for item in candidates
                    if int(item["claim_id"]) in eligible
                }
            assembled = _memory().memory_graph.assemble_rows(
                walk,
                claim_rows,
                limit=row_cap,
                deadline=deadline,
                started=started,
                screen=_memory().screen_endpoint,
                match_subject_keys=match_subject_keys,
            )
        except _memory().sqlite3.Error:
            return self._graph_abstain(
                report, "error", "database busy or unreadable", started
            )
        rows = list(assembled.get("rows") or [])
        self._mark_retracted_chain_rows(rows, claim_rows)
        report.update(dict(assembled.get("report") or {}))
        report["channel"] = "graph"
        report["lane_mode"] = None if lane_mode is None else str(lane_mode)
        # Set after the merge so the walk's report can never mask it.
        report["subjects_dropped"] = subjects_dropped
        # The operator should still hear that the main lane could not tell
        # which stored subject the question named, whenever the graph answered
        # anyway (design 2.3d, 5.9).  Since 10.3 item 1 that no longer depends
        # on the graph having been restricted to exact names, so it is read
        # from the lane mode directly.
        report["lane_abstained"] = bool(
            lane in {"identity-overflow", "identity-conflict"}
            and str(report.get("mode") or "") == "complete"
        )
        report["abstained"] = str(report.get("mode") or "") not in {"complete"}
        report["elapsed_ms"] = round((_memory().time.monotonic() - started) * 1000.0, 3)
        self._last_graph_recall_report = report
        return {
            "rows": rows,
            "overflow": list(assembled.get("overflow") or []),
            "report": dict(report),
        }

    def _mark_retracted_chain_rows(
        self,
        rows: Sequence[dict[str, Any]],
        claim_rows: Mapping[int, Mapping[str, Any]],
    ) -> None:
        """Flag a superseded chain row whose key has no current value.

        ``Forget`` leaves a key with history and nothing current, and the cue
        has to say so or a temporal answer reads as merely out of date rather
        than retracted (design 3.2).  The walk cannot know it — the answer is
        in ``memory_claims``, not in the graph — so it is computed here, in
        one batched query over the keys of the rows being returned.
        """
        superseded = [
            row for row in rows if str(row.get("status") or "") == "superseded"
        ]
        if not superseded:
            return
        by_claim_id = {
            int(claim_id): (
                str(claim.get("scope") or ""), str(claim.get("claim_key") or "")
            )
            for claim_id, claim in claim_rows.items()
        }
        keyed: list[tuple[dict[str, Any], tuple[str, str]]] = []
        for row in superseded:
            claim_id = row.get("claim_id")
            if not isinstance(claim_id, int) or isinstance(claim_id, bool):
                continue
            key = by_claim_id.get(int(claim_id))
            if key is not None and key[1]:
                keyed.append((row, key))
        if not keyed:
            return
        distinct = list(dict.fromkeys(key for _row, key in keyed))
        current: set[tuple[str, str]] = set()
        for offset in range(0, len(distinct), 400):
            chunk = distinct[offset:offset + 400]
            placeholders = ",".join("(?, ?)" for _pair in chunk)
            current.update(
                (str(found["scope"]), str(found["claim_key"]))
                for found in self.db.execute(
                    f"""SELECT scope, claim_key FROM memory_claims
                        WHERE status IN ('active', 'disputed')
                          AND (scope, claim_key) IN ({placeholders})""",
                    [value for pair in chunk for value in pair],
                ).fetchall()
            )
        for row, key in keyed:
            if key not in current:
                row["retracted"] = True

    @_with_read_snapshot
    def spine_tail(self, limit: int = 20) -> list[dict[str, Any]]:
        """Recent spine events, payload keys only."""
        self._ensure_open()
        if not self._spine_ready:
            return []
        return _memory().memory_spine.recent_events(self.db, limit=limit)

    @_with_read_snapshot
    def _current_claims_read(
        self,
        query: str = "",
        limit: int = 8,
        *,
        clock_mode: str = "disabled",
        stale_threshold: float = 0.70,
        as_of: str | None = None,
        project_id: int | None = None,
    ) -> list[dict[str, Any]]:
        self._ensure_open()
        self._last_claim_recall_report = _memory()._blank_claim_recall_report("or")
        raw_query = str(query)
        if len(raw_query) > _memory().MAX_SEARCH_QUERY_CHARS:
            raise ValueError(
                f"Claim search query exceeds {_memory().MAX_SEARCH_QUERY_CHARS} characters"
            )
        if _memory().contains_secret(raw_query):
            raise ValueError("Potential secret detected; claim search refused")
        if _memory().contains_private_identifier(raw_query):
            # Claim retrieval is an information-returning boundary.  Queries
            # containing email addresses or other private identifiers must not
            # be allowed to use that identifier as an authority anchor.
            return self._abstain_claims("screened", "private identifier in query")
        clock_mode = str(clock_mode).strip().casefold()
        if clock_mode not in {"disabled", "shadow", "enforce"}:
            raise ValueError("Claim clock mode must be disabled, shadow, or enforce")
        read_at = str(as_of or _memory().now_iso())
        if clock_mode != "disabled" and not _memory()._recall_timestamp_valid(read_at):
            raise ValueError(
                "Claim clock as_of must be a privacy-clean timezone-aware timestamp"
            )
        stale_threshold = float(stale_threshold)
        if not _memory().math.isfinite(stale_threshold) or not 0.5 <= stale_threshold <= 0.99:
            raise ValueError("Claim stale threshold must be between 0.5 and 0.99")
        limit = _memory()._bounded_limit(limit, 50)
        if not limit:
            return []
        project_scope = None
        if project_id is not None:
            normalized_project = self._project_id(project_id)
            project = self.db.execute(
                "SELECT enabled FROM agent_projects WHERE id=?",
                (normalized_project,),
            ).fetchone()
            if project is None or not bool(project["enabled"]):
                return self._abstain_claims(
                    "project-unavailable", "project missing or disabled"
                )
            project_scope = _memory().project_claim_scope(normalized_project)
        visible_scopes = (
            ("global",)
            if project_scope is None
            else ("global", project_scope)
        )
        scope_filter_sql, scope_filter_parameters = self._claim_scope_filter(
            visible_scopes, project_scope
        )
        query_terms = _memory()._claim_query_terms(raw_query)
        raw_query_terms = _memory()._memory_tokens(raw_query, meaningful_only=True)
        raw_query_term_set = set(raw_query_terms)
        raw_query_proper_terms = {
            _memory()._normalize_memory_token(surface)
            for surface in _memory().re.findall(r"[^\W_]+", raw_query, _memory().re.UNICODE)
            if surface[:1].isupper()
        }
        explicit_multi_fact_query = _memory().re.search(
            r"\b(?:and|plus)\b|[&+]", raw_query, _memory().re.I
        ) is not None
        if raw_query.strip() and (
            not query_terms or _memory()._memory_query_targets_authority_evasion(raw_query)
        ):
            return self._abstain_claims(
                "screened", "no usable query terms or authority evasion"
            )
        terms = _memory()._memory_like_terms(
            raw_query,
            _memory()._memory_candidate_terms(raw_query),
            max_terms=_memory()._MAX_MEMORY_QUERY_TERM_CANDIDATES * 2,
        )
        relevance_sql = ""
        relevance_order_sql = ""
        parameters: list[Any] = [*scope_filter_parameters]
        if terms:
            relevance_sql = " AND (" + " OR ".join(
                "instr(lower(subject || ' ' || predicate || ' ' || value), ?) > 0"
                for _term in terms
            ) + ")"
            relevance_order_sql = "(" + " + ".join(
                "CASE WHEN instr(lower(subject || ' ' || predicate || ' ' || value), ?) > 0 "
                "THEN 1 ELSE 0 END"
                for _term in terms
            ) + ") DESC, "
            parameters.extend(terms)
            parameters.extend(terms)
        parameters.append(_memory().MAX_MEMORY_SEARCH_CANDIDATES + 1)
        candidate_select_sql = f"""SELECT c.id AS claim_id, c.memory_id, c.scope, c.claim_key,
                       c.created_at, c.updated_at,
                       c.subject, c.predicate, c.value, c.value_sha256,
                       c.source, c.authority,
                       c.confidence, c.status
                FROM memory_claims AS c
                WHERE {scope_filter_sql}
                  AND c.status IN ('active', 'disputed')"""
        candidate_order_sql = f"""ORDER BY {relevance_order_sql}CASE authority
                             WHEN 'operator' THEN 4 WHEN 'verified' THEN 3
                             WHEN 'learned' THEN 2 ELSE 1 END DESC,
                         updated_at DESC, id DESC
                LIMIT ?"""
        rows = self.db.execute(
            f"{candidate_select_sql}{relevance_sql}\n                {candidate_order_sql}",
            parameters,
        ).fetchall()
        discovery_mode = "or" if terms else "all"
        if len(rows) > _memory().MAX_MEMORY_SEARCH_CANDIDATES and len(terms) > 1:
            # One everyday term (a predicate every subject shares) overflowed
            # the bounded pool.  Narrow to rows that contain every term before
            # abstaining, so a unique subject still answers an exact lookup.
            # Conflict detection then sees only full-match rows, which is the
            # conservative direction: partial matches could only add anchors.
            all_terms_sql = " AND (" + " AND ".join(
                "instr(lower(subject || ' ' || predicate || ' ' || value), ?) > 0"
                for _term in terms
            ) + ")"
            rows = self.db.execute(
                f"{candidate_select_sql}{all_terms_sql}\n                {candidate_order_sql}",
                parameters,
            ).fetchall()
            discovery_mode = "all-terms"
        if len(rows) > _memory().MAX_MEMORY_SEARCH_CANDIDATES:
            # A bounded recency window must never hide an older, stronger
            # conflicting identity and expose a newer weak substitute.
            return self._abstain_claims(
                "overflow",
                "candidate pool exceeds the bound after narrowing",
                candidates=len(rows),
            )
        self._last_claim_recall_report.update(
            mode=discovery_mode,
            candidates=len(rows),
            discovery_terms=len(terms),
        )
        items = [dict(row) for row in rows]
        if project_scope is not None:
            project_claim_keys = {
                str(item["claim_key"])
                for item in items
                if str(item["scope"]) == project_scope
            }
            items = [
                item
                for item in items
                if (
                    str(item["scope"]) == project_scope
                    or str(item["claim_key"]) not in project_claim_keys
                )
            ]
        if query_terms:
            # Rank structurally first over the pure, uncached token path, then
            # validate only the strongest tier and the selected rows.  No
            # persisted field of an unvalidated row is admitted to the per-store
            # cache, and a corrupt strongest candidate still forces abstention
            # below.  This keeps the privacy scan proportional to the answer,
            # not to every claim that shares one everyday term.
            eligible_candidate_ids: set[int] = set()

            def claim_field_tokens(
                item: Mapping[str, Any],
                text: str,
                *,
                meaningful_only: bool,
            ) -> list[str]:
                return _memory()._memory_tokens(
                    text,
                    meaningful_only=meaningful_only,
                    cache_allowed=(
                        int(item["claim_id"]) in eligible_candidate_ids
                    ),
                )

            def claim_terms_match(
                item: Mapping[str, Any],
                query_set: set[str],
                record_set: set[str],
            ) -> set[str]:
                return _memory()._claim_matched_query_terms(
                    query_set,
                    record_set,
                    cache_allowed=(
                        int(item["claim_id"]) in eligible_candidate_ids
                    ),
                )

            # Identity is a safety boundary, not a scoring hint.  Inspect every
            # current claim subject against the full bounded query so an
            # identity inserted beyond the scoring-term cap cannot disappear.
            identity_rows_by_id: dict[int, _memory().sqlite3.Row] = {}
            for offset in range(
                0, len(raw_query_terms), _memory()._MAX_MEMORY_QUERY_TERM_CANDIDATES
            ):
                identity_terms = raw_query_terms[
                    offset:offset + _memory()._MAX_MEMORY_QUERY_TERM_CANDIDATES
                ]
                identity_patterns = [
                    f"%{_memory()._escape_like(term)}%" for term in identity_terms
                ]
                identity_where = " OR ".join(
                    "lower(subject) LIKE ? ESCAPE '\\'"
                    for _ in identity_patterns
                )
                identity_chunk = self.db.execute(
                    f"""SELECT c.id AS claim_id, c.scope, c.claim_key,
                               c.subject, c.predicate, c.value
                        FROM memory_claims AS c
                        WHERE {scope_filter_sql}
                          AND c.status IN ('active', 'disputed')
                          AND ({identity_where})
                        ORDER BY updated_at DESC, id DESC
                        LIMIT ?""",
                    [
                        *scope_filter_parameters,
                        *identity_patterns,
                        _memory().MAX_MEMORY_SEARCH_CANDIDATES + 1,
                    ],
                ).fetchall()
                if len(identity_chunk) > _memory().MAX_MEMORY_SEARCH_CANDIDATES:
                    return self._abstain_claims(
                        "identity-overflow", "identity candidates exceed the bound"
                    )
                for identity_row in identity_chunk:
                    identity_rows_by_id.setdefault(
                        int(identity_row["claim_id"]), identity_row
                    )
                if len(identity_rows_by_id) > _memory().MAX_MEMORY_SEARCH_CANDIDATES:
                    return self._abstain_claims(
                        "identity-overflow", "identity candidates exceed the bound"
                    )
            if project_scope is not None:
                project_identity_keys = {
                    str(row["claim_key"])
                    for row in identity_rows_by_id.values()
                    if str(row["scope"]) == project_scope
                }
                identity_rows_by_id = {
                    claim_id: row
                    for claim_id, row in identity_rows_by_id.items()
                    if (
                        str(row["scope"]) == project_scope
                        or str(row["claim_key"]) not in project_identity_keys
                    )
                }
            explicit_named_subject_heads: set[str] = set()
            support_inferred_subject_heads: set[str] = set()
            for identity_row in identity_rows_by_id.values():
                # This is a later, narrower SELECT with only a partial claim
                # snapshot. Never transfer cache admission by ID from the
                # earlier full-row validation.
                identity_cache_allowed = False
                identity_subject_terms = _memory()._memory_tokens(
                    str(identity_row["subject"]),
                    meaningful_only=True,
                    cache_allowed=identity_cache_allowed,
                )
                if not identity_subject_terms:
                    continue
                identity_head = identity_subject_terms[0]
                if not _memory()._claim_matched_query_terms(
                    raw_query_term_set,
                    {identity_head},
                    cache_allowed=identity_cache_allowed,
                ):
                    continue
                proper_or_structured_head = bool(
                    identity_head in raw_query_proper_terms
                    or (
                        any(character.isalpha() for character in identity_head)
                        and any(character.isdigit() for character in identity_head)
                    )
                    or bool(
                        set(identity_subject_terms[1:]).intersection(
                            _memory()._CLAIM_IDENTITY_DESCRIPTOR_TERMS
                        )
                    )
                )
                identity_support_terms = set(identity_subject_terms[1:])
                identity_support_terms.update(_memory()._memory_tokens(
                    f"{identity_row['predicate']} {identity_row['value']}",
                    meaningful_only=True,
                    cache_allowed=identity_cache_allowed,
                ))
                if proper_or_structured_head:
                    explicit_named_subject_heads.add(identity_head)
                elif _memory()._claim_matched_query_terms(
                    raw_query_term_set,
                    identity_support_terms,
                    cache_allowed=identity_cache_allowed,
                ):
                    support_inferred_subject_heads.add(identity_head)
            # An explicit proper/structured subject is stronger evidence than
            # a generic head inferred only because its predicate shares topic
            # words with the query. Without this priority, filler claims such
            # as "cluster node" can manufacture false identity ambiguity.
            raw_named_subject_heads = (
                explicit_named_subject_heads or support_inferred_subject_heads
            )
            if (
                len(raw_named_subject_heads) > 1
                and not explicit_multi_fact_query
            ):
                return self._abstain_claims(
                    "identity-conflict", "query names more than one stored subject"
                )
            # Two-anchor questions often ask for two independent facts (for
            # example, "tone and port"), so one exact anchor per claim is still
            # useful. Longer requests must match at least two non-metadata terms.
            minimum_matches = 1 if len(query_terms) <= 2 else 2
            structural_items: list[
                tuple[
                    tuple[int, int, int, int, str, int],
                    dict[str, Any],
                    int,
                    bool,
                ]
            ] = []
            query_term_set = set(query_terms)
            candidate_tokens = {
                int(candidate["claim_id"]): set(claim_field_tokens(
                    candidate,
                    " ".join((
                        str(candidate["subject"]),
                        str(candidate["predicate"]),
                        str(candidate["value"]),
                    )),
                    meaningful_only=True,
                ))
                for candidate in items
            }
            candidate_value_tokens = {
                int(candidate["claim_id"]): set(claim_field_tokens(
                    candidate,
                    str(candidate["value"]),
                    meaningful_only=True,
                ))
                for candidate in items
            }
            source_qualified_query = _memory().re.search(
                r"\b(?:according\s+to|reported\s+by|observed\s+by|"
                r"(?:source|authority)\s+(?:says|said|reports?|reported)|"
                r"(?:operator|verified|external|learned)\s+"
                r"(?:says|said|reports?|reported|source|statement|observation))\b",
                raw_query,
                _memory().re.I,
            ) is not None
            qualified_source_terms: set[str] = set()
            source_match = _memory().re.search(
                r"\b(?:according\s+to|reported\s+by|observed\s+by)\s+"
                r"([^,;:.!?]+)",
                raw_query,
                _memory().re.I,
            )
            if source_match:
                qualified_source_terms = set(_memory()._memory_tokens(
                    source_match.group(1), meaningful_only=True
                ))
            raw_query_identity_tokens = set(_memory()._memory_tokens(
                raw_query, meaningful_only=False
            ))
            ambiguous_compact_query = bool(
                len(query_terms) == 2
                and not explicit_multi_fact_query
            )
            items_by_claim_id = {
                int(candidate["claim_id"]): candidate for candidate in items
            }
            subject_head_by_claim = {
                int(candidate["claim_id"]): tokens[0]
                for candidate in items
                if (tokens := claim_field_tokens(
                    candidate,
                    str(candidate["subject"]),
                    meaningful_only=True,
                ))
            }
            named_subject_heads = raw_named_subject_heads or {
                head for claim_id, head in subject_head_by_claim.items()
                if claim_terms_match(
                    items_by_claim_id[claim_id], query_term_set, {head}
                )
            }
            if len(named_subject_heads) > 1 and not explicit_multi_fact_query:
                return self._abstain_claims(
                    "identity-conflict", "query names more than one stored subject"
                )
            candidate_query_matches = {
                claim_id: claim_terms_match(
                    items_by_claim_id[claim_id],
                    query_term_set,
                    record_tokens,
                )
                for claim_id, record_tokens in candidate_tokens.items()
            }
            independently_relevant_claim_ids = {
                claim_id
                for claim_id, matches in candidate_query_matches.items()
                if len(matches) >= minimum_matches
            }
            # ``_claim_matched_query_terms`` evaluates each query term
            # independently, so one whole-set match per claim yields exactly
            # the per-term anchors that one call per (term, claim) pair did,
            # at a cost that no longer multiplies by the number of query terms.
            query_anchor_claims = {
                term: {
                    claim_id
                    for claim_id in independently_relevant_claim_ids
                    if term in candidate_query_matches[claim_id]
                }
                for term in query_term_set
            }
            candidate_value_matches = {
                claim_id: claim_terms_match(
                    items_by_claim_id[claim_id],
                    query_term_set,
                    value_tokens,
                )
                for claim_id, value_tokens in candidate_value_tokens.items()
            }
            value_anchor_claims = {
                term: {
                    claim_id
                    for claim_id, value_matches in candidate_value_matches.items()
                    if term in value_matches
                }
                for term in query_term_set
            }
            for item in items:
                cache_allowed = int(item["claim_id"]) in eligible_candidate_ids
                subject_token_list = claim_field_tokens(
                    item,
                    str(item["subject"]),
                    meaningful_only=True,
                )
                raw_subject_token_list = claim_field_tokens(
                    item,
                    str(item["subject"]),
                    meaningful_only=False,
                )
                subject_tokens = set(subject_token_list)
                predicate_tokens = set(claim_field_tokens(
                    item,
                    str(item["predicate"]),
                    meaningful_only=True,
                ))
                value_tokens = set(claim_field_tokens(
                    item,
                    str(item["value"]),
                    meaningful_only=True,
                ))
                subject_matched = _memory()._claim_matched_query_terms(
                    query_term_set,
                    subject_tokens,
                    cache_allowed=cache_allowed,
                )
                predicate_matched = _memory()._claim_matched_query_terms(
                    query_term_set,
                    predicate_tokens,
                    cache_allowed=cache_allowed,
                )
                value_matched = _memory()._claim_matched_query_terms(
                    query_term_set,
                    value_tokens,
                    cache_allowed=cache_allowed,
                )
                matched = subject_matched | predicate_matched | value_matched
                if len(matched) < minimum_matches:
                    continue
                subject_head = (
                    subject_token_list[0] if subject_token_list else ""
                )
                head_matched = bool(subject_head) and bool(
                    _memory()._claim_matched_query_terms(
                        query_term_set,
                        {subject_head},
                        cache_allowed=cache_allowed,
                    )
                )
                raw_endpoint_identity_match = (
                    len(raw_subject_token_list) >= 4
                    and raw_subject_token_list[0]
                    in raw_query_identity_tokens
                    and raw_subject_token_list[-1]
                    in raw_query_identity_tokens
                )
                tail_subject_matched = subject_matched - (
                    {subject_head} if head_matched else set()
                )
                unmatched_query_terms = query_term_set - matched
                conflicting_value_anchors = {
                    term for term in unmatched_query_terms
                    if value_anchor_claims.get(term, set())
                    - {int(item["claim_id"])}
                }
                source_authority_tokens = set(claim_field_tokens(
                    item,
                    f"{item['source']} {item['authority']}",
                    meaningful_only=True,
                ))
                source_authority_matched = _memory()._claim_matched_query_terms(
                    raw_query_term_set,
                    source_authority_tokens,
                    cache_allowed=cache_allowed,
                )
                if source_qualified_query and (
                    not source_authority_matched
                    or (
                        qualified_source_terms
                        and not qualified_source_terms.issubset(
                            source_authority_tokens
                        )
                    )
                ):
                    continue
                unmatched_non_subject = (
                    query_term_set
                    - predicate_matched
                    - value_matched
                )
                other_claim_anchors = {
                    term
                    for term in unmatched_non_subject
                    if query_anchor_claims.get(term, set())
                    - {int(item["claim_id"])}
                }
                identity_conflict = (
                    (
                        ambiguous_compact_query
                        and len(matched) < len(query_term_set)
                    )
                    or (
                        len(subject_matched) >= 1
                        and subject_token_list
                        and subject_token_list[0] not in subject_matched
                        and _memory()._claim_subject_identity_conflict(
                            subject_token_list[0], query_term_set - matched
                        )
                    )
                    or (
                        not subject_matched
                        and bool(predicate_matched)
                        and bool(unmatched_non_subject)
                        and (
                            len(query_term_set) > 2
                            or not unmatched_non_subject.issubset(
                                other_claim_anchors
                            )
                        )
                    )
                    or (
                        not explicit_multi_fact_query
                        and bool(named_subject_heads)
                        and subject_head not in named_subject_heads
                    )
                    or (
                        not head_matched
                        and bool(tail_subject_matched)
                        and bool(unmatched_query_terms)
                        and (
                            len(value_matched) < 2
                            or bool(
                                unmatched_query_terms
                                & raw_query_proper_terms
                            )
                            or bool(
                                tail_subject_matched
                                & _memory()._CLAIM_IDENTITY_DESCRIPTOR_TERMS
                            )
                        )
                    )
                    or (
                        not explicit_multi_fact_query
                        and bool(subject_matched)
                        and bool(predicate_matched)
                        and bool(conflicting_value_anchors)
                    )
                )
                if len(query_term_set) > 2:
                    subject_matches = len(subject_matched)
                    predicate_matches = len(predicate_matched)
                    value_matches = len(value_matched)
                    required_subject_matches = (
                        1 if len(subject_tokens) <= 2 else 2
                    )
                    field_aligned = (
                        predicate_matches >= 1
                        and subject_matches >= required_subject_matches
                    )
                    compact_predicate_lookup = (
                        len(query_term_set) <= 4
                        and predicate_matches >= 2
                    )
                    predicate_value_aligned = (
                        predicate_matches >= 1 and value_matches >= 2
                    )
                    subject_value_aligned = (
                        value_matches >= 2
                        and (
                            (
                                len(subject_tokens) <= 2
                                and subject_matches >= 1
                            )
                            or (
                                len(subject_tokens) == 3
                                and subject_matches == len(subject_tokens)
                            )
                            or (
                                subject_matches >= 1
                                and raw_endpoint_identity_match
                            )
                        )
                    )
                    specific_subject_lookup = (
                        subject_matches >= 3
                        and subject_matched == query_term_set
                    )
                    if not (
                        field_aligned
                        or compact_predicate_lookup
                        or predicate_value_aligned
                        or subject_value_aligned
                        or specific_subject_lookup
                    ):
                        # Long natural questions contain verbs and qualifiers
                        # that do not belong in the stored claim. Require the
                        # query to identify both the claim subject and its
                        # predicate instead of using raw whole-query coverage,
                        # which rejected valid paraphrases. Predicate-only
                        # lookups remain available when they are compact and
                        # specific.
                        continue
                claim_score = (
                    len(matched),
                    len(predicate_matched),
                    len(subject_matched),
                    _memory()._CLAIM_AUTHORITY_WEIGHT[str(item["authority"])],
                    str(item["updated_at"]),
                    int(item["claim_id"]),
                )
                structural_items.append((
                    claim_score,
                    item,
                    len(matched),
                    identity_conflict,
                ))
            structural_items.sort(key=lambda pair: pair[0], reverse=True)
            scored_items: list[
                tuple[tuple[int, int, int, int, str, int], dict[str, Any], int]
            ] = []
            if structural_items:
                # Eligibility remains a blocking safety boundary, but it no
                # longer needs one SQL round trip for every lexical candidate.
                # The strongest score[:3] tier determines whether recall may
                # proceed: one corrupt or identity-conflicting peer in that tier
                # still forces abstention instead of a weaker substitution.
                strongest_blocking_tier = structural_items[0][0][:3]
                blocking_tier = [
                    pair for pair in structural_items
                    if pair[0][:3] == strongest_blocking_tier
                ]
                if any(
                    identity_conflict
                    for _score, _item, _count, identity_conflict in blocking_tier
                ):
                    return self._abstain_claims(
                        "identity-conflict", "strongest tier has an identity conflict"
                    )
                selection_relevance = (
                    structural_items[0][0][:1]
                    if len(query_term_set) <= 2
                    else strongest_blocking_tier
                )
                selected_structural = [
                    pair for pair in structural_items
                    if pair[0][:len(selection_relevance)] == selection_relevance
                ]
                validation_rows = {
                    int(item["claim_id"]): item
                    for _score, item, _count, _identity_conflict in (
                        *blocking_tier,
                        *selected_structural,
                    )
                }
                eligible_candidate_ids = self._claim_rows_recall_eligible(
                    list(validation_rows.values()), project_id=project_id
                )
                eligible_ids = eligible_candidate_ids
                strongest_claim_ids = {
                    int(item["claim_id"])
                    for _score, item, _count, _identity_conflict in blocking_tier
                }
                if not strongest_claim_ids.issubset(eligible_ids):
                    return self._abstain_claims(
                        "corrupt-strongest",
                        "strongest candidate failed integrity or privacy checks",
                    )
                scored_items = [
                    (score, item, matched_count)
                    for score, item, matched_count, identity_conflict
                    in selected_structural
                    if (
                        not identity_conflict
                        and int(item["claim_id"]) in eligible_ids
                    )
                ]
            if scored_items:
                # Return only the strongest lexical specificity tier.  This
                # preserves equal-strength dispute pairs and compact multi-fact
                # lookups while preventing boilerplate overlap from appending
                # unrelated claim keys behind the actual answer.
                strongest_relevance = (
                    scored_items[0][0][:1]
                    if len(query_term_set) <= 2
                    else scored_items[0][0][:3]
                )
                scored_items = [
                    pair for pair in scored_items
                    if pair[0][:len(strongest_relevance)] == strongest_relevance
                ]
                if not explicit_multi_fact_query:
                    ambiguity_items = [
                        pair for pair in scored_items
                        if (
                            not named_subject_heads
                            or subject_head_by_claim.get(
                                int(pair[1]["claim_id"]), ""
                            ) in named_subject_heads
                        )
                    ]
                    ambiguous_keys = {
                        str(item["claim_key"])
                        for _score, item, _matched_count in ambiguity_items
                    }
                    if len(ambiguous_keys) > 1:
                        distinct_subjects = {
                            " ".join(claim_field_tokens(
                                item,
                                str(item["subject"]),
                                meaningful_only=True,
                            ))
                            for _score, item, _matched_count in ambiguity_items
                        }
                        fully_qualified_constellation = (
                            len(query_term_set) > 2
                            and len(ambiguity_items) <= limit
                            and all(
                                matched_count == len(query_term_set)
                                for _score, _item, matched_count
                                in ambiguity_items
                            )
                        )
                        if (
                            len(distinct_subjects) > 1
                            and not fully_qualified_constellation
                        ):
                            return self._abstain_claims(
                                "ambiguous", "equal-strength claims about different subjects"
                            )

            # Compare current candidates only with their own canonical history.
            # Cap work per claim identity, and fail closed for an identity whose
            # history exceeds that cap, so an old conflicting value can never be
            # hidden beyond one global recency limit.
            candidate_identities = list(dict.fromkeys(
                (str(item["scope"]), str(item["claim_key"]))
                for _score, item, _matched_count in scored_items
            ))
            superseded_by_key: dict[tuple[str, str], list[set[str]]] = {}
            truncated_history_keys: set[tuple[str, str]] = set()
            try:
                for offset in range(0, len(candidate_identities), 400):
                    identity_chunk = candidate_identities[offset:offset + 400]
                    placeholders = ",".join("(?, ?)" for _pair in identity_chunk)
                    identity_parameters = [
                        value
                        for pair in identity_chunk
                        for value in pair
                    ]
                    historical_rows = self.db.execute(
                        f"""SELECT scope, claim_key, subject, predicate, value,
                                   version_count
                            FROM (
                                SELECT scope, claim_key, subject, predicate, value,
                                       COUNT(*) OVER (
                                           PARTITION BY scope, claim_key
                                       ) AS version_count,
                                       ROW_NUMBER() OVER (
                                           PARTITION BY scope, claim_key
                                           ORDER BY updated_at DESC, id DESC
                                       ) AS version_rank
                                FROM memory_claims
                                WHERE status='superseded'
                                  AND (scope, claim_key) IN ({placeholders})
                            )
                            WHERE version_rank<=?""",
                        [*identity_parameters, _memory()._MAX_SUPERSEDED_CLAIM_VERSIONS],
                    ).fetchall()
                    for historical in historical_rows:
                        key = (
                            str(historical["scope"]),
                            str(historical["claim_key"]),
                        )
                        if int(historical["version_count"]) > (
                            _memory()._MAX_SUPERSEDED_CLAIM_VERSIONS
                        ):
                            truncated_history_keys.add(key)
                        historical_tokens = set(_memory()._memory_tokens(
                            " ".join((
                                str(historical["subject"]),
                                str(historical["predicate"]),
                                str(historical["value"]),
                            )),
                            meaningful_only=True,
                            cache_allowed=False,
                        ))
                        superseded_by_key.setdefault(key, []).append(
                            historical_tokens
                        )
            except _memory().sqlite3.DatabaseError:
                return self._abstain_claims("error", "claim history read failed")

            relevant_items: list[
                tuple[tuple[int, int, int, int, str, int], dict[str, Any]]
            ] = []
            for score, item, matched_count in scored_items:
                key = (str(item["scope"]), str(item["claim_key"]))
                if key in truncated_history_keys:
                    continue
                historical_versions = superseded_by_key.get(key, ())
                if any(
                    len(_memory()._claim_matched_query_terms(
                        query_term_set,
                        historical_tokens,
                        cache_allowed=False,
                    ))
                    > matched_count
                    for historical_tokens in historical_versions
                ):
                    # The query fits an older value better than the current
                    # value. Abstain instead of substituting the newer fact and
                    # pretending it answered the operator's exact question.
                    continue
                relevant_items.append((score, item))
            items = [item for _score, item in relevant_items]
        else:
            eligible_items: list[dict[str, Any]] = []
            batch_size = max(32, min(_memory()._CLAIM_RECALL_BATCH_SIZE, limit * 2))
            for offset in range(0, len(items), batch_size):
                chunk = items[offset:offset + batch_size]
                eligible_ids = self._claim_rows_recall_eligible(
                    chunk, project_id=project_id
                )
                eligible_items.extend(
                    item for item in chunk
                    if int(item["claim_id"]) in eligible_ids
                )
                if len(eligible_items) >= limit:
                    break
            items = eligible_items
        for item in items:
            item.pop("claim_key", None)
        items = items[:limit]
        self._last_claim_recall_report["returned"] = len(items)
        if clock_mode == "disabled":
            return items
        for item in items:
            if str(item.get("scope") or "") != "global":
                # An exact operator-authored project fact remains current until
                # it is explicitly superseded. Do not age it with a volatility
                # model learned from global or other-project observations.
                stored_confidence = float(item["confidence"])
                item.update(
                    {
                        "stored_confidence": stored_confidence,
                        "effective_confidence": stored_confidence,
                        "hazard_per_day": 0.0,
                        "clock_pair_count": 0,
                        "clock_status": "explicit_project",
                        "supported_at": str(item["updated_at"]),
                        "age_days": 0.0,
                    }
                )
                continue
            normalized = self._claim_clock_predicate(str(item["predicate"]))
            fit = self.db.execute(
                """SELECT hazard_per_day, pair_count, vocabulary_size, fitted_at
                   FROM memory_claim_volatility WHERE predicate=?""",
                (normalized,),
            ).fetchone()
            support_rows = self.db.execute(
                """SELECT observed_at
                   FROM memory_claim_observations WHERE claim_id=?
                   ORDER BY id DESC LIMIT 4001""",
                (int(item["claim_id"]),),
            ).fetchall()
            hazard = (
                float(fit["hazard_per_day"])
                if fit is not None else _memory().DEFAULT_HAZARD_PER_DAY
            )
            pair_count = int(fit["pair_count"]) if fit is not None else 0
            vocabulary_size = int(fit["vocabulary_size"]) if fit is not None else 2
            supported_at = str(item["updated_at"])
            if len(support_rows) <= 4000 and _memory()._recall_timestamp_valid(read_at):
                read_time = _memory().datetime.fromisoformat(
                    read_at.replace("Z", "+00:00")
                )
                valid_support: list[tuple[_memory().datetime, str]] = []
                for support in support_rows:
                    candidate = str(support["observed_at"] or "")
                    if not _memory()._recall_timestamp_valid(candidate):
                        continue
                    parsed_candidate = _memory().datetime.fromisoformat(
                        candidate.replace("Z", "+00:00")
                    )
                    if parsed_candidate <= read_time:
                        valid_support.append((parsed_candidate, candidate))
                if valid_support:
                    supported_at = max(valid_support, key=lambda pair: pair[0])[1]
            elapsed = _memory().claim_age_days(supported_at, read_at)
            immutable = _memory().protected_predicate(normalized)
            stored_confidence = float(item["confidence"])
            effective = _memory().claim_effective_confidence(
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
            self._pending_claim_clock_updates.append(
                (int(item["claim_id"]), stale_read, effective, clock_status, read_at)
            )
        return items
