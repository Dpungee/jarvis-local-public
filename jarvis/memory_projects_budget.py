"""Current Memory domain extracted without changing method control flow.

Global dependencies resolve through jarvis.memory at call time. The facade keeps
public imports, patch seams, constants and authorization helpers authoritative.
"""
from __future__ import annotations

from typing import Any
import sqlite3
from .memory_runtime import (_memory)


class ProjectsBudgetMemoryMixin:
    """Mechanically extracted current Memory methods."""

    def _enabled_project_claim_scope(self, project_id: int | None) -> str | None:
        """Resolve an optional enabled project for generic claim shadowing."""
        if project_id is None:
            return None
        normalized_project = self._project_id(project_id)
        project = self.db.execute(
            "SELECT enabled FROM agent_projects WHERE id=?",
            (normalized_project,),
        ).fetchone()
        if project is None or not bool(project["enabled"]):
            raise ValueError("Recall project does not exist or is disabled")
        return _memory().project_claim_scope(normalized_project)

    @staticmethod
    def _project_relative_path(value: str) -> str:
        raw = str(value).strip().replace("\\", "/")
        if not raw or len(raw) > 500 or _memory().contains_secret(raw):
            raise ValueError("Project path must be bounded non-secret relative text")
        path = _memory().PurePosixPath(raw)
        if path.is_absolute() or any(part in {"", ".."} for part in path.parts):
            raise ValueError("Project path must stay beneath the configured workspace")
        normalized = path.as_posix().rstrip("/") or "."
        if normalized != "." and (
            len(path.parts) != 2
            or path.parts[0] != "@projects"
            or not _memory().re.fullmatch(
                r"[a-z0-9](?:[a-z0-9-]{0,58}[a-z0-9])?",
                path.parts[1],
            )
        ):
            raise ValueError("Project path must be '.' or one canonical isolated project path")
        return normalized

    def add_project(self, name: str, relative_path: str) -> int:
        safe_name = _memory().redact_secrets(str(name).strip())[:120]
        if not safe_name:
            raise ValueError("Project name must not be empty")
        safe_path = self._project_relative_path(relative_path)
        stamp = _memory().now_iso()
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
        if key not in _memory().SPECIALIST_BY_KEY:
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
        scope = _memory()._validated_nonsecret_metadata(value, "Model budget scope")
        if (
            not scope
            or len(scope) > 200
            or _memory().re.fullmatch(r"[a-z][a-z0-9_-]{0,31}:[A-Za-z0-9._-]{1,160}", scope)
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
                raise _memory().ModelBudgetExceeded(
                    f"request model-call limit reached ({maximum_calls})"
                )
            if prompt_tokens + estimate > maximum_prompt:
                raise _memory().ModelBudgetExceeded(
                    f"request prompt-token limit reached ({maximum_prompt})"
                )
            if completion_tokens >= maximum_completion:
                raise _memory().ModelBudgetExceeded(
                    f"request completion-token limit reached ({maximum_completion})"
                )
            cursor = self.db.execute(
                """INSERT INTO model_call_budget_events(
                       created_at, budget_scope, state, estimated_prompt_tokens
                   ) VALUES (?, ?, 'reserved', ?)""",
                (_memory().now_iso(), scope, estimate),
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
                    _memory().now_iso(),
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
        safe_provider = _memory()._validated_nonsecret_metadata(provider, "provider")[:40]
        safe_model = _memory()._validated_nonsecret_metadata(model, "model")[:200]
        safe_profile = _memory()._validated_nonsecret_metadata(profile, "profile")[:40]
        if not safe_provider or not safe_model or not safe_profile:
            raise ValueError("provider, model, and profile must not be empty")
        safe_failure = None
        if failure_kind is not None:
            safe_failure = _memory()._validated_nonsecret_metadata(failure_kind, "failure_kind")[:100]
        safe_scope = (
            None if budget_scope is None else self._model_budget_scope(budget_scope)
        )
        cursor = self.db.execute(
            """INSERT INTO model_call_metrics(
                created_at, provider, model, profile, latency_ms,
                prompt_tokens, completion_tokens, success, failure_kind, budget_scope
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                _memory().now_iso(), safe_provider, safe_model, safe_profile, latency_ms,
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
            since = (_memory().datetime.now(_memory().timezone.utc) - _memory().timedelta(hours=hours)).isoformat()
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
            p95_index = max(0, _memory().math.ceil(len(latencies) * 0.95) - 1)
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
