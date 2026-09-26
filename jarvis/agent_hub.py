"""Central Agent Hub: a separate web interface over the JARVIS agent backend.

JARVIS stays the source of truth. The Hub server holds no agent registry, task list or
memory of its own; every view is read from the backend on request and every command goes
through the backend's services. Closing a browser tab changes nothing on the backend.

Access model (the same pattern JARVIS already uses for Presence):

* The server binds to loopback only. Remote devices reach it through an HTTPS private-
  network proxy such as Tailscale Serve, and only hosts on an explicit allowlist are
  accepted.
* The local operator authenticates with a random token printed at launch.
* A remote device pairs with a one-time, ten-minute code created on the backend host and
  receives a revocable session. Codes and session tokens are stored only as digests.
* Every state-changing command is recorded in a durable audit log.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import socket
import sqlite3
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .agent_hub_runtime import (
    DEFAULT_PERMISSIONS,
    EFFORT_LABELS,
    GOAL_CATEGORIES,
    KNOWN_MODELS,
    PERMISSION_LABELS,
    TERMINAL_STATES,
    AgentRuntime,
    TaskError,
    _project_file,
    normalize_permissions,
    validate_model,
)
from .command_center import CommandCenterService, _jsonable
from .multi_agent_runtime import AgentLifecycle, MultiAgentRuntimeError, MultiAgentRuntimeStore

MAX_BODY = 64 * 1024
STATIC_DIR = Path(__file__).with_name("agent_hub_static")
STATIC_FILES = {"/": "index.html", "/hub.css": "hub.css", "/hub.js": "hub.js"}
LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
PREVIEWABLE_IMAGES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
# Played inline by the browser's own media element; never interpreted as a document.
PREVIEWABLE_MEDIA = frozenset({"video/mp4", "video/webm", "audio/mpeg", "audio/wav", "audio/x-wav",
                               "audio/ogg", "audio/webm", "audio/mp4", "audio/flac", "audio/aac"})
MAX_FILE_SERVE = 200 * 1024 * 1024
TEXT_LIKE = ("text/", "application/json", "application/xml", "application/javascript")
STALE_AFTER = 300.0


def _json_default(value: Any) -> Any:
    if hasattr(value, "value"):
        return value.value
    return str(value)


class HubError(ValueError):
    """A request the Hub refuses with a readable reason."""


class HubService:
    """Read models and commands for the Hub. Backend services do the actual work."""

    def __init__(self, *, state_dir: Path, provider_profile_dir: Path, capacity: int = 2,
                 workspace_roots: dict[str, Path] | None = None, runtime_kwargs: dict[str, Any] | None = None,
                 command_center: CommandCenterService | None = None,
                 chat_providers: dict[str, Any] | None = None) -> None:
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.projects_dir = self.state_dir / "projects"
        self.projects_dir.mkdir(exist_ok=True)
        if chat_providers is None:
            from .subscription_chat import ClaudeSubscription, CodexSubscription
            from .agent_providers import newest_claude_executable
            claude = ClaudeSubscription()
            claude.executable = newest_claude_executable() or claude.executable
            chat_providers = {'claude-cli': claude, 'codex-cli': CodexSubscription(provider_profile_dir)}
        self.command_center = command_center or CommandCenterService(
            self.state_dir / "runtime.db", self.state_dir / "execution.db",
            providers=chat_providers, max_workers=capacity, workspace_roots=dict(workspace_roots or {}))
        self.runtime_path = self.command_center.runtime_path
        for project in self.command_center.conversations.projects():
            self._ensure_root(project["project_id"])
        self.runtime = AgentRuntime(state_dir=self.state_dir, runtime_path=self.runtime_path,
                                    provider_profile_dir=provider_profile_dir, project_root=self.project_root,
                                    capacity=capacity, **(runtime_kwargs or {}))
        self._audit_init()

    def chat(self, agent_id: str, chat_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        agent = self.agent_detail(agent_id)
        if not isinstance(chat_id, str) or not chat_id:
            raise HubError('Choose a chat first.')
        scope = self.command_center.conversations.chat_scope(agent['project_id'], agent_id, chat_id)
        legacy = self.command_center.conversations.snapshot(scope, agent_id)
        if payload is not None:
            if set(payload) != {'body', 'request_id'}:
                raise HubError('Chat accepts only message text and a request identifier; permissions are managed separately.')
            if agent['archived']:
                raise HubError('This agent is archived.')
            from .subscription_chat import release_operator_text, release_text
            # The operator's own message may carry addresses and paths they chose to give;
            # secrets are still refused.
            body = release_operator_text(payload['body'])
            request_id = payload['request_id']
            if not isinstance(request_id, str) or not 1 <= len(request_id) <= 200:
                raise HubError('A bounded request identifier is required.')
            history = [{'role':'user' if m['role']=='operator' else 'assistant','content':m['body']}
                       for m in legacy['messages'] if m['state'] in {'COMPLETED','APPLIED'}]
            for message in history:  # screened as text; JSON escaping doubled backslashes
                (release_operator_text if message['role'] == 'user' else release_text)(message['content'])
            digest = hashlib.sha256(json.dumps([agent_id,chat_id,body]).encode()).hexdigest()
            self.runtime.set_chat_archived(agent_id, chat_id, False)
            return self.runtime.create_task(agent_id, title=body[:80], request=body,
                _chat={'chat_id':chat_id,'request_id':request_id,'digest':digest,'history':history})
        turns = [turn for turn in self.runtime.chat_tasks(agent_id, chat_id)
                 # A watch that found nothing to report stays out of the conversation.
                 if not (turn['request'].startswith('⏰') and (turn['result'] or '').strip() == 'NO_UPDATE')]
        messages = list(legacy['messages'])
        for turn in turns:
            reply = {'role':'assistant','body':turn['result'] or turn['blocker'] or turn['progress'] or 'Received — waiting for execution.',
                     'state':turn['state'],'message_id':turn['task_id']+'-assistant'}
            if turn['state'] == 'RUNNING' and turn.get('partial'):
                # Streamed while the model writes; the verified result replaces it at the end.
                reply.update(body=turn['partial'], provisional=True)
            messages.extend([
                {'role':'operator','body':turn['request'],'state':'APPLIED','message_id':turn['task_id']+'-user'},
                reply])

        def live_steps(turn: dict[str, Any]) -> list[dict[str, Any]]:
            # Real recorded activity for work still in flight: tool calls and phases.
            if turn['state'] not in {'QUEUED', 'RUNNING'}:
                return []
            events = self.runtime.latest_events(task_id=turn['task_id'], limit=12)
            return [{'kind': e['kind'], 'summary': e['summary'], 'ts': e['ts']}
                    for e in events if e['kind'] in {'tool','progress','model','steering','recovery','failover'}][-6:]

        return {'usage': self.runtime.chat_usage(agent_id, chat_id),
                'messages':messages,'agent_turns':[dict(t, approval=self.runtime.approval_detail(t), steps=live_steps(t),
                                                   previews=self.runtime.previews(task_id=t['task_id'])) for t in turns],
                'live_turns':self.command_center.live.snapshot(scope,agent_id)['live_turns'],
                'capabilities':agent['permissions'],'context_note':'Recent conversation context is retained; older context may be omitted when the context window fills.'}

    def create_chat(self, agent_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if set(payload) != {'title'}:
            raise HubError('New chat accepts only a title.')
        agent = self.agent_detail(agent_id)
        if agent['archived']:
            raise HubError('This agent is archived.')
        return self.command_center.composer_control(agent_id, agent['project_id'], 'chats', payload)

    # ---------------------------------------------------------------- projects
    def _ensure_root(self, project_id: str) -> Path:
        roots = self.command_center.conversations.roots
        if project_id not in roots:
            # Managed project folders are chosen by the backend, never taken from a request.
            path = self.projects_dir / project_id
            path.mkdir(parents=True, exist_ok=True)
            roots[project_id] = path.resolve()
        return Path(roots[project_id])

    def project_root(self, project_id: str) -> Path:
        return self._ensure_root(project_id or "command-center")

    def create_project(self, name: str) -> dict[str, Any]:
        name = " ".join(str(name or "").split())
        if not name or len(name) > 120:
            raise HubError("Give the project a name (up to 120 characters).")
        project = self.command_center.conversations.create_project({"name": name})
        self._ensure_root(project["project_id"])
        self.runtime.event(None, None, project["project_id"], "project", f"Project created · {name}")
        return project

    # ------------------------------------------------------------------ agents
    def _agents(self) -> list[dict[str, Any]]:
        with MultiAgentRuntimeStore(self.runtime_path) as store:
            rows = store.db.execute("SELECT * FROM runtime_agents ORDER BY created_at, agent_id").fetchall()
            return [_jsonable(store._agent_from_row(row)) for row in rows]

    def _status(self, agent: dict[str, Any], settings: dict[str, Any], summary: dict[str, Any],
                providers: dict[str, Any]) -> dict[str, Any]:
        current = summary.get("current")
        counts = summary.get("counts", {})
        provider = providers.get(agent["model_provider"], {})
        last_event = summary.get("last_event")

        def status(code: str, label: str, detail: str, tone: str) -> dict[str, Any]:
            return {"code": code, "label": label, "detail": detail, "tone": tone}

        if settings["archived"]:
            return status("archived", "Archived", "Kept with all its reports and work. Restore to use it again.", "muted")
        lifecycle = agent["lifecycle"]
        if lifecycle == "STOPPED":
            return status("disabled", "Disabled", "Enable it to accept work again.", "muted")
        if lifecycle == "CREATED":
            return status("not_enabled", "Not enabled", "Enable it before assigning work.", "attention")
        if lifecycle == "PAUSED":
            return status("paused", "Paused", "Queued work waits until you resume the agent.", "attention")
        if current and current["state"] == "RUNNING":
            running = current["task_id"] in self.runtime.running_ids()
            quiet = time.time() - float((last_event or {}).get("ts") or current["started_at"] or time.time())
            if not running:
                return status("stale", "Stale", "Marked running but no executor holds it.", "danger")
            if quiet > STALE_AFTER:
                return status("stale", "No recent activity",
                              f"Running, but nothing reported for {int(quiet // 60)} min.", "attention")
            return status("running", "Running", current.get("progress") or "Working", "live")
        if current and current["state"] == "WAITING_APPROVAL":
            return status("waiting_approval", "Waiting for approval", current.get("blocker") or "", "attention")
        if current and current["state"] == "WAITING_INPUT":
            return status("waiting_input", "Waiting for your input", current.get("blocker") or "", "attention")
        if current and current["state"] == "WAITING_PROVIDER":
            return status("waiting_provider", "Waiting for provider", current.get("blocker") or "", "attention")
        if current and current["state"] in {"PAUSED", "INTERRUPTED"}:
            label = "Task paused" if current["state"] == "PAUSED" else "Interrupted"
            return status(current["state"].lower(), label, current.get("blocker") or "", "attention")
        if not provider.get("installed") or not provider.get("authenticated"):
            return status("provider", "Provider not signed in", provider.get("detail") or "", "danger")
        if counts.get("QUEUED"):
            return status("queued", "Queued", current.get("progress") if current else "Waiting for a free slot.", "info")
        last_task = summary.get("last_task")
        if last_task and last_task["state"] == "FAILED":
            return status("failed", "Last task failed", last_task.get("blocker") or "", "danger")
        return status("idle", "Idle", "Ready for work.", "ok")

    def overview(self) -> dict[str, Any]:
        providers = self.runtime.providers()
        summaries = self.runtime.agent_summaries()
        projects = {p["project_id"]: p for p in self.command_center.conversations.projects()}
        agents = []
        for agent in self._agents():
            settings = self.runtime.agent_settings(agent["agent_id"])
            summary = summaries.get(agent["agent_id"], {})
            counts = summary.get("counts", {})
            current = summary.get("current")
            agents.append({
                "agent_id": agent["agent_id"], "name": agent["display_name"], "role": agent["role"],
                "purpose": agent["purpose"], "lifecycle": agent["lifecycle"],
                "provider": agent["model_provider"], "model": agent["model_name"],
                "project_id": agent["project_id"],
                "project_name": projects.get(agent["project_id"], {}).get("name", agent["project_id"]),
                "archived": settings["archived"],
                "permissions": settings["permissions"],
                "effort": self.runtime.agent_effort(agent["agent_id"]),
                "effort_levels": self.runtime.effort_levels(agent["model_provider"], agent["model_name"]),
                "status": self._status(agent, settings, summary, providers),
                "current_task": None if not current else {
                    "task_id": current["task_id"], "title": current["title"], "state": current["state"],
                    "progress": current["progress"], "started_at": current["started_at"],
                    "model": current["model_override"] or current["model_configured"]},
                "last_model_used": (summary.get("last_task") or {}).get("model_used"),
                "last_activity": summary.get("last_event"),
                "counts": {
                    "running": counts.get("RUNNING", 0), "queued": counts.get("QUEUED", 0),
                    "blocked": sum(counts.get(s, 0) for s in ("WAITING_APPROVAL", "WAITING_INPUT",
                                                              "WAITING_PROVIDER", "PAUSED", "INTERRUPTED")),
                    "completed": counts.get("COMPLETED", 0), "failed": counts.get("FAILED", 0),
                    "cancelled": counts.get("CANCELLED", 0)},
            })
        return {
            "agents": agents,
            "projects": [p | {"has_workspace": True} for p in projects.values()],
            "providers": providers,
            "defaults": self.runtime.defaults(),
            "capacity": {"slots": self.runtime.capacity, "running": len(self.runtime.running_ids())},
            "permissions": {"labels": PERMISSION_LABELS, "defaults": DEFAULT_PERMISSIONS},
            "known_models": KNOWN_MODELS,
            "features": {"archive": True, "goals": True, "search": True, "artifacts": True, "schedules": True,
                         "effort": True, "usage": True},
            "goal_categories": GOAL_CATEGORIES,
            "effort_labels": EFFORT_LABELS,
            "last_seq": self.runtime.last_seq(),
            "server_time": time.time(),
        }

    def agent_detail(self, agent_id: str) -> dict[str, Any]:
        overview = self.overview()
        agent = next((a for a in overview["agents"] if a["agent_id"] == agent_id), None)
        if agent is None:
            raise HubError("Unknown agent.")
        settings = self.runtime.agent_settings(agent_id)
        chats = self.command_center.conversations.chats(agent["project_id"] or "command-center", agent_id)
        activity = self.runtime.chat_activity(agent_id)
        archived_chats = self.runtime.archived(agent_id, "chat")
        chats = [c | {"archived": c["chat_id"] in archived_chats,
                      "last_at": max(float(c["created_at"]), float(activity.get(c["chat_id"], {}).get("last_at") or 0)),
                      "turns": activity.get(c["chat_id"], {}).get("turns", 0),
                      "active": activity.get(c["chat_id"], {}).get("active", False)} for c in chats]
        tasks = self.runtime.tasks(agent_id=agent_id, limit=100)
        archived_tasks = self.runtime.archived(agent_id, "task")
        task_chats = self.runtime.task_chats(agent_id)
        tasks = [t | {"archived": t["task_id"] in archived_tasks, "chat_id": task_chats.get(t["task_id"])}
                 for t in tasks]
        approvals = []
        for task in tasks:
            if task["state"] == "WAITING_APPROVAL":
                approvals.append({"task_id": task["task_id"], "title": task["title"],
                                  "approval": self.runtime.approval_detail(task)})
        return agent | {
            "instructions": settings["instructions"],
            "chats": chats,
            "tasks": tasks,
            "approvals": approvals,
            "artifacts": self.runtime.artifacts(agent_id=agent_id, limit=100),
            "events": self.runtime.latest_events(agent_id=agent_id, limit=80),
            "errors": [e for e in self.runtime.latest_events(agent_id=agent_id, limit=200)
                       if e["level"] in {"error", "warn"}][-20:],
            "providers": overview["providers"],
            "known_models": KNOWN_MODELS,
            "permission_labels": PERMISSION_LABELS,
            "goals": self.runtime.goals(agent_id, include_archived=True),
            "schedules": self.runtime.schedules(agent_id),
        }

    # ------------------------------------------------ conversations and tasks
    def _agent_project(self, agent_id: str) -> str:
        agent = next((a for a in self._agents() if a["agent_id"] == agent_id), None)
        if agent is None:
            raise HubError("Unknown agent.")
        return agent["project_id"] or "command-center"

    def archive_chat(self, agent_id: str, chat_id: str, archived: bool) -> dict[str, Any]:
        self.command_center.conversations.chat_scope(self._agent_project(agent_id), agent_id, str(chat_id))
        return self.runtime.set_chat_archived(agent_id, str(chat_id), archived)

    def delete_chat(self, agent_id: str, chat_id: str) -> dict[str, Any]:
        project, chat_id = self._agent_project(agent_id), str(chat_id)
        conversations = self.command_center.conversations
        conversations.delete_chat(project, agent_id, chat_id, check_only=True)
        result = self.runtime.delete_chat(agent_id, chat_id)
        conversations.delete_chat(project, agent_id, chat_id)
        return result

    def search(self, agent_id: str, query: str) -> dict[str, Any]:
        project = self._agent_project(agent_id)
        needle = " ".join(str(query or "").split()).casefold()
        chats = [c for c in self.command_center.conversations.chats(project, agent_id)
                 if needle and needle in str(c["title"]).casefold()]
        return {"query": query, "chats": chats[:20], "messages": self.runtime.search(agent_id, query)}

    def artifacts(self, agent_id: str) -> dict[str, Any]:
        project = self._agent_project(agent_id)
        return self.runtime.artifact_gallery(agent_id, self.project_root(project))

    def project_file(self, agent_id: str, relative: str) -> tuple[Path, str]:
        file = _project_file(self.project_root(self._agent_project(agent_id)), str(relative or ""))
        if file is None:
            raise HubError("That file is not in this agent's project folder.")
        import mimetypes

        return file, mimetypes.guess_type(file.name)[0] or "application/octet-stream"

    def task_detail(self, task_id: str) -> dict[str, Any]:
        task = self.runtime.task(task_id)
        agent = next((a for a in self.overview()["agents"] if a["agent_id"] == task["agent_id"]), None)
        return task | {
            "agent": None if agent is None else {"agent_id": agent["agent_id"], "name": agent["name"],
                                                 "provider": agent["provider"], "model": agent["model"]},
            "project_name": next((p["name"] for p in self.command_center.conversations.projects()
                                  if p["project_id"] == task["project_id"]), task["project_id"]),
            "timeline": self.runtime.events(task_id=task_id, limit=500),
            "approval": self.runtime.approval_detail(task),
        }

    def projects(self) -> list[dict[str, Any]]:
        overview = self.overview()
        result = []
        for project in overview["projects"]:
            pid = project["project_id"]
            agents = [a for a in overview["agents"] if a["project_id"] == pid]
            tasks = self.runtime.tasks(project_id=pid, limit=200)
            result.append(project | {
                "agents": [{"agent_id": a["agent_id"], "name": a["name"], "status": a["status"]} for a in agents],
                "tasks": tasks,
                "artifacts": self.runtime.artifacts(project_id=pid, limit=300),
            })
        return result

    @staticmethod
    def _permission_payload(payload: dict[str, Any]) -> dict[str, Any]:
        """Translate explicit groups without adding defaults or broadening grants.

        The original schema groups document tools with files_write. A standalone
        documents grant cannot be represented faithfully and is refused.
        Actor is never accepted from payloads: HTTP authentication supplies audit identity.
        """
        if 'tool_groups' not in payload:
            return payload
        if 'permissions' in payload:
            raise HubError('Specify permissions or tool_groups, not both.')
        groups = payload['tool_groups']
        if not isinstance(groups, list) or any(not isinstance(g, str) or g not in PERMISSION_LABELS for g in groups):
            raise HubError('Unsupported tool_groups for this saved-state schema.')
        result = {k: v for k, v in payload.items() if k != 'tool_groups'}
        result['permissions'] = {key: key in groups for key in PERMISSION_LABELS}
        return result

    def create_agent(self, payload: dict[str, Any]) -> dict[str, Any]:
        payload = self._permission_payload(payload)
        allowed = {"name", "role", "purpose", "instructions", "provider", "model", "project_id",
                   "permissions", "enable"}
        if set(payload) - allowed:
            raise HubError("Unsupported agent fields.")
        name = " ".join(str(payload.get("name") or "").split())
        role = " ".join(str(payload.get("role") or "").split())
        if not name or not role:
            raise HubError("An agent needs a name and a role.")
        defaults = self.runtime.defaults()
        provider, model = validate_model(payload.get("provider") or defaults["provider"],
                                         payload.get("model") or defaults["model"])
        project_id = str(payload.get("project_id") or "command-center")
        if project_id not in {p["project_id"] for p in self.command_center.conversations.projects()}:
            raise HubError("Choose an existing project.")
        permissions = normalize_permissions(payload.get("permissions"))
        with MultiAgentRuntimeStore(self.runtime_path) as store:
            agent = store.create_agent(display_name=name[:120], role=role[:200],
                                       purpose=str(payload.get("purpose") or "")[:4000], specialties=(),
                                       model_provider=provider, model_name=model, project_id=project_id,
                                       idempotency_key=f"hub-agent-{secrets.token_hex(8)}")
        self._ensure_root(project_id)
        self.runtime.save_agent_settings(agent.agent_id, instructions=str(payload.get("instructions") or ""),
                                         permissions=permissions, project_id=project_id)
        self.runtime.event(agent.agent_id, None, project_id, "lifecycle",
                           f"Agent created · {name} · {provider} · {model}")
        if payload.get("enable") is True:
            self.lifecycle(agent.agent_id, "start")
        return self.agent_detail(agent.agent_id)

    def grant_full_access(self) -> dict[str, Any]:
        """Operator action: every non-archived agent gets every Hub permission group."""
        full = {key: True for key in PERMISSION_LABELS}
        changed = []
        for agent in self._agents():
            settings = self.runtime.agent_settings(agent["agent_id"])
            if settings["archived"]:
                continue
            if settings["permissions"] != full:
                self.runtime.save_agent_settings(agent["agent_id"], permissions=full,
                                                 project_id=agent.get("project_id"))
                changed.append(agent["agent_id"])
        return {"updated": changed, "permissions": full}

    def configure_agent(self, agent_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        payload = self._permission_payload(payload)
        if set(payload) - {"instructions", "permissions", "provider", "model"}:
            raise HubError("Unsupported configuration fields.")
        detail = self.agent_detail(agent_id)
        notes = []
        if "provider" in payload or "model" in payload:
            provider, model = validate_model(payload.get("provider") or detail["provider"],
                                             payload.get("model") or detail["model"])
            if (provider, model) != (detail["provider"], detail["model"]):
                with MultiAgentRuntimeStore(self.runtime_path) as store:
                    store.update_agent_model(agent_id, actor_id="owner", model_provider=provider,
                                             model_name=model, idempotency_key=f"hub-model-{secrets.token_hex(8)}")
                running = detail["current_task"] and detail["current_task"]["state"] == "RUNNING"
                when = "from the next task (the running task keeps its model)" if running else "from the next task"
                self.runtime.event(agent_id, None, detail["project_id"], "settings",
                                   f"Model changed to {provider} · {model} — applies {when}")
                notes.append(f"Model change applies {when}.")
        self.runtime.save_agent_settings(agent_id, instructions=payload.get("instructions"),
                                         permissions=payload.get("permissions"), project_id=detail["project_id"])
        return self.agent_detail(agent_id) | {"notes": notes}

    def lifecycle(self, agent_id: str, action: str) -> dict[str, Any]:
        if action not in {"start", "pause", "stop"}:
            raise HubError("Unsupported lifecycle action.")
        target = {"start": AgentLifecycle.RUNNING, "pause": AgentLifecycle.PAUSED, "stop": AgentLifecycle.STOPPED}[action]
        with MultiAgentRuntimeStore(self.runtime_path) as store:
            current = store.get_agent(agent_id)
            if current.lifecycle is not target:
                store.set_agent_lifecycle(agent_id, target, actor_id="owner",
                                          idempotency_key=f"hub-{action}-{secrets.token_hex(8)}")
        if action in {"pause", "stop"}:
            for task in self.runtime.tasks(agent_id=agent_id, limit=50):
                if task["state"] == "RUNNING":
                    self.runtime.pause(task["task_id"]) if action == "pause" else self.runtime.cancel(task["task_id"])
        label = {"start": "Enabled", "pause": "Paused", "stop": "Disabled"}[action]
        self.runtime.event(agent_id, None, None, "lifecycle", f"Agent {label.lower()}")
        return self.agent_detail(agent_id)

    def archive(self, agent_id: str, archived: bool) -> dict[str, Any]:
        detail = self.agent_detail(agent_id)
        if archived and any(t["state"] not in TERMINAL_STATES for t in detail["tasks"]):
            raise HubError("Finish or cancel this agent's open tasks before archiving it.")
        if archived:
            self.lifecycle(agent_id, "stop")
        self.runtime.save_agent_settings(agent_id, archived=archived, project_id=detail["project_id"])
        return self.agent_detail(agent_id)

    # ------------------------------------------------------------------- audit
    def _audit_init(self) -> None:
        with self.runtime.db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS hub_audit(
                seq INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, actor TEXT NOT NULL,
                action TEXT NOT NULL, target TEXT, outcome TEXT NOT NULL, detail TEXT)""")

    def audit(self, actor: str, action: str, target: str | None, outcome: str, detail: str = "") -> None:
        with self.runtime.db() as db:
            db.execute("INSERT INTO hub_audit(ts, actor, action, target, outcome, detail) VALUES (?,?,?,?,?,?)",
                       (time.time(), actor[:80], action[:80], (target or "")[:120], outcome[:20], detail[:500]))

    def audit_log(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.runtime.db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM hub_audit ORDER BY seq DESC LIMIT ?", (limit,))]

    def close(self) -> None:
        self.runtime.close()
        self.command_center.close()


class HubAuth:
    """Local operator token plus revocable paired remote sessions (JARVIS pairing store)."""

    def __init__(self, state_dir: Path, *, remote_mode: str = "disabled", trusted_hosts: tuple[str, ...] = ()) -> None:
        self.state_dir = Path(state_dir)
        self.operator_token = secrets.token_urlsafe(32)
        self.remote_mode = remote_mode
        self.trusted_hosts = frozenset(h.casefold() for h in trusted_hosts)
        self._memory = None
        self._lock = threading.Lock()
        self._failures: dict[str, list[float]] = {}

    def _store(self) -> Any:
        with self._lock:
            if self._memory is None:
                from .memory import Memory

                self._memory = Memory(self.state_dir / "hub-auth.db")
            return self._memory

    def actor(self, authorization: str) -> str | None:
        if not authorization.startswith("Bearer "):
            return None
        token = authorization[7:].strip()
        if secrets.compare_digest(token, self.operator_token):
            return "local-operator"
        if self.remote_mode == "paired" and token:
            try:
                if self._store().authenticate_presence_session(token):
                    return "paired-device"
            except Exception:
                return None
        return None

    def pair(self, code: str, client: str) -> dict[str, Any] | None:
        window = [t for t in self._failures.get(client, []) if time.time() - t < 600]
        if len(window) >= 8:
            raise HubError("Too many pairing attempts. Wait ten minutes.")
        session = self._store().consume_presence_pairing_code(code) if self.remote_mode == "paired" else None
        if session is None:
            self._failures[client] = window + [time.time()]
        return session

    def create_code(self, label: str) -> dict[str, Any]:
        return self._store().create_presence_pairing_code(label=label)

    def sessions(self) -> list[dict[str, Any]]:
        return self._store().list_presence_sessions() if self.remote_mode == "paired" else []

    def revoke(self, session_id: str) -> bool:
        return bool(self._store().revoke_presence_session(session_id))


class HubHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.allow_reuse_address = False
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def __init__(self, address: tuple[str, int], service: HubService, auth: HubAuth,
                 static_dir: Path = STATIC_DIR) -> None:
        if address[0] not in LOOPBACK:
            raise ValueError("The Hub binds to loopback; use an HTTPS private-network proxy for remote access.")
        super().__init__(address, HubHandler)
        self.service, self.auth, self.static_dir = service, auth, Path(static_dir)


class HubHandler(BaseHTTPRequestHandler):
    server: HubHTTPServer
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------- plumbing
    def log_message(self, format: str, *args: Any) -> None:
        return

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").casefold()
        return host in LOOPBACK or host in self.server.auth.trusted_hosts

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        parsed = urlparse(origin)
        return (parsed.hostname or "").casefold() == (self.headers.get("Host") or "").rsplit(":", 1)[0].casefold() \
            and parsed.scheme in {"http", "https"}

    def _send(self, status: int, body: bytes, content_type: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         # Remote https images appear in chat as thumbnails the agent linked; the page
                         # sends no referrer. Scripts, styles and connections stay same-origin.
                         "default-src 'self'; img-src 'self' blob: https:; media-src 'self' blob:; "
                         "style-src 'self'; script-src 'self'; "
                         "connect-src 'self'; frame-src http://127.0.0.1:* http://localhost:* http://[::1]:*; "
                         "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: Any) -> None:
        self._send(status, json.dumps(payload, default=_json_default, separators=(",", ":")).encode(),
                   "application/json; charset=utf-8")

    def _body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise HubError("Invalid request length.") from exc
        if length < 1 or length > MAX_BODY:
            raise HubError("Request body is missing or too large.")
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            raise HubError("Requests must be JSON.")
        value = json.loads(self.rfile.read(length))
        if not isinstance(value, dict):
            raise HubError("Request body must be a JSON object.")
        return value

    def _actor(self) -> str | None:
        return self.server.auth.actor(self.headers.get("Authorization") or "")

    # ----------------------------------------------------------------- GET
    def do_GET(self) -> None:
        if not self._host_ok():
            self._json(HTTPStatus.MISDIRECTED_REQUEST, {"error": "Unrecognised host."})
            return
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        if path in STATIC_FILES:
            file = self.server.static_dir / STATIC_FILES[path]
            kind = {"html": "text/html", "css": "text/css", "js": "text/javascript"}[file.suffix[1:]]
            self._send(HTTPStatus.OK, file.read_bytes(), f"{kind}; charset=utf-8")
            return
        if not path.startswith("/api/"):
            self._json(HTTPStatus.NOT_FOUND, {"error": "Not found."})
            return
        actor = self._actor()
        if actor is None:
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "Sign in to the Hub."})
            return
        service = self.server.service
        parts = [p for p in path.split("/") if p]
        q = {k: v[0] for k, v in query.items()}
        try:
            if parts == ["api", "session"]:
                result = {"actor": actor, "remote_mode": self.server.auth.remote_mode}
            elif parts == ["api", "overview"]:
                result = service.overview()
            elif parts == ["api", "providers"]:
                result = {"providers": service.runtime.providers(), "defaults": service.runtime.defaults()}
            elif parts == ["api", "projects"]:
                result = service.projects()
            elif parts == ["api", "audit"]:
                result = service.audit_log()
            elif parts == ["api", "sessions"]:
                result = self.server.auth.sessions()
            elif parts == ["api", "events"]:
                kinds = [k for k in (q.get("kinds") or "").split(",") if k]
                result = {"events": service.runtime.events(after=int(q.get("after") or 0), agent_id=q.get("agent"),
                                                           task_id=q.get("task"), project_id=q.get("project"),
                                                           kinds=kinds or None, limit=int(q.get("limit") or 200)),
                          "last_seq": service.runtime.last_seq()}
            elif len(parts) == 3 and parts[1] == "agents":
                result = service.agent_detail(parts[2])
            elif len(parts) == 4 and parts[1] == "agents" and parts[3] == "chat":
                result = service.chat(parts[2], q.get('chat', ''))
            elif len(parts) == 4 and parts[1] == "agents" and parts[3] == "usage":
                result = service.runtime.chat_usage(parts[2], q.get("chat") or None)
            elif len(parts) == 4 and parts[1] == "agents" and parts[3] == "search":
                result = service.search(parts[2], q.get("q", ""))
            elif len(parts) == 4 and parts[1] == "agents" and parts[3] == "artifacts":
                result = service.artifacts(parts[2])
            elif len(parts) == 5 and parts[1] == "agents" and parts[3:] == ["files", "content"]:
                self._project_file(parts[2], q.get("path", ""), download=q.get("download") == "1")
                return
            elif len(parts) == 3 and parts[1] == "tasks":
                result = service.task_detail(parts[2])
            elif len(parts) == 3 and parts[1] == "artifacts":
                result = service.runtime.artifact(parts[2])
            elif len(parts) == 4 and parts[1] == "artifacts" and parts[3] == "diff":
                artifact = service.runtime.artifact(parts[2])
                result = {"diff": artifact.get("diff") or ""}
            elif len(parts) == 4 and parts[1] == "artifacts" and parts[3] == "content":
                self._artifact_content(parts[2], download=q.get("download") == "1")
                return
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "Not found."})
                return
            self._json(HTTPStatus.OK, result)
        except (HubError, TaskError, MultiAgentRuntimeError, PermissionError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except (ValueError, sqlite3.Error):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "The request could not be processed."})

    def _artifact_content(self, artifact_id: str, *, download: bool) -> None:
        data, row = self.server.service.runtime.artifact_bytes(artifact_id)
        if data is None:
            self._json(HTTPStatus.NOT_FOUND, {"error": "No stored content for this version."})
            return
        mime = row.get("mime") or "application/octet-stream"
        name = Path(row["path"]).name.replace('"', "").replace("\r", "").replace("\n", "") or "artifact"
        headers = {"Content-Security-Policy": "default-src 'none'; img-src 'self'; style-src 'none'; sandbox"}
        if (mime in PREVIEWABLE_IMAGES or mime in PREVIEWABLE_MEDIA) and not download:
            self._send(HTTPStatus.OK, data, mime, headers)
            return
        if not download and mime.startswith(TEXT_LIKE):
            # Agent-written HTML, SVG or script is never rendered by the Hub: it is served as text.
            self._send(HTTPStatus.OK, data, "text/plain; charset=utf-8", headers)
            return
        headers["Content-Disposition"] = f'attachment; filename="{name}"'
        self._send(HTTPStatus.OK, data, "application/octet-stream", headers)

    def _project_file(self, agent_id: str, relative: str, *, download: bool) -> None:
        file, mime = self.server.service.project_file(agent_id, relative)
        size = file.stat().st_size
        if size > MAX_FILE_SERVE:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "This file is too large to open from the Hub."})
            return
        data = file.read_bytes()
        name = file.name.replace('"', "").replace("\r", "").replace("\n", "") or "file"
        headers = {"Content-Security-Policy": "default-src 'none'; img-src 'self'; media-src 'self'; style-src 'none'; sandbox"}
        if (mime in PREVIEWABLE_IMAGES or mime in PREVIEWABLE_MEDIA) and not download:
            self._send(HTTPStatus.OK, data, mime, headers)
            return
        if not download and (mime.startswith(TEXT_LIKE) or mime == "application/octet-stream" and size <= 2_000_000
                             and b"\x00" not in data[:4096]):
            # Agent-written HTML, SVG or script is never rendered by the Hub: it is served as text.
            self._send(HTTPStatus.OK, data, "text/plain; charset=utf-8", headers)
            return
        headers["Content-Disposition"] = f'attachment; filename="{name}"'
        self._send(HTTPStatus.OK, data, "application/octet-stream", headers)

    # ---------------------------------------------------------------- POST
    def do_POST(self) -> None:
        if not self._host_ok() or not self._origin_ok():
            self._json(HTTPStatus.FORBIDDEN, {"error": "Request origin not allowed."})
            return
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        service, runtime = self.server.service, self.server.service.runtime
        if parts == ["api", "pair"]:
            try:
                body = self._body()
                session = self.server.auth.pair(str(body.get("code") or ""), self.client_address[0])
            except (HubError, ValueError) as exc:
                self._json(HTTPStatus.TOO_MANY_REQUESTS, {"error": str(exc)})
                return
            if session is None:
                self._json(HTTPStatus.UNAUTHORIZED, {"error": "That code is invalid or expired."})
                return
            service.audit("pairing", "pair-device", session.get("session_id"), "ok", str(session.get("label") or ""))
            self._json(HTTPStatus.OK, {"token": session.get("token"), "expires_at": session.get("expires_at")})
            return
        actor = self._actor()
        if actor is None:
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "Sign in to the Hub."})
            return
        action = "/".join(parts[1:3]) if len(parts) >= 3 else "/".join(parts[1:])
        target = parts[2] if len(parts) >= 3 else None
        try:
            body = self._body()
            if parts == ["api", "agents"]:
                result = service.create_agent(body)
                target = result["agent_id"]
            elif parts == ["api", "projects"]:
                result = service.create_project(str(body.get("name") or ""))
            elif parts == ["api", "providers", "refresh"]:
                result = {"providers": runtime.refresh_providers()}
            elif parts == ["api", "providers", "verify"]:
                result = runtime.verify_model(str(body.get("provider")), str(body.get("model")))
            elif parts == ["api", "settings", "default-model"]:
                result = runtime.set_default_model(str(body.get("provider")), str(body.get("model")))
            elif parts == ["api", "agents", "full-access"]:
                if body:
                    raise HubError("Full access takes no fields.")
                action = "agents/full-access"
                result = service.grant_full_access()
            elif parts == ["api", "sessions", "revoke"]:
                result = {"revoked": self.server.auth.revoke(str(body.get("session_id") or ""))}
            elif len(parts) == 6 and parts[1] == "agents" and parts[3] == "chats":
                agent_id, chat_id, verb = parts[2], parts[4], parts[5]
                action, target = f"chat/{verb}", chat_id
                if verb == "archive":
                    if set(body) != {"archived"} or not isinstance(body["archived"], bool):
                        raise HubError("Archive takes archived: true or false.")
                    result = service.archive_chat(agent_id, chat_id, body["archived"])
                elif verb == "delete":
                    if body:
                        raise HubError("Delete takes no fields.")
                    result = service.delete_chat(agent_id, chat_id)
                elif verb == "compact":
                    if body:
                        raise HubError("Compact takes no fields.")
                    service.command_center.conversations.chat_scope(service._agent_project(agent_id), agent_id, chat_id)
                    result = runtime.compact_chat(agent_id, chat_id)
                else:
                    raise HubError("Unknown conversation command.")
            elif len(parts) == 4 and parts[1] == "agents" and parts[3] == "goals":
                if set(body) - {"title", "category", "detail"}:
                    raise HubError("A goal takes a title, category and detail.")
                action, target = "goal/create", parts[2]
                result = runtime.create_goal(parts[2], title=str(body.get("title") or ""),
                                             category=str(body.get("category") or "other"),
                                             detail=str(body.get("detail") or ""))
            elif len(parts) == 4 and parts[1] == "goals":
                goal_id, verb = parts[2], parts[3]
                action = f"goal/{verb}"
                if verb == "update":
                    result = runtime.update_goal(goal_id, **body)
                elif verb == "delete":
                    if body:
                        raise HubError("Delete takes no fields.")
                    result = runtime.delete_goal(goal_id)
                else:
                    raise HubError("Unknown goal command.")
            elif len(parts) == 4 and parts[1] == "schedules":
                schedule_id, verb = parts[2], parts[3]
                action = f"schedule/{verb}"
                if body:
                    raise HubError("Schedule commands take no fields.")
                if verb in {"pause", "resume"}:
                    result = runtime.set_schedule_enabled(schedule_id, verb == "resume")
                elif verb == "delete":
                    result = runtime.delete_schedule(schedule_id)
                else:
                    raise HubError("Unknown schedule command.")
            elif len(parts) == 4 and parts[1] == "agents":
                agent_id, verb = parts[2], parts[3]
                action = f"agent/{verb}"
                if verb == "config":
                    result = service.configure_agent(agent_id, body)
                elif verb == "lifecycle":
                    result = service.lifecycle(agent_id, str(body.get("action") or ""))
                elif verb == "archive":
                    result = service.archive(agent_id, bool(body.get("archived")))
                elif verb == "tasks":
                    if set(body) - {"title", "request", "model"}:
                        raise HubError("Unsupported task fields.")
                    result = runtime.create_task(agent_id, title=str(body.get("title") or ""),
                                                 request=str(body.get("request") or ""),
                                                 model_override=body.get("model") or None)
                elif verb == "chats":
                    result = service.create_chat(agent_id, body)
                elif verb == "effort":
                    if set(body) != {"effort"}:
                        raise HubError("Effort takes one field: effort.")
                    result = runtime.set_agent_effort(agent_id, str(body["effort"]))
                elif verb == "messages":
                    chat_id = body.pop('chat_id', '')
                    result = service.chat(agent_id, chat_id, body)
                else:
                    raise HubError("Unknown agent command.")
            elif len(parts) == 4 and parts[1] == "previews":
                preview_id, verb = parts[2], parts[3]
                action = f"preview/{verb}"
                if body:
                    raise HubError("Preview commands take no fields.")
                if verb == "stop":
                    result = runtime.stop_preview(preview_id)
                elif verb == "start":
                    result = runtime.start_preview(preview_id)
                else:
                    raise HubError("Unknown preview command.")
            elif len(parts) == 4 and parts[1] == "tasks":
                task_id, verb = parts[2], parts[3]
                action = f"task/{verb}"
                if verb == "archive":
                    if set(body) != {"archived"} or not isinstance(body["archived"], bool):
                        raise HubError("Archive takes archived: true or false.")
                    result = runtime.set_task_archived(task_id, body["archived"])
                elif verb == "delete":
                    if body:
                        raise HubError("Delete takes no fields.")
                    result = runtime.delete_task(task_id)
                elif verb in {"cancel", "pause", "resume", "retry"}:
                    result = getattr(runtime, verb)(task_id)
                elif verb == "steer":
                    result = runtime.steer(task_id, str(body.get("body") or ""))
                elif verb == "approval":
                    decision = body.get("decision")
                    if decision not in {"approve", "deny"}:
                        raise HubError("Choose approve or deny.")
                    result = runtime.decide_approval(task_id, decision == "approve")
                else:
                    raise HubError("Unknown task command.")
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "Not found."})
                return
            service.audit(actor, action, target, "ok")
            self._json(HTTPStatus.OK, result)
        except (HubError, TaskError, MultiAgentRuntimeError, PermissionError) as exc:
            service.audit(actor, action, target, "refused", str(exc))
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except (ValueError, sqlite3.Error) as exc:
            service.audit(actor, action, target, "error", type(exc).__name__)
            self._json(HTTPStatus.BAD_REQUEST, {"error": "The request could not be processed."})


class _StateLock:
    """One backend per state directory, so two dispatchers never run the same queue."""

    def __init__(self, state_dir: Path) -> None:
        self.path = Path(state_dir) / ".hub.lock"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = open(self.path, "a+b")  # noqa: SIM115 - held for the process lifetime
        try:
            if os.name == "nt":
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._handle.close()
            raise SystemExit(f"Another Hub backend is already using {state_dir}.") from exc


def default_state_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "agent-hub"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the JARVIS Central Agent Hub")
    sub = parser.add_subparsers(dest="command")
    parser.add_argument("--state-dir", type=Path, default=default_state_dir())
    parser.add_argument("--provider-profile", type=Path,
                        default=Path(__file__).resolve().parent.parent / "data",
                        help="JARVIS data directory holding the signed-in provider profile")
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--capacity", type=int, default=2)
    parser.add_argument("--remote-access", choices=("disabled", "paired"), default="disabled")
    parser.add_argument("--trusted-host", action="append", default=[],
                        help="Exact HTTPS proxy hostname accepted for remote access (repeatable)")
    parser.add_argument("--static-dir", type=Path, default=STATIC_DIR)
    parser.add_argument("--workspace", action="append", default=[], metavar="PROJECT_ID=PATH")
    pairing = sub.add_parser("pairing", help="Create a one-time pairing code for a remote device")
    pairing.add_argument("--label", default="device")
    args = parser.parse_args(argv)
    os.environ.setdefault("JARVIS_WORKSPACE", str(Path(args.state_dir) / "projects"))
    os.environ.setdefault("JARVIS_DATA", str(Path(args.state_dir) / "backend-data"))
    if args.command == "pairing":
        auth = HubAuth(args.state_dir, remote_mode="paired")
        code = auth.create_code(args.label)
        print(f"Pairing code: {code.get('code')}  (one use, expires {code.get('expires_at')})")
        return 0
    lock = _StateLock(args.state_dir)  # noqa: F841 - held until exit
    roots = {key: Path(path) for key, path in (entry.split("=", 1) for entry in args.workspace)}
    service = HubService(state_dir=args.state_dir, provider_profile_dir=args.provider_profile,
                         capacity=args.capacity, workspace_roots=roots)
    service.runtime.refresh_providers()
    auth = HubAuth(args.state_dir, remote_mode=args.remote_access, trusted_hosts=tuple(args.trusted_host))
    server = HubHTTPServer(("127.0.0.1", args.port), service, auth, static_dir=args.static_dir)
    service.runtime.reserved_ports.add(server.server_port)
    print(f"JARVIS Agent Hub: http://127.0.0.1:{server.server_port}/#token={auth.operator_token}", flush=True)
    print(f"State: {args.state_dir}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
