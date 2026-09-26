"""Current Memory domain extracted without changing method control flow.

Global dependencies resolve through jarvis.memory at call time. The facade keeps
public imports, patch seams, constants and authorization helpers authoritative.
"""
from __future__ import annotations

from . import memory_compaction
from collections.abc import Mapping
from typing import Any
from .memory_runtime import (_memory, _with_read_snapshot)


class ConversationCompactionMemoryMixin:
    """Mechanically extracted current Memory methods."""

    def _compaction_message_rows(
        self, conversation_id: int
    ) -> list[Any]:
        """Every persisted turn of one conversation, as ``MessageRow``s."""
        return [
            _memory().memory_compaction.MessageRow(
                id=int(row["id"]),
                conversation_id=int(row["conversation_id"]),
                created_at=str(row["created_at"]),
                role=str(row["role"]),
                content=str(row["content"]),
            )
            for row in self.db.execute(
                """SELECT id, conversation_id, created_at, role, content
                   FROM messages WHERE conversation_id=? ORDER BY id""",
                (int(conversation_id),),
            )
        ]

    def _held_back_message_ids(self, conversation_id: int) -> list[int]:
        """Message ids a live fact proposal references (H-2).

        These stay live and the candidate region is partitioned around them.
        The proposal row is the anti-forgery record a ``store it`` confirmation
        resolves against, so it is never deleted, nulled, or stripped of its
        foreign key -- and the key is why an unpartitioned delete would abort
        with ``IntegrityError`` in the first place.
        """
        return [
            int(row[0])
            for row in self.db.execute(
                """SELECT assistant_message_id FROM memory_fact_proposals
                   WHERE conversation_id=? AND assistant_message_id IS NOT NULL""",
                (int(conversation_id),),
            )
        ]

    def _span_busy_reason(self, conversation_id: int) -> str | None:
        """Why this conversation may not be compacted right now, or ``None``.

        Every read is existence-guarded: ``long_horizon_plans`` is created
        lazily by ``long_horizon``, not by ``_migrate``, so an unguarded query
        raises ``OperationalError`` on a store that never ran a workflow (L-4).
        ``approvals`` has no ``conversation_id`` column and is joined by scope.
        """
        scope = f"conversation:{int(conversation_id)}"
        if self.db.execute(
            "SELECT 1 FROM approvals WHERE scope=? AND status='pending' LIMIT 1",
            (scope,),
        ).fetchone() is not None:
            return "approval_pending"
        if self.db.execute(
            """SELECT 1 FROM presence_jobs WHERE conversation_id=?
               AND status IN ('queued', 'running') LIMIT 1""",
            (int(conversation_id),),
        ).fetchone() is not None:
            return "job_active"
        if _memory()._sqlite_table_exists(self.db, "long_horizon_plans"):
            if self.db.execute(
                """SELECT 1 FROM long_horizon_plans WHERE conversation_id=?
                   AND status IN ('active', 'paused') LIMIT 1""",
                (int(conversation_id),),
            ).fetchone() is not None:
                return "workflow_active"
        return None

    def _conversation_scope(self, conversation_id: int) -> int | None:
        """The conversation's project at READ time, never a denormalised copy
        (M-18): a conversation can be moved between projects."""
        row = self.db.execute(
            "SELECT project_id FROM conversations WHERE id=?",
            (int(conversation_id),),
        ).fetchone()
        return None if row is None else int(row[0] or 1)

    def _compaction_events(
        self, conversation_id: int, after: int, through: int | None = None
    ) -> tuple[Any, ...]:
        """Spine events for one conversation in ``(after, through]``.

        The SELECT is built from ``memory_compaction.SPINE_EVENT_COLUMNS`` so
        the column ORDER cannot drift from the reader's: core's own rebuild hit
        a ``TypeError`` on a connection without a row factory, and this store
        would never have reproduced it because ``Memory`` sets ``sqlite3.Row``.
        """
        columns = ", ".join(_memory().memory_compaction.SPINE_EVENT_COLUMNS)
        if through is None:
            rows = self.db.execute(
                f"""SELECT {columns} FROM memory_spine_events
                    WHERE conversation_id=? AND id > ? ORDER BY id""",
                (int(conversation_id), int(after)),
            ).fetchall()
        else:
            rows = self.db.execute(
                f"""SELECT {columns} FROM memory_spine_events
                    WHERE conversation_id=? AND id > ? AND id <= ? ORDER BY id""",
                (int(conversation_id), int(after), int(through)),
            ).fetchall()
        return _memory().memory_compaction.spine_event_rows(rows)

    def _previous_watermark(self, conversation_id: int) -> int:
        """The newest milestone's ``through`` for this conversation, 0 if none.

        ``after`` for the next milestone, so the ranges stay disjoint and
        gap-free (N-3).
        """
        row = self.db.execute(
            """SELECT id, invariants_json FROM memory_milestones
               WHERE conversation_id=? ORDER BY seq DESC LIMIT 1""",
            (int(conversation_id),),
        ).fetchone()
        if row is None:
            # No previous milestone is not a failure: the first span starts at
            # zero by definition.
            return 0
        try:
            derived = _memory().json.loads(str(row["invariants_json"]))["derived"]
            return int(derived["event_range"]["through"])
        except (TypeError, ValueError, KeyError) as error:
            # Red team M-7.  This used to fall back to 0, which is the most
            # dangerous value available: it makes the NEXT milestone claim
            # every event from the beginning of the conversation, overlapping
            # every range already written, and nothing downstream detects it
            # because overlapping ranges still replay.  An unreadable previous
            # milestone means the watermark is unknown, and an unknown
            # watermark refuses.
            raise _memory().memory_spine.SpineError(
                f"milestone {int(row['id'])} has an unreadable event range; "
                "refusing to compute a watermark from it (run compaction "
                "verify)",
                code="watermark_unreadable",
            ) from error

    @classmethod
    def _compaction_plan_token(cls, plan: Mapping[str, Any]) -> str:
        """Twelve hex binding a dry run to the store it described: the
        conversation, every span's identity digest, and the held-back set."""
        return _memory().memory_spine.sha256_hex(
            _memory().memory_spine.canonical([
                "compaction",
                int(plan.get("conversation_id") or 0),
                [str(span["span_unkeyed_sha256"]) for span in plan.get("spans") or []],
                sorted(int(item) for item in plan.get("held_back_messages") or []),
            ])
        )[:12]

    def compact_conversation(
        self,
        conversation_id: int,
        *,
        keep_turns: int | None = None,
        min_span_chars: int | None = None,
        max_span_chars: int | None = None,
        apply: bool = False,
        plan_token: str | None = None,
    ) -> dict[str, Any]:
        """Plan or apply one compaction pass over a conversation.

        Dry run by default, returning the plan and a token that binds it to the
        store it described; ``apply=True`` requires that token and refuses
        ``stale_plan`` if the store moved.  There is no model author and no
        summariser argument: M5 ships neither (M-16).
        """
        if not apply:
            return self._compact_conversation_dry_run(
                conversation_id, keep_turns=keep_turns,
                min_span_chars=min_span_chars, max_span_chars=max_span_chars,
            )
        return self._apply_compaction(
            conversation_id, keep_turns=keep_turns,
            min_span_chars=min_span_chars, max_span_chars=max_span_chars,
            plan_token=plan_token,
        )

    def _compaction_settings(
        self,
        keep_turns: int | None,
        min_span_chars: int | None,
        max_span_chars: int | None,
    ) -> dict[str, int]:
        """Module constants, never environment: half A ships no
        ``JARVIS_COMPACTION_*`` surface at all, and therefore no flag anyone
        can leave on by accident."""
        return {
            "keep_turns": (_memory().memory_compaction.DEFAULT_KEEP_TURNS
                           if keep_turns is None else max(0, int(keep_turns))),
            "min_span_chars": (_memory().memory_compaction.DEFAULT_MIN_SPAN_CHARS
                               if min_span_chars is None
                               else max(0, int(min_span_chars))),
            "max_span_chars": (_memory().memory_compaction.DEFAULT_MAX_SPAN_CHARS
                               if max_span_chars is None
                               else max(1, int(max_span_chars))),
            "max_span_messages": _memory().memory_compaction.MAX_SPAN_MESSAGES,
        }

    @_with_read_snapshot
    def _compact_conversation_dry_run(
        self,
        conversation_id: int,
        *,
        keep_turns: int | None = None,
        min_span_chars: int | None = None,
        max_span_chars: int | None = None,
    ) -> dict[str, Any]:
        self._ensure_open()
        settings = self._compaction_settings(
            keep_turns, min_span_chars, max_span_chars
        )
        report: dict[str, Any] = {
            "conversation_id": int(conversation_id),
            "applied": False,
            "refusal": None,
            "refusal_detail": None,
            "plan_token": None,
            "spans": [],
            "skipped": [],
            "held_back_messages": [],
            "candidate_rows": 0,
            "candidate_chars": 0,
            **settings,
        }
        refusal = self._compaction_refusal()
        if refusal is not None:
            report["refusal"] = refusal
            return report
        if self._conversation_scope(conversation_id) is None:
            report["refusal"] = "error"
            report["refusal_detail"] = "no such conversation"
            return report
        rows = self._compaction_message_rows(conversation_id)
        plan = _memory().memory_compaction.plan_spans(
            int(conversation_id),
            rows,
            proposal_message_ids=self._held_back_message_ids(conversation_id),
            busy_reason=self._span_busy_reason(conversation_id),
            **settings,
        )
        by_id = {int(row.id): row for row in rows}
        report["candidate_rows"] = plan.candidate_rows
        report["candidate_chars"] = plan.candidate_chars
        report["held_back_messages"] = [
            int(item) for item in plan.held_back_message_ids
        ]
        report["skipped"] = [
            {"first_message_id": item.first_message_id,
             "last_message_id": item.last_message_id,
             "message_count": item.message_count,
             "source_chars": item.source_chars,
             "reason": item.reason}
            for item in plan.skipped
        ]
        if plan.refusal is not None:
            report["refusal"] = plan.refusal
            report["refusal_detail"] = plan.refusal_detail
            return report
        seq = _memory().memory_compaction.next_seq(self.db, int(conversation_id))
        after = self._previous_watermark(conversation_id)
        for bounds in plan.spans:
            span = self._build_span(bounds, by_id, seq=seq, after=after)
            report["spans"].append({
                "seq": span.seq,
                "handle": span.handle,
                "first_message_id": span.first_message_id,
                "last_message_id": span.last_message_id,
                "message_count": span.message_count,
                "source_chars": span.source_chars,
                "stored_bytes": span.stored_bytes,
                "summary_chars": span.summary_chars,
                "span_unkeyed_sha256": span.span_unkeyed_sha256,
                "screened": span.screened,
                "event_range": span.event_range,
                "reduction_ratio": span.reduction_ratio,
            })
            after = int(span.event_range["through"])
            seq += 1
        report["plan_token"] = self._compaction_plan_token(report)
        return report

    def _build_span(
        self, bounds: Any, by_id: Mapping[int, Any], *, seq: int, after: int
    ) -> Any:
        """One sub-region's whole record, built by ``memory_compaction``.

        ``CompactionPlan.spans`` carries ``SpanBounds`` -- the ids, not the
        rows -- so this layer supplies the messages from the list it already
        read.  Selecting by the bounds' own ``message_ids`` rather than by a
        range keeps the N-1 discipline even here: a range lookup over a global
        id sequence would pull in another conversation's interleaved rows.

        The watermark is computed per sub-region (N-3): ``through`` is the
        largest event id for this conversation at or before THIS sub-region's
        own last message.  One pass-wide watermark would give every event to
        whichever milestone happened to be written first and leave the rest
        with an empty range -- a false statement that replays false identically
        and so passes a rebuild check.
        """
        messages = [
            by_id[int(message_id)]
            for message_id in bounds.message_ids
            if int(message_id) in by_id
        ]
        events = self._compaction_events(bounds.conversation_id, after)
        return _memory().memory_compaction.build_compacted_span(
            span=bounds,
            messages=messages,
            events=events,
            after=int(after),
            seq=int(seq),
            key=self._spine_key,
        )

    def _compaction_refusal(self) -> str | None:
        """The fail-closed preconditions shared by every compaction entry."""
        if not self._compaction_ready:
            return "schema_too_old"
        if not self._spine_ready:
            return "spine_unverified"
        try:
            if not self.verify_spine()["ok"]:
                return "spine_unverified"
        except _memory().memory_spine.SpineError:
            return "spine_unverified"
        if not self._spine_key:
            return "key_unavailable"
        return None

    def _apply_compaction(
        self,
        conversation_id: int,
        *,
        keep_turns: int | None,
        min_span_chars: int | None,
        max_span_chars: int | None,
        plan_token: str | None,
    ) -> dict[str, Any]:
        self._ensure_open()
        if self.db.in_transaction:
            plan = {"conversation_id": int(conversation_id), "applied": False,
                    "refusal": "transaction_already_open", "spans": []}
            return plan
        plan = self._compact_conversation_dry_run(
            conversation_id, keep_turns=keep_turns,
            min_span_chars=min_span_chars, max_span_chars=max_span_chars,
        )
        if plan["refusal"] is not None or not plan["spans"]:
            return plan
        if plan_token is not None and plan_token != plan["plan_token"]:
            plan["refusal"] = "stale_plan"
            return plan
        settings = self._compaction_settings(
            keep_turns, min_span_chars, max_span_chars
        )
        stamp = _memory().now_iso()
        written: list[dict[str, Any]] = []
        self._recall_cache.clear()
        try:
            return self._apply_compaction_locked(
                conversation_id, plan, settings, stamp, written
            )
        except _memory().sqlite3.Error as error:
            # Design 2.9: any ``sqlite3.Error`` aborts and reports, and a
            # locked database never turns a turn into a crash.  The
            # transaction context manager has already rolled back, so nothing
            # is half-written; what must not happen is the traceback escaping
            # into a caller that was only asking to tidy a transcript.
            plan["refusal"] = "error"
            plan["refusal_detail"] = type(error).__name__
            return plan

    def _apply_compaction_locked(
        self,
        conversation_id: int,
        plan: dict[str, Any],
        settings: dict[str, int],
        stamp: str,
        written: list[dict[str, Any]],
    ) -> dict[str, Any]:
        with self._immediate_transaction():
            # Re-resolve under the lock and refuse ``stale_span`` if the store
            # moved between the snapshot and the write (M-17's TOCTOU).
            live_rows = self._compaction_message_rows(conversation_id)
            current = _memory().memory_compaction.plan_spans(
                int(conversation_id),
                live_rows,
                proposal_message_ids=self._held_back_message_ids(conversation_id),
                busy_reason=self._span_busy_reason(conversation_id),
                **settings,
            )
            live_by_id = {int(row.id): row for row in live_rows}
            if current.refusal is not None:
                plan["refusal"] = current.refusal
                plan["refusal_detail"] = current.refusal_detail
                return plan
            seq = _memory().memory_compaction.next_seq(self.db, int(conversation_id))
            after = self._previous_watermark(conversation_id)
            rebuilt: list[Any] = []
            for bounds in current.spans:
                span = self._build_span(bounds, live_by_id, seq=seq, after=after)
                rebuilt.append(span)
                after = int(span.event_range["through"])
                seq += 1
            if [item.span_unkeyed_sha256 for item in rebuilt] != [
                str(item["span_unkeyed_sha256"]) for item in plan["spans"]
            ]:
                plan["refusal"] = "stale_span"
                return plan
            # The FTS scrub configures SUBSEQUENT deletions, so it goes BEFORE
            # them, matching ``delete_conversation`` (M-4).
            _memory().memory_spine.fts_secure_delete(self.db, "message_fts")
            for span in rebuilt:
                event_id = _memory().memory_spine.append_event(
                    self.db,
                    self._spine_key,
                    kind=_memory().memory_compaction.COMPACTION_SPINE_KIND,
                    actor="operator",
                    source="transcript compaction",
                    scope=(f"project:{self._conversation_scope(conversation_id)}"),
                    permission="operator:cli",
                    outcome="applied",
                    payload=span.spine_payload(at=stamp),
                    now=stamp,
                    conversation_id=int(conversation_id),
                    subject_kind="conversation",
                    subject_id=int(conversation_id),
                )
                milestone = span.milestone_row(
                    created_at=stamp, spine_event_id=int(event_id)
                )
                columns = ", ".join(milestone)
                placeholders = ", ".join("?" for _ in milestone)
                cursor = self.db.execute(
                    f"INSERT INTO memory_milestones({columns}) "
                    f"VALUES ({placeholders})",
                    tuple(milestone.values()),
                )
                milestone_id = int(cursor.lastrowid)
                row = span.span_row(milestone_id=milestone_id)
                columns = ", ".join(row)
                placeholders = ", ".join("?" for _ in row)
                self.db.execute(
                    f"INSERT INTO memory_compacted_spans({columns}) "
                    f"VALUES ({placeholders})",
                    tuple(row.values()),
                )
                # ``conversation_id`` is not optional on this DELETE and is not
                # defensive tidiness (N-1).  ``messages.id`` is one global
                # sequence, so an unscoped range names the live rows of every
                # other conversation whose ids interleave -- rows that exist in
                # no span blob.  Measured on this host: a 60-conversation store
                # gives one conversation an id range spanning 49,922 rows of
                # which 834 are its own.
                predicate, parameters = span.range_predicate()
                self.db.execute(
                    f"DELETE FROM messages WHERE {predicate}", parameters
                )
                written.append({
                    "seq": span.seq, "handle": span.handle,
                    "milestone_id": milestone_id, "spine_event_id": int(event_id),
                    "message_count": span.message_count,
                    "source_chars": span.source_chars,
                    "stored_bytes": span.stored_bytes,
                })
        self._recall_cache.clear()
        plan["applied"] = True
        plan["written"] = written
        return plan

    @_with_read_snapshot
    def conversation_milestones(
        self,
        conversation_id: int,
        *,
        project_id: int | None = None,
        before_message_id: int | None = None,
        limit: int = 6,
        char_budget: int = memory_compaction.COMPACTED_HISTORY_LIMIT,
    ) -> dict[str, Any]:
        """Milestone summaries covering history the caller is about to drop.

        Never raises: every failure is a mode.  The read runs in one deferred
        snapshot and never takes the write lock, so a concurrent writer can
        never turn a foreground turn into an error.
        """
        blank = {"rows": [], "overflow": False, "report": {"mode": "none"}}
        try:
            self._ensure_open()
            if not self._compaction_ready:
                blank["report"] = {"mode": "none"}
                return blank
            scope = self._conversation_scope(conversation_id)
            if scope is None or (
                project_id is not None and int(project_id) != scope
            ):
                return {"rows": [], "overflow": False,
                        "report": {"mode": "project-unavailable"}}
            deadline = _memory().time.monotonic() + (
                _memory().memory_compaction.DEFAULT_READ_BUDGET_MS / 1000.0
            )
            # ``before_message_id`` carries ``conversation_id`` alongside the
            # range: message ids are global and interleave (N-1).
            # Red team M-6.  The scan was unbounded, so a conversation with
            # 3,011 milestones did not merely go slow -- the 10 ms deadline
            # expired part-way through and the call returned ZERO rows.  That
            # is the silent scale cliff M1 shipped and a whole phase went into
            # repairing: the store answering "nothing" when it means "too
            # many".  The SQL is bounded instead, and the bound is generous
            # enough that skipped rows (a milestone whose span is gone) cannot
            # starve the page.
            scan_limit = max(1, int(limit)) * 4 + 8
            if before_message_id is None:
                cursor = self.db.execute(
                    """SELECT * FROM memory_milestones WHERE conversation_id=?
                       ORDER BY seq DESC LIMIT ?""",
                    (int(conversation_id), scan_limit + 1),
                )
            else:
                cursor = self.db.execute(
                    """SELECT * FROM memory_milestones
                       WHERE conversation_id=? AND last_message_id < ?
                       ORDER BY seq DESC LIMIT ?""",
                    (int(conversation_id), int(before_message_id),
                     scan_limit + 1),
                )
            # One query for span existence over the whole bounded page,
            # instead of one per scanned row.  The set is small by
            # construction because the scan itself is bounded.
            candidates = cursor.fetchall()
            page_handles = [str(item["handle"]) for item in candidates]
            live_handles: set[str] = set()
            if page_handles:
                placeholders = ", ".join("?" for _ in page_handles)
                live_handles = {
                    str(item[0]) for item in self.db.execute(
                        f"SELECT handle FROM memory_compacted_spans "
                        f"WHERE handle IN ({placeholders})",
                        page_handles,
                    )
                }
            rows: list[dict[str, Any]] = []
            partial = False
            scanned = 0
            beyond_scan = False
            for row in candidates:
                scanned += 1
                if scanned > scan_limit:
                    # More milestones exist than this page will consider.  Say
                    # so; do not return an empty page and let the caller read
                    # it as "no history".
                    beyond_scan = True
                    break
                if _memory().time.monotonic() > deadline:
                    return {"rows": [], "overflow": True,
                            "report": {"mode": "budget-exceeded"}}
                if str(row["handle"]) not in live_handles:
                    # The milestone outlived its span: a real state after an
                    # erase, reported as such and never as an error.
                    partial = True
                    continue
                try:
                    # Parsed ONCE per row.  Parsing the same blob a second
                    # time for ``observed`` doubled the JSON cost of the
                    # whole scan, which at 3,011 milestones was measurable.
                    invariants = _memory().json.loads(str(row["invariants_json"]))
                    derived = invariants["derived"]
                except (TypeError, ValueError, KeyError):
                    partial = True
                    continue
                rows.append({
                    "seq": int(row["seq"]),
                    "handle": str(row["handle"]),
                    "summary": str(row["summary"]),
                    "message_ids": derived.get("message_ids") or {},
                    "claim_keys": list(derived.get("claim_keys") or []),
                    "files_touched": list(
                        (invariants.get("observed") or {}).get(
                            "files_touched") or []
                    ),
                    # Design item 11.19, one layer upstream of the renderer's
                    # fix.  ``or "complete"`` manufactured the STRONGEST value
                    # in the closed set out of an absence, on the one surface
                    # that reaches a model: the closed set absorbed the
                    # unknown, the model was told the span completed, and the
                    # renderer's ``outcome_missing`` counter could never leave
                    # zero because it never saw a silent row.  The absence is
                    # propagated instead; ``render_compacted_history_block``
                    # maps anything outside ``DERIVED_OUTCOMES`` to
                    # ``unstated`` and counts it.
                    "outcome": derived.get("outcome"),
                })
            rows.reverse()
            kept, overflow = _memory().memory_compaction.fit_history_rows(
                rows, char_budget=int(char_budget), max_rows=int(limit)
            )
            # Screen AFTER the trim, not during the scan.  H-6 requires the
            # screen on every row this call RETURNS, which is what this is;
            # screening every row it merely SCANNED cost 21 of the 24 ms
            # measured at 3,011 milestones, because ``screen_endpoint`` ran
            # twice per scanned row instead of twice per returned one.
            # Screening only ever drops entries, so a row can only get
            # smaller and the budget ``fit_history_rows`` enforced still holds.
            for entry in kept:
                claim_keys, _dropped = _memory().memory_compaction.screen_entries(
                    entry["claim_keys"]
                )
                files_touched, _dropped = _memory().memory_compaction.screen_entries(
                    entry["files_touched"]
                )
                entry["claim_keys"] = list(claim_keys)
                entry["files_touched"] = list(files_touched)
            if not kept:
                mode = "partial" if (partial or beyond_scan) else "none"
            else:
                mode = "partial" if (partial or beyond_scan) else "complete"
            return {"rows": kept,
                    "overflow": bool(overflow or partial or beyond_scan),
                    "report": {"mode": mode}}
        except _memory().sqlite3.Error:
            return {"rows": [], "overflow": False, "report": {"mode": "error"}}

    @_with_read_snapshot
    def rehydrate(
        self, handle: str, *, project_id: int | None = None
    ) -> dict[str, Any]:
        """The exact original rows of one compacted span, or a closed refusal.

        Scope is resolved here, and an out-of-scope handle raises
        ``unknown_handle`` rather than a distinct code: a scope-specific
        refusal is a cross-project existence oracle (M-15).
        """
        self._ensure_open()
        if not self._compaction_ready:
            raise _memory().memory_compaction.RehydrationError(
                "compaction tables are absent", code="store_unavailable"
            )
        parsed = _memory().memory_compaction.try_parse_handle(str(handle))
        if parsed is None:
            raise _memory().memory_compaction.RehydrationError(
                "handle is malformed", code="malformed_handle"
            )
        if project_id is not None:
            scope = self._conversation_scope(parsed.conversation_id)
            if scope is None or int(project_id) != scope:
                raise _memory().memory_compaction.RehydrationError(
                    "no such handle", code="unknown_handle"
                )
        return _memory().memory_compaction.rehydrate(self.db, self._spine_key, str(handle))

    def verify_compaction(self) -> dict[str, Any]:
        """Compaction health, with the chain qualifier resolved here.

        ``memory_compaction.verify_compaction`` confirms every receipt is
        present and every recorded digest matches its record, and it never
        verifies the chain those records live on -- correctly, because a pure
        function cannot know.  A forged chain leaves all of those facts true,
        so a bare ``ok`` renders a clean compaction line over a spine that does
        not verify.  ``Memory`` owns ``verify_spine``, so the wrapper answers
        the question rather than passing it to an operator surface where the
        reader would supply the optimistic answer.  ``chain_verified`` is never
        ``None`` from here.
        """
        self._ensure_open()
        return _memory().memory_compaction.verify_compaction(
            self.db, self._spine_key, spine_ok=self._spine_chain_ok()
        )

    def _spine_chain_ok(self) -> bool:
        """Whether the keyed chain verifies, as a plain bool that never
        raises: a broken spine is the case this exists to report."""
        if not self._spine_ready:
            return False
        try:
            return bool(self.verify_spine()["ok"])
        except (_memory().memory_spine.SpineError, _memory().sqlite3.Error):
            return False

    def rebuild_milestones(
        self, *, include_derived: bool = False
    ) -> dict[str, Any]:
        """Re-derive every milestone's ``derived`` from the spine and JUDGE it.

        Design 11.18: the equivalence is computed HERE, from invariant rows
        this method fetches itself, against digests ``memory_compaction``
        derived from the spine.  Two sources, one comparison.  An equality
        computed inside a single call that reads both sides cannot fail, and a
        gate that cannot fail is not a gate -- a defect populating the stored
        side from the rebuilt value would make E-2 pass unconditionally.

        ``rebuild_equivalence_derived`` is ``None`` with a ``reason`` whenever
        it cannot honestly be a ratio: nothing derived, nothing to compare, or
        a partial derivation.  It is never a flattering ``1.0`` over an empty
        or incomplete set.
        """
        self._ensure_open()
        # ONE spine verification, shared by both calls: the rebuild and the
        # verifier must be describing the same store, and verifying twice
        # invites them to disagree.
        spine_ok = self._spine_chain_ok()
        report = _memory().memory_compaction.rebuild_milestones(
            self.db, self._spine_key,
            spine_ok=spine_ok, include_derived=True,
        )
        # Red team H-1.  The gate used to be computed without ever asking the
        # component whose job is to say whether the DATA is intact, so a store
        # with every span blob deleted -- a restore that lost the entire
        # compacted transcript -- returned ok=True, equivalence 1.0.  E-2
        # exists to catch a system that is wrong; a gate that cannot fail for
        # the reason it was built to detect is not a gate.  This is the
        # equality-by-construction problem one level up, and the answer is the
        # same: the judgement consults a second, independent source.
        verification = _memory().memory_compaction.verify_compaction(
            self.db, self._spine_key, spine_ok=spine_ok
        )
        report["verify_ok"] = bool(verification.get("ok"))
        report["verify_problems"] = list(verification.get("problems") or [])
        report["verify_refusal"] = verification.get("refusal")
        report["rebuild_equivalence_derived"] = None
        report["equivalence_reason"] = None
        report["ok"] = False
        derived = report.get("derived")
        skipped = list(report.get("derived_skipped") or [])
        if derived is None:
            report["equivalence_reason"] = (
                report.get("refusal") or "not_derived"
            )
            if not include_derived:
                report.pop("derived", None)
            return report
        if skipped:
            # A ratio over a partial set is the same failure as a ratio over an
            # empty one, one row later.
            report["equivalence_reason"] = "partial_derivation"
            if not include_derived:
                report.pop("derived", None)
                report.pop("derived_skipped", None)
            return report
        if not derived:
            report["equivalence_reason"] = "nothing_derived"
            if not include_derived:
                report.pop("derived", None)
                report.pop("derived_skipped", None)
            return report
        matched = 0
        mismatched: list[int] = []
        for milestone_id, block in derived.items():
            stored = self._stored_derived_block(int(milestone_id))
            if stored is None:
                mismatched.append(int(milestone_id))
                continue
            if _memory().memory_compaction.derived_digest(stored) == str(
                block.get("rebuilt_sha256") or ""
            ):
                matched += 1
            else:
                mismatched.append(int(milestone_id))
        total = matched + len(mismatched)
        report["equivalence_mismatched"] = sorted(mismatched)
        if not report["verify_ok"]:
            # A store the verifier calls broken produces NO equivalence
            # number, not a passing one with a caveat beside it: the figure is
            # what everything downstream quotes, and it would be quoted out of
            # its qualifier within one hop.
            report["rebuild_equivalence_derived"] = None
            report["equivalence_reason"] = "store_unverified"
            report["ok"] = False
            if not include_derived:
                report.pop("derived", None)
                report.pop("derived_skipped", None)
            return report
        report["rebuild_equivalence_derived"] = (
            None if total == 0 else matched / total
        )
        report["equivalence_reason"] = None if total else "nothing_derived"
        report["ok"] = bool(
            total
            and not mismatched
            and report.get("chain_verified") is True
            and report.get("refusal") is None
        )
        if not include_derived:
            report.pop("derived", None)
            report.pop("derived_skipped", None)
        return report

    def _stored_derived_block(self, milestone_id: int) -> dict[str, Any] | None:
        """This layer's OWN read of one milestone's stored ``derived`` block.

        Deliberately not taken from the rebuild's result: the whole point of
        11.18 is that the two sides of the comparison come from two places.
        """
        row = self.db.execute(
            "SELECT invariants_json FROM memory_milestones WHERE id=?",
            (int(milestone_id),),
        ).fetchone()
        if row is None:
            return None
        try:
            block = _memory().json.loads(str(row[0]))["derived"]
        except (TypeError, ValueError, KeyError):
            return None
        return block if isinstance(block, dict) else None
