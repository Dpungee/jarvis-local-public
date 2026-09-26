"""Current Memory domain extracted without changing method control flow.

Global dependencies resolve through jarvis.memory at call time. The facade keeps
public imports, patch seams, constants and authorization helpers authoritative.
"""
from __future__ import annotations

from typing import Any
from .memory_runtime import (_memory)


class ApprovalsMemoryMixin:
    """Mechanically extracted current Memory methods."""

    @staticmethod
    def approval_fingerprint(action: str, resource: str, scope: str) -> str:
        normalized = _memory().json.dumps(
            {"action": str(action), "resource": str(resource), "scope": str(scope)},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        return _memory().hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    @staticmethod
    def approval_effect_fingerprint(action: str, resource: str) -> str:
        """Bind a standing grant to one canonical action/resource effect."""
        raw_resource = str(resource).strip()
        try:
            parsed = _memory().json.loads(raw_resource)
        except (TypeError, ValueError, _memory().json.JSONDecodeError):
            canonical_resource = raw_resource
        else:
            canonical_resource = _memory().json.dumps(
                parsed,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        normalized = _memory().json.dumps(
            {"action": str(action), "resource": canonical_resource},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return _memory().hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    @classmethod
    def session_approval_fingerprint(
        cls,
        action: str,
        resource: str,
        scope: str,
    ) -> str:
        normalized = _memory().json.dumps(
            {
                "effect": cls.approval_effect_fingerprint(action, resource),
                "grant_kind": "session",
                "scope": str(scope),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return _memory().hashlib.sha256(normalized.encode("utf-8")).hexdigest()

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
            parsed = _memory().json.loads(str(resource))
        except (TypeError, ValueError, _memory().json.JSONDecodeError):
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
            tool in _memory()._PERSISTENT_READ_APPROVAL_TOOLS
            and isinstance(arguments, dict)
            and set(arguments) == expected_keys.get(tool)
            and isinstance(digest, str)
            and _memory().re.fullmatch(r"[0-9a-f]{64}", digest) is not None
        )

    @staticmethod
    def _validated_approval_scope(scope: str, task_id: int | None) -> str:
        normalized = str(scope).strip()
        if normalized == "foreground":
            if task_id is not None:
                raise ValueError("Foreground approval scope cannot carry a task id")
            return normalized
        request_match = _memory().re.fullmatch(r"request:[0-9a-f]{24}", normalized)
        if request_match is not None:
            if task_id is not None:
                raise ValueError("Request approval scope cannot carry a task id")
            return normalized
        match = _memory().re.fullmatch(r"(task|conversation):([1-9][0-9]*)", normalized)
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
        action = _memory()._validated_nonsecret_metadata(action, "Approval action")[:100]
        exact_resource = str(resource).strip()
        presented_resource = (
            exact_resource if display_resource is None else str(display_resource).strip()
        )
        if display_resource is not None and presented_resource != exact_resource:
            try:
                exact_payload = _memory().json.loads(exact_resource)
                display_payload = _memory().json.loads(presented_resource)
            except (TypeError, ValueError, _memory().json.JSONDecodeError) as exc:
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
            parsed_resource = _memory().json.loads(presented_resource)
        except (TypeError, ValueError, _memory().json.JSONDecodeError):
            persisted_resource = _memory().redact_secrets(presented_resource)
        else:
            if isinstance(parsed_resource, dict):
                approval_tool = str(parsed_resource.get("tool") or "").casefold()
            persisted_resource = _memory()._redacted_json_text(parsed_resource)
        if action == "control_desktop_application" and approval_tool == "desktop_interact":
            if len(persisted_resource) > 32_000:
                raise ValueError("Desktop interaction approval resource is too large")
            resource = persisted_resource
        else:
            resource = _memory()._bounded_persisted_text(
                persisted_resource, 2_000, "approval resource"
            )
        reason = _memory()._bounded_persisted_text(
            _memory().redact_secrets(str(reason).strip()), 1_000, "approval reason"
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
        stamp = _memory().now_iso()
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
                            _memory().json.dumps(
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
                            _memory().json.dumps(
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
                            _memory().json.dumps(
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
                    _memory().json.dumps({"approval_id": approval_id, "scope": scope}, sort_keys=True),
                ),
            )
        return False, approval_id

    def decide_approval_always(self, approval_id: int) -> int | None:
        """Create or reactivate one reversible exact-effect read-only grant."""
        stamp_dt = _memory()._as_utc()
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
                    _memory().json.dumps(
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
        stamp_dt = _memory()._as_utc()
        stamp = stamp_dt.isoformat()
        ttl_hours = max(1, min(int(ttl_hours), 168))
        expires = (stamp_dt + _memory().timedelta(hours=ttl_hours)).isoformat()
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
            if _memory().re.fullmatch(r"conversation:[1-9][0-9]{0,18}", scope) is None:
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
                    _memory().json.dumps(
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
        limit = _memory()._bounded_limit(limit, 1_000)
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
                (_memory().now_iso(), limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def revoke_persistent_approval(self, grant_id: int) -> bool:
        stamp = _memory().now_iso()
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
                        _memory().json.dumps({"grant_id": int(grant_id)}, sort_keys=True),
                    ),
                )
        return updated.rowcount == 1

    def decide_approval(self, approval_id: int, approve: bool, *, ttl_hours: int = 24) -> bool:
        stamp_dt = _memory()._as_utc()
        stamp = stamp_dt.isoformat()
        ttl_hours = max(1, min(int(ttl_hours), 720))
        status = "approved" if approve else "denied"
        expires = (stamp_dt + _memory().timedelta(hours=ttl_hours)).isoformat() if approve else None
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
                        _memory().json.dumps(
                            {"approval_id": int(approval_id), "scope": row["scope"]},
                            sort_keys=True,
                        ),
                    ),
                )
        return updated.rowcount == 1

    def list_approvals(self, limit: int = 100) -> list[dict[str, Any]]:
        self._ensure_open()
        limit = _memory()._bounded_limit(limit, 1_000)
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
        elif isinstance(approval_id, str) and _memory().re.fullmatch(
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
