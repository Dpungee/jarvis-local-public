"""Current Memory domain extracted without changing method control flow.

Global dependencies resolve through jarvis.memory at call time. The facade keeps
public imports, patch seams, constants and authorization helpers authoritative.
"""
from __future__ import annotations

import sqlite3
from .memory_runtime import (_memory)


class SchemaMigrationsMemoryMixin:
    """Mechanically extracted current Memory methods."""

    def _migrate_v46(self) -> None:
        """Add the memory spine: append-only keyed event chain, claim lineage,
        explicit claim ids, and a backfill of every existing claim."""
        _memory().memory_spine.migrate_memory_spine_v46(
            self.db, self._spine_key, now=_memory().now_iso()
        )

    def _migrate_v47(self) -> None:
        """Put ordinary memories and lessons on the spine: explicit memory
        ids, lineage on every ``memories`` row (a claim's backing row carries
        the claim's event), and a digest-only ``memory.imported`` backfill of
        every other row.  Runs in the same transaction as v46 for a legacy
        store; a store that already has memory events is re-linked, never
        re-imported, and a row whose content no longer matches its event is
        refused (see ``memory_spine.migrate_memory_spine_v47``)."""
        _memory().memory_spine.migrate_memory_spine_v47(
            self.db, self._spine_key, now=_memory().now_iso()
        )

    def _migrate_v48(self) -> None:
        """Add the temporal graph: entities, edges with validity intervals,
        and a backfill of every non-excluded claim row.

        The three tables were dropped at the top of ``_migrate`` (design 4.3
        step 0), so this step always builds the projection from scratch and is
        idempotent by construction.  It reads ``memory_claims`` and writes no
        spine table except its own ``projection.rebuilt`` receipt, which
        ``memory_graph`` appends because it holds the key; the spine is ready
        on every path here, since 46 and 47 ran earlier in this transaction
        for a legacy store.  Nothing is written to ``memory_claims`` — in
        particular no ``valid_until`` backfill on rows superseded in place
        before schema 46 (design 3.2, review R9).
        """
        _memory().memory_graph.migrate_memory_graph_v48(
            self.db, self._spine_key, now=_memory().now_iso()
        )

    def _migrate_v49(self) -> None:
        """Add the learning ladder: two record tables, one shared id sequence,
        their lineage and append-only triggers, and the nullable
        ``lesson_applications.tool_name`` column (schema 49).

        Four things and nothing else (M4 design 4.3):

        1. **Refuse rather than rebuild.**  If the spine records ``ladder.*``
           events but a record table is absent, or present without the rows
           those events name, the store **fails to open** with the fixed
           reason ``ladder_records_missing`` — the same shape as migration
           46's refusal of an authentic spine below 46.  Dropping the tables
           on every re-migration, as the first draft did, would mean one
           ``PRAGMA user_version = 48`` plus a reopen permanently destroys
           every sealed epoch and every promotion record (exactly the move
           someone makes to erase an inconvenient ledger), and would leave the
           store broken: spine events cannot be deleted, so every ``ladder.*``
           event would survive naming a row that no longer exists.
        2. Create the DDL when the tables are absent.  **Only
           ``ladder_id_sequence`` is ever dropped and rebuilt**, from the two
           tables and the spine, because a sequence is derivable and a record
           is not.
        3. Add ``lesson_applications.tool_name``; existing rows keep ``NULL``,
           and a promotion built from them honestly records "none recorded"
           rather than inventing a tool list.
        4. **Seal no epochs and grandfather nothing.**  The ledger starts
           empty: a store with 50,000 historical predictions does not get a
           fabricated history.  Bulk sealing is the operator's ``ladder seal
           --all``, and even then the boundaries are mechanical (design 2.2),
           so it is a catch-up rather than a choice.  Grandfathering pre-M4
           live documents needs a workspace, which ``Memory`` does not have,
           and is the separate receipted pass ``grandfather_ladder``.

        No receipt is appended.  The ladder is not a projection, so
        ``projection.rebuilt`` would be a lie and ``"ladder"`` is deliberately
        absent from ``memory_spine._REBUILT_PROJECTIONS`` (design 4.3, M-12);
        the grandfather pass appends its own kind instead.
        """
        ledger_present = _memory()._sqlite_table_exists(self.db, "memory_calibration_ledger")
        promotions_present = _memory()._sqlite_table_exists(self.db, "ladder_promotions")
        self._refuse_unbacked_ladder_events_locked(
            ledger_present=ledger_present, promotions_present=promotions_present
        )
        # Widen the events table's closed CHECK lists for the seven
        # ``ladder.*`` kinds, the two new subject kinds and ``lesson.applied``,
        # exactly as migration 47 widened them for the memory kinds: SQLite
        # cannot alter a CHECK, so the table is copied column for column and
        # every keyed digest still verifies.  The copy is idempotent -- it
        # compares the stored SQL against ``memory_spine._EVENT_TABLE_SQL``
        # and does nothing when they already match -- so a re-migration over
        # an already-widened store touches no row.
        #
        # **Every spine trigger comes down first, and this is not optional.**
        # The rebuild ends in ``ALTER TABLE ... RENAME``, and SQLite re-parses
        # every trigger in the schema on a rename.  Two of them --
        # ``memory_claims_require_spine_event`` and
        # ``memories_require_spine_event`` -- sit on OTHER tables, so the
        # ``DROP TABLE memory_spine_events`` does not take them with it, and
        # they reference a table that does not exist between the drop and the
        # rename: ``OperationalError: no such table: main.memory_spine_events``
        # on every real store carrying claims or memories.  Migration 47 never
        # met this because ``_migrate`` drops the spine triggers below 47; at
        # 48 all four are installed.  Dropping and recreating them around the
        # rebuild is the same thing migration 47 relies on, done explicitly.
        if _memory().memory_spine.spine_ready(self.db):
            _memory().memory_spine.drop_spine_triggers(self.db)
            try:
                _memory().memory_spine._rebuild_events_table(self.db)
            finally:
                _memory().memory_spine.create_spine_triggers(self.db)
        if not ledger_present:
            self.db.execute(_memory()._LADDER_LEDGER_SQL)
        if not promotions_present:
            self.db.execute(_memory()._LADDER_PROMOTIONS_SQL)
        for statement in _memory()._LADDER_INDEX_SQL:
            self.db.execute(statement)
        for name, statement in _memory().LADDER_TRIGGER_SQL.items():
            # Triggers are definitions, not records: restoring them
            # deterministically on every open is the v44 discipline, and a
            # store whose triggers were dropped out of band gets them back.
            self.db.execute(f"DROP TRIGGER IF EXISTS {name}")
            self.db.execute(statement)
        self.db.execute("DROP TABLE IF EXISTS ladder_id_sequence")
        self.db.execute(_memory()._LADDER_SEQUENCE_SQL)
        self.db.execute(
            "INSERT INTO ladder_id_sequence(id, next_id) VALUES (1, ?)",
            (_memory().ladder_sequence_floor(self.db) + 1,),
        )
        if "tool_name" not in _memory()._sqlite_table_columns(self.db, "lesson_applications"):
            self.db.execute("ALTER TABLE lesson_applications ADD COLUMN tool_name TEXT")

    def _migrate_v50(self) -> None:
        """Add typed-invariant compaction: milestones, compacted spans, their
        lineage and immutability triggers, and the one new spine kind
        (schema 50 / spine 49).

        Three things and nothing else (M5 design 2.11):

        1. **Widen the events table for ``transcript.compacted`` first.**
           ``memory_spine.migrate_memory_spine_v49`` copies the events table
           column for column because SQLite cannot alter a CHECK, and the copy
           ends in ``ALTER TABLE ... RENAME``.  SQLite re-parses every trigger
           in the schema on a rename, and two of them --
           ``memory_claims_require_spine_event`` and
           ``memories_require_spine_event`` -- sit on OTHER tables, so the
           ``DROP TABLE`` does not take them with it and they reference a
           table that does not exist between the drop and the rename.  Every
           real store carrying claims or memories would raise
           ``OperationalError: no such table: main.memory_spine_events``.
           ``_migrate`` drops the spine triggers only below 47, so at 49 all
           four are installed and this bracket is mandatory, not defensive.
           It is the M4 correctness review's HIGH-1, paid for once already.
        2. **Create the two record tables when absent, and never drop them.**
           A compacted span is the only copy of the transcript rows it
           replaced, so the graph's drop-and-rebuild rule does not transfer:
           the graph is derived and a span is not (design 2.11, H-1).
        3. **No backfill.**  Existing conversations are not compacted by the
           migration and no ``transcript.compacted`` event is synthesised for
           history that was never compacted.

        No receipt is appended.  Compaction is not a projection, so
        ``projection.rebuilt`` would be a lie; the compaction pass appends its
        own receipt when it actually runs.
        """
        if _memory().memory_spine.spine_ready(self.db):
            # EVERY trigger that lives on another table and mentions
            # ``memory_spine_events`` has to come down, not just the ones
            # ``memory_spine`` knows about.  M4 learned half of this: its
            # migration dropped the spine's own two external triggers
            # (``memory_claims_require_spine_event``,
            # ``memories_require_spine_event``) around the rebuild.  By 49 the
            # ladder has added two more of exactly the same shape, and
            # ``memory_spine.drop_spine_triggers`` cannot know about them --
            # so a real schema-49 store failed to open with
            # ``error in trigger ladder_promotions_require_spine_event: no
            # such table: main.memory_spine_events``.  Only the real-store
            # test finds this: a store built by the current tree gets the
            # widened CHECK directly and never runs the copy at all.
            #
            # Derived from the live schema rather than listed, so the next
            # phase to add a lineage trigger is carried automatically instead
            # of discovering this a third time.
            external = [
                (str(row["name"]), str(row["sql"]))
                for row in self.db.execute(
                    """SELECT name, sql FROM sqlite_master
                       WHERE type='trigger' AND sql IS NOT NULL
                         AND tbl_name <> 'memory_spine_events'
                         AND sql LIKE '%memory_spine_events%'"""
                ).fetchall()
            ]
            _memory().memory_spine.drop_spine_triggers(self.db)
            for name, _sql in external:
                self.db.execute(f"DROP TRIGGER IF EXISTS {name}")
            try:
                _memory().memory_spine.migrate_memory_spine_v49(
                    self.db, self._spine_key, now=_memory().now_iso()
                )
            finally:
                _memory().memory_spine.create_spine_triggers(self.db)
                for name, sql in external:
                    # Idempotent whichever set ``create_spine_triggers``
                    # already restored.
                    self.db.execute(f"DROP TRIGGER IF EXISTS {name}")
                    self.db.execute(sql)
        _memory().memory_compaction.migrate_compaction_v50(
            self.db, self._spine_key, now=_memory().now_iso()
        )

    def _migrate_v51(self) -> None:
        """Add the runtime-to-memory replay bridge (schema 51).

        Purely additive.  It creates ``memory_bridge_*`` tables, one initial
        generation and one consumer cursor, and it reads, alters or deletes
        nothing that already exists: no claim, lesson, promotion, milestone,
        spine event or transcript row is touched.  That is deliberate -- the
        bridge owns whole tables rather than a column on shared ones, so a
        projection rebuild can never reach memory-authored data.

        The memory spine is not widened here.  Bridge activity is audited in
        ``memory_bridge_batches`` instead of a new ``SPINE_KINDS`` value, because
        changing that closed CHECK is a contract change both owners must accept
        first (contract reconciliation, correction 2).
        """
        _memory().memory_bridge.migrate_bridge_v51(self.db, now=_memory().now_iso())

    def _migrate_v52(self) -> None:
        """Add the bridge ingest lease and the resume audit (schema 52).

        Additive, like v51: two new tables, nothing existing read or altered.
        The lease exists so that two workers against one store cannot both
        ingest, and so a worker that dies mid-pass releases by expiry instead of
        wedging the bridge.  The resume audit exists because a halt cleared
        without a record is, to any later reader, a halt that never happened.
        """
        _memory().memory_bridge.migrate_bridge_v52(self.db, now=_memory().now_iso())

    def _migrate_v53(self) -> None:
        """Add the bridge rebuild and cutover audit (schema 53).

        Additive, like v51 and v52.  It exists because a refused rebuild rolls
        back everything it touched, including the evidence that it was tried:
        without this table an operator whose generation cutover was rejected
        sees a store that looks untouched and no record of the refusal.
        """
        _memory().memory_bridge.migrate_bridge_v53(self.db, now=_memory().now_iso())

    def _migrate_v54(self) -> None:
        """Add bridge ingest health and the incident ledger (schema 54).

        Additive.  It exists because every ingest failure short of a durable
        halt used to leave no trace once the worker process exited: a reader
        error, a lost lease, or a halt that could not be persisted were visible
        only in whatever stdout happened to be attached at the time.
        """
        _memory().memory_bridge.migrate_bridge_v54(self.db, now=_memory().now_iso())

    def _refuse_compaction_downgrade_locked(self, version: int) -> None:
        """Refuse to open a store whose compacted spans predate its schema.

        The shape is ``_refuse_unbacked_ladder_events_locked``'s, and the
        reason is stronger.  A ladder downgrade destroys records that cannot
        be re-derived; a compaction downgrade destroys the operator's
        transcript itself, because the ``messages`` rows were deleted when the
        span was written and ``memory_compacted_spans`` holds the only copy.
        Migration 50 therefore never drops either table, and a store that
        arrives below 50 with spans in it fails to open and says what to type.

        A real store below schema 50 has no spans at all, so this state can
        only be a hand-set ``user_version`` over the record tables.
        """
        if version >= 50:
            return
        spans = _memory().memory_compaction.compaction_downgrade_blocked(self.db)
        if not spans:
            return
        raise _memory().memory_spine.SpineError(
            _memory().memory_compaction.compaction_downgrade_message(
                int(spans), version=int(version)
            ),
            code="compaction_downgrade_refused",
        )

    def _refuse_unbacked_ladder_events_locked(
        self, *, ledger_present: bool, promotions_present: bool
    ) -> None:
        """Refuse to open a store whose ladder events have lost their records.

        A real store below schema 49 has no ``ladder.*`` event at all, so an
        event with no row can only be a manual downgrade over the record
        tables.  Rebuilding is not an option — the numbers a sealed epoch
        froze are not derivable from anything else — so the store fails to
        open and says why, exactly as ``migrate_memory_spine_v46`` refuses an
        authentic keyed head below 46 (design 4.3 step 1, H-6).
        """
        if not _memory()._sqlite_table_exists(self.db, "memory_spine_events"):
            return
        placeholders = ", ".join("?" for _ in _memory().LADDER_SPINE_KINDS)
        events = self.db.execute(
            f"""SELECT id, kind, subject_kind, subject_id
                FROM memory_spine_events WHERE kind IN ({placeholders})
                ORDER BY id""",
            _memory().LADDER_SPINE_KINDS,
        ).fetchall()
        if not events:
            return
        reason = None
        if not ledger_present or not promotions_present:
            reason = "the ladder record tables are absent"
        else:
            orphans: list[int] = []
            for event in events:
                kind = str(event["kind"])
                if kind == _memory().LADDER_LEDGER_CREATING_KIND:
                    table = "memory_calibration_ledger"
                elif kind in _memory().LADDER_PROMOTION_CREATING_KINDS:
                    table = "ladder_promotions"
                else:
                    # Status events (staged, approved, rolled back, withdrawn)
                    # do not create a row; the creating event for their
                    # subject does, and it is checked on its own line.
                    continue
                subject_id = event["subject_id"]
                if subject_id is None:
                    orphans.append(int(event["id"]))
                    continue
                backing = self.db.execute(
                    f"SELECT 1 FROM {table} WHERE id=? AND spine_event_id=?",
                    (int(subject_id), int(event["id"])),
                ).fetchone()
                if backing is None:
                    orphans.append(int(event["id"]))
            if orphans:
                reason = (
                    f"{len(orphans)} ladder event(s) name a record row that is "
                    f"missing (first: event {orphans[0]})"
                )
        if reason is None:
            return
        raise _memory().memory_spine.SpineError(
            "ladder_records_missing: the memory spine records ladder events "
            f"but {reason}; refusing to open (a real store below schema 49 "
            "has no ladder events, so this is a schema downgrade over the "
            "record tables; see docs/LEARNING_LADDER.md)",
            code="ladder_records_missing",
        )

    def _migrate_v45(self) -> None:
        """Keep the runtime's own record of every shown project-fact proposal.

        user_version<45 proves no proposal row is authoritative, so a partial
        table from an interrupted migration is dropped before creation.
        """
        self.db.execute("DROP TABLE IF EXISTS memory_fact_proposals")
        self.db.execute(
            """CREATE TABLE memory_fact_proposals (
                id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL,
                conversation_id INTEGER NOT NULL,
                assistant_message_id INTEGER NOT NULL UNIQUE,
                project_id INTEGER NOT NULL,
                command TEXT NOT NULL,
                command_sha256 TEXT NOT NULL CHECK(length(command_sha256)=64),
                assisted INTEGER NOT NULL CHECK(assisted IN (0, 1)),
                reply_asked_question INTEGER NOT NULL
                    CHECK(reply_asked_question IN (0, 1)),
                status TEXT NOT NULL CHECK(status IN
                    ('shown', 'confirmed', 'refused', 'expired')),
                resolved_at TEXT,
                claim_id INTEGER,
                FOREIGN KEY(conversation_id) REFERENCES conversations(id),
                FOREIGN KEY(assistant_message_id) REFERENCES messages(id),
                FOREIGN KEY(claim_id) REFERENCES memory_claims(id)
            )"""
        )
        self.db.execute(
            """CREATE INDEX IF NOT EXISTS idx_memory_fact_proposals_conversation
               ON memory_fact_proposals(conversation_id, status, id)"""
        )

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
            (_memory().now_iso(),),
        )

    def _migrate_v6(self) -> None:
        """Bind every live approval to an explicit execution scope."""
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(approvals)")}
        if "scope" not in columns:
            self.db.execute(
                "ALTER TABLE approvals ADD COLUMN scope TEXT NOT NULL DEFAULT 'legacy'"
            )
            stamp = _memory().now_iso()
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
        stamp = _memory().now_iso()
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
        stamp = _memory().now_iso()
        for specialist in _memory().SPECIALISTS:
            self.db.execute(
                """INSERT OR IGNORE INTO specialist_agents(
                       agent_key, name, purpose, model_profile, families_json,
                       status, active_task_id, completed_tasks, failed_tasks,
                       created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, 'ready', NULL, 0, 0, ?, ?)""",
                (
                    specialist.key, specialist.name, specialist.purpose,
                    specialist.model_profile,
                    _memory().json.dumps(specialist.families, separators=(",", ":")),
                    stamp, stamp,
                ),
            )
            self.db.execute(
                """UPDATE specialist_agents
                   SET name=?, purpose=?, model_profile=?, families_json=?, updated_at=?
                   WHERE agent_key=?""",
                (
                    specialist.name, specialist.purpose, specialist.model_profile,
                    _memory().json.dumps(specialist.families, separators=(",", ":")),
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
                safe_source = _memory().redact_secrets(
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
                confidence = raw_confidence if _memory().math.isfinite(raw_confidence) else 0.0
                self._remember_claim_locked(
                    "user",
                    f"preference:{str(row['name']).casefold()[:100]}",
                    _memory().redact_secrets(str(row["value"]))[:2_000],
                    source=safe_source or "legacy preference",
                    authority=authority,
                    confidence=max(0.0, min(confidence, 1.0)),
                    stamp=str(row["updated_at"] or _memory().now_iso()),
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
                    _memory().claim_source_key(str(row["authority"])),
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
        stamp = _memory().now_iso()
        for row in rows:
            try:
                parsed = _memory().json.loads(str(row["resource"]))
            except (TypeError, ValueError, _memory().json.JSONDecodeError):
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

        prediction_counts = _memory().Counter(int(prediction["id"]) for _, prediction in candidates)
        reflection_counts = _memory().Counter(int(row["reflection_id"]) for row, _ in candidates)
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
                    prediction_id, int(row["id"]), reflection_id, _memory().now_iso(),
                    _memory().hashlib.sha256(str(row["content"]).encode("utf-8")).hexdigest(),
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
            if content is None or str(row["content_sha256"] or "") != _memory().hashlib.sha256(
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
            (_memory().now_iso(),),
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
            (_memory().now_iso(),),
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
            (_memory().now_iso(),),
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

    def _migrate_v37(self) -> None:
        """Bind reusable lessons to project scope and an integrity-checked lifecycle."""
        # Version 37 was never authoritative before this migration completed.
        # Rebuild even a partially created table before referring to its columns.
        self.db.execute("DROP TABLE IF EXISTS lesson_controls")
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS lesson_controls (
                memory_id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL,
                observed_at TEXT NOT NULL,
                valid_until TEXT NOT NULL,
                lifecycle_status TEXT NOT NULL CHECK(lifecycle_status IN
                    ('active','contradicted','superseded','quarantined')),
                superseded_by INTEGER,
                recorded_at TEXT NOT NULL,
                control_sha256 TEXT NOT NULL CHECK(
                    length(control_sha256)=64 AND
                    control_sha256 NOT GLOB '*[^0-9a-f]*'),
                FOREIGN KEY(memory_id) REFERENCES memories(id) ON DELETE CASCADE,
                FOREIGN KEY(project_id) REFERENCES agent_projects(id),
                FOREIGN KEY(superseded_by) REFERENCES memories(id),
                CHECK((lifecycle_status IN ('contradicted','superseded') AND
                       superseded_by IS NOT NULL) OR
                      (lifecycle_status IN ('active','quarantined') AND
                       superseded_by IS NULL))
            )"""
        )
        self.db.execute(
            """CREATE INDEX IF NOT EXISTS idx_lesson_controls_scope
               ON lesson_controls(project_id, lifecycle_status, valid_until, memory_id)"""
        )
        # Rebuild from the exact provenance chain so a partial/dev migration
        # cannot keep stale scope or lifecycle receipts.
        # Only already-valid, non-practice provenance is promoted.  Everything
        # else remains stored for audit but receives no reusable control record.
        rows = self.db.execute(
            """SELECT lp.memory_id, lp.prediction_id, lp.reflection_id,
                      lp.content_sha256, lp.provenance_sha256,
                      r.created_at AS observed_at, p.task_id, p.conversation_id
               FROM lesson_provenance AS lp
               JOIN reflections AS r ON r.id=lp.reflection_id
               JOIN task_predictions AS p ON p.id=lp.prediction_id
               ORDER BY lp.memory_id"""
        ).fetchall()
        for row in rows:
            memory_id = int(row["memory_id"])
            if not self._lesson_provenance_validation(memory_id)[0]:
                continue
            project_id = self._lesson_project_for_context(
                row["task_id"], row["conversation_id"]
            )
            if project_id is None:
                continue
            observed_at = self._canonical_utc_timestamp(row["observed_at"])
            if observed_at is None:
                continue
            valid_until = (
                _memory().datetime.fromisoformat(observed_at)
                + _memory().timedelta(days=_memory().LESSON_DEFAULT_TTL_DAYS)
            ).isoformat()
            material = self._lesson_control_material(
                memory_id=memory_id,
                prediction_id=int(row["prediction_id"]),
                reflection_id=int(row["reflection_id"]),
                content_sha256=str(row["content_sha256"]),
                provenance_sha256=str(row["provenance_sha256"] or ""),
                project_id=project_id,
                observed_at=observed_at,
                valid_until=valid_until,
                lifecycle_status="active",
                superseded_by=None,
            )
            self.db.execute(
                """INSERT OR IGNORE INTO lesson_controls(
                       memory_id, project_id, observed_at, valid_until,
                       lifecycle_status, superseded_by, recorded_at, control_sha256
                   ) VALUES (?, ?, ?, ?, 'active', NULL, ?, ?)""",
                (
                    memory_id, project_id, observed_at, valid_until, _memory().now_iso(),
                    self._lesson_control_digest(material),
                ),
            )
        # Rebuild the application ledger with database-enforced constraints.
        # Older rows were trusted by Python only; retain solely the rows that
        # still prove an eligible same-family, same-project relationship.
        family_values = ",".join(
            "'" + family.replace("'", "''") + "'"
            for family in sorted(self.PREDICTION_FAMILIES)
        )
        self.db.execute("DROP TABLE IF EXISTS lesson_applications_v37")
        self.db.execute(
            f"""CREATE TABLE lesson_applications_v37 (
                id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL,
                prediction_id INTEGER NOT NULL,
                memory_id INTEGER NOT NULL,
                family TEXT NOT NULL CHECK(family IN ({family_values})),
                rank INTEGER NOT NULL CHECK(rank BETWEEN 1 AND 10),
                resolved_at TEXT,
                successful INTEGER CHECK(successful IN (0, 1)),
                UNIQUE(prediction_id, memory_id),
                CHECK((resolved_at IS NULL AND successful IS NULL) OR
                      (resolved_at IS NOT NULL AND successful IN (0, 1))),
                FOREIGN KEY(prediction_id) REFERENCES task_predictions(id)
                    ON DELETE CASCADE,
                FOREIGN KEY(memory_id) REFERENCES memories(id) ON DELETE CASCADE
            )"""
        )
        migration_at = _memory().now_iso()
        applications = self.db.execute(
            """SELECT id, created_at, prediction_id, memory_id, family, rank,
                      resolved_at, successful
               FROM lesson_applications ORDER BY id"""
        ).fetchall()
        for application in applications:
            try:
                prediction = self.db.execute(
                    """SELECT family, origin, created_at, resolved_at,
                              actual_status, evidence_ok, predicted_verification,
                              task_id, conversation_id
                       FROM task_predictions WHERE id=?""",
                    (int(application["prediction_id"]),),
                ).fetchone()
                lesson = self.db.execute(
                    """SELECT family, outcome_status FROM memories
                       WHERE id=? AND kind='lesson'""",
                    (int(application["memory_id"]),),
                ).fetchone()
                if prediction is None or lesson is None:
                    continue
                family = str(application["family"])
                if (
                    family not in self.PREDICTION_FAMILIES
                    or str(prediction["family"]) != family
                    or str(prediction["origin"])
                    not in _memory().LESSON_REUSABLE_PREDICTION_ORIGINS
                    or str(lesson["family"]) != family
                    or str(lesson["outcome_status"] or "") != "complete"
                ):
                    continue
                project_id = self._lesson_project_for_context(
                    prediction["task_id"], prediction["conversation_id"]
                )
                control = self.db.execute(
                    """SELECT observed_at, valid_until FROM lesson_controls
                       WHERE memory_id=?""",
                    (int(application["memory_id"]),),
                ).fetchone()
                normalized_application = (
                    None if control is None else self._lesson_application_values(
                        family=family,
                        application_created_at=application["created_at"],
                        application_resolved_at=application["resolved_at"],
                        application_successful=application["successful"],
                        prediction_created_at=prediction["created_at"],
                        prediction_resolved_at=prediction["resolved_at"],
                        prediction_actual_status=prediction["actual_status"],
                        prediction_evidence_ok=prediction["evidence_ok"],
                        prediction_verification=prediction[
                            "predicted_verification"
                        ],
                        lesson_observed_at=control["observed_at"],
                        lesson_valid_until=control["valid_until"],
                        validation_at=migration_at,
                    )
                )
                if (
                    project_id is None
                    or control is None
                    or normalized_application is None
                    or not self._lesson_provenance_validation(
                        int(application["memory_id"])
                    )[0]
                    or not self._lesson_control_validation(
                        int(application["memory_id"]),
                        project_id=project_id,
                        as_of=normalized_application[0],
                    )[0]
                ):
                    continue
                app_created_at, app_resolved_at, app_successful = (
                    normalized_application
                )
                self.db.execute(
                    """INSERT INTO lesson_applications_v37(
                           id, created_at, prediction_id, memory_id, family, rank,
                           resolved_at, successful
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        int(application["id"]), app_created_at,
                        int(application["prediction_id"]),
                        int(application["memory_id"]), family,
                        int(application["rank"]), app_resolved_at,
                        app_successful,
                    ),
                )
            except (_memory().sqlite3.DatabaseError, TypeError, ValueError):
                continue
        self.db.execute("DROP TABLE lesson_applications")
        self.db.execute(
            "ALTER TABLE lesson_applications_v37 RENAME TO lesson_applications"
        )
        self.db.execute(
            """CREATE INDEX IF NOT EXISTS idx_lesson_applications_prediction
               ON lesson_applications(prediction_id, rank)"""
        )

    def _migrate_v38(self) -> None:
        """Persist integrity-bound cross-family strategy evidence and receipts."""
        # Version 38 is not authoritative until this transaction completes. A
        # partially created development table must never be trusted as durable
        # strategy evidence on the next startup.
        self.db.execute("DROP TABLE IF EXISTS strategy_transfer_attestations")
        self.db.execute("DROP TABLE IF EXISTS strategy_transfer_applications")
        self.db.execute("DROP TABLE IF EXISTS task_strategy_observations")
        family_values = ",".join(
            "'" + family.replace("'", "''") + "'"
            for family in sorted(self.PREDICTION_FAMILIES)
        )
        strategy_values = ",".join(
            "'" + strategy.replace("'", "''") + "'"
            for strategy in sorted(_memory().STRATEGY_SET)
        )
        mode_values = ",".join(
            "'" + mode.replace("'", "''") + "'"
            for mode in sorted(_memory().STRATEGY_TRANSFER_APPLICATION_MODES)
        )
        self.db.execute(
            f"""CREATE TABLE task_strategy_observations (
                prediction_id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL,
                project_id INTEGER NOT NULL,
                source_family TEXT NOT NULL CHECK(source_family IN ({family_values})),
                evidence_json TEXT NOT NULL,
                strategies_json TEXT NOT NULL,
                observation_sha256 TEXT NOT NULL CHECK(
                    length(observation_sha256)=64 AND
                    observation_sha256 NOT GLOB '*[^0-9a-f]*'),
                FOREIGN KEY(prediction_id) REFERENCES task_predictions(id)
                    ON DELETE CASCADE,
                FOREIGN KEY(project_id) REFERENCES agent_projects(id)
            )"""
        )
        self.db.execute(
            """CREATE INDEX idx_task_strategy_observations_scope
               ON task_strategy_observations(
                   project_id, source_family, prediction_id
               )"""
        )
        self.db.execute(
            f"""CREATE TABLE strategy_transfer_applications (
                id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL,
                prediction_id INTEGER NOT NULL,
                memory_id INTEGER NOT NULL,
                project_id INTEGER NOT NULL,
                strategy TEXT NOT NULL CHECK(strategy IN ({strategy_values})),
                source_family TEXT NOT NULL CHECK(source_family IN ({family_values})),
                target_family TEXT NOT NULL CHECK(target_family IN ({family_values})),
                mode TEXT NOT NULL CHECK(mode IN ({mode_values})),
                applied INTEGER NOT NULL CHECK(applied IN (0, 1)),
                rank INTEGER NOT NULL CHECK(rank BETWEEN 1 AND 32),
                source_observation_sha256 TEXT NOT NULL CHECK(
                    length(source_observation_sha256)=64 AND
                    source_observation_sha256 NOT GLOB '*[^0-9a-f]*'),
                source_provenance_sha256 TEXT NOT NULL CHECK(
                    length(source_provenance_sha256)=64 AND
                    source_provenance_sha256 NOT GLOB '*[^0-9a-f]*'),
                source_control_sha256 TEXT NOT NULL CHECK(
                    length(source_control_sha256)=64 AND
                    source_control_sha256 NOT GLOB '*[^0-9a-f]*'),
                resolved_at TEXT,
                successful INTEGER CHECK(successful IN (0, 1)),
                application_sha256 TEXT NOT NULL CHECK(
                    length(application_sha256)=64 AND
                    application_sha256 NOT GLOB '*[^0-9a-f]*'),
                UNIQUE(prediction_id, memory_id, strategy),
                CHECK(source_family <> target_family),
                CHECK(mode='advise' OR applied=0),
                CHECK((resolved_at IS NULL AND successful IS NULL) OR
                      (resolved_at IS NOT NULL AND successful IN (0, 1))),
                FOREIGN KEY(prediction_id) REFERENCES task_predictions(id)
                    ON DELETE CASCADE,
                FOREIGN KEY(memory_id) REFERENCES memories(id) ON DELETE CASCADE,
                FOREIGN KEY(project_id) REFERENCES agent_projects(id)
            )"""
        )
        self.db.execute(
            """CREATE INDEX idx_strategy_transfer_applications_prediction
               ON strategy_transfer_applications(prediction_id, rank, id)"""
        )
        self.db.execute(
            """CREATE INDEX idx_strategy_transfer_applications_effectiveness
               ON strategy_transfer_applications(
                   target_family, strategy, mode, applied, resolved_at,
                   prediction_id
               )"""
        )
        attestation_kinds = ",".join(
            "'" + kind.replace("'", "''") + "'"
            for kind in sorted(_memory().STRATEGY_TRANSFER_ATTESTATION_KINDS)
        )
        self.db.execute(
            f"""CREATE TABLE strategy_transfer_attestations (
                id INTEGER PRIMARY KEY,
                kind TEXT NOT NULL CHECK(kind IN ({attestation_kinds})),
                recorded_at TEXT NOT NULL,
                evaluator_version TEXT NOT NULL,
                evaluator_sha256 TEXT NOT NULL CHECK(
                    length(evaluator_sha256)=64 AND
                    evaluator_sha256 NOT GLOB '*[^0-9a-f]*'),
                config_sha256 TEXT NOT NULL CHECK(
                    length(config_sha256)=64 AND
                    config_sha256 NOT GLOB '*[^0-9a-f]*'),
                fixture_sha256 TEXT,
                assignment_manifest_sha256 TEXT,
                artifact_json TEXT NOT NULL,
                artifact_sha256 TEXT NOT NULL CHECK(
                    length(artifact_sha256)=64 AND
                    artifact_sha256 NOT GLOB '*[^0-9a-f]*'),
                attestation_sha256 TEXT NOT NULL CHECK(
                    length(attestation_sha256)=64 AND
                    attestation_sha256 NOT GLOB '*[^0-9a-f]*'),
                UNIQUE(kind, artifact_sha256),
                UNIQUE(kind, attestation_sha256),
                CHECK((kind='sealed_benchmark' AND fixture_sha256 IS NOT NULL
                       AND assignment_manifest_sha256 IS NULL) OR
                      (kind='applied_ab' AND fixture_sha256 IS NULL
                       AND assignment_manifest_sha256 IS NOT NULL))
            )"""
        )
        self.db.execute(
            """CREATE INDEX idx_strategy_transfer_attestations_compatibility
               ON strategy_transfer_attestations(
                   kind, evaluator_version, evaluator_sha256,
                   config_sha256, id
               )"""
        )

    def _migrate_v39(self) -> None:
        """Add bounded, pre-outcome randomized strategy-transfer trials."""
        # A failed migration leaves user_version=38. Remove only v39-owned
        # partial tables so reopening can deterministically reconstruct them.
        self.db.execute("DROP TABLE IF EXISTS strategy_transfer_trial_assignments")
        self.db.execute("DROP TABLE IF EXISTS strategy_transfer_trial_manifests")
        family_values = ",".join(
            "'" + family.replace("'", "''") + "'"
            for family in sorted(self.PREDICTION_FAMILIES)
        )
        strategy_values = ",".join(
            "'" + strategy.replace("'", "''") + "'"
            for strategy in sorted(_memory().STRATEGY_SET)
        )
        mode_values = ",".join(
            "'" + mode.replace("'", "''") + "'"
            for mode in sorted(_memory().STRATEGY_TRANSFER_APPLICATION_MODES)
        )

        application_sql_row = self.db.execute(
            """SELECT sql FROM sqlite_master
               WHERE type='table' AND name='strategy_transfer_applications'"""
        ).fetchone()
        if application_sql_row is None:
            raise RuntimeError("Phase 4A application ledger is unavailable")
        if "mode IN ('advise', 'trial') OR applied=0" not in str(
            application_sql_row[0]
        ):
            self.db.execute(
                """ALTER TABLE strategy_transfer_applications
                   RENAME TO strategy_transfer_applications_v38"""
            )
            self.db.execute(
                f"""CREATE TABLE strategy_transfer_applications (
                    id INTEGER PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    prediction_id INTEGER NOT NULL,
                    memory_id INTEGER NOT NULL,
                    project_id INTEGER NOT NULL,
                    strategy TEXT NOT NULL CHECK(strategy IN ({strategy_values})),
                    source_family TEXT NOT NULL CHECK(source_family IN ({family_values})),
                    target_family TEXT NOT NULL CHECK(target_family IN ({family_values})),
                    mode TEXT NOT NULL CHECK(mode IN ({mode_values})),
                    applied INTEGER NOT NULL CHECK(applied IN (0, 1)),
                    rank INTEGER NOT NULL CHECK(rank BETWEEN 1 AND 32),
                    source_observation_sha256 TEXT NOT NULL CHECK(
                        length(source_observation_sha256)=64 AND
                        source_observation_sha256 NOT GLOB '*[^0-9a-f]*'),
                    source_provenance_sha256 TEXT NOT NULL CHECK(
                        length(source_provenance_sha256)=64 AND
                        source_provenance_sha256 NOT GLOB '*[^0-9a-f]*'),
                    source_control_sha256 TEXT NOT NULL CHECK(
                        length(source_control_sha256)=64 AND
                        source_control_sha256 NOT GLOB '*[^0-9a-f]*'),
                    resolved_at TEXT,
                    successful INTEGER CHECK(successful IN (0, 1)),
                    application_sha256 TEXT NOT NULL CHECK(
                        length(application_sha256)=64 AND
                        application_sha256 NOT GLOB '*[^0-9a-f]*'),
                    UNIQUE(prediction_id, memory_id, strategy),
                    CHECK(source_family <> target_family),
                    CHECK(mode IN ('advise', 'trial') OR applied=0),
                    CHECK((resolved_at IS NULL AND successful IS NULL) OR
                          (resolved_at IS NOT NULL AND successful IN (0, 1))),
                    FOREIGN KEY(prediction_id) REFERENCES task_predictions(id)
                        ON DELETE CASCADE,
                    FOREIGN KEY(memory_id) REFERENCES memories(id) ON DELETE CASCADE,
                    FOREIGN KEY(project_id) REFERENCES agent_projects(id)
                )"""
            )
            self.db.execute(
                """INSERT INTO strategy_transfer_applications(
                       id, created_at, prediction_id, memory_id, project_id,
                       strategy, source_family, target_family, mode, applied,
                       rank, source_observation_sha256,
                       source_provenance_sha256, source_control_sha256,
                       resolved_at, successful, application_sha256
                   ) SELECT id, created_at, prediction_id, memory_id, project_id,
                            strategy, source_family, target_family, mode, applied,
                            rank, source_observation_sha256,
                            source_provenance_sha256, source_control_sha256,
                            resolved_at, successful, application_sha256
                     FROM strategy_transfer_applications_v38"""
            )
            self.db.execute("DROP TABLE strategy_transfer_applications_v38")
            self.db.execute(
                """CREATE INDEX idx_strategy_transfer_applications_prediction
                   ON strategy_transfer_applications(prediction_id, rank, id)"""
            )
            self.db.execute(
                """CREATE INDEX idx_strategy_transfer_applications_effectiveness
                   ON strategy_transfer_applications(
                       target_family, strategy, mode, applied, resolved_at,
                       prediction_id
                   )"""
            )

        manifest_statuses = ",".join(
            "'" + value.replace("'", "''") + "'"
            for value in sorted(_memory().TRIAL_MANIFEST_STATUSES)
        )
        manifest_reasons = ",".join(
            "'" + value.replace("'", "''") + "'"
            for value in sorted(_memory().TRIAL_ABORT_REASONS | {
                "operator_promoted", "trial_complete",
            })
        )
        self.db.execute(
            f"""CREATE TABLE strategy_transfer_trial_manifests (
                id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                project_id INTEGER NOT NULL,
                target_families_json TEXT NOT NULL,
                family_caps_json TEXT NOT NULL,
                strategies_json TEXT NOT NULL,
                sample_cap INTEGER NOT NULL CHECK(
                    sample_cap BETWEEN 40 AND 200 AND sample_cap % 4 = 0),
                block_size INTEGER NOT NULL CHECK(block_size=4),
                seed TEXT NOT NULL CHECK(
                    length(seed)=64 AND seed NOT GLOB '*[^0-9a-f]*'),
                evaluator_version TEXT NOT NULL,
                evaluator_sha256 TEXT NOT NULL CHECK(
                    length(evaluator_sha256)=64 AND
                    evaluator_sha256 NOT GLOB '*[^0-9a-f]*'),
                fixture_sha256 TEXT NOT NULL CHECK(
                    length(fixture_sha256)=64 AND
                    fixture_sha256 NOT GLOB '*[^0-9a-f]*'),
                config_sha256 TEXT NOT NULL CHECK(
                    length(config_sha256)=64 AND
                    config_sha256 NOT GLOB '*[^0-9a-f]*'),
                runtime_sha256 TEXT NOT NULL CHECK(
                    length(runtime_sha256)=64 AND
                    runtime_sha256 NOT GLOB '*[^0-9a-f]*'),
                operator_confirmed INTEGER NOT NULL CHECK(operator_confirmed=1),
                status TEXT NOT NULL CHECK(status IN ({manifest_statuses})),
                status_reason TEXT CHECK(
                    status_reason IS NULL OR status_reason IN ({manifest_reasons})),
                closed_at TEXT,
                promoted_at TEXT,
                manifest_sha256 TEXT NOT NULL UNIQUE CHECK(
                    length(manifest_sha256)=64 AND
                    manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
                state_sha256 TEXT NOT NULL CHECK(
                    length(state_sha256)=64 AND
                    state_sha256 NOT GLOB '*[^0-9a-f]*'),
                UNIQUE(project_id, seed),
                CHECK((status='active' AND closed_at IS NULL AND promoted_at IS NULL)
                   OR (status IN ('closed', 'aborted') AND closed_at IS NOT NULL
                       AND promoted_at IS NULL)
                   OR (status='promoted' AND closed_at IS NOT NULL
                       AND promoted_at IS NOT NULL)),
                FOREIGN KEY(project_id) REFERENCES agent_projects(id)
            )"""
        )
        self.db.execute(
            """CREATE INDEX idx_strategy_transfer_trial_manifest_scope
               ON strategy_transfer_trial_manifests(
                   project_id, status, expires_at, id
               )"""
        )
        assignment_statuses = ",".join(
            "'" + value.replace("'", "''") + "'"
            for value in sorted(_memory().TRIAL_ASSIGNMENT_STATUSES)
        )
        contamination_reasons = ",".join(
            "'" + value.replace("'", "''") + "'"
            for value in sorted(_memory().TRIAL_CONTAMINATION_REASONS)
        )
        arms = ",".join(
            "'" + value.replace("'", "''") + "'"
            for value in sorted(_memory().TRIAL_ARMS)
        )
        self.db.execute(
            f"""CREATE TABLE strategy_transfer_trial_assignments (
                id INTEGER PRIMARY KEY,
                manifest_id INTEGER NOT NULL,
                prediction_id INTEGER NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                project_id INTEGER NOT NULL,
                target_family TEXT NOT NULL CHECK(target_family IN ({family_values})),
                family_sequence INTEGER NOT NULL CHECK(family_sequence>=0),
                block_index INTEGER NOT NULL CHECK(block_index>=0),
                block_slot INTEGER NOT NULL CHECK(block_slot BETWEEN 0 AND 3),
                arm TEXT NOT NULL CHECK(arm IN ({arms})),
                strategies_json TEXT NOT NULL,
                selection_sha256 TEXT NOT NULL CHECK(
                    length(selection_sha256)=64 AND
                    selection_sha256 NOT GLOB '*[^0-9a-f]*'),
                assignment_sha256 TEXT NOT NULL UNIQUE CHECK(
                    length(assignment_sha256)=64 AND
                    assignment_sha256 NOT GLOB '*[^0-9a-f]*'),
                prompt_recorded_at TEXT,
                base_prompt_sha256 TEXT,
                final_prompt_sha256 TEXT,
                advice_applied INTEGER CHECK(advice_applied IN (0, 1)),
                prompt_receipt_sha256 TEXT,
                provider_dispatched_at TEXT,
                provider_dispatch_sha256 TEXT,
                status TEXT NOT NULL CHECK(status IN ({assignment_statuses})),
                status_reason TEXT CHECK(
                    status_reason IS NULL OR
                    status_reason IN ({contamination_reasons})),
                resolved_at TEXT,
                successful INTEGER CHECK(successful IN (0, 1)),
                outcome_sha256 TEXT,
                UNIQUE(manifest_id, target_family, family_sequence),
                UNIQUE(manifest_id, target_family, block_index, block_slot),
                CHECK((prompt_recorded_at IS NULL AND base_prompt_sha256 IS NULL
                       AND final_prompt_sha256 IS NULL AND advice_applied IS NULL
                       AND prompt_receipt_sha256 IS NULL) OR
                      (prompt_recorded_at IS NOT NULL
                       AND length(base_prompt_sha256)=64
                       AND base_prompt_sha256 NOT GLOB '*[^0-9a-f]*'
                       AND length(final_prompt_sha256)=64
                       AND final_prompt_sha256 NOT GLOB '*[^0-9a-f]*'
                       AND advice_applied IN (0, 1)
                       AND length(prompt_receipt_sha256)=64
                       AND prompt_receipt_sha256 NOT GLOB '*[^0-9a-f]*')),
                CHECK((provider_dispatched_at IS NULL
                       AND provider_dispatch_sha256 IS NULL) OR
                      (provider_dispatched_at IS NOT NULL
                       AND length(provider_dispatch_sha256)=64
                       AND provider_dispatch_sha256 NOT GLOB '*[^0-9a-f]*')),
                CHECK((status='assigned' AND resolved_at IS NULL
                       AND successful IS NULL AND outcome_sha256 IS NULL
                       AND status_reason IS NULL) OR
                      (status='resolved' AND resolved_at IS NOT NULL
                       AND successful IN (0, 1) AND outcome_sha256 IS NOT NULL
                       AND status_reason IS NULL) OR
                      (status IN ('aborted', 'contaminated')
                       AND resolved_at IS NOT NULL AND successful IS NULL
                       AND outcome_sha256 IS NOT NULL
                       AND status_reason IS NOT NULL)),
                FOREIGN KEY(manifest_id)
                    REFERENCES strategy_transfer_trial_manifests(id),
                FOREIGN KEY(prediction_id) REFERENCES task_predictions(id),
                FOREIGN KEY(project_id) REFERENCES agent_projects(id)
            )"""
        )
        self.db.execute(
            """CREATE INDEX idx_strategy_transfer_trial_assignment_manifest
               ON strategy_transfer_trial_assignments(
                   manifest_id, target_family, status, family_sequence
               )"""
        )

    def _migrate_v40(self) -> None:
        """Add the fail-closed long-horizon workflow substrate."""
        from .long_horizon import migrate_long_horizon_v40

        # user_version<40 proves no v40 row is authoritative. Remove only
        # v40-owned partial tables so an interrupted/manual partial migration
        # cannot preserve a weaker schema across reopen.
        for table in (
            "long_horizon_final_verifications",
            "long_horizon_authorities",
            "long_horizon_usage_reservations",
            "long_horizon_retry_receipts",
            "long_horizon_mutation_receipts",
            "long_horizon_checkpoints",
            "long_horizon_stages",
            "long_horizon_plans",
        ):
            self.db.execute(f"DROP TABLE IF EXISTS {table}")
        migrate_long_horizon_v40(self.db)

    def _migrate_v41(self) -> None:
        """Bind idempotent tasks to immutable original scheduling intent."""
        self.db.execute("DROP TRIGGER IF EXISTS tasks_schedule_binding_immutable")
        columns = {
            str(row["name"])
            for row in self.db.execute("PRAGMA table_info(tasks)").fetchall()
        }
        if "initial_available_at" not in columns:
            self.db.execute("ALTER TABLE tasks ADD COLUMN initial_available_at TEXT")
        if "availability_mode" not in columns:
            self.db.execute(
                """ALTER TABLE tasks ADD COLUMN availability_mode TEXT NOT NULL
                   DEFAULT 'legacy_unknown'
                   CHECK(availability_mode IN
                       ('immediate','scheduled','legacy_unknown'))"""
            )
        # user_version<41 proves the original scheduling intent was never
        # recorded authoritatively. Do not infer it from mutable available_at,
        # which may already contain retry/backoff or approval-resume timing.
        self.db.execute(
            """UPDATE tasks
               SET initial_available_at=NULL,
                   availability_mode='legacy_unknown'"""
        )
        self.db.execute(
            """CREATE TRIGGER tasks_schedule_binding_immutable
               BEFORE UPDATE OF initial_available_at, availability_mode ON tasks
               WHEN OLD.initial_available_at IS NOT NEW.initial_available_at
                 OR OLD.availability_mode IS NOT NEW.availability_mode
               BEGIN
                   SELECT RAISE(ABORT, 'task scheduling intent is immutable');
               END"""
        )

    def _migrate_v42(self) -> None:
        """Scope temporal claims so project facts cannot collide or leak."""
        columns = {
            str(row["name"])
            for row in self.db.execute("PRAGMA table_info(memory_claims)").fetchall()
        }
        if "scope" not in columns:
            self.db.execute(
                "ALTER TABLE memory_claims "
                "ADD COLUMN scope TEXT NOT NULL DEFAULT 'global'"
            )
        self.db.execute(
            """CREATE INDEX IF NOT EXISTS idx_memory_claims_scope_key
               ON memory_claims(scope, claim_key, status, id)"""
        )
        self.db.execute("DROP TRIGGER IF EXISTS memory_claim_scope_valid_insert")
        self.db.execute("DROP TRIGGER IF EXISTS memory_claim_scope_immutable")
        self.db.execute(
            """CREATE TRIGGER memory_claim_scope_valid_insert
               BEFORE INSERT ON memory_claims
               WHEN NEW.scope <> 'global' AND (
                   length(NEW.scope) > 27
                   OR substr(NEW.scope, 1, 8) <> 'project:'
                   OR length(substr(NEW.scope, 9)) = 0
                   OR substr(NEW.scope, 9) GLOB '*[^0-9]*'
                   OR CAST(substr(NEW.scope, 9) AS INTEGER) <= 0
                   OR NEW.scope <> 'project:' || CAST(
                       CAST(substr(NEW.scope, 9) AS INTEGER) AS TEXT
                   )
               )
               BEGIN
                   SELECT RAISE(ABORT, 'invalid memory claim scope');
               END"""
        )
        self.db.execute(
            """CREATE TRIGGER memory_claim_scope_immutable
               BEFORE UPDATE OF scope ON memory_claims
               WHEN NEW.scope <> OLD.scope
               BEGIN
                   SELECT RAISE(ABORT, 'memory claim scope is immutable');
               END"""
        )

    def _migrate_v43(self) -> None:
        """Persist authenticated learning-quality quarantine membership."""
        # user_version<43 proves no row in a partial development copy is an
        # authoritative marker. Rebuild this derived membership atomically;
        # canonical memories and provenance remain untouched.
        for trigger in (
            "ordinary_memory_quality_memory_changed",
            "ordinary_memory_quality_provenance_changed",
            "ordinary_memory_quality_provenance_deleted",
        ):
            self.db.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        self.db.execute("DROP TABLE IF EXISTS ordinary_memory_quality_quarantine")
        self.db.execute(
            f"""CREATE TABLE ordinary_memory_quality_quarantine (
                    memory_id INTEGER PRIMARY KEY,
                    recorded_at TEXT NOT NULL,
                    contract_version INTEGER NOT NULL CHECK(
                        contract_version={_memory().TRAINING_QUALITY_CONTRACT_VERSION}
                    ),
                    content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),
                    source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64),
                    provenance_sha256 TEXT NOT NULL CHECK(length(provenance_sha256)=64),
                    FOREIGN KEY(memory_id) REFERENCES memories(id) ON DELETE CASCADE
                )"""
        )
        rows = self.db.execute(
            """SELECT m.id AS memory_id, m.created_at, m.kind, m.content, m.source,
                      omp.origin, omp.eligible, omp.content_sha256,
                      omp.provenance_sha256
               FROM memories AS m
               JOIN ordinary_memory_provenance AS omp ON omp.memory_id=m.id
               WHERE m.kind='learning'"""
        ).fetchall()
        stamp = _memory().now_iso()
        quarantined = [
            (
                int(row["memory_id"]),
                stamp,
                _memory().TRAINING_QUALITY_CONTRACT_VERSION,
                str(row["content_sha256"]),
                _memory().hashlib.sha256(
                    str(row["source"] or "").encode("utf-8")
                ).hexdigest(),
                str(row["provenance_sha256"]),
            )
            for row in rows
            if _memory()._learning_quality_quarantined_record(
                row["memory_id"],
                row["created_at"],
                row["kind"],
                row["content"],
                row["source"],
                row["origin"],
                row["eligible"],
                row["content_sha256"],
                row["provenance_sha256"],
            )
        ]
        self.db.executemany(
            """INSERT INTO ordinary_memory_quality_quarantine(
                   memory_id, recorded_at, contract_version, content_sha256,
                   source_sha256, provenance_sha256
               ) VALUES (?, ?, ?, ?, ?, ?)""",
            quarantined,
        )
        self.db.execute(
            """DELETE FROM memory_embeddings
               WHERE memory_id IN (
                   SELECT memory_id FROM ordinary_memory_quality_quarantine
               )"""
        )
        self.db.execute(
            """DELETE FROM memory_embedding_leases
               WHERE memory_id IN (
                   SELECT memory_id FROM ordinary_memory_quality_quarantine
               )"""
        )
        self.db.execute(
            """CREATE TRIGGER ordinary_memory_quality_memory_changed
               AFTER UPDATE OF created_at, kind, content, source ON memories
               BEGIN
                   DELETE FROM ordinary_memory_quality_quarantine
                   WHERE memory_id=NEW.id;
               END"""
        )
        self.db.execute(
            """CREATE TRIGGER ordinary_memory_quality_provenance_changed
               AFTER UPDATE ON ordinary_memory_provenance
               BEGIN
                   DELETE FROM ordinary_memory_quality_quarantine
                   WHERE memory_id=NEW.memory_id;
               END"""
        )
        self.db.execute(
            """CREATE TRIGGER ordinary_memory_quality_provenance_deleted
               AFTER DELETE ON ordinary_memory_provenance
               BEGIN
                   DELETE FROM ordinary_memory_quality_quarantine
                   WHERE memory_id=OLD.memory_id;
               END"""
        )

    def _migrate_v44(self) -> None:
        """Replace negative quarantine membership with explicit tri-state decisions."""
        for trigger in (
            "ordinary_memory_quality_memory_changed",
            "ordinary_memory_quality_provenance_inserted",
            "ordinary_memory_quality_provenance_changed",
            "ordinary_memory_quality_provenance_deleted",
            "ordinary_memory_quality_assessment_inserted",
            "ordinary_memory_quality_assessment_changed",
            "ordinary_memory_quality_assessment_deleted",
        ):
            self.db.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        self.db.execute("DROP TABLE IF EXISTS ordinary_memory_quality_quarantine")
        self.db.execute("DROP TABLE IF EXISTS ordinary_memory_quality_assessments")
        self._create_learning_quality_assessment_schema_locked()

    def _create_learning_quality_assessment_schema_locked(self) -> None:
        self.db.execute(
            f"""CREATE TABLE IF NOT EXISTS ordinary_memory_quality_assessments (
                    memory_id INTEGER PRIMARY KEY,
                    recorded_at TEXT NOT NULL,
                    contract_version INTEGER NOT NULL CHECK(
                        contract_version={_memory().TRAINING_QUALITY_CONTRACT_VERSION}
                    ),
                    recall_allowed INTEGER NOT NULL CHECK(recall_allowed IN (0,1)),
                    content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),
                    source_is_null INTEGER NOT NULL CHECK(source_is_null IN (0,1)),
                    source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64),
                    provenance_sha256 TEXT NOT NULL CHECK(length(provenance_sha256)=64),
                    FOREIGN KEY(memory_id) REFERENCES memories(id) ON DELETE CASCADE
                )"""
        )

    def _learning_quality_schema_healthy_locked(self) -> bool:
        table = self.db.execute(
            """SELECT 1 FROM sqlite_master
               WHERE type='table' AND name='ordinary_memory_quality_assessments'"""
        ).fetchone()
        if table is None:
            return False
        columns = {
            str(row["name"]) for row in self.db.execute(
                "PRAGMA table_info(ordinary_memory_quality_assessments)"
            ).fetchall()
        }
        if columns != {
            "memory_id", "recorded_at", "contract_version", "recall_allowed",
            "content_sha256", "source_is_null", "source_sha256",
            "provenance_sha256",
        }:
            return False
        def canonical_sql(value: str) -> str:
            return " ".join(
                str(value).replace(" IF NOT EXISTS", "", 1).split()
            ).casefold()

        expected: dict[str, str] = {}
        for statement in self._learning_quality_assessment_trigger_statements():
            match = _memory().re.match(
                r"\s*CREATE\s+TRIGGER(?:\s+IF\s+NOT\s+EXISTS)?\s+([^\s]+)",
                statement,
                _memory().re.I,
            )
            if match is None:
                return False
            expected[match.group(1)] = canonical_sql(statement)
        actual = {
            str(row["name"]): canonical_sql(str(row["sql"] or ""))
            for row in self.db.execute(
                """SELECT name, sql FROM sqlite_master
                   WHERE type='trigger' AND name LIKE 'ordinary_memory_quality_%'"""
            ).fetchall()
        }
        return actual == expected

    @staticmethod
    def _learning_quality_assessment_trigger_statements() -> tuple[str, ...]:
        return (
            """CREATE TRIGGER IF NOT EXISTS ordinary_memory_quality_memory_changed
               AFTER UPDATE OF created_at,kind,content,source ON memories
               WHEN NEW.created_at IS NOT OLD.created_at
                 OR NEW.kind IS NOT OLD.kind
                 OR NEW.content IS NOT OLD.content
                 OR NEW.source IS NOT OLD.source
               BEGIN
                   DELETE FROM ordinary_memory_quality_assessments
                   WHERE memory_id IN (OLD.id, NEW.id);
                   DELETE FROM memory_embeddings WHERE memory_id IN (OLD.id, NEW.id);
                   DELETE FROM memory_embedding_leases
                   WHERE memory_id IN (OLD.id, NEW.id);
               END""",
            """CREATE TRIGGER IF NOT EXISTS ordinary_memory_quality_provenance_inserted
               AFTER INSERT ON ordinary_memory_provenance
               BEGIN
                   DELETE FROM ordinary_memory_quality_assessments
                   WHERE memory_id=NEW.memory_id;
                   DELETE FROM memory_embeddings WHERE memory_id=NEW.memory_id;
                   DELETE FROM memory_embedding_leases WHERE memory_id=NEW.memory_id;
               END""",
            """CREATE TRIGGER IF NOT EXISTS ordinary_memory_quality_provenance_changed
               AFTER UPDATE OF memory_id,origin,eligible,content_sha256,provenance_sha256
               ON ordinary_memory_provenance
               WHEN NEW.memory_id IS NOT OLD.memory_id
                 OR NEW.origin IS NOT OLD.origin
                 OR NEW.eligible IS NOT OLD.eligible
                 OR NEW.content_sha256 IS NOT OLD.content_sha256
                 OR NEW.provenance_sha256 IS NOT OLD.provenance_sha256
               BEGIN
                   DELETE FROM ordinary_memory_quality_assessments
                   WHERE memory_id IN (OLD.memory_id, NEW.memory_id);
                   DELETE FROM memory_embeddings
                   WHERE memory_id IN (OLD.memory_id, NEW.memory_id);
                   DELETE FROM memory_embedding_leases
                   WHERE memory_id IN (OLD.memory_id, NEW.memory_id);
               END""",
            """CREATE TRIGGER IF NOT EXISTS ordinary_memory_quality_provenance_deleted
               AFTER DELETE ON ordinary_memory_provenance
               BEGIN
                   DELETE FROM ordinary_memory_quality_assessments
                   WHERE memory_id=OLD.memory_id;
                   DELETE FROM memory_embeddings WHERE memory_id=OLD.memory_id;
                   DELETE FROM memory_embedding_leases WHERE memory_id=OLD.memory_id;
               END""",
            """CREATE TRIGGER IF NOT EXISTS ordinary_memory_quality_assessment_inserted
               AFTER INSERT ON ordinary_memory_quality_assessments
               BEGIN
                   DELETE FROM memory_embeddings WHERE memory_id=NEW.memory_id;
                   DELETE FROM memory_embedding_leases WHERE memory_id=NEW.memory_id;
               END""",
            """CREATE TRIGGER IF NOT EXISTS ordinary_memory_quality_assessment_changed
               AFTER UPDATE ON ordinary_memory_quality_assessments
               WHEN NEW.memory_id IS NOT OLD.memory_id
                 OR NEW.contract_version IS NOT OLD.contract_version
                 OR NEW.recall_allowed IS NOT OLD.recall_allowed
                 OR NEW.content_sha256 IS NOT OLD.content_sha256
                 OR NEW.source_is_null IS NOT OLD.source_is_null
                 OR NEW.source_sha256 IS NOT OLD.source_sha256
                 OR NEW.provenance_sha256 IS NOT OLD.provenance_sha256
               BEGIN
                   DELETE FROM ordinary_memory_quality_assessments
                   WHERE memory_id IN (OLD.memory_id, NEW.memory_id);
                   DELETE FROM memory_embeddings
                   WHERE memory_id IN (OLD.memory_id, NEW.memory_id);
                   DELETE FROM memory_embedding_leases
                   WHERE memory_id IN (OLD.memory_id, NEW.memory_id);
               END""",
            """CREATE TRIGGER IF NOT EXISTS ordinary_memory_quality_assessment_deleted
               AFTER DELETE ON ordinary_memory_quality_assessments
               BEGIN
                   DELETE FROM memory_embeddings WHERE memory_id=OLD.memory_id;
                   DELETE FROM memory_embedding_leases WHERE memory_id=OLD.memory_id;
               END""",
        )

    def _install_learning_quality_assessment_triggers_locked(self) -> None:
        # A same-name trigger is not proof of the expected fail-closed body.
        # Recreate every trigger in this namespace inside the startup
        # transaction so direct database edits cannot preserve a no-op or an
        # extra trigger with side effects.
        rows = self.db.execute(
            """SELECT name FROM sqlite_master
               WHERE type='trigger' AND name LIKE 'ordinary_memory_quality_%'"""
        ).fetchall()
        for row in rows:
            quoted_name = str(row["name"]).replace('"', '""')
            self.db.execute(f'DROP TRIGGER IF EXISTS "{quoted_name}"')
        for statement in self._learning_quality_assessment_trigger_statements():
            self.db.execute(statement)

    def _reconcile_learning_quality_assessments_locked(
        self,
        *,
        reinstall_triggers: bool,
    ) -> None:
        """Repair derived decisions/triggers from canonical rows on every open."""
        self._create_learning_quality_assessment_schema_locked()
        rows = self.db.execute(
            """SELECT m.id AS memory_id, m.created_at, m.kind, m.content, m.source,
                      omp.origin, omp.eligible, omp.content_sha256,
                      omp.provenance_sha256
               FROM memories AS m
               LEFT JOIN ordinary_memory_provenance AS omp ON omp.memory_id=m.id
               LEFT JOIN ordinary_memory_quality_assessments AS omqa
                 ON omqa.memory_id=m.id
               WHERE lower(m.kind)='learning'
                 AND omqa.memory_id IS NULL"""
        ).fetchall()
        stamp = _memory().now_iso()
        for row in rows:
            decision = _memory()._learning_quality_assessment(
                row["memory_id"], row["created_at"], row["kind"], row["content"],
                row["source"], row["origin"], row["eligible"],
                row["content_sha256"], row["provenance_sha256"],
            )
            memory_id = int(row["memory_id"])
            if decision is None:
                self.db.execute(
                    """DELETE FROM ordinary_memory_quality_assessments
                       WHERE memory_id=?""",
                    (memory_id,),
                )
                continue
            source_sha256 = _memory().hashlib.sha256(
                str(row["source"] or "").encode("utf-8")
            ).hexdigest()
            self.db.execute(
                """INSERT INTO ordinary_memory_quality_assessments(
                       memory_id, recorded_at, contract_version, recall_allowed,
                       content_sha256, source_is_null, source_sha256,
                       provenance_sha256
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(memory_id) DO UPDATE SET
                       recorded_at=excluded.recorded_at,
                       contract_version=excluded.contract_version,
                       recall_allowed=excluded.recall_allowed,
                       content_sha256=excluded.content_sha256,
                       source_is_null=excluded.source_is_null,
                       source_sha256=excluded.source_sha256,
                       provenance_sha256=excluded.provenance_sha256
                   WHERE contract_version IS NOT excluded.contract_version
                      OR recall_allowed IS NOT excluded.recall_allowed
                      OR content_sha256 IS NOT excluded.content_sha256
                      OR source_is_null IS NOT excluded.source_is_null
                      OR source_sha256 IS NOT excluded.source_sha256
                      OR provenance_sha256 IS NOT excluded.provenance_sha256""",
                (
                    memory_id, stamp, _memory().TRAINING_QUALITY_CONTRACT_VERSION,
                    int(decision), str(row["content_sha256"]),
                    int(row["source"] is None),
                    source_sha256,
                    str(row["provenance_sha256"]),
                ),
            )
        self.db.execute(
            """DELETE FROM ordinary_memory_quality_assessments
               WHERE NOT EXISTS (
                   SELECT 1 FROM memories AS m
                   WHERE m.id=ordinary_memory_quality_assessments.memory_id
                     AND lower(m.kind)='learning'
               )"""
        )
        self.db.execute(
            """DELETE FROM memory_embeddings
               WHERE EXISTS (
                   SELECT 1 FROM memories AS m
                   LEFT JOIN ordinary_memory_quality_assessments AS omqa
                     ON omqa.memory_id=m.id
                   WHERE m.id=memory_embeddings.memory_id
                     AND lower(m.kind)='learning'
                     AND (
                         COALESCE(omqa.recall_allowed, 0)<>1
                         OR memory_embeddings.content_sha256
                            IS NOT omqa.content_sha256
                     )
               )"""
        )
        self.db.execute(
            """DELETE FROM memory_embedding_leases
               WHERE EXISTS (
                   SELECT 1 FROM memories AS m
                   LEFT JOIN ordinary_memory_quality_assessments AS omqa
                     ON omqa.memory_id=m.id
                   WHERE m.id=memory_embedding_leases.memory_id
                     AND lower(m.kind)='learning'
                     AND (
                         COALESCE(omqa.recall_allowed, 0)<>1
                         OR memory_embedding_leases.content_sha256
                            IS NOT omqa.content_sha256
                     )
               )"""
        )
        if reinstall_triggers:
            self._install_learning_quality_assessment_triggers_locked()

    def _sync_learning_quality_quarantine_locked(self, memory_id: int) -> None:
        """Refresh one explicit learning-quality decision after canonical writes."""
        row = self.db.execute(
            """SELECT m.id AS memory_id, m.created_at, m.kind, m.content, m.source,
                      omp.origin, omp.eligible, omp.content_sha256,
                      omp.provenance_sha256
               FROM memories AS m
               LEFT JOIN ordinary_memory_provenance AS omp ON omp.memory_id=m.id
               WHERE m.id=?""",
            (int(memory_id),),
        ).fetchone()
        decision = None if row is None else _memory()._learning_quality_assessment(
            row["memory_id"], row["created_at"], row["kind"], row["content"],
            row["source"], row["origin"], row["eligible"],
            row["content_sha256"], row["provenance_sha256"],
        )
        if row is None or decision is None:
            self.db.execute(
                "DELETE FROM ordinary_memory_quality_assessments WHERE memory_id=?",
                (int(memory_id),),
            )
            self.db.execute(
                "DELETE FROM memory_embeddings WHERE memory_id=?", (int(memory_id),)
            )
            self.db.execute(
                "DELETE FROM memory_embedding_leases WHERE memory_id=?",
                (int(memory_id),),
            )
            return
        source_sha256 = _memory().hashlib.sha256(
            str(row["source"] or "").encode("utf-8")
        ).hexdigest()
        expected = (
            _memory().TRAINING_QUALITY_CONTRACT_VERSION,
            int(decision),
            str(row["content_sha256"]),
            int(row["source"] is None),
            source_sha256,
            str(row["provenance_sha256"]),
        )
        current = self.db.execute(
            """SELECT contract_version, recall_allowed, content_sha256,
                      source_is_null, source_sha256, provenance_sha256
               FROM ordinary_memory_quality_assessments WHERE memory_id=?""",
            (int(memory_id),),
        ).fetchone()
        if current is not None and tuple(current) != expected:
            # The assessment UPDATE trigger invalidates changed rows. Delete
            # first, then insert the freshly derived decision atomically.
            self.db.execute(
                "DELETE FROM ordinary_memory_quality_assessments WHERE memory_id=?",
                (int(memory_id),),
            )
        self.db.execute(
            """INSERT INTO ordinary_memory_quality_assessments(
                   memory_id, recorded_at, contract_version, recall_allowed,
                   content_sha256, source_is_null, source_sha256,
                   provenance_sha256
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(memory_id) DO UPDATE SET
                   recorded_at=excluded.recorded_at,
                   contract_version=excluded.contract_version,
                   recall_allowed=excluded.recall_allowed,
                   content_sha256=excluded.content_sha256,
                   source_is_null=excluded.source_is_null,
                   source_sha256=excluded.source_sha256,
                   provenance_sha256=excluded.provenance_sha256
               WHERE contract_version IS NOT excluded.contract_version
                  OR recall_allowed IS NOT excluded.recall_allowed
                  OR content_sha256 IS NOT excluded.content_sha256
                  OR source_is_null IS NOT excluded.source_is_null
                  OR source_sha256 IS NOT excluded.source_sha256
                  OR provenance_sha256 IS NOT excluded.provenance_sha256""",
            (
                int(memory_id),
                _memory().now_iso(),
                _memory().TRAINING_QUALITY_CONTRACT_VERSION,
                int(decision),
                str(row["content_sha256"]),
                int(row["source"] is None),
                source_sha256,
                str(row["provenance_sha256"]),
            ),
        )
        if not decision:
            self.db.execute(
                "DELETE FROM memory_embeddings WHERE memory_id=?", (int(memory_id),)
            )
            self.db.execute(
                "DELETE FROM memory_embedding_leases WHERE memory_id=?",
                (int(memory_id),),
            )
