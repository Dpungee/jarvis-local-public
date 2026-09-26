"""Loopback-only operator command center for persistent independent agents."""

from __future__ import annotations

import argparse
import json
import secrets
import socket
import sqlite3
import threading
import time
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import asdict, dataclass
from enum import Enum
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse, parse_qs

from .integration_harness import default_registry
from .conversation_workspace import ConversationWorkspace
from .live_conversation import LiveConversations
from .subscription_chat import subscription_providers
from .multi_agent_runtime import (
    AgentLifecycle,
    MultiAgentRuntimeError,
    MultiAgentRuntimeStore,
    TaskStatus,
)

MAX_BODY_BYTES = 64 * 1024
SUPPORTED_PROVIDERS = {
    "codex-cli": {
        "label": "Codex CLI subscription",
        "enabled": False,
        "reason": "Live provider execution is gated for this preview.",
    },
    "claude-cli": {
        "label": "Claude CLI subscription",
        "enabled": False,
        "reason": "Live provider execution is gated for this preview.",
    },
}


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "__dataclass_fields__"):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


UNUSABLE_CONNECTIONS = frozenset({"UNAVAILABLE", "BLOCKED"})


def agent_status(
    *,
    lifecycle: str,
    provider_name: str,
    provider: dict[str, Any] | None,
    registered: bool,
    live: dict[str, Any] | None,
    work: dict[str, int],
    running_total: int,
    capacity: int,
) -> dict[str, Any]:
    """One truthful status per agent, derived only from persisted state.

    ``live`` is real subscription work; ``work`` is offline-simulator work. The two are
    never merged into one label, so simulated progress can never read as a live agent.
    """
    simulated = provider_name == "offline-demo" or bool(provider and provider.get("simulated"))
    real = registered and not simulated
    live = live or {"counts": {}, "running": None, "latest": None}
    counts = live["counts"]
    connection = (provider or {}).get("connection")
    base = {"real_provider": real, "simulated": simulated, "provider": provider_name,
            "connection": connection, "needs_action": False}

    def result(code: str, label: str, detail: str, needs_action: bool = False,
               action: str | None = None) -> dict[str, Any]:
        # ``action`` is the one lifecycle control that resolves this state, if any.
        return base | {"code": code, "label": label, "detail": detail,
                       "needs_action": needs_action, "action": action}

    if lifecycle == "STOPPED":
        return result("stopped", "Stopped", "Enable this agent to accept new work.", True, "start")
    if lifecycle == "CREATED":
        return result("not_enabled", "Not enabled",
                      "Enable this agent before sending work. Enabling does not call a model.", True, "start")
    if not registered:
        why = ("The offline demo adapter is not loaded in this session (start with --demo)."
               if provider_name == "offline-demo" else "This provider is not available in this session.")
        return result("unavailable", "Unavailable", why, True)
    if connection in UNUSABLE_CONNECTIONS:
        return result("unavailable", "Unavailable", str((provider or {}).get("reason") or "Provider unavailable."), True)
    if lifecycle == "PAUSED":
        held = counts.get("PAUSED", 0) + counts.get("INTERRUPTED", 0) + work.get("PAUSED", 0)
        return result("paused", "Paused",
                      f"{held} item(s) held; resume to continue." if held else "Resume to accept work.", True, "resume")
    running = live["running"]
    if running:
        phase = {"starting": "Starting", "connecting": "Connecting to provider",
                 "responding": "Receiving response"}.get(running["phase"] or "", "Running")
        return result("working", "Working", f"{phase} · {running['provider']} · {running['model']}")
    if work.get("RUNNING"):
        return result("working", "Simulating", "Offline simulation in progress. No model is called.")
    if work.get("AWAITING_REPLY"):
        return result("waiting", "Waiting for you", "Simulated work is waiting for your clarification reply.", True)
    interrupted = counts.get("INTERRUPTED", 0) + work.get("INTERRUPTED", 0)
    if interrupted:
        return result("waiting", "Waiting for you",
                      f"{interrupted} item(s) interrupted by a restart; resume to run them again.", True, "resume")
    if counts.get("QUEUED"):
        detail = (f"Waiting for a free slot ({running_total} of {capacity} in use)."
                  if running_total >= capacity else "Starting shortly.")
        return result("queued", "Queued", detail)
    if work.get("QUEUED"):
        return result("queued", "Queued", "Queued in the offline simulator.")
    latest = live["latest"]
    if latest and latest["state"] == "FAILED":
        return result("failed", "Last turn failed", latest.get("error") or "The provider did not return a response.", True)
    if simulated:
        return result("idle", "Idle · demo", "Offline simulation only. Nothing this agent produces is live.")
    note = {"CONNECTED": "Provider verified", "UNTESTED": "Provider not yet contacted; the first send connects",
            "ERROR": "The provider's last turn failed; the next send retries"}.get(connection or "", "Ready")
    return result("idle", "Idle · ready", note)


class ProviderAdapter(Protocol):
    label: str
    simulated: bool

    def run(
        self,
        *,
        agent_id: str,
        model: str,
        purpose: str,
        task_title: str,
        prompt: str,
        cancel_event: threading.Event,
    ) -> str: ...


class OfflineDemoProvider:
    label = "Offline demo adapter"
    simulated = True

    def run(self, **kwargs: Any) -> str:
        if kwargs["cancel_event"].is_set():
            raise RuntimeError("execution cancelled")
        return (
            "SIMULATED RESULT — no model or tool was called. "
            f"Agent {kwargs['agent_id']} accepted task “{kwargs['task_title']}”."
        )


@dataclass(frozen=True)
class ExecutionRecord:
    run_id: str
    agent_id: str
    task_id: str
    state: str
    provider: str
    simulated: bool
    created_at: float
    started_at: float | None
    finished_at: float | None
    detail: str


class CommandCenterService:
    """Persistent operator controls plus bounded per-agent execution lanes."""

    def __init__(
        self,
        runtime_path: Path,
        execution_path: Path,
        *,
        providers: dict[str, ProviderAdapter] | None = None,
        max_workers: int = 2,
        workspace_roots: dict[str, Path] | None = None,
        simulator_interval: float = 1.0,
    ) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be positive")
        self.runtime_path = Path(runtime_path)
        self.execution_path = Path(execution_path)
        self.providers = dict(providers or {})
        self.integrations = default_registry()
        self.max_workers = max_workers
        self._pool = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="jarvis-agent"
        )
        self._lock = threading.RLock()
        self._agent_locks: dict[str, threading.Lock] = {}
        self._cancel: dict[str, threading.Event] = {}
        self._closed = False
        self._initialize_execution_store()
        self.conversations = ConversationWorkspace(self.execution_path, roots=workspace_roots)
        self.live = LiveConversations(self.conversations, {k:v for k,v in self.providers.items() if hasattr(v, 'chat')}, max_workers)
        for agent in self.list_agents():
            self.conversations.ensure_project(agent['project_id'] or 'command-center')
        self.conversations.ensure_project('command-center', 'Command Center')
        self._conversation_stop = threading.Event()
        self._conversation_error = False
        self._conversation_thread = threading.Thread(
            target=self._conversation_loop, args=(simulator_interval,), daemon=True,
            name='jarvis-offline-conversations')
        self._conversation_thread.start()

    def _conversation_loop(self, interval):
        while not self._conversation_stop.wait(interval):
            try:
                with self._lock:
                    if 'offline-demo' in self.providers:
                        self.conversations.tick(self.list_agents(), self.max_workers)
                    self.live.tick(self.list_agents())
                self._conversation_error = False
            except Exception:
                # Never disclose raw database/path or provider exceptions to the UI.
                self._conversation_error = True

    def _thread_agent(self, agent_id, project_id):
        with MultiAgentRuntimeStore(self.runtime_path) as store:
            agent = store.get_agent(agent_id)
        if (agent.project_id or 'command-center') != project_id:
            raise PermissionError('Agent does not belong to this project.')
        return agent

    def conversation(self, agent_id, project_id, payload=None, chat_id=None):
        with self._lock:
            agent = self._thread_agent(agent_id, project_id)
            if payload is not None:
                payload = dict(payload)
                chat_id = payload.pop('chat_id',chat_id)
            scope = self.conversations.chat_scope(project_id,agent_id,chat_id)
            if payload is None:
                return self.conversations.snapshot(scope, agent_id) | self.live.snapshot(scope, agent_id) | {
                    'grants':self.conversations.grants(project_id,agent_id),
                    'chats':self.conversations.chats(project_id,agent_id)}
            if payload.get('attachments') and not self.conversations.grants(project_id,agent_id)['attachments']:
                raise PermissionError('Attachment permission has been revoked; remove the attachment before sending.')
            if agent.model_provider in self.live.providers and payload.get('kind', 'discuss') != 'delegate':
                adapter = self.live.providers[agent.model_provider]
                if getattr(adapter, 'status', None) in UNUSABLE_CONNECTIONS:
                    # Refuse up front rather than queue work that is certain to fail.
                    raise ValueError(f'{adapter.label} is unavailable: {adapter.reason} Nothing was sent or queued.')
                return self.live.send(scope, agent, payload)
            if agent.model_provider == 'offline-demo':
                simulated = isinstance(self.providers.get('offline-demo'), OfflineDemoProvider)
            else:
                simulated = False
            return self.conversations.send(scope, agent_id, payload,
                simulated=simulated, enabled=agent.lifecycle is AgentLifecycle.RUNNING,
                model=agent.model_name)

    def conversation_approval(self, agent_id, project_id, payload):
        with self._lock:
            self._thread_agent(agent_id, project_id)
            payload = dict(payload)
            scope = self.conversations.chat_scope(project_id,agent_id,payload.pop('chat_id',None))
            return self.conversations.approve(scope, agent_id, payload)

    def composer_control(self, agent_id, project_id, action, payload):
        with self._lock:
            self._thread_agent(agent_id,project_id)
            payload = dict(payload)
            chat_id = payload.pop('chat_id',None)
            if action == 'chats':
                if set(payload) != {'title'}:
                    raise ValueError('A conversation title is required.')
                return self.conversations.create_chat(project_id,agent_id,payload['title'])
            if action == 'grants':
                return self.conversations.set_grant(project_id,agent_id,payload)
            if action == 'attachments':
                return self.conversations.attachment(project_id,agent_id,chat_id,payload)
            if action == 'remove-attachment' and set(payload) == {'attachment_id'}:
                return self.conversations.remove_attachment(project_id,agent_id,chat_id,payload['attachment_id'])
            raise ValueError('Unsupported composer action.')

    def project_files(self, agent_id, project_id, relative):
        with self._lock:
            self._thread_agent(agent_id,project_id)
            if not self.conversations.grants(project_id,agent_id)['file_preview']:
                raise PermissionError('File preview permission is not enabled.')
            return self.conversations.files(project_id,relative)

    def _initialize_execution_store(self) -> None:
        self.execution_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.execution_path)) as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS command_center_runs(
                    run_id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    task_id TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL CHECK(state IN
                        ('QUEUED','RUNNING','COMPLETED','FAILED','CANCELLED')),
                    provider TEXT NOT NULL,
                    simulated INTEGER NOT NULL CHECK(simulated IN (0,1)),
                    created_at REAL NOT NULL,
                    started_at REAL,
                    finished_at REAL,
                    detail TEXT NOT NULL
                )"""
            )
            interrupted = [
                (str(row[0]), str(row[1]), str(row[2])) for row in db.execute(
                    "SELECT run_id, agent_id, task_id FROM command_center_runs"
                    " WHERE state IN ('QUEUED','RUNNING')"
                )
            ]
            db.execute(
                """UPDATE command_center_runs
                   SET state='FAILED', finished_at=?,
                       detail='Interrupted by command-center restart.'
                   WHERE state='RUNNING'""",
                (time.time(),),
            )
            # The executor pool that would have run these is gone; left QUEUED they would
            # display as waiting forever while nothing could ever pick them up.
            db.execute(
                """UPDATE command_center_runs
                   SET state='FAILED', finished_at=?,
                       detail='Not started: interrupted by command-center restart. Assign it again.'
                   WHERE state='QUEUED'""",
                (time.time(),),
            )
            db.commit()
        if not interrupted or not self.runtime_path.exists():
            return
        with MultiAgentRuntimeStore(self.runtime_path) as store:
            for run_id, agent_id, task_id in interrupted:
                try:
                    task = store.get_task(task_id)
                    if task.status in {TaskStatus.OPEN, TaskStatus.ASSIGNED, TaskStatus.RUNNING}:
                        store.update_task_status(
                            task_id=task_id, actor_id=agent_id, status=TaskStatus.FAILED,
                            result="Interrupted by command-center restart.",
                            idempotency_key=f"restart-{run_id}",
                        )
                except MultiAgentRuntimeError:
                    # The runtime refused this transition; the run record above stays truthful.
                    pass

    def close(self) -> None:
        self._conversation_stop.set()
        self._conversation_thread.join(timeout=12)
        with self._lock:
            self._closed = True
            for event in self._cancel.values():
                event.set()
        self._pool.shutdown(wait=True, cancel_futures=True)
        self.live.close()

    def provider_status(self) -> dict[str, dict[str, Any]]:
        status = {name: dict(value) for name, value in SUPPORTED_PROVIDERS.items()}
        for name, provider in self.providers.items():
            status[name] = {
                "label": provider.label,
                "enabled": True,
                "simulated": bool(provider.simulated),
                "reason": (
                    "Offline simulation; no model or tool is called."
                    if provider.simulated
                    else "Injected provider adapter enabled."
                ),
            }
            if hasattr(provider, 'chat'):
                status[name].update(connection=provider.status, reason=provider.reason,
                                    enabled=provider.status not in {'BLOCKED', 'UNAVAILABLE'},
                                    text_only=True)
        return status

    def list_agents(self) -> list[dict[str, Any]]:
        with MultiAgentRuntimeStore(self.runtime_path) as store:
            rows = store.db.execute(
                "SELECT * FROM runtime_agents ORDER BY created_at, agent_id"
            ).fetchall()
            agents = [store._agent_from_row(row) for row in rows]
            result = []
            for agent in agents:
                item = _jsonable(agent)
                item["tasks"] = _jsonable(store.list_agent_tasks(agent.agent_id))
                item["tools"] = []
                item["permission_status"] = "No tool grants active"
                result.append(item)
            return result

    def create_agent(self, payload: dict[str, Any]) -> dict[str, Any]:
        provider = str(payload.get("provider", "")).strip()
        if provider not in self.provider_status():
            raise ValueError("unsupported provider")
        with MultiAgentRuntimeStore(self.runtime_path) as store:
            agent = store.create_agent(
                display_name=str(payload.get("name", "")),
                role=str(payload.get("role", "")),
                purpose=str(payload.get("purpose", "")),
                specialties=tuple(payload.get("specialties") or ()),
                model_provider=provider,
                model_name=str(payload.get("model", "default")),
                project_id=str(payload.get("project_id") or "command-center"),
                idempotency_key=str(payload.get("request_id") or uuid.uuid4()),
            )
            self.conversations.ensure_project(agent.project_id or 'command-center')
        # Enabling only makes the agent eligible for work; it never calls a model.
        if payload.get("enable") in {True, "on", "true"}:
            return self.set_lifecycle(agent.agent_id, "start")
        return _jsonable(agent)

    def set_lifecycle(self, agent_id: str, action: str) -> dict[str, Any]:
        with self._lock:
            return self._set_lifecycle(agent_id, action)

    def _set_lifecycle(self, agent_id: str, action: str) -> dict[str, Any]:
        target = {
            "start": AgentLifecycle.RUNNING,
            "resume": AgentLifecycle.RUNNING,
            "pause": AgentLifecycle.PAUSED,
            "stop": AgentLifecycle.STOPPED,
        }.get(action)
        if target is None:
            raise ValueError("unsupported lifecycle action")
        if action == "stop":
            with self._lock:
                for run_id, event in self._cancel.items():
                    if self._run_agent(run_id) == agent_id:
                        event.set()
        with MultiAgentRuntimeStore(self.runtime_path) as store:
            current = store.get_agent(agent_id)
            if current.lifecycle is target:
                self.live.control(agent_id, action)
                self.conversations.control(agent_id, action)
                return _jsonable(current)
            agent = store.set_agent_lifecycle(
                agent_id,
                target,
                actor_id="owner",
                idempotency_key=f"ui-{action}-{uuid.uuid4()}",
            )
            self.live.control(agent_id, action)
            self.conversations.control(agent_id, action)
            return _jsonable(agent)

    def update_model(self, agent_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            return self._update_model(agent_id, payload)

    def _update_model(self, agent_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        provider = str(payload.get("provider", "")).strip()
        if provider not in self.provider_status():
            raise ValueError("unsupported provider")
        with MultiAgentRuntimeStore(self.runtime_path) as store:
            agent = store.update_agent_model(
                agent_id,
                actor_id="owner",
                model_provider=provider,
                model_name=str(payload.get("model", "default")),
                idempotency_key=f"ui-model-{uuid.uuid4()}",
            )
            self.live.change_binding(agent_id, agent.model_provider, agent.model_name)
            return _jsonable(agent)

    def assign_task(self, agent_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with MultiAgentRuntimeStore(self.runtime_path) as store:
            agent = store.get_agent(agent_id)
            if agent.lifecycle is not AgentLifecycle.RUNNING:
                raise ValueError("agent must be running before work can be queued")
            provider = self.providers.get(agent.model_provider)
            if provider is None or hasattr(provider, 'chat'):
                raise ValueError("provider execution is disabled; no task was created")
            task = store.create_task(
                creator_id=agent_id,
                owner_id=agent_id,
                title=str(payload.get("title", "")),
                description=str(payload.get("prompt", "")),
                project_id=agent.project_id,
                idempotency_key=str(payload.get("request_id") or uuid.uuid4()),
            )
        run_id = f"run_{uuid.uuid4().hex}"
        now = time.time()
        with closing(sqlite3.connect(self.execution_path)) as db:
            db.execute(
                """INSERT INTO command_center_runs
                   VALUES (?, ?, ?, 'QUEUED', ?, ?, ?, NULL, NULL, ?)""",
                (
                    run_id,
                    agent_id,
                    task.task_id,
                    agent.model_provider,
                    int(provider.simulated),
                    now,
                    "Waiting for an execution slot.",
                ),
            )
            db.commit()
        event = threading.Event()
        with self._lock:
            self._cancel[run_id] = event
            lane = self._agent_locks.setdefault(agent_id, threading.Lock())
        self._pool.submit(self._execute, run_id, task.task_id, agent_id, provider, lane, event)
        return _jsonable(task) | {"run_id": run_id, "execution_state": "QUEUED"}

    def _execute(
        self,
        run_id: str,
        task_id: str,
        agent_id: str,
        provider: ProviderAdapter,
        lane: threading.Lock,
        cancel_event: threading.Event,
    ) -> None:
        with lane:
            if cancel_event.is_set():
                self._finish_run(run_id, "CANCELLED", "Cancelled before execution.")
                return
            try:
                with MultiAgentRuntimeStore(self.runtime_path) as store:
                    agent = store.get_agent(agent_id)
                    task = store.get_task(task_id)
                    if agent.lifecycle is not AgentLifecycle.RUNNING:
                        self._finish_run(run_id, "CANCELLED", "Agent is not running.")
                        return
                    store.update_task_status(
                        task_id=task_id,
                        actor_id=agent_id,
                        status=TaskStatus.RUNNING,
                        idempotency_key=f"exec-start-{run_id}",
                    )
                self._set_run(run_id, "RUNNING", "Provider adapter is executing.")
                result = provider.run(
                    agent_id=agent_id,
                    model=agent.model_name,
                    purpose=agent.purpose,
                    task_title=task.title,
                    prompt=task.description,
                    cancel_event=cancel_event,
                )
                if cancel_event.is_set():
                    raise RuntimeError("execution cancelled")
                with MultiAgentRuntimeStore(self.runtime_path) as store:
                    store.update_task_status(
                        task_id=task_id,
                        actor_id=agent_id,
                        status=TaskStatus.COMPLETED,
                        result=str(result),
                        idempotency_key=f"exec-complete-{run_id}",
                    )
                self._finish_run(run_id, "COMPLETED", "Result stored on task.")
            except Exception as exc:
                detail = "Execution cancelled." if cancel_event.is_set() else type(exc).__name__
                try:
                    with MultiAgentRuntimeStore(self.runtime_path) as store:
                        task = store.get_task(task_id)
                        if task.status is TaskStatus.RUNNING:
                            store.update_task_status(
                                task_id=task_id,
                                actor_id=agent_id,
                                status=TaskStatus.FAILED,
                                result=detail,
                                idempotency_key=f"exec-fail-{run_id}",
                            )
                except MultiAgentRuntimeError:
                    pass
                self._finish_run(
                    run_id, "CANCELLED" if cancel_event.is_set() else "FAILED", detail
                )
            finally:
                with self._lock:
                    self._cancel.pop(run_id, None)

    def _set_run(self, run_id: str, state: str, detail: str) -> None:
        with closing(sqlite3.connect(self.execution_path)) as db:
            db.execute(
                """UPDATE command_center_runs SET state=?, detail=?,
                   started_at=COALESCE(started_at, ?) WHERE run_id=?""",
                (state, detail, time.time(), run_id),
            )
            db.commit()

    def _finish_run(self, run_id: str, state: str, detail: str) -> None:
        with closing(sqlite3.connect(self.execution_path)) as db:
            db.execute(
                """UPDATE command_center_runs SET state=?, detail=?,
                   finished_at=? WHERE run_id=?""",
                (state, detail, time.time(), run_id),
            )
            db.commit()

    def _run_agent(self, run_id: str) -> str | None:
        with closing(sqlite3.connect(self.execution_path)) as db:
            row = db.execute(
                "SELECT agent_id FROM command_center_runs WHERE run_id=?", (run_id,)
            ).fetchone()
        return None if row is None else str(row[0])

    def runs(self) -> list[dict[str, Any]]:
        with closing(sqlite3.connect(self.execution_path)) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT * FROM command_center_runs ORDER BY created_at DESC LIMIT 200"
            ).fetchall()
        return [dict(row) | {"simulated": bool(row["simulated"])} for row in rows]

    def _simulated_work_counts(self) -> dict[str, dict[str, int]]:
        counts: dict[str, dict[str, int]] = {}
        with self.conversations.db() as db:
            for row in db.execute(
                "SELECT agent_id, state, COUNT(*) AS n FROM cc_work"
                " WHERE state IN ('QUEUED','RUNNING','AWAITING_REPLY','PAUSED','INTERRUPTED')"
                " GROUP BY agent_id, state"
            ):
                counts.setdefault(row["agent_id"], {})[row["state"]] = row["n"]
        return counts

    def agents_with_status(self, providers: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        providers = providers if providers is not None else self.provider_status()
        activity = self.live.agent_activity()
        work = self._simulated_work_counts()
        agents = self.list_agents()
        for item in agents:
            name = item["model_provider"]
            item["status"] = agent_status(
                lifecycle=item["lifecycle"],
                provider_name=name,
                provider=providers.get(name),
                registered=name in self.providers,
                live=activity["agents"].get(item["agent_id"]),
                work=work.get(item["agent_id"], {}),
                running_total=activity["running_total"],
                capacity=activity["capacity"],
            )
        return agents

    def state(self) -> dict[str, Any]:
        providers = self.provider_status()
        return {
            "agents": self.agents_with_status(providers),
            "server_time": time.time(),
            "runs": self.runs(),
            "providers": providers,
            "capacity": {
                "worker_slots": self.max_workers,
                "agent_count_limit": None,
                "note": "Excess tasks queue; provider and machine limits still apply.",
            },
            "bridge": {"enabled": False, "label": "Memory bridge disabled"},
            "tools": {"enabled": False, "label": "No execution tools granted"},
            "integrations": self.integrations.list_public(),
            "projects": self.conversations.projects(),
            "conversation_health": 'UNAVAILABLE' if self._conversation_error else 'AVAILABLE',
        }


class CommandCenterHTTPServer(ThreadingHTTPServer):
    def server_bind(self):
        # Windows SO_REUSEADDR permits multiple listeners on the same endpoint.
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            self.allow_reuse_address = False
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def __init__(self, address: tuple[str, int], service: CommandCenterService):
        if address[0] not in {"127.0.0.1", "localhost"}:
            raise ValueError("command center preview must bind to loopback")
        super().__init__(address, CommandCenterHandler)
        self.service = service
        self.token = secrets.token_urlsafe(32)


class CommandCenterHandler(BaseHTTPRequestHandler):
    server: CommandCenterHTTPServer

    def _headers(self, status: int, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'",
        )
        self.end_headers()

    def _authorized(self) -> bool:
        return secrets.compare_digest(
            self.headers.get("Authorization", ""), f"Bearer {self.server.token}"
        )

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        try:
            parsed = urlparse(origin)
            host, port = self.server.server_address
            return parsed.scheme == "http" and parsed.hostname in {host, "localhost"} and parsed.port == port
        except ValueError:
            return False

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(_jsonable(payload), separators=(",", ":")).encode()
        self._headers(status, "application/json; charset=utf-8")
        self.wfile.write(body)

    def _body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("invalid content length") from exc
        if length < 1 or length > MAX_BODY_BYTES:
            raise ValueError("request body size is invalid")
        value = json.loads(self.rfile.read(length))
        if not isinstance(value, dict):
            raise ValueError("JSON body must be an object")
        return value

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in {"/", "/command-center.css", "/command-center.js"}:
            name = {
                "/": "command_center.html",
                "/command-center.css": "command_center.css",
                "/command-center.js": "command_center.js",
            }[path]
            content_type = "text/html; charset=utf-8" if path == "/" else (
                "text/css; charset=utf-8" if path.endswith(".css") else "text/javascript; charset=utf-8"
            )
            data = Path(__file__).with_name(name).read_bytes()
            self._headers(HTTPStatus.OK, content_type)
            self.wfile.write(data)
            return
        if path.startswith('/api/') and (not self._authorized() or not self._origin_ok()):
            self._json(HTTPStatus.UNAUTHORIZED, {'error': 'unauthorized'})
            return
        if path == "/api/state" and self._authorized():
            self._json(HTTPStatus.OK, self.server.service.state())
            return
        try:
            params = parse_qs(urlparse(self.path).query)
            if path == '/api/conversation':
                result = self.server.service.conversation(params.get('agent', [''])[0], params.get('project', [''])[0], chat_id=params.get('chat',[None])[0])
            elif path == '/api/files':
                result = self.server.service.project_files(params.get('agent',[''])[0],params.get('project', [''])[0], params.get('path', [''])[0])
            else:
                self._json(HTTPStatus.NOT_FOUND, {'error': 'not found'})
                return
            self._json(HTTPStatus.OK, result)
            return
        except (ValueError, PermissionError, MultiAgentRuntimeError):
            self._json(HTTPStatus.BAD_REQUEST, {'error': 'Requested project, thread or file is unavailable or outside its allowed scope.'})
            return
        except (OSError, UnicodeError):
            self._json(HTTPStatus.BAD_REQUEST, {'error': 'File preview unavailable.'})
            return
        self._json(HTTPStatus.UNAUTHORIZED if path.startswith("/api/") else HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:
        if not self._authorized() or not self._origin_ok():
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return
        path = urlparse(self.path).path
        try:
            payload = self._body()
            parts = [part for part in path.split("/") if part]
            if parts == ["api", "agents"]:
                result = self.server.service.create_agent(payload)
            elif parts == ['api', 'projects']:
                result = self.server.service.conversations.create_project(payload)
            elif len(parts) == 6 and parts[:2] == ['api', 'projects'] and parts[3] == 'agents' and parts[5] in {'messages', 'approval'}:
                method = self.server.service.conversation if parts[5] == 'messages' else self.server.service.conversation_approval
                result = method(parts[4], parts[2], payload)
            elif len(parts) == 6 and parts[:2] == ['api','projects'] and parts[3] == 'agents' and parts[5] in {'chats','grants','attachments','remove-attachment'}:
                result = self.server.service.composer_control(parts[4],parts[2],parts[5],payload)
            elif len(parts) == 4 and parts[:2] == ["api", "agents"] and parts[3] == "lifecycle":
                result = self.server.service.set_lifecycle(parts[2], str(payload.get("action", "")))
            elif len(parts) == 4 and parts[:2] == ["api", "agents"] and parts[3] == "model":
                result = self.server.service.update_model(parts[2], payload)
            elif len(parts) == 4 and parts[:2] == ["api", "agents"] and parts[3] == "tasks":
                result = self.server.service.assign_task(parts[2], payload)
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            self._json(HTTPStatus.OK, result)
        except (ValueError, PermissionError, MultiAgentRuntimeError, json.JSONDecodeError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def log_message(self, format: str, *args: Any) -> None:
        return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the JARVIS command center preview")
    parser.add_argument("--runtime-db", type=Path, default=Path(".jarvis-command-runtime.db"))
    parser.add_argument("--execution-db", type=Path, default=Path(".jarvis-command-execution.db"))
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--demo", action="store_true", help="enable visibly simulated offline execution")
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument('--workspace', action='append', default=[], metavar='PROJECT_ID=PATH',
                        help='Explicit read-only project root; never inferred or enabled by the UI')
    args = parser.parse_args(argv)
    providers = subscription_providers()
    if args.demo:
        providers["offline-demo"] = OfflineDemoProvider()
    service = CommandCenterService(
        args.runtime_db, args.execution_db, providers=providers, max_workers=args.workers,
        workspace_roots={key: Path(path) for key, path in (entry.split('=', 1) for entry in args.workspace)}
    )
    server = CommandCenterHTTPServer(("127.0.0.1", args.port), service)
    url = f"http://127.0.0.1:{server.server_port}/#token={server.token}"
    print(f"JARVIS Command Center: {url}")
    print("Loopback text chat. Provider readiness is shown in the UI; external tools stay disabled.")
    if not args.no_open:
        webbrowser.open(url)
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
