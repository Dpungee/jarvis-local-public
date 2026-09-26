"""Current Memory domain extracted without changing method control flow.

Global dependencies resolve through jarvis.memory at call time. The facade keeps
public imports, patch seams, constants and authorization helpers authoritative.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from .memory_runtime import (DEFAULT_LEASE_SECONDS, _memory)


class TasksSchedulingMemoryMixin:
    """Mechanically extracted current Memory methods."""

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
            "prompt": _memory()._bounded_persisted_text(
                _memory().redact_secrets(prompt.strip()), 50_000, "training prompt"
            ),
            "response": _memory()._bounded_persisted_text(
                _memory().redact_secrets(response.strip()), 100_000, "training response"
            ),
            "model": _memory()._validated_nonsecret_metadata(model, "Training model")[:200],
            "profile": _memory()._validated_nonsecret_metadata(profile, "Training profile")[:40],
            "task_kind": _memory()._validated_nonsecret_metadata(task_kind, "Training task kind")[:40],
        }
        if not all(fields.values()):
            raise ValueError("Training examples require non-empty text and metadata")
        score = float(quality_score)
        if not 0.0 <= score <= 1.0:
            raise ValueError("Training quality score must be between 0 and 1")
        evidence_json = _memory()._redacted_json_text(evidence)
        digest_source = _memory().json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        content_hash = _memory().hashlib.sha256(digest_source.encode("utf-8")).hexdigest()
        split = _memory().training_prompt_split(fields["prompt"], fields["task_kind"])
        with self._immediate_transaction():
            cursor = self.db.execute(
                """INSERT OR IGNORE INTO training_examples(
                    created_at, conversation_id, prompt, response, model, profile, task_kind,
                    evidence_json, quality_score, verified, split, content_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    _memory().now_iso(), conversation_id, fields["prompt"], fields["response"],
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
        limit = _memory()._bounded_limit(limit, 100_000)
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
        name = _memory()._validated_nonsecret_metadata(name, "Evaluation name")[:200]
        prompt = _memory().redact_secrets(prompt.strip())
        expected = [
            _memory().redact_secrets(str(item).strip())
            for item in expected_contains
            if str(item).strip()
        ]
        if not name or not prompt or not expected:
            raise ValueError("Evaluation cases require a name, prompt, and expected text")
        encoded = _memory().json.dumps(expected, ensure_ascii=False, separators=(",", ":"))
        with self._immediate_transaction():
            self.db.execute(
                """INSERT INTO evaluation_cases(created_at, name, prompt, expected_contains_json)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET
                       prompt=excluded.prompt,
                       expected_contains_json=excluded.expected_contains_json,
                       enabled=1""",
                (_memory().now_iso(), name, prompt, encoded),
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

    def _insert_task_locked(
        self,
        prompt: str,
        *,
        stamp: str,
        available_at: str,
        initial_available_at: str | None,
        availability_mode: str,
        max_attempts: int,
        idempotency_key: str | None,
        project_id: int = 1,
        requested_model: str | None = None,
        specialist_key: str | None = None,
        delegated_by: str | None = None,
        parent_conversation_id: int | None = None,
        model_budget_scope: str | None = None,
    ) -> tuple[int, bool]:
        prompt = _memory().redact_secrets(str(prompt))
        availability_mode = str(availability_mode).strip().casefold()
        if availability_mode not in {"immediate", "scheduled"}:
            raise ValueError("New tasks require immediate or scheduled availability")
        if availability_mode == "scheduled":
            if not initial_available_at:
                raise ValueError("Scheduled tasks require an original availability time")
            initial_available_at = str(initial_available_at)
        elif initial_available_at is not None:
            raise ValueError("Immediate tasks may not bind a scheduled availability time")
        if model_budget_scope is not None:
            model_budget_scope = self._model_budget_scope(model_budget_scope)
        if idempotency_key is not None:
            idempotency_key = _memory()._validated_nonsecret_metadata(
                idempotency_key, "Task idempotency key"
            )
        cur = self.db.execute(
            """INSERT OR IGNORE INTO tasks(
                created_at, updated_at, status, prompt, available_at,
                initial_available_at, availability_mode,
                attempt_count, max_attempts, idempotency_key, project_id, requested_model,
                specialist_key, delegated_by, parent_conversation_id,
                model_budget_scope
            ) VALUES (?, ?, 'queued', ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                stamp, stamp, prompt, available_at, initial_available_at,
                availability_mode, max_attempts, idempotency_key, project_id,
                requested_model, specialist_key, delegated_by,
                parent_conversation_id, model_budget_scope,
            ),
        )
        if cur.rowcount:
            return int(cur.lastrowid), True
        if not idempotency_key:
            raise RuntimeError("Task insert was ignored without an idempotency key")
        row = self.db.execute(
            """SELECT id, prompt, max_attempts, project_id, requested_model,
                      specialist_key, delegated_by, parent_conversation_id,
                      model_budget_scope, initial_available_at, availability_mode,
                      created_at, available_at
               FROM tasks WHERE idempotency_key=?""",
            (idempotency_key,),
        ).fetchone()
        if row is None:
            raise RuntimeError("Could not resolve idempotent task insert")
        if str(row["prompt"]) != prompt:
            raise ValueError(
                "Task idempotency key is already bound to a different prompt"
            )
        if int(row["max_attempts"]) != int(max_attempts):
            raise ValueError(
                "Task idempotency key is already bound to a different retry policy"
            )
        stored_availability_mode = str(
            row["availability_mode"] or "legacy_unknown"
        )
        if stored_availability_mode == "legacy_unknown":
            if availability_mode == "scheduled":
                raise ValueError(
                    "Legacy task scheduling intent is unverifiable; explicit scheduled "
                    "replay is not allowed"
                )
            if str(row["available_at"]) != str(row["created_at"]):
                raise ValueError(
                    "Legacy task scheduling intent is unverifiable; default replay is "
                    "only allowed when the original row was immediately available"
                )
        elif stored_availability_mode != availability_mode:
            raise ValueError(
                "Task idempotency key is already bound to a different availability mode"
            )
        elif (
            availability_mode == "scheduled"
            and row["initial_available_at"] != initial_available_at
        ):
            raise ValueError(
                "Task idempotency key is already bound to a different original schedule"
            )
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
            _memory()._validated_nonsecret_metadata(key, "Task idempotency key")
        normalized_project = self._project_id(project_id)
        project = self.get_project(normalized_project)
        if project is None or not bool(project["enabled"]):
            raise ValueError(f"Project #{normalized_project} does not exist or is disabled")
        safe_model = None
        if requested_model is not None:
            safe_model = _memory()._validated_nonsecret_metadata(
                str(requested_model).strip(), "Task requested model"
            )[:200] or None
        stamp = _memory().now_iso()
        scheduled_text = (
            _memory()._as_utc(available_at).isoformat() if available_at is not None else None
        )
        availability_mode = "scheduled" if scheduled_text is not None else "immediate"
        available_text = scheduled_text or stamp
        with self._immediate_transaction():
            task_id, created = self._insert_task_locked(
                prompt,
                stamp=stamp,
                available_at=available_text,
                initial_available_at=scheduled_text,
                availability_mode=availability_mode,
                max_attempts=max_attempts,
                idempotency_key=key,
                project_id=normalized_project,
                requested_model=safe_model,
            )
            if created and (goal_id is not None or backlog_id is not None):
                self.db.execute(
                    "UPDATE tasks SET goal_id=?, backlog_id=? WHERE id=?",
                    (goal_id, backlog_id, task_id),
                )
            elif not created:
                provenance = self.db.execute(
                    "SELECT goal_id, backlog_id FROM tasks WHERE id=?",
                    (task_id,),
                ).fetchone()
                if provenance is None:
                    raise RuntimeError("Idempotent task disappeared")
                if (
                    provenance["goal_id"] != goal_id
                    or provenance["backlog_id"] != backlog_id
                ):
                    raise ValueError(
                        "Task idempotency key is already bound to different goal or "
                        "backlog provenance"
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
        specialist = _memory().SPECIALIST_BY_KEY.get(key)
        if specialist is None or self.get_specialist_agent(key) is None:
            raise ValueError("Unknown specialist")
        declared = _memory().specialist_for_consultation_prompt(text)
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
                f"conversation:{parent}" if parent is not None else f"request:{_memory().uuid4().hex}"
            )
        )
        stamp = _memory().now_iso()
        idempotency_key = None
        if parent is not None:
            digest = _memory().hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:24]
            scope_digest = _memory().hashlib.sha256(scope.encode("utf-8")).hexdigest()[:12]
            idempotency_key = f"delegation:{parent}:{scope_digest}:{key}:{digest}"
        with self._immediate_transaction():
            task_id, created = self._insert_task_locked(
                text,
                stamp=stamp,
                available_at=stamp,
                initial_available_at=None,
                availability_mode="immediate",
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
                    raise _memory().ModelBudgetExceeded(
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
                        _memory()._redacted_json_text({
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
        bounded = _memory()._bounded_limit(limit, 500)
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
        current = _memory()._as_utc(now)
        with self._immediate_transaction():
            return self._recover_stale_locked(current)

    def claim_task(
        self,
        worker_id: str | None = None,
        *,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        owner = _memory()._validated_worker_id(self.worker_id if worker_id is None else worker_id)
        lease_seconds = max(1, min(int(lease_seconds), 24 * 60 * 60))
        current = _memory()._as_utc(now)
        current_text = current.isoformat()
        lease_expires = (current + _memory().timedelta(seconds=lease_seconds)).isoformat()
        with self._immediate_transaction():
            control = self.db.execute(
                "SELECT state FROM runtime_control WHERE id=1"
            ).fetchone()
            if control is None or str(control["state"]) != "running":
                return None
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
                          available_at, initial_available_at, availability_mode,
                          lease_owner, lease_expires_at,
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
        owner = _memory()._validated_worker_id(self.worker_id if worker_id is None else worker_id)
        lease_seconds = max(1, min(int(lease_seconds), 24 * 60 * 60))
        current = _memory()._as_utc(now)
        with self._immediate_transaction():
            updated = self.db.execute(
                """UPDATE tasks SET updated_at=?, lease_expires_at=?
                   WHERE id=? AND status='running' AND lease_owner=?""",
                (
                    current.isoformat(),
                    (current + _memory().timedelta(seconds=lease_seconds)).isoformat(),
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
        if status not in _memory().TERMINAL_TASK_STATUSES:
            raise ValueError(f"Terminal task status required, got {status!r}")
        safe_result = _memory().redact_secrets(str(result))
        result_text = _memory()._bounded_persisted_text(safe_result, _memory().MAX_TASK_RESULT_CHARS, "task result")
        error_text = _memory()._bounded_persisted_text(safe_result, _memory().MAX_TASK_ERROR_CHARS, "task error") if status == "failed" else None
        owner = _memory()._validated_worker_id(self.worker_id if worker_id is None else worker_id)
        stamp = _memory().now_iso()
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
        error_text = _memory()._bounded_persisted_text(
            _memory().redact_secrets(str(error)), _memory().MAX_TASK_ERROR_CHARS, "task error"
        )
        owner = _memory()._validated_worker_id(self.worker_id if worker_id is None else worker_id)
        current = _memory()._as_utc(now)
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
            available_at = (current + _memory().timedelta(seconds=delay)).isoformat()
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
        owner = _memory()._validated_worker_id(self.worker_id if worker_id is None else worker_id)
        stamp = _memory().now_iso()
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
        limit = _memory()._bounded_limit(limit, 10_000)
        if not limit:
            return []
        rows = self.db.execute(
            """SELECT id, created_at, updated_at, status, prompt, result,
                      available_at, initial_available_at, availability_mode,
                      lease_owner, lease_expires_at,
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
        safe_name = _memory().redact_secrets(str(name).strip())[:120]
        safe_prompt = _memory().redact_secrets(str(prompt).strip())
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
        current = _memory()._as_utc(now)
        stamp = current.isoformat()
        next_run = (current + _memory().timedelta(minutes=interval)).isoformat()
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
        bounded = _memory()._bounded_limit(limit, 200)
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
        current = _memory()._as_utc(now)
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
                    (current + _memory().timedelta(minutes=1)).isoformat(),
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
        current = _memory()._as_utc(now)
        current_text = current.isoformat()
        queued = 0
        with self._immediate_transaction():
            control = self.db.execute(
                "SELECT state FROM runtime_control WHERE id=1"
            ).fetchone()
            if control is None or str(control["state"]) != "running":
                return 0
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
                specialist = _memory().specialist_for_scheduled_prompt(prompt)
                task_id, created = self._insert_task_locked(
                    prompt,
                    stamp=current_text,
                    available_at=current_text,
                    initial_available_at=scheduled_for,
                    availability_mode="scheduled",
                    max_attempts=3,
                    idempotency_key=f"schedule:{job_id}:{scheduled_for}",
                    project_id=int(row["project_id"]),
                    requested_model=(specialist.model_profile if specialist else None),
                    specialist_key=(specialist.key if specialist else None),
                    delegated_by="schedule",
                )
                queued += int(created)
                interval = _memory().timedelta(minutes=int(row["interval_minutes"]))
                next_run = _memory().datetime.fromisoformat(scheduled_for)
                if next_run.tzinfo is None:
                    next_run = next_run.replace(tzinfo=_memory().timezone.utc)
                next_run = next_run.astimezone(_memory().timezone.utc)
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
        topic = _memory()._validated_learning_topic(topic)
        interval_hours = max(1, min(int(interval_hours), 24 * 365))
        stamp = _memory().now_iso()
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
        topic = _memory()._validated_learning_topic(topic)
        interval_hours = max(1, min(int(interval_hours), 24 * 365))
        current = _memory().datetime.now(_memory().timezone.utc)
        stamp = current.isoformat()
        next_run = (current + _memory().timedelta(hours=interval_hours)).isoformat()
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
        current = _memory()._as_utc(now)
        current_text = current.isoformat()
        queued = 0
        with self._immediate_transaction():
            control = self.db.execute(
                "SELECT state FROM runtime_control WHERE id=1"
            ).fetchone()
            if control is None or str(control["state"]) != "running":
                return 0
            rows = self.db.execute(
                """SELECT id, topic, interval_hours, next_run
                   FROM learning_topics
                   WHERE enabled=1 AND next_run<=?
                   ORDER BY id""",
                (current_text,),
            ).fetchall()
            for row in rows:
                try:
                    topic = _memory()._validated_learning_topic(row["topic"])
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
                        _memory().specialist_for_prompt(prompt)
                        or _memory().SPECIALIST_BY_KEY["research"]
                    )
                    task_id, created = self._insert_task_locked(
                        prompt,
                        stamp=current_text,
                        available_at=current_text,
                        initial_available_at=str(scheduled_for),
                        availability_mode="scheduled",
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
                next_run = current + _memory().timedelta(hours=int(row["interval_hours"]))
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
        stamp = _memory().now_iso()
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
        limit = _memory()._bounded_limit(limit, 10_000)
        if not limit:
            return []
        rows = self.db.execute(
            """SELECT id, topic_id, scheduled_for, task_id, created_at
               FROM learning_runs ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]
