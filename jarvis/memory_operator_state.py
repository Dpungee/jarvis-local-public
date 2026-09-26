"""Current Memory domain extracted without changing method control flow.

Global dependencies resolve through jarvis.memory at call time. The facade keeps
public imports, patch seams, constants and authorization helpers authoritative.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
import sqlite3
from .memory_runtime import (_memory)


class OperatorStateMemoryMixin:
    """Mechanically extracted current Memory methods."""

    def control_state(self) -> dict[str, Any]:
        self._ensure_open()
        row = self.db.execute(
            "SELECT state, updated_at, reason FROM runtime_control WHERE id=1"
        ).fetchone()
        return dict(row) if row else {"state": "stopped", "updated_at": _memory().now_iso(), "reason": "missing control row"}

    def set_control_state(self, state: str, reason: str | None = None) -> None:
        state = str(state).strip().casefold()
        if state not in {"running", "paused", "stopped"}:
            raise ValueError("Control state must be running, paused, or stopped")
        reason_text = _memory().redact_secrets(str(reason).strip())[:1000] if reason else None
        stamp = _memory().now_iso()
        with self._immediate_transaction():
            self.db.execute(
                "UPDATE runtime_control SET state=?, updated_at=?, reason=? WHERE id=1",
                (state, stamp, reason_text),
            )
            self.db.execute(
                "INSERT INTO activity_log(created_at, category, action, status, details_json) "
                "VALUES (?, 'control', ?, 'complete', ?)",
                (stamp, state, _memory().json.dumps({"reason": reason_text}, ensure_ascii=False)),
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
        category = _memory()._validated_nonsecret_metadata(category, "Activity category")
        action = _memory()._validated_nonsecret_metadata(action, "Activity action")
        status = _memory()._validated_nonsecret_metadata(status, "Activity status")
        payload = _memory()._redacted_json_text(details or {})
        payload = _memory()._bounded_persisted_text(payload, 8_000, "activity details")
        with self._immediate_transaction():
            cur = self.db.execute(
                """INSERT INTO activity_log(
                       created_at, category, action, status, task_id, details_json
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    _memory().now_iso(), category[:40], action[:100], status[:30], task_id, payload,
                ),
            )
        return int(cur.lastrowid)

    def list_activity(self, limit: int = 100) -> list[dict[str, Any]]:
        self._ensure_open()
        limit = _memory()._bounded_limit(limit, 10_000)
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
            (category, _memory()._as_utc(since).isoformat()),
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
        title = _memory().redact_secrets(str(title).strip())
        kind = str(kind).strip().casefold()
        if not title or len(title) > 300:
            raise ValueError("Goal title must contain 1-300 characters")
        if kind not in {"goal", "project"}:
            raise ValueError("Goal kind must be goal or project")
        description = _memory().redact_secrets(str(description).strip())
        if len(description) > 8_000:
            raise ValueError("Goal description exceeds 8,000 characters")
        priority = max(0, min(int(priority), 100))
        stamp = _memory().now_iso()
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
                (status, _memory().now_iso(), normalized_goal),
            )
        return updated.rowcount == 1

    def list_goals(self, limit: int = 100) -> list[dict[str, Any]]:
        self._ensure_open()
        limit = _memory()._bounded_limit(limit, 1_000)
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
        content = _memory().redact_secrets(str(content).strip())
        if not content or len(content) > 20_000:
            raise ValueError("Journal entry must contain 1-20,000 characters")
        kind = _memory()._validated_nonsecret_metadata(kind, "Journal kind")[:40] or "note"
        with self._immediate_transaction():
            goal = self.db.execute(
                "SELECT title FROM goals WHERE id=?", (int(goal_id),)
            ).fetchone()
            if goal is None:
                raise ValueError(f"Goal #{goal_id} does not exist")
            cur = self.db.execute(
                """INSERT INTO journal_entries(goal_id, created_at, kind, content, task_id)
                   VALUES (?, ?, ?, ?, ?)""",
                (int(goal_id), _memory().now_iso(), kind, content, task_id),
            )
            self.db.execute("UPDATE goals SET updated_at=? WHERE id=?", (_memory().now_iso(), int(goal_id)))
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
        limit = _memory()._bounded_limit(limit, 1_000)
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
        actor: str = "operator",
        conversation_id: int | None = None,
        permission: str = "operator",
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
            actor=actor, conversation_id=conversation_id, permission=permission,
        )
        return int(row["id"])

    def set_preference(
        self,
        name: str,
        value: str,
        *,
        source: str = "user",
        confidence: float = 1.0,
        actor: str = "operator",
        conversation_id: int | None = None,
        permission: str = "operator",
    ) -> int:
        name = _memory()._validated_nonsecret_metadata(name, "Preference name").casefold()
        value = _memory().redact_secrets(str(value).strip())
        if not name or len(name) > 100 or not value or len(value) > 2_000:
            raise ValueError("Preference name/value is empty or too long")
        confidence = float(confidence)
        if not _memory().math.isfinite(confidence):
            raise ValueError("Preference confidence must be finite")
        confidence = max(0.0, min(confidence, 1.0))
        safe_source = _memory()._validated_nonsecret_metadata(source, "Preference source")[:100]
        authority = (
            "operator"
            if safe_source.casefold() in {
                "user", "explicit user preference", "explicit user feedback",
                "explicit user profile statement",
            }
            else "verified" if safe_source.casefold().startswith("verified")
            else "learned"
        )
        stamp = _memory().now_iso()
        with self._immediate_transaction():
            return self._set_preference_locked(
                name,
                value,
                source=safe_source,
                authority=authority,
                confidence=confidence,
                stamp=stamp,
                actor=actor,
                conversation_id=conversation_id,
                permission=permission,
            )

    def list_preferences(self) -> list[dict[str, Any]]:
        self._ensure_open()
        rows = self.db.execute(
            """SELECT id, updated_at, name, value, source, confidence
               FROM preferences WHERE active=1 ORDER BY updated_at DESC, id DESC LIMIT 100"""
        ).fetchall()
        return [dict(row) for row in rows]

    def approve_subject(self, subject: str, notes: str = "") -> int:
        subject = _memory()._validated_nonsecret_metadata(subject, "Approved subject")
        notes = _memory().redact_secrets(str(notes).strip())
        if not subject or len(subject) > 500:
            raise ValueError("Subject must contain 1-500 characters")
        with self._immediate_transaction():
            self.db.execute(
                """INSERT INTO approved_subjects(created_at, subject, notes, enabled)
                   VALUES (?, ?, ?, 1)
                   ON CONFLICT(subject) DO UPDATE SET notes=excluded.notes, enabled=1""",
                (_memory().now_iso(), subject, notes[:2_000]),
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
        instructions = _memory().redact_secrets(str(instructions).strip())
        if len(instructions) > 8_000:
            raise ValueError("Backlog instructions exceed 8,000 characters")
        priority = max(0, min(int(priority), 100))
        interval_hours = max(1, min(int(interval_hours), 24 * 365))
        stamp = _memory().now_iso()
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
                (int(enabled), _memory().now_iso(), normalized_backlog),
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
        current = _memory()._as_utc(now)
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
            specialist = _memory().specialist_for_prompt(proactive_prompt) or _memory().SPECIALIST_BY_KEY[
                "coding" if row["kind"] == "prototype" else "research"
            ]
            task_id, created = self._insert_task_locked(
                proactive_prompt, stamp=current_text, available_at=current_text,
                initial_available_at=str(row["next_run"]),
                availability_mode="scheduled",
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
                ((current + _memory().timedelta(hours=int(row["interval_hours"]))).isoformat(),
                 current_text, row["id"]),
            )
            if created:
                self.db.execute(
                    """INSERT INTO activity_log(
                           created_at, category, action, status, task_id, details_json
                       ) VALUES (?, 'scheduler', 'idle_select', 'queued', ?, ?)""",
                    (current_text, task_id, _memory().json.dumps({
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
        status_text = _memory()._validated_nonsecret_metadata(status, "Reflection status")[:30]
        summary = _memory().redact_private_identifiers(str(summary))
        mistakes = _memory().redact_private_identifiers(str(mistakes))
        improvements = _memory().redact_private_identifiers(str(improvements))
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
                _memory().json.dumps(
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
        summary = _memory()._bounded_persisted_text(
            _memory().redact_private_identifiers(str(summary).strip()),
            4_000,
            "reflection summary",
        )
        mistakes = _memory()._bounded_persisted_text(
            _memory().redact_private_identifiers(str(mistakes).strip()),
            4_000,
            "reflection mistakes",
        )
        improvements = _memory()._bounded_persisted_text(
            _memory().redact_private_identifiers(str(improvements).strip()),
            4_000,
            "reflection improvements",
        )
        if not summary:
            raise ValueError("Reflection summary must not be empty")
        normalized_prediction: int | None = None
        bound_family: str | None = None
        if prediction_id is not None:
            normalized_prediction = self._prediction_optional_id(
                prediction_id, "prediction_id"
            )
            prediction = self.db.execute(
                """SELECT task_id, conversation_id, family, predicted_verification,
                          actual_status, actual_steps, evidence_ok, resolved_at
                   FROM task_predictions WHERE id=?""",
                (normalized_prediction,),
            ).fetchone()
            if prediction is None or prediction["resolved_at"] is None:
                raise ValueError("Reflection requires an already resolved prediction")
            bound_family = str(prediction["family"])
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
        stamp = _memory().now_iso()
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
        family = bound_family or self._prediction_family_for_context(
            task_id, conversation_id
        )
        if improvements and family is not None:
            lesson_project_id = self._lesson_project_for_context(
                task_id, conversation_id
            )
            try:
                if lesson_project_id is None:
                    raise ValueError("Verified lesson lacks a project scope")
                lesson_content = self._canonical_reflection_lesson_content(
                    family=family,
                    outcome_status=status,
                    summary=summary,
                    mistakes=mistakes,
                    improvements=improvements,
                    project_id=lesson_project_id,
                    reflection_id=reflection_id,
                )
                if lesson_content is None:
                    raise ValueError("Verified lesson content is unavailable")
                self.remember_verified_lesson(
                    lesson_content,
                    family=family,
                    outcome_status=status,
                    reflection_id=reflection_id,
                )
            except (RuntimeError, ValueError) as error:
                # A reflection remains useful audit evidence, but a reusable
                # lesson must fail closed when its exact resolved prediction
                # cannot be proven. Never fall back to an unbound lesson row.
                try:
                    with self._immediate_transaction():
                        self.db.execute(
                            """INSERT INTO activity_log(
                                   created_at, category, action, status,
                                   details_json
                               ) VALUES (?, 'memory', 'lesson_persist', 'failed', ?)""",
                            (
                                _memory().now_iso(),
                                _memory().json.dumps(
                                    {
                                        "reflection_id": reflection_id,
                                        "error_type": type(error).__name__,
                                    },
                                    sort_keys=True,
                                    separators=(",", ":"),
                                ),
                            ),
                        )
                except _memory().sqlite3.DatabaseError:
                    pass
        return reflection_id

    def list_reflections(self, limit: int = 50) -> list[dict[str, Any]]:
        self._ensure_open()
        limit = _memory()._bounded_limit(limit, 1_000)
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
        safe_trigger = _memory()._bounded_persisted_text(
            _memory().redact_secrets(str(trigger).strip()), 4_000, "repair trigger"
        )
        safe_diff = _memory()._bounded_persisted_text(
            _memory().redact_secrets(str(diff_text)), 200_000, "repair diff"
        )
        safe_reason = (
            _memory()._bounded_persisted_text(
                _memory().redact_secrets(str(void_reason)), 4_000, "repair void reason"
            )
            if void_reason else None
        )
        digest = _memory().hashlib.sha256(safe_diff.encode("utf-8")).hexdigest()
        with self._immediate_transaction():
            cur = self.db.execute(
                """INSERT INTO self_repair_proposals(
                       created_at, trigger_text, failing_tests_json, diff_text,
                       diff_sha256, verification_json, status, void_reason,
                       candidate_path
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    _memory().now_iso(), safe_trigger,
                    _memory()._redacted_json_text([str(item)[:1_000] for item in failing_tests[:100]]),
                    safe_diff, digest, _memory()._redacted_json_text(verification), status,
                    safe_reason, _memory().redact_secrets(str(candidate_path))[:4_000],
                ),
            )
            proposal_id = int(cur.lastrowid)
            self.db.execute(
                """INSERT INTO activity_log(
                       created_at, category, action, status, details_json
                   ) VALUES (?, 'self_repair', 'draft', ?, ?)""",
                (
                    _memory().now_iso(), status,
                    _memory()._redacted_json_text({"proposal_id": proposal_id, "diff_sha256": digest}),
                ),
            )
        return proposal_id

    def list_repair_proposals(self, limit: int = 50) -> list[dict[str, Any]]:
        limit = _memory()._bounded_limit(limit, 500)
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
        if _memory().re.fullmatch(r"[0-9a-f]{64}", str(runtime_sha256)) is None:
            raise ValueError("Recovery runtime hash must be lowercase SHA-256")
        with self._immediate_transaction():
            cur = self.db.execute(
                """INSERT INTO recovery_attestations(
                       created_at, runtime_sha256, schema_version, passed, evidence_json
                   ) VALUES (?, ?, ?, ?, ?)""",
                (
                    _memory().now_iso(), runtime_sha256, _memory().SCHEMA_VERSION, int(bool(passed)),
                    _memory()._redacted_json_text(evidence),
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
        safe_name = _memory()._validated_nonsecret_metadata(name, "Domain name")
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
        stamp = _memory().now_iso()
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
                (_memory().now_iso(), normalized),
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
        safe_key = _memory()._validated_nonsecret_metadata(signal_key, "Initiative signal key")
        safe_kind = _memory()._validated_nonsecret_metadata(signal_kind, "Initiative signal kind")
        if not safe_key or len(safe_key) > 500 or not safe_kind or len(safe_kind) > 100:
            raise ValueError("Initiative signal metadata is invalid")
        normalized_project = self._project_id(project_id)
        safe_summary = _memory()._bounded_persisted_text(
            _memory().redact_secrets(str(summary).strip()), 4_000, "initiative summary"
        )
        with self._immediate_transaction():
            cur = self.db.execute(
                """INSERT OR IGNORE INTO initiative_events(
                       created_at, signal_key, signal_kind, tier, domain_id,
                       project_id, summary, evidence_json, status
                   ) VALUES (?, ?, ?, 0, NULL, ?, ?, ?, 'observed')""",
                (
                    _memory().now_iso(), safe_key, safe_kind, normalized_project,
                    safe_summary, _memory()._redacted_json_text(evidence),
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
        current = _memory()._as_utc(now)
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
            specialist = _memory().specialist_for_family(
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
                    _memory()._redacted_json_text({
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
                initial_available_at=None,
                availability_mode="immediate",
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
                    _memory()._redacted_json_text({
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
        bounded = _memory()._bounded_limit(limit, 2_000)
        where = "WHERE i.created_at>=?" if since is not None else ""
        parameters: tuple[Any, ...] = (_memory()._as_utc(since).isoformat(),) if since else ()
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

    def save_self_snapshot(self, snapshot: dict[str, Any]) -> int:
        payload = _memory()._redacted_json_text(snapshot)
        payload = _memory()._bounded_persisted_text(payload, 100_000, "self snapshot")
        with self._immediate_transaction():
            cur = self.db.execute(
                "INSERT INTO self_snapshots(created_at, snapshot_json) VALUES (?, ?)",
                (_memory().now_iso(), payload),
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
