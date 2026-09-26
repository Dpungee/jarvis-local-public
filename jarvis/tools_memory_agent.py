"""Current ToolBox methods, mechanically extracted by domain.

Shared globals are resolved through jarvis.tools to preserve runtime patches.
"""
from __future__ import annotations


def _tools():
    from . import tools

    return tools


class MemoryAgentToolsMixin:
    def screen_companion_status(self) -> dict[str, _tools().Any]:
        """Read the shared Companion control plane without exposing screen content."""
        state = self.memory.screen_companion_state()
        state["learning"] = self.memory.screen_companion_learning_stats()
        return _tools().public_screen_companion_state(state)

    def screen_companion_control(
        self,
        action: str,
        mode: str | None = None,
    ) -> dict[str, _tools().Any]:
        """Apply one explicit bounded control and return an exact database readback."""
        normalized_action = str(action).strip().casefold()
        if self.config.autonomy == "readonly" and normalized_action not in {
            "off", "pause",
        }:
            raise PermissionError(
                "Readonly mode may only pause or turn off Screen Companion"
            )
        state = self.memory.control_screen_companion_state(action=action, mode=mode)
        state["learning"] = self.memory.screen_companion_learning_stats()
        return _tools().public_screen_companion_state(state)

    def remember(self, content: str, kind: str = "fact", source: str | None = None) -> str:
        if self.config.autonomy == "readonly":
            raise PermissionError("Durable memory writes are disabled in readonly mode")
        content = content.strip()
        source = source.strip() if source else None
        if not content or len(content) > 4000:
            raise ValueError("Memory content must contain 1-4000 characters")
        if source and len(source) > 1000:
            raise ValueError("Memory source is too long")
        if kind not in {"fact", "preference", "research"}:
            raise ValueError(
                "Memory kind must be fact, preference, or research; verified lessons "
                "are written only by the outcome-provenance pipeline"
            )
        combined = f"{content}\n{source or ''}"
        if _tools()._contains_secret(combined):
            raise ValueError("Potential secret detected; memory write refused")
        if _tools()._INSTRUCTION_PATTERN.search(content):
            raise ValueError("Instruction-like memory refused")
        context = getattr(self, "memory_write_context", None)
        if isinstance(context, dict) and context:
            # Set by the agent for exactly one dispatched call and reset in
            # its finally: the spine records who wrote the row.
            conversation_id = context.get("conversation_id")
            return self.memory.remember_verified(
                content,
                kind,
                source,
                origin="explicit_operator_memory",
                actor=str(context.get("actor") or "model"),
                permission=str(context.get("permission") or "runtime")[:80],
                conversation_id=(
                    int(conversation_id)
                    if isinstance(conversation_id, int)
                    and not isinstance(conversation_id, bool)
                    else None
                ),
            )
        return self.memory.remember_verified(
            content,
            kind,
            source,
            origin="explicit_operator_memory",
        )

    def delegate_specialist(self, task: str, max_attempts: int = 3) -> dict[str, _tools().Any]:
        context = self._agent_execution_context.get()
        if context is None:
            raise PermissionError("Specialist delegation requires an active Jarvis context")
        project_id, conversation_id, specialist_key, model_budget_scope = context
        if specialist_key is not None:
            raise PermissionError("Specialists cannot delegate or discover peer agents")
        selected = _tools().specialist_for_prompt(task)
        if selected is None:
            raise ValueError(
                "No single-purpose specialist matches this task; Jarvis must handle or clarify it"
            )
        task_id = self.memory.delegate_specialist_task(
            task,
            specialist_key=selected.key,
            project_id=project_id,
            parent_conversation_id=conversation_id,
            max_attempts=max_attempts,
            model_budget_scope=model_budget_scope,
            max_delegations=int(
                getattr(self.config, "specialist_delegation_limit_per_request", 4)
            ),
        )
        return {
            "task_id": task_id,
            "specialist": selected.name,
            "purpose": selected.purpose,
            "model_profile": selected.model_profile,
            "project_id": project_id,
            "status": "queued",
            "report_to": "JARVIS",
        }

    def specialist_reports(
        self,
        task_id: int | None = None,
        limit: int = 20,
        wait_seconds: int | None = None,
    ) -> list[dict[str, _tools().Any]]:
        context = self._agent_execution_context.get()
        if context is None:
            raise PermissionError("Specialist reports require an active Jarvis context")
        project_id, _conversation_id, specialist_key, _model_budget_scope = context
        if specialist_key is not None:
            raise PermissionError("Specialists cannot discover peer agents or reports")
        bounded_wait = (
            10 if task_id is not None and wait_seconds is None else int(wait_seconds or 0)
        )
        bounded_wait = max(0, min(bounded_wait, 30))
        deadline = _tools().time.monotonic() + bounded_wait
        while True:
            reports = self.memory.specialist_task_reports(
                project_id=project_id,
                task_id=task_id,
                limit=limit,
            )
            if (
                task_id is None
                or not reports
                or str(reports[0].get("status") or "").casefold() in {"done", "failed"}
                or _tools().time.monotonic() >= deadline
            ):
                break
            _tools().time.sleep(min(0.25, max(0.0, deadline - _tools().time.monotonic())))
        return [
            {
                "task_id": int(item["id"]),
                "specialist": str(item["specialist_name"]),
                "purpose": str(item["specialist_purpose"]),
                "status": str(item["status"]),
                "model_profile": str(item.get("requested_model") or "auto"),
                "attempts": int(item.get("attempt_count") or 0),
                "result": str(item.get("result") or "")[:12_000],
                "last_error": str(item.get("last_error") or "")[:2_000],
            }
            for item in reports
        ]

    def recall(self, query: str) -> list[dict[str, _tools().Any]]:
        context = self._agent_execution_context.get()
        if context is None:
            return self.memory.search(query)
        return self.memory.search(query, project_id=context[0])

    def session_search(
        self,
        query: str,
        limit: int = 8,
    ) -> list[dict[str, _tools().Any]]:
        context = self._agent_execution_context.get()
        project_id = context[0] if context is not None else None
        return self.memory.search_messages(query, limit, project_id=project_id)

    def _schedule_project_id(self) -> int:
        context = self._agent_execution_context.get()
        if context is None:
            raise PermissionError("Schedules require an active Jarvis project context")
        project_id, _conversation_id, specialist_key, _model_budget_scope = context
        if specialist_key is not None:
            raise PermissionError("Specialists cannot create or manage Jarvis schedules")
        return int(project_id)

    def schedule_create(
        self,
        name: str,
        task: str,
        interval_minutes: int,
    ) -> dict[str, _tools().Any]:
        if self.config.autonomy == "readonly":
            raise PermissionError("Schedule creation is disabled in readonly mode")
        return self.memory.add_scheduled_job(
            name,
            task,
            interval_minutes,
            project_id=self._schedule_project_id(),
        )

    def schedule_list(self, limit: int = 50) -> list[dict[str, _tools().Any]]:
        return self.memory.list_scheduled_jobs(
            project_id=self._schedule_project_id(),
            limit=limit,
        )

    def schedule_set_enabled(self, job_id: int, enabled: bool) -> dict[str, _tools().Any]:
        if self.config.autonomy == "readonly":
            raise PermissionError("Schedule changes are disabled in readonly mode")
        changed = self.memory.set_scheduled_job_enabled(
            job_id,
            enabled,
            project_id=self._schedule_project_id(),
        )
        if not changed:
            raise KeyError(f"Scheduled job #{job_id} was not found in this project")
        return {"job_id": int(job_id), "enabled": bool(enabled)}

    def schedule_delete(self, job_id: int) -> dict[str, _tools().Any]:
        if self.config.autonomy == "readonly":
            raise PermissionError("Schedule deletion is disabled in readonly mode")
        deleted = self.memory.delete_scheduled_job(
            job_id,
            project_id=self._schedule_project_id(),
        )
        if not deleted:
            raise KeyError(f"Scheduled job #{job_id} was not found in this project")
        return {"job_id": int(job_id), "deleted": True}
