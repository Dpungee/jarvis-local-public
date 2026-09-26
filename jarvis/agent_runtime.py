"""Real task execution for Command Center agents, with a durable, observable record.

JARVIS stays the authority. Agent identity and lifecycle live in the multi-agent runtime
store; this module owns execution detail: tasks, attempts, steering, an append-only event
log, artifacts and settings, all in ``hub.db`` under the backend state directory.

A task runs through JARVIS's own ``Agent`` tool loop. The provider CLI is a tool-less model
backend (see ``agent_providers``); every tool call executes inside JARVIS, restricted to the
agent's granted capability groups by removing every other tool from the agent's ToolBox —
the single dispatch point for offered, model-requested and internal tool calls alike.

What the Hub shows is derived from execution: state transitions written by this module,
tool calls observed at ``ToolBox.execute`` with their real outcome, and files that actually
changed in the project workspace. Model-written text is shown as the result, never as proof.
"""

from __future__ import annotations

import dataclasses
import difflib
import hashlib
import json
import mimetypes
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from .agent_providers import PROVIDERS, REQUESTED_DEFAULT, ProviderRegistry, build_agent_client, valid_model_name
from .multi_agent_runtime import AgentLifecycle, MultiAgentRuntimeError, MultiAgentRuntimeStore, TaskStatus

SCHEMA_VERSION = 1

# Closed capability groups. Anything not listed here is never offered to a Hub agent.
TOOL_GROUPS: dict[str, dict[str, Any]] = {
    "files_read": {"label": "Read project files", "tools": (
        "list_files", "read_file", "read_files", "search_files", "detect_project")},
    "files_write": {"label": "Create and edit project files", "tools": (
        "write_file", "edit_file", "make_directory", "copy_path", "move_path", "trash_path")},
    "run_commands": {"label": "Run programs in the project (tests, scripts)", "tools": (
        "run_process", "start_process", "stop_process", "process_status", "process_logs", "http_health")},
    "web_research": {"label": "Search and read the public web", "tools": (
        "web_search", "web_fetch", "research_question")},
    "documents": {"label": "Build documents (docx, pptx, xlsx, pdf)", "tools": (
        "build_document", "build_document_preview")},
    "memory": {"label": "Remember and recall within this agent", "tools": (
        "recall", "remember", "session_search")},
}
DEFAULT_GROUPS = ("files_read", "files_write", "web_research", "memory")
WRITE_GROUPS = frozenset({"files_write", "run_commands", "documents"})

STATES = ("QUEUED", "RUNNING", "WAITING_APPROVAL", "WAITING_PROVIDER", "PAUSED",
          "INTERRUPTED", "COMPLETED", "FAILED", "CANCELLED")
ACTIVE_STATES = frozenset({"QUEUED", "RUNNING", "WAITING_APPROVAL", "WAITING_PROVIDER", "PAUSED", "INTERRUPTED"})
TERMINAL_STATES = frozenset({"COMPLETED", "FAILED", "CANCELLED"})
MAX_PROVIDER_WAITS = 3
PROVIDER_BACKOFF_SECONDS = (30.0, 90.0, 240.0)
STALE_AFTER_SECONDS = 300.0

SKIP_DIRS = frozenset({".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
                       ".pytest_cache", ".mypy_cache", ".ruff_cache", ".jarvis", ".idea", ".vscode"})
MAX_SNAPSHOT_FILES = 5000
MAX_DIFF_BYTES = 256 * 1024
MAX_STORED_BYTES = 8 * 1024 * 1024
TEXT_SUFFIXES = frozenset({".py", ".md", ".txt", ".json", ".csv", ".js", ".cjs", ".mjs", ".ts", ".tsx",
                           ".jsx", ".css", ".html", ".htm", ".toml", ".yaml", ".yml", ".ini", ".cfg",
                           ".sh", ".ps1", ".sql", ".xml", ".rst", ".log", ".env.example"})
_ID_RE = re.compile(r"^[a-z]{2,12}_[0-9a-f]{12,32}$")
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _now() -> float:
    return time.time()


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def _text(value: Any, limit: int, *, field: str, required: bool = True) -> str:
    text = str(value if value is not None else "").strip()
    if required and not text:
        raise ValueError(f"{field} is required.")
    if len(text) > limit:
        raise ValueError(f"{field} must be at most {limit} characters.")
    if any(ord(ch) < 32 and ch not in "\n\r\t" for ch in text):
        raise ValueError(f"{field} contains control characters.")
    return text


def _bounded(text: Any, limit: int = 300) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


class TaskError(ValueError):
    """A request the runtime refuses; the message is safe to show the operator."""


# --------------------------------------------------------------------------- store


class HubStore:
    """SQLite store for execution detail. Every write is one short IMMEDIATE transaction."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.tx() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS hub_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS hub_settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS hub_projects(
                    project_id TEXT PRIMARY KEY, name TEXT NOT NULL, root TEXT NOT NULL,
                    managed INTEGER NOT NULL, created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS hub_agents(
                    agent_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, instructions TEXT NOT NULL,
                    groups_json TEXT NOT NULL, archived_at REAL, created_at REAL NOT NULL,
                    updated_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS hub_chats(
                    chat_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, title TEXT NOT NULL,
                    conversation_id INTEGER, created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS hub_tasks(
                    task_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, project_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL, title TEXT NOT NULL, request TEXT NOT NULL,
                    state TEXT NOT NULL, provider TEXT NOT NULL, model_configured TEXT NOT NULL,
                    model_override TEXT, model_used TEXT, groups_json TEXT NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 0, retry_of TEXT, followup_of TEXT,
                    created_at REAL NOT NULL, queued_at REAL, started_at REAL, finished_at REAL,
                    updated_at REAL NOT NULL, last_activity_at REAL,
                    result TEXT, error TEXT, error_kind TEXT, approval_id INTEGER,
                    tool_calls INTEGER NOT NULL DEFAULT 0, provider_waits INTEGER NOT NULL DEFAULT 0,
                    not_before REAL, control TEXT, runtime_task_id TEXT, request_id TEXT UNIQUE);
                CREATE INDEX IF NOT EXISTS hub_tasks_agent ON hub_tasks(agent_id, created_at);
                CREATE INDEX IF NOT EXISTS hub_tasks_state ON hub_tasks(state, queued_at);
                CREATE TABLE IF NOT EXISTS hub_steering(
                    steer_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, body TEXT NOT NULL,
                    state TEXT NOT NULL, created_at REAL NOT NULL, applied_task_id TEXT, applied_at REAL);
                CREATE TABLE IF NOT EXISTS hub_events(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, agent_id TEXT,
                    task_id TEXT, project_id TEXT, kind TEXT NOT NULL, summary TEXT NOT NULL,
                    detail_json TEXT NOT NULL DEFAULT '{}');
                CREATE INDEX IF NOT EXISTS hub_events_task ON hub_events(task_id, seq);
                CREATE TABLE IF NOT EXISTS hub_artifacts(
                    artifact_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, agent_id TEXT NOT NULL,
                    project_id TEXT NOT NULL, path TEXT NOT NULL, version INTEGER NOT NULL,
                    change TEXT NOT NULL, sha256 TEXT, size INTEGER, mime TEXT, stored INTEGER NOT NULL,
                    diff TEXT, created_at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS hub_artifacts_path ON hub_artifacts(project_id, path, version);
                CREATE TABLE IF NOT EXISTS hub_audit(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, actor TEXT NOT NULL,
                    action TEXT NOT NULL, target TEXT, outcome TEXT NOT NULL);
            """)
            db.execute("INSERT OR IGNORE INTO hub_meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    @staticmethod
    def event(db: sqlite3.Connection, kind: str, summary: str, *, agent_id: str | None = None,
              task_id: str | None = None, project_id: str | None = None,
              detail: dict[str, Any] | None = None) -> int:
        cursor = db.execute(
            "INSERT INTO hub_events(ts, agent_id, task_id, project_id, kind, summary, detail_json)"
            " VALUES (?,?,?,?,?,?,?)",
            (_now(), agent_id, task_id, project_id, kind, _bounded(summary, 400),
             _event_detail_json(detail)))
        if task_id:
            db.execute("UPDATE hub_tasks SET last_activity_at=? WHERE task_id=?", (_now(), task_id))
        return int(cursor.lastrowid)


def _event_detail_json(detail: dict[str, Any] | None) -> str:
    encoded = json.dumps(detail or {}, default=str)
    if len(encoded) <= 8000:
        return encoded
    # Slicing serialized JSON can permanently break every later event read.
    return json.dumps({"truncated": True, "original_chars": len(encoded)})


# --------------------------------------------------------------------------- workspace snapshots


def _skip(rel_parts: tuple[str, ...]) -> bool:
    return any(part in SKIP_DIRS or part.startswith(".jarvis") for part in rel_parts)


def snapshot_workspace(root: Path) -> dict[str, dict[str, Any]]:
    """Bounded record of every regular file: size, mtime and, for text files, content."""
    result: dict[str, dict[str, Any]] = {}
    if not root.is_dir():
        return result
    for current, dirs, files in os.walk(root):
        rel_dir = Path(current).relative_to(root)
        dirs[:] = [d for d in dirs if not _skip((*rel_dir.parts, d)) and not os.path.islink(os.path.join(current, d))]
        for name in files:
            if len(result) >= MAX_SNAPSHOT_FILES:
                return result
            path = Path(current) / name
            try:
                info = os.lstat(path)
            except OSError:
                continue
            if not os.path.isfile(path) or os.path.islink(path):
                continue
            rel = (rel_dir / name).as_posix()
            entry: dict[str, Any] = {"size": info.st_size, "mtime": info.st_mtime_ns}
            if info.st_size <= MAX_DIFF_BYTES and _is_text_name(name):
                try:
                    entry["text"] = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    pass
            result[rel] = entry
    return result


def _is_text_name(name: str) -> bool:
    lowered = name.lower()
    return any(lowered.endswith(suffix) for suffix in TEXT_SUFFIXES) or "." not in lowered


def _mime(path: str) -> str:
    lowered = path.lower()
    if lowered.endswith(".md"):
        return "text/markdown"
    if lowered.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
        return mimetypes.guess_type(lowered)[0] or "application/octet-stream"
    if _is_text_name(lowered):
        return "text/plain"
    return mimetypes.guess_type(lowered)[0] or "application/octet-stream"


# --------------------------------------------------------------------------- runtime


@dataclasses.dataclass
class _Running:
    task_id: str
    agent_id: str
    project_id: str
    writes: bool
    cancel: threading.Event
    thread: threading.Thread
    control: str | None = None  # "cancel" | "pause"


class AgentRuntime:
    """Owns task execution for Command Center agents. Thread-safe; one instance per backend."""

    def __init__(
        self,
        *,
        state_dir: Path,
        runtime_path: Path,
        provider_profile_dir: Path,
        base_config: Any,
        projects: dict[str, tuple[str, Path]] | None = None,
        max_concurrent: int = 2,
        registry: ProviderRegistry | None = None,
        agent_factory: Callable[..., Any] | None = None,
        client_factory: Callable[[str, str], Any] | None = None,
        dispatch_interval: float = 0.5,
        start: bool = True,
    ) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be positive")
        self.state_dir = Path(state_dir).resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.runtime_path = Path(runtime_path)
        self.profile_dir = Path(provider_profile_dir).resolve()
        self.base_config = base_config
        self.max_concurrent = max_concurrent
        self.store = HubStore(self.state_dir / "hub.db")
        self.registry = registry or ProviderRegistry(self.profile_dir)
        self._agent_factory = agent_factory
        self._client_factory = client_factory or (lambda provider, model: build_agent_client(self.profile_dir, provider, model))
        self._lock = threading.RLock()
        self._running: dict[str, _Running] = {}
        self._stop = threading.Event()
        self._dispatch_interval = dispatch_interval
        self.artifact_dir = self.state_dir / "artifacts"
        self.artifact_dir.mkdir(exist_ok=True)
        for project_id, (name, root) in (projects or {}).items():
            self.register_project(project_id, name, Path(root), managed=False)
        self._recover()
        self._thread = threading.Thread(target=self._dispatch_loop, name="jarvis-hub-dispatch", daemon=True)
        if start:
            self._thread.start()

    # -- lifecycle ---------------------------------------------------------------

    def close(self, timeout: float = 30.0) -> None:
        self._stop.set()
        with self._lock:
            running = list(self._running.values())
        for item in running:
            item.control = "shutdown"
            item.cancel.set()
        for item in running:
            item.thread.join(timeout)
        if self._thread.is_alive():
            self._thread.join(timeout)

    def _recover(self) -> None:
        """Restart never completes or re-runs work silently.

        RUNNING work is marked INTERRUPTED and waits for an explicit resume, because side
        effects may already have happened. QUEUED work never started, so it stays queued.
        """
        with self.store.tx() as db:
            rows = db.execute("SELECT * FROM hub_tasks WHERE state='RUNNING'").fetchall()
            for row in rows:
                db.execute("UPDATE hub_tasks SET state='INTERRUPTED', updated_at=?, control=NULL WHERE task_id=?",
                           (_now(), row["task_id"]))
                self.store.event(db, "task.interrupted",
                                 "Interrupted by a backend restart; resume to run it again from its request.",
                                 agent_id=row["agent_id"], task_id=row["task_id"], project_id=row["project_id"])

    # -- settings, projects, agents ------------------------------------------------

    def default_model(self) -> dict[str, Any]:
        with self.store.read() as db:
            rows = {r["key"]: r["value"] for r in db.execute("SELECT * FROM hub_settings")}
        provider = rows.get("default_provider", REQUESTED_DEFAULT[0])
        model = rows.get("default_model", REQUESTED_DEFAULT[1])
        verified = self.registry.snapshot().get(provider, {}).get("verified_models", {}).get(model)
        return {"provider": provider, "model": model, "requested": list(REQUESTED_DEFAULT),
                "verification": verified}

    def set_default_model(self, provider: str, model: str, actor: str) -> dict[str, Any]:
        if provider not in PROVIDERS or not valid_model_name(model):
            raise TaskError("Unsupported provider or model identifier.")
        with self.store.tx() as db:
            db.execute("INSERT OR REPLACE INTO hub_settings VALUES ('default_provider', ?)", (provider,))
            db.execute("INSERT OR REPLACE INTO hub_settings VALUES ('default_model', ?)", (model,))
            self.store.event(db, "settings.default_model", f"Default model for new agents: {provider} / {model}")
            self._audit(db, actor, "settings.default_model", f"{provider}:{model}", "ok")
        return self.default_model()

    def register_project(self, project_id: str, name: str, root: Path, *, managed: bool) -> dict[str, Any]:
        root = Path(root).resolve()
        root.mkdir(parents=True, exist_ok=True)
        with self.store.tx() as db:
            existing = db.execute("SELECT * FROM hub_projects WHERE project_id=?", (project_id,)).fetchone()
            if existing is None:
                db.execute("INSERT INTO hub_projects VALUES (?,?,?,?,?)",
                           (project_id, name, str(root), int(managed), _now()))
                self.store.event(db, "project.registered", f"Project “{name}” registered.", project_id=project_id)
            elif Path(existing["root"]) != root:
                db.execute("UPDATE hub_projects SET root=?, name=? WHERE project_id=?", (str(root), name, project_id))
        return self.project(project_id)

    def create_project(self, name: str, actor: str) -> dict[str, Any]:
        """A backend-managed project: its workspace is a new directory the backend creates.

        Arbitrary host paths are only ever registered at backend startup, never over HTTP.
        """
        name = _text(name, 80, field="Project name")
        slug = _SLUG_RE.sub("-", name.lower()).strip("-")[:40] or "project"
        project_id = f"{slug}-{uuid.uuid4().hex[:6]}"
        root = self.state_dir / "projects" / project_id
        project = self.register_project(project_id, name, root, managed=True)
        with self.store.tx() as db:
            self._audit(db, actor, "project.create", project_id, "ok")
        return project

    def project(self, project_id: str) -> dict[str, Any]:
        with self.store.read() as db:
            row = db.execute("SELECT * FROM hub_projects WHERE project_id=?", (project_id,)).fetchone()
        if row is None:
            raise TaskError("Unknown project.")
        return {"project_id": row["project_id"], "name": row["name"], "managed": bool(row["managed"])}

    def _project_root(self, project_id: str) -> Path:
        with self.store.read() as db:
            row = db.execute("SELECT root FROM hub_projects WHERE project_id=?", (project_id,)).fetchone()
        if row is None:
            raise TaskError("Unknown project.")
        return Path(row["root"])

    def projects(self) -> list[dict[str, Any]]:
        with self.store.read() as db:
            rows = db.execute("SELECT * FROM hub_projects ORDER BY created_at").fetchall()
        return [{"project_id": r["project_id"], "name": r["name"], "managed": bool(r["managed"])} for r in rows]

    def create_agent(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        allowed = {"name", "role", "purpose", "instructions", "provider", "model", "project_id",
                   "tool_groups", "enable", "request_id"}
        extra = set(payload) - allowed
        if extra:
            raise TaskError(f"Unsupported agent fields: {', '.join(sorted(extra))}.")
        name = _text(payload.get("name"), 80, field="Name")
        role = _text(payload.get("role") or "Agent", 120, field="Role")
        purpose = _text(payload.get("purpose"), 2000, field="Purpose", required=False)
        instructions = _text(payload.get("instructions"), 8000, field="Instructions", required=False)
        default = self.default_model()
        provider = str(payload.get("provider") or default["provider"])
        model = str(payload.get("model") or default["model"])
        if provider not in PROVIDERS or not valid_model_name(model):
            raise TaskError("Choose Claude CLI or Codex CLI and a valid model identifier.")
        project_id = str(payload.get("project_id") or "")
        self.project(project_id)
        groups = self._groups(payload.get("tool_groups", list(DEFAULT_GROUPS)))
        with MultiAgentRuntimeStore(self.runtime_path) as runtime:
            agent = runtime.create_agent(
                display_name=name, role=role, purpose=purpose, specialties=(),
                model_provider=provider, model_name=model, project_id=project_id,
                idempotency_key=str(payload.get("request_id") or uuid.uuid4()))
            if payload.get("enable") in (True, "on", "true"):
                runtime.set_agent_lifecycle(agent.agent_id, AgentLifecycle.RUNNING, actor_id="owner",
                                            idempotency_key=f"hub-enable-{agent.agent_id}")
        now = _now()
        with self.store.tx() as db:
            db.execute("INSERT OR IGNORE INTO hub_agents VALUES (?,?,?,?,?,?,?)",
                       (agent.agent_id, project_id, instructions, json.dumps(groups), None, now, now))
            db.execute("INSERT OR IGNORE INTO hub_chats VALUES (?,?,?,?,?)",
                       (_id("chat"), agent.agent_id, "Main", None, now))
            self.store.event(db, "agent.created", f"Agent “{name}” created · {provider} / {model}",
                             agent_id=agent.agent_id, project_id=project_id,
                             detail={"tool_groups": groups})
            self._audit(db, actor, "agent.create", agent.agent_id, "ok")
        return self.agent(agent.agent_id)

    def update_agent(self, agent_id: str, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        allowed = {"instructions", "provider", "model", "tool_groups", "project_id"}
        extra = set(payload) - allowed
        if extra:
            raise TaskError(f"Unsupported fields: {', '.join(sorted(extra))}.")
        current = self.agent(agent_id)
        changes = []
        with MultiAgentRuntimeStore(self.runtime_path) as runtime:
            provider = str(payload.get("provider") or current["provider"])
            model = str(payload.get("model") or current["model"])
            if (provider, model) != (current["provider"], current["model"]):
                if provider not in PROVIDERS or not valid_model_name(model):
                    raise TaskError("Choose Claude CLI or Codex CLI and a valid model identifier.")
                runtime.update_agent_model(agent_id, actor_id="owner", model_provider=provider,
                                           model_name=model, idempotency_key=f"hub-model-{uuid.uuid4()}")
                changes.append(f"model → {provider} / {model} (applies from the next task)")
        with self.store.tx() as db:
            if "instructions" in payload:
                db.execute("UPDATE hub_agents SET instructions=?, updated_at=? WHERE agent_id=?",
                           (_text(payload["instructions"], 8000, field="Instructions", required=False), _now(), agent_id))
                changes.append("instructions updated")
            if "tool_groups" in payload:
                groups = self._groups(payload["tool_groups"])
                db.execute("UPDATE hub_agents SET groups_json=?, updated_at=? WHERE agent_id=?",
                           (json.dumps(groups), _now(), agent_id))
                changes.append("tool access: " + (", ".join(groups) or "none") + " (applies from the next task)")
            if "project_id" in payload and payload["project_id"] != current["project_id"]:
                raise TaskError("An agent's project is fixed at creation; create a new agent for another project.")
            if changes:
                self.store.event(db, "agent.updated", "Configuration changed: " + "; ".join(changes),
                                 agent_id=agent_id, project_id=current["project_id"])
                self._audit(db, actor, "agent.update", agent_id, "; ".join(changes))
        return self.agent(agent_id)

    def set_lifecycle(self, agent_id: str, action: str, actor: str) -> dict[str, Any]:
        target = {"enable": AgentLifecycle.RUNNING, "resume": AgentLifecycle.RUNNING,
                  "pause": AgentLifecycle.PAUSED, "disable": AgentLifecycle.STOPPED,
                  "archive": AgentLifecycle.STOPPED}.get(action)
        if target is None:
            raise TaskError("Unsupported lifecycle action.")
        current = self.agent(agent_id)
        with MultiAgentRuntimeStore(self.runtime_path) as runtime:
            if runtime.get_agent(agent_id).lifecycle is not target:
                runtime.set_agent_lifecycle(agent_id, target, actor_id="owner",
                                            idempotency_key=f"hub-{action}-{uuid.uuid4()}")
        if action in {"pause", "disable", "archive"}:
            # Stop the agent's active attempt; its task is held (paused) rather than lost.
            with self._lock:
                running = [r for r in self._running.values() if r.agent_id == agent_id]
            for item in running:
                self._control(item.task_id, "pause", actor)
        with self.store.tx() as db:
            if action == "archive":
                db.execute("UPDATE hub_agents SET archived_at=? WHERE agent_id=?", (_now(), agent_id))
            if action in {"enable", "resume"}:
                db.execute("UPDATE hub_agents SET archived_at=NULL WHERE agent_id=?", (agent_id,))
            label = {"enable": "enabled", "resume": "resumed", "pause": "paused",
                     "disable": "disabled", "archive": "archived (history and deliverables kept)"}[action]
            self.store.event(db, "agent.lifecycle", f"Agent {label}.", agent_id=agent_id,
                             project_id=current["project_id"])
            self._audit(db, actor, f"agent.{action}", agent_id, "ok")
        return self.agent(agent_id)

    @staticmethod
    def _groups(value: Any) -> list[str]:
        if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
            raise TaskError("Tool access must be a list of capability groups.")
        unknown = [v for v in value if v not in TOOL_GROUPS]
        if unknown:
            raise TaskError(f"Unknown capability group: {', '.join(unknown)}.")
        return [g for g in TOOL_GROUPS if g in value]

    def agent(self, agent_id: str) -> dict[str, Any]:
        if not isinstance(agent_id, str) or len(agent_id) > 80:
            raise TaskError("Unknown agent.")
        with MultiAgentRuntimeStore(self.runtime_path) as runtime:
            try:
                record = runtime.get_agent(agent_id)
            except MultiAgentRuntimeError as exc:
                raise TaskError("Unknown agent.") from exc
        with self.store.read() as db:
            row = db.execute("SELECT * FROM hub_agents WHERE agent_id=?", (agent_id,)).fetchone()
            chats = [dict(r) for r in db.execute(
                "SELECT chat_id, title, conversation_id, created_at FROM hub_chats WHERE agent_id=? ORDER BY created_at",
                (agent_id,))]
        if row is None:
            raise TaskError("This agent was not created through the Hub runtime.")
        return {
            "agent_id": record.agent_id, "name": record.display_name, "role": record.role,
            "purpose": record.purpose, "instructions": row["instructions"],
            "provider": record.model_provider, "model": record.model_name,
            "lifecycle": record.lifecycle.value, "project_id": row["project_id"],
            "tool_groups": json.loads(row["groups_json"]), "archived": row["archived_at"] is not None,
            "created_at": row["created_at"], "chats": chats,
        }

    def agents(self) -> list[dict[str, Any]]:
        with self.store.read() as db:
            ids = [r["agent_id"] for r in db.execute("SELECT agent_id FROM hub_agents ORDER BY created_at")]
        result = []
        for agent_id in ids:
            try:
                result.append(self.agent(agent_id))
            except TaskError:
                continue
        return result

    # -- tasks ---------------------------------------------------------------------

    def submit_task(self, agent_id: str, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        allowed = {"request", "title", "model_override", "chat_id", "request_id"}
        extra = set(payload) - allowed
        if extra:
            raise TaskError(f"Unsupported task fields: {', '.join(sorted(extra))}.")
        request = _text(payload.get("request"), 20000, field="Task request")
        title = _text(payload.get("title") or request.splitlines()[0], 120, field="Title")
        agent = self.agent(agent_id)
        chat_id = payload.get("chat_id") or agent["chats"][0]["chat_id"]
        if chat_id not in {c["chat_id"] for c in agent["chats"]}:
            raise TaskError("Unknown chat for this agent.")
        request_id = payload.get("request_id")
        if request_id is not None:
            request_id = _text(request_id, 100, field="Request id")
            with self.store.read() as db:
                existing = db.execute(
                    "SELECT task_id, request, agent_id, chat_id, title, model_override "
                    "FROM hub_tasks WHERE request_id=?", (request_id,)).fetchone()
            if existing is not None:
                if (existing["request"] != request or existing["agent_id"] != agent_id
                        or existing["chat_id"] != chat_id
                        or ("title" in payload and existing["title"] != title)
                        or ("model_override" in payload and existing["model_override"] != payload["model_override"])):
                    raise TaskError("Request id reused for a different request.")
                return self.task(existing["task_id"])
        if agent["archived"]:
            raise TaskError("This agent is archived. Enable it to give it new work.")
        if agent["lifecycle"] != "RUNNING":
            raise TaskError("Enable this agent before assigning work.")
        override = payload.get("model_override")
        if override:
            override = str(override)
            if not valid_model_name(override):
                raise TaskError("Invalid model override.")
        return self._enqueue(agent, title=title, request=request, chat_id=chat_id,
                             model_override=override, request_id=request_id, actor=actor)

    def _enqueue(self, agent: dict[str, Any], *, title: str, request: str, chat_id: str,
                 model_override: str | None = None, request_id: str | None = None,
                 retry_of: str | None = None, followup_of: str | None = None, actor: str) -> dict[str, Any]:
        runtime_task_id = None
        try:
            with MultiAgentRuntimeStore(self.runtime_path) as runtime:
                mirror = runtime.create_task(
                    creator_id=agent["agent_id"], owner_id=agent["agent_id"], title=title[:120],
                    description=request[:4000], project_id=agent["project_id"],
                    idempotency_key=f"hub-task-{uuid.uuid4()}")
                runtime_task_id = mirror.task_id
        except MultiAgentRuntimeError:
            runtime_task_id = None  # the execution record below is still authoritative
        task_id, now = _id("task"), _now()
        with self.store.tx() as db:
            db.execute(
                """INSERT INTO hub_tasks(task_id, agent_id, project_id, chat_id, title, request, state,
                       provider, model_configured, model_override, groups_json, retry_of, followup_of,
                       created_at, queued_at, updated_at, last_activity_at, runtime_task_id, request_id)
                   VALUES (?,?,?,?,?,?,'QUEUED',?,?,?,?,?,?,?,?,?,?,?,?)""",
                (task_id, agent["agent_id"], agent["project_id"], chat_id, title, request,
                 agent["provider"], agent["model"], model_override, json.dumps(agent["tool_groups"]),
                 retry_of, followup_of, now, now, now, now, runtime_task_id, request_id))
            model = model_override or agent["model"]
            note = f" (task override; agent default is {agent['model']})" if model_override else ""
            self.store.event(db, "task.queued", f"Task queued: {title} · {agent['provider']} / {model}{note}",
                             agent_id=agent["agent_id"], task_id=task_id, project_id=agent["project_id"])
            self._audit(db, actor, "task.submit", task_id, "ok")
        return self.task(task_id)

    def task(self, task_id: str) -> dict[str, Any]:
        with self.store.read() as db:
            row = db.execute("SELECT * FROM hub_tasks WHERE task_id=?", (task_id,)).fetchone()
            if row is None:
                raise TaskError("Unknown task.")
            steering = [dict(r) for r in db.execute(
                "SELECT * FROM hub_steering WHERE task_id=? ORDER BY created_at", (task_id,))]
            attempts = [dict(r) for r in db.execute(
                "SELECT task_id, state, created_at, finished_at FROM hub_tasks WHERE retry_of=? OR followup_of=? ORDER BY created_at",
                (task_id, task_id))]
        task = dict(row)
        task["tool_groups"] = json.loads(task.pop("groups_json"))
        task["steering"], task["related"] = steering, attempts
        task["model_requested"] = task["model_override"] or task["model_configured"]
        return task

    def tasks(self, *, agent_id: str | None = None, project_id: str | None = None,
              states: set[str] | None = None, limit: int = 200) -> list[dict[str, Any]]:
        clauses, args = [], []
        if agent_id:
            clauses.append("agent_id=?"); args.append(agent_id)
        if project_id:
            clauses.append("project_id=?"); args.append(project_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self.store.read() as db:
            rows = db.execute(f"SELECT * FROM hub_tasks{where} ORDER BY created_at DESC LIMIT ?",
                              (*args, max(1, min(limit, 500)))).fetchall()
        result = []
        for row in rows:
            if states and row["state"] not in states:
                continue
            item = dict(row)
            item["tool_groups"] = json.loads(item.pop("groups_json"))
            item["model_requested"] = item["model_override"] or item["model_configured"]
            result.append(item)
        return result

    def control(self, task_id: str, action: str, actor: str) -> dict[str, Any]:
        if action not in {"cancel", "pause", "resume", "retry"}:
            raise TaskError("Unsupported task action.")
        if action in {"cancel", "pause"}:
            self._control(task_id, action, actor)
        elif action == "resume":
            self._resume(task_id, actor)
        else:
            return self._retry(task_id, actor)
        return self.task(task_id)

    def _control(self, task_id: str, action: str, actor: str) -> None:
        task = self.task(task_id)
        with self._lock:
            running = self._running.get(task_id)
            if running is not None:
                running.control = action
                running.cancel.set()
        if running is not None:
            with self.store.tx() as db:
                db.execute("UPDATE hub_tasks SET control=?, updated_at=? WHERE task_id=?", (action, _now(), task_id))
                self.store.event(db, f"task.{action}_requested",
                                 f"{action.title()} requested; the agent stops before its next action.",
                                 agent_id=task["agent_id"], task_id=task_id, project_id=task["project_id"])
                self._audit(db, actor, f"task.{action}", task_id, "requested")
            return
        if task["state"] not in ACTIVE_STATES:
            raise TaskError(f"This task is {task['state'].lower()}; there is nothing to {action}.")
        new_state = "CANCELLED" if action == "cancel" else "PAUSED"
        with self.store.tx() as db:
            current = db.execute("SELECT state FROM hub_tasks WHERE task_id=?", (task_id,)).fetchone()["state"]
            if current == "RUNNING":
                raise TaskError("The task is starting; try again in a moment.")
            if action == "pause" and current == "PAUSED":
                return
            db.execute("UPDATE hub_tasks SET state=?, updated_at=?, finished_at=CASE WHEN ?='CANCELLED' THEN ? ELSE finished_at END WHERE task_id=?",
                       (new_state, _now(), new_state, _now(), task_id))
            message = ("Cancelled before it ran." if new_state == "CANCELLED"
                       else "Paused; it will not start until resumed.")
            self.store.event(db, f"task.{new_state.lower()}", message, agent_id=task["agent_id"],
                             task_id=task_id, project_id=task["project_id"])
            if new_state == "CANCELLED":
                db.execute("UPDATE hub_steering SET state='DISCARDED' WHERE task_id=? AND state='PENDING'", (task_id,))
            self._audit(db, actor, f"task.{action}", task_id, "ok")
        if new_state == "CANCELLED":
            self._mirror(task, TaskStatus.CANCELLED)

    def _resume(self, task_id: str, actor: str) -> None:
        task = self.task(task_id)
        if task["state"] not in {"PAUSED", "INTERRUPTED", "WAITING_PROVIDER"}:
            raise TaskError(f"This task is {task['state'].lower()}; only paused, interrupted or provider-waiting tasks resume.")
        agent = self.agent(task["agent_id"])
        if agent["lifecycle"] != "RUNNING":
            raise TaskError("Enable or resume the agent first.")
        with self.store.tx() as db:
            db.execute("UPDATE hub_tasks SET state='QUEUED', queued_at=?, updated_at=?, not_before=NULL, "
                       "provider_waits=0, control=NULL WHERE task_id=?", (_now(), _now(), task_id))
            self.store.event(db, "task.resumed",
                             "Resumed: it restarts from its request and conversation. Changes an earlier attempt "
                             "already made in the workspace are kept, not undone.",
                             agent_id=task["agent_id"], task_id=task_id, project_id=task["project_id"])
            self._audit(db, actor, "task.resume", task_id, "ok")

    def _retry(self, task_id: str, actor: str) -> dict[str, Any]:
        task = self.task(task_id)
        if task["state"] not in {"FAILED", "CANCELLED", "INTERRUPTED"}:
            raise TaskError("Only failed, cancelled or interrupted tasks can be retried. "
                            "Completed work is never re-run automatically; assign a new task instead.")
        agent = self.agent(task["agent_id"])
        request = (task["request"] + "\n\n[Retry of an earlier attempt that ended "
                   f"{task['state'].lower()}: {(task['error'] or 'no detail')[:300]}. Inspect the "
                   "workspace first and continue from its current state; do not redo work that is "
                   "already present.]")
        retried = self._enqueue(agent, title=f"Retry: {task['title']}"[:120], request=request,
                                chat_id=task["chat_id"], model_override=task["model_override"],
                                retry_of=task_id, actor=actor)
        if task["state"] == "INTERRUPTED":
            with self.store.tx() as db:
                db.execute("UPDATE hub_tasks SET state='CANCELLED', finished_at=?, updated_at=? WHERE task_id=?",
                           (_now(), _now(), task_id))
                self.store.event(db, "task.superseded", f"Superseded by retry {retried['task_id']}.",
                                 agent_id=task["agent_id"], task_id=task_id, project_id=task["project_id"])
        return retried

    def steer(self, task_id: str, body: str, actor: str) -> dict[str, Any]:
        body = _text(body, 4000, field="Follow-up instruction")
        task = self.task(task_id)
        if task["state"] in TERMINAL_STATES:
            raise TaskError("This task has finished. Assign a new task to continue the work.")
        with self.store.tx() as db:
            db.execute("INSERT INTO hub_steering VALUES (?,?,?,?,?,?,?)",
                       (_id("steer"), task_id, body, "PENDING", _now(), None, None))
            when = ("after the current step finishes — the runtime cannot change a model call in flight"
                    if task["state"] == "RUNNING" else "when this task runs")
            self.store.event(db, "task.steer_received", f"Follow-up received; applies {when}.",
                             agent_id=task["agent_id"], task_id=task_id, project_id=task["project_id"],
                             detail={"body": _bounded(body, 300)})
            self._audit(db, actor, "task.steer", task_id, "ok")
        return self.task(task_id)

    # -- approvals -------------------------------------------------------------------

    def approvals(self, agent_id: str) -> list[dict[str, Any]]:
        memory = self._open_memory(agent_id)
        try:
            rows = memory.list_approvals(limit=50)
        finally:
            memory.close()
        return [{"approval_id": r.get("id"), "action": r.get("action"), "resource": _bounded(r.get("resource"), 600),
                 "reason": _bounded(r.get("reason"), 300), "status": r.get("status"),
                 "created_at": r.get("created_at")} for r in rows]

    def decide_approval(self, agent_id: str, approval_id: int, approve: bool, actor: str) -> dict[str, Any]:
        memory = self._open_memory(agent_id)
        try:
            decided = memory.decide_approval(int(approval_id), bool(approve))
        finally:
            memory.close()
        if not decided:
            raise TaskError("That approval is no longer pending.")
        with self.store.tx() as db:
            rows = db.execute("SELECT * FROM hub_tasks WHERE agent_id=? AND state='WAITING_APPROVAL' AND approval_id=?",
                              (agent_id, int(approval_id))).fetchall()
            for row in rows:
                if approve:
                    db.execute("UPDATE hub_tasks SET state='QUEUED', queued_at=?, updated_at=? WHERE task_id=?",
                               (_now(), _now(), row["task_id"]))
                else:
                    db.execute("UPDATE hub_tasks SET state='CANCELLED', finished_at=?, updated_at=?, "
                               "error='Approval denied by the operator.' WHERE task_id=?",
                               (_now(), _now(), row["task_id"]))
                self.store.event(db, "approval.decided",
                                 "Approved; the task resumes with exactly the approved action." if approve
                                 else "Denied; the task was cancelled.",
                                 agent_id=agent_id, task_id=row["task_id"], project_id=row["project_id"])
            self._audit(db, actor, "approval.approve" if approve else "approval.deny", f"{agent_id}#{approval_id}", "ok")
        return {"approval_id": int(approval_id), "approved": bool(approve)}

    # -- events, artifacts, audit ----------------------------------------------------

    def events(self, *, after: int = 0, limit: int = 200, agent_id: str | None = None,
               task_id: str | None = None, project_id: str | None = None,
               kinds: set[str] | None = None) -> list[dict[str, Any]]:
        clauses, args = ["seq>?"], [int(after)]
        for column, value in (("agent_id", agent_id), ("task_id", task_id), ("project_id", project_id)):
            if value:
                clauses.append(f"{column}=?"); args.append(value)
        with self.store.read() as db:
            rows = db.execute(f"SELECT * FROM hub_events WHERE {' AND '.join(clauses)} ORDER BY seq LIMIT ?",
                              (*args, max(1, min(limit, 1000)))).fetchall()
        out = []
        for row in rows:
            if kinds and row["kind"].split(".")[0] not in kinds and row["kind"] not in kinds:
                continue
            item = dict(row)
            item["detail"] = json.loads(item.pop("detail_json") or "{}")
            out.append(item)
        return out

    def last_event_seq(self) -> int:
        with self.store.read() as db:
            return int(db.execute("SELECT COALESCE(MAX(seq),0) FROM hub_events").fetchone()[0])

    def artifacts(self, *, task_id: str | None = None, agent_id: str | None = None,
                  project_id: str | None = None) -> list[dict[str, Any]]:
        clauses, args = [], []
        for column, value in (("task_id", task_id), ("agent_id", agent_id), ("project_id", project_id)):
            if value:
                clauses.append(f"{column}=?"); args.append(value)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self.store.read() as db:
            rows = db.execute(f"SELECT artifact_id, task_id, agent_id, project_id, path, version, change, sha256, size, mime, stored, created_at, diff IS NOT NULL AS has_diff FROM hub_artifacts{where} ORDER BY created_at DESC LIMIT 500", args).fetchall()
        return [dict(r) | {"stored": bool(r["stored"]), "has_diff": bool(r["has_diff"])} for r in rows]

    def artifact(self, artifact_id: str) -> dict[str, Any]:
        with self.store.read() as db:
            row = db.execute("SELECT * FROM hub_artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
            if row is None:
                raise TaskError("Unknown artifact.")
            versions = [dict(r) for r in db.execute(
                "SELECT artifact_id, version, change, task_id, created_at FROM hub_artifacts WHERE project_id=? AND path=? ORDER BY version",
                (row["project_id"], row["path"]))]
        item = dict(row)
        item["stored"], item["versions"] = bool(item["stored"]), versions
        return item

    def artifact_content(self, artifact_id: str) -> tuple[bytes, str, str]:
        item = self.artifact(artifact_id)
        if not item["stored"] or not item["sha256"]:
            raise TaskError("No stored content for this artifact version.")
        blob = self.artifact_dir / item["sha256"][:2] / item["sha256"]
        data = blob.read_bytes()
        if hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise TaskError("Stored artifact failed its integrity check.")
        return data, item["mime"] or "application/octet-stream", Path(item["path"]).name

    def audit(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.store.read() as db:
            return [dict(r) for r in db.execute("SELECT * FROM hub_audit ORDER BY seq DESC LIMIT ?", (limit,))]

    def record_audit(self, actor: str, action: str, target: str | None, outcome: str) -> None:
        with self.store.tx() as db:
            self._audit(db, actor, action, target, outcome)

    @staticmethod
    def _audit(db: sqlite3.Connection, actor: str, action: str, target: str | None, outcome: str) -> None:
        db.execute("INSERT INTO hub_audit(ts, actor, action, target, outcome) VALUES (?,?,?,?,?)",
                   (_now(), _bounded(actor, 80), action, _bounded(target, 200) if target else None, _bounded(outcome, 300)))

    # -- dispatch ------------------------------------------------------------------------

    def _dispatch_loop(self) -> None:
        while not self._stop.wait(self._dispatch_interval):
            try:
                self.dispatch_once()
            except Exception:  # the loop must survive; failures surface on the next pass
                time.sleep(1.0)

    def queue_reason(self, task: dict[str, Any], agents: dict[str, dict[str, Any]] | None = None) -> str:
        """Why a QUEUED task has not started, derived from current backend state."""
        agent = (agents or {}).get(task["agent_id"]) or self.agent(task["agent_id"])
        if agent["lifecycle"] != "RUNNING":
            return "Waiting: the agent is not enabled."
        with self._lock:
            running = list(self._running.values())
        if any(r.agent_id == task["agent_id"] for r in running):
            return "Waiting for this agent's current task to finish."
        writes = bool(set(task["tool_groups"]) & WRITE_GROUPS)
        holder = next((r for r in running if writes and r.writes and r.project_id == task["project_id"]), None)
        if holder is not None:
            return "Waiting for the project's write lock (another agent is changing this project)."
        if len(running) >= self.max_concurrent:
            return f"Waiting for a free slot ({len(running)} of {self.max_concurrent} in use)."
        if task.get("not_before") and task["not_before"] > _now():
            return f"Waiting {int(task['not_before'] - _now())}s before retrying the provider."
        return "Starting."

    def dispatch_once(self) -> None:
        now = _now()
        with self.store.read() as db:
            waiting = db.execute("SELECT * FROM hub_tasks WHERE state='WAITING_PROVIDER' ORDER BY queued_at").fetchall()
            queued = db.execute("SELECT * FROM hub_tasks WHERE state='QUEUED' ORDER BY queued_at").fetchall()
        for row in waiting:
            if row["not_before"] and row["not_before"] > now:
                continue
            ready, _reason = self.registry.ready(row["provider"])
            if ready:
                with self.store.tx() as db:
                    db.execute("UPDATE hub_tasks SET state='QUEUED', updated_at=? WHERE task_id=? AND state='WAITING_PROVIDER'",
                               (now, row["task_id"]))
                    self.store.event(db, "task.provider_available", "Provider available again; task re-queued.",
                                     agent_id=row["agent_id"], task_id=row["task_id"], project_id=row["project_id"])
        for row in queued:
            task = dict(row)
            task["tool_groups"] = json.loads(task["groups_json"])
            if task.get("not_before") and task["not_before"] > now:
                continue
            if self.queue_reason(task) != "Starting.":
                continue
            ready, reason = self.registry.ready(task["provider"])
            if not ready:
                self._wait_for_provider(task, reason, kind="login")
                continue
            self._start(task)

    def _start(self, task: dict[str, Any]) -> None:
        cancel = threading.Event()
        writes = bool(set(task["tool_groups"]) & WRITE_GROUPS)
        with self.store.tx() as db:
            changed = db.execute(
                "UPDATE hub_tasks SET state='RUNNING', started_at=?, updated_at=?, attempt=attempt+1, "
                "control=NULL, error=NULL, error_kind=NULL WHERE task_id=? AND state='QUEUED'",
                (_now(), _now(), task["task_id"])).rowcount
            if changed != 1:
                return
            model = task["model_override"] or task["model_configured"]
            self.store.event(db, "task.started", f"Started · {task['provider']} / {model}",
                             agent_id=task["agent_id"], task_id=task["task_id"], project_id=task["project_id"])
        self._mirror(task, TaskStatus.RUNNING)
        thread = threading.Thread(target=self._execute, args=(task["task_id"], cancel),
                                  name=f"jarvis-hub-{task['task_id']}", daemon=True)
        with self._lock:
            self._running[task["task_id"]] = _Running(task["task_id"], task["agent_id"], task["project_id"],
                                                      writes, cancel, thread)
        thread.start()

    def _wait_for_provider(self, task: dict[str, Any], reason: str, *, kind: str) -> None:
        waits = int(task.get("provider_waits") or 0)
        with self.store.tx() as db:
            if waits >= MAX_PROVIDER_WAITS:
                db.execute("UPDATE hub_tasks SET state='FAILED', finished_at=?, updated_at=?, error=?, error_kind=? "
                           "WHERE task_id=?", (_now(), _now(),
                                              f"Provider unavailable after {MAX_PROVIDER_WAITS} automatic waits: {reason}",
                                              f"provider_{kind}", task["task_id"]))
                self.store.event(db, "task.failed", f"Gave up after {MAX_PROVIDER_WAITS} provider waits: {_bounded(reason, 200)}",
                                 agent_id=task["agent_id"], task_id=task["task_id"], project_id=task["project_id"])
                return
            backoff = PROVIDER_BACKOFF_SECONDS[min(waits, len(PROVIDER_BACKOFF_SECONDS) - 1)]
            db.execute("UPDATE hub_tasks SET state='WAITING_PROVIDER', updated_at=?, not_before=?, "
                       "provider_waits=provider_waits+1, error=?, error_kind=? WHERE task_id=?",
                       (_now(), _now() + backoff, _bounded(reason, 500), f"provider_{kind}", task["task_id"]))
            label = "sign-in" if kind == "login" else "capacity" if kind == "capacity" else "availability"
            self.store.event(db, "task.waiting_provider",
                             f"Waiting for provider {label}: {_bounded(reason, 200)} Next check in {int(backoff)}s "
                             f"(automatic attempt {waits + 1} of {MAX_PROVIDER_WAITS}).",
                             agent_id=task["agent_id"], task_id=task["task_id"], project_id=task["project_id"])

    # -- execution -------------------------------------------------------------------

    def _agent_dir(self, agent_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_\-]{1,80}", agent_id):
            raise TaskError("Invalid agent id.")
        path = self.state_dir / "agents" / agent_id
        (path / "data").mkdir(parents=True, exist_ok=True)
        return path

    def _open_memory(self, agent_id: str) -> Any:
        from .memory import Memory
        return Memory(self._agent_dir(agent_id) / "data" / "jarvis.db")

    def agent_config(self, task: dict[str, Any], workspace: Path, provider: str, model: str) -> Any:
        groups = set(task["tool_groups"])
        ref = f"{provider}:{model}"
        return dataclasses.replace(
            self.base_config, workspace=workspace, data_dir=self._agent_dir(task["agent_id"]) / "data",
            model=ref, fast_model=ref, reasoning_model=ref, coding_model=ref, deep_model=ref,
            learning_model=None, ollama_enabled=False, openai_api_enabled=False, anthropic_api_enabled=False,
            claude_cli_enabled=provider == "claude-cli", codex_cli_enabled=provider == "codex-cli",
            execution_mode="trusted-host" if "run_commands" in groups else "disabled",
            autonomy="autonomous" if groups & WRITE_GROUPS else "readonly",
            network_access="disabled", computer_access="disabled", external_access="disabled",
            self_inspect="disabled", self_repair="disabled", initiative="disabled", proactive_enabled=False,
            memory_embeddings="disabled", screen_companion_mode="disabled", public_presence_enabled=False,
            home_assistant_access="disabled", bluetooth_access="disabled", gateway_channel="",
            vault_dir=None, cloud_max_retries=1)

    def _allowed_tools(self, groups: list[str]) -> set[str]:
        return {tool for group in groups for tool in TOOL_GROUPS[group]["tools"]}

    def _compose_prompt(self, task: dict[str, Any], agent: dict[str, Any], steering: list[dict[str, Any]]) -> str:
        parts = []
        profile = [f"Agent name: {agent['name']}", f"Role: {agent['role']}"]
        if agent["purpose"]:
            profile.append(f"Purpose: {agent['purpose']}")
        if agent["instructions"]:
            profile.append(f"Standing instructions from the operator:\n{agent['instructions']}")
        parts.append("[Agent profile, set by the operator]\n" + "\n".join(profile))
        parts.append("[Task]\n" + task["request"])
        if steering:
            parts.append("[Follow-up instructions from the operator, in order]\n" +
                         "\n".join(f"- {s['body']}" for s in steering))
        return "\n\n".join(parts)

    def _execute(self, task_id: str, cancel: threading.Event) -> None:
        task = self.task(task_id)
        agent_id, project_id = task["agent_id"], task["project_id"]
        provider, model = task["provider"], task["model_override"] or task["model_configured"]
        client = memory = None
        before: dict[str, dict[str, Any]] = {}
        outcome: dict[str, Any] = {}
        try:
            agent_info = self.agent(agent_id)
            workspace = self._project_root(project_id)
            with self.store.tx() as db:
                steering = [dict(r) for r in db.execute(
                    "SELECT * FROM hub_steering WHERE task_id=? AND state='PENDING' ORDER BY created_at", (task_id,))]
                for s in steering:
                    db.execute("UPDATE hub_steering SET state='APPLIED', applied_task_id=?, applied_at=? WHERE steer_id=?",
                               (task_id, _now(), s["steer_id"]))
                if steering:
                    self.store.event(db, "task.steer_applied",
                                     f"{len(steering)} follow-up instruction(s) applied to this attempt.",
                                     agent_id=agent_id, task_id=task_id, project_id=project_id)
                chat = db.execute("SELECT * FROM hub_chats WHERE chat_id=?", (task["chat_id"],)).fetchone()
            before = snapshot_workspace(workspace)
            config = self.agent_config(task, workspace, provider, model)
            memory = self._open_memory(agent_id)
            client = self._client_factory(provider, model)
            recorder = _EventRecorder(self, task)
            if self._agent_factory is not None:
                agent = self._agent_factory(config, memory, recorder.on_event, client)
            else:
                from .agent import Agent
                agent = Agent(config, memory, on_event=recorder.on_event, client=client)
            allowed = self._allowed_tools(task["tool_groups"])
            agent.toolbox.tools = {name: tool for name, tool in agent.toolbox.tools.items() if name in allowed}
            recorder.wrap(agent.toolbox)
            prompt = self._compose_prompt(task, agent_info, steering)
            result = agent.run(prompt, conversation_id=chat["conversation_id"] if chat else None,
                               cancellation_guard=cancel.is_set, stream_callback=recorder.on_stream)
            outcome = {"result": result, "tool_calls": recorder.tool_calls}
            if chat is not None and getattr(result, "conversation_id", None) and not chat["conversation_id"]:
                with self.store.tx() as db:
                    db.execute("UPDATE hub_chats SET conversation_id=? WHERE chat_id=?",
                               (int(result.conversation_id), chat["chat_id"]))
        except Exception as exc:  # classified below; the runtime never crashes on a task
            outcome = {"exception": exc, "tool_calls": locals().get("recorder").tool_calls if locals().get("recorder") else 0}
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
            if memory is not None:
                try:
                    memory.close()
                except Exception:
                    pass
        try:
            self._record_artifacts(task, before)
        except Exception as exc:
            with self.store.tx() as db:
                self.store.event(db, "artifact.error", f"Could not record changed files: {type(exc).__name__}.",
                                 agent_id=agent_id, task_id=task_id, project_id=project_id)
        with self._lock:
            running = self._running.pop(task_id, None)
        self._finish(task, outcome, running.control if running else None)

    def _finish(self, task: dict[str, Any], outcome: dict[str, Any], control: str | None) -> None:
        from .agent import AgentRunCancelled
        task_id, agent_id, project_id = task["task_id"], task["agent_id"], task["project_id"]
        result, exc = outcome.get("result"), outcome.get("exception")
        tool_calls = int(outcome.get("tool_calls") or 0)
        cancelled = isinstance(exc, AgentRunCancelled) or (control is not None)
        now = _now()
        mirror: TaskStatus | None = None
        with self.store.tx() as db:
            if cancelled and control in {"pause", "shutdown"}:
                state = "PAUSED" if control == "pause" else "INTERRUPTED"
                message = ("Paused: the current attempt stopped before its next action. Resume restarts it "
                           "from its request; changes already made are kept." if state == "PAUSED" else
                           "Interrupted by backend shutdown; resume to run it again.")
                db.execute("UPDATE hub_tasks SET state=?, updated_at=?, tool_calls=tool_calls+?, control=NULL WHERE task_id=?",
                           (state, now, tool_calls, task_id))
                self.store.event(db, f"task.{state.lower()}", message, agent_id=agent_id, task_id=task_id, project_id=project_id)
            elif cancelled:
                db.execute("UPDATE hub_tasks SET state='CANCELLED', finished_at=?, updated_at=?, tool_calls=tool_calls+?, "
                           "control=NULL, error='Cancelled by the operator.' WHERE task_id=?", (now, now, tool_calls, task_id))
                db.execute("UPDATE hub_steering SET state='DISCARDED' WHERE task_id=? AND state='PENDING'", (task_id,))
                self.store.event(db, "task.cancelled", "Cancelled: the agent stopped before its next action. Changes "
                                 "already made in the workspace are listed under deliverables.",
                                 agent_id=agent_id, task_id=task_id, project_id=project_id)
                mirror = TaskStatus.CANCELLED
            elif exc is not None:
                db.execute("UPDATE hub_tasks SET state='FAILED', finished_at=?, updated_at=?, tool_calls=tool_calls+?, "
                           "error=?, error_kind='runtime' WHERE task_id=?",
                           (now, now, tool_calls, f"{type(exc).__name__}: {_bounded(exc, 400)}", task_id))
                self.store.event(db, "task.failed", f"Failed inside the runtime: {type(exc).__name__}: {_bounded(exc, 200)}",
                                 agent_id=agent_id, task_id=task_id, project_id=project_id)
                mirror = TaskStatus.FAILED
            else:
                model_used = getattr(result, "model", None)
                model_used = model_used.split(":", 1)[1] if model_used and ":" in model_used else model_used
                text = str(result)
                if getattr(result, "waiting_for_approval", False):
                    db.execute("UPDATE hub_tasks SET state='WAITING_APPROVAL', updated_at=?, approval_id=?, model_used=?, "
                               "tool_calls=tool_calls+?, result=? WHERE task_id=?",
                               (now, getattr(result, "approval_id", None), model_used, tool_calls, text[:20000], task_id))
                    self.store.event(db, "task.waiting_approval",
                                     f"Waiting for your approval (request #{getattr(result, 'approval_id', '?')}).",
                                     agent_id=agent_id, task_id=task_id, project_id=project_id)
                elif getattr(result, "status", "complete") == "complete":
                    db.execute("UPDATE hub_tasks SET state='COMPLETED', finished_at=?, updated_at=?, model_used=?, "
                               "tool_calls=tool_calls+?, result=? WHERE task_id=?",
                               (now, now, model_used, tool_calls, text[:20000], task_id))
                    requested = task["model_override"] or task["model_configured"]
                    note = "" if model_used in (None, requested) else f" — note: ran on {model_used}, not {requested}"
                    self.store.event(db, "task.completed", f"Completed with {tool_calls} tool call(s){note}.",
                                     agent_id=agent_id, task_id=task_id, project_id=project_id,
                                     detail={"model_used": model_used})
                    mirror = TaskStatus.COMPLETED
                else:
                    reason = str(getattr(result, "reason", "") or "")
                    kind = _classify_provider_failure(reason + " " + text)
                    db.execute("UPDATE hub_tasks SET model_used=?, tool_calls=tool_calls+?, result=? WHERE task_id=?",
                               (model_used, tool_calls, text[:20000], task_id))
                    if kind is not None:
                        db.execute("UPDATE hub_tasks SET state='QUEUED', updated_at=? WHERE task_id=?", (now, task_id))
                        pending = (dict(task), _bounded(text, 400), kind)
                    else:
                        db.execute("UPDATE hub_tasks SET state='FAILED', finished_at=?, updated_at=?, error=?, "
                                   "error_kind='incomplete' WHERE task_id=?",
                                   (now, now, _bounded(reason or text, 500), task_id))
                        self.store.event(db, "task.failed", f"Did not complete: {_bounded(reason or text, 220)}",
                                         agent_id=agent_id, task_id=task_id, project_id=project_id)
                        mirror = TaskStatus.FAILED
        if locals().get("pending"):
            snapshot, reason, kind = pending  # type: ignore[name-defined]
            refreshed = self.task(task_id)
            self._wait_for_provider(refreshed, reason, kind=kind)
        if mirror is not None:
            self._mirror(task, mirror)
        self._queue_followups(task_id)

    def _queue_followups(self, task_id: str) -> None:
        """Follow-ups that arrived after the attempt's prompt was built run as a linked task."""
        task = self.task(task_id)
        pending = [s for s in task["steering"] if s["state"] == "PENDING"]
        if not pending or task["state"] != "COMPLETED":
            return
        agent = self.agent(task["agent_id"])
        body = "\n".join(f"- {s['body']}" for s in pending)
        followup = self._enqueue(agent, title=f"Follow-up: {task['title']}"[:120],
                                 request=f"Continue the previous task in this conversation with these follow-up "
                                         f"instructions from the operator:\n{body}",
                                 chat_id=task["chat_id"], model_override=task["model_override"],
                                 followup_of=task_id, actor="runtime")
        with self.store.tx() as db:
            for s in pending:
                db.execute("UPDATE hub_steering SET state='APPLIED', applied_task_id=?, applied_at=? WHERE steer_id=?",
                           (followup["task_id"], _now(), s["steer_id"]))
            self.store.event(db, "task.steer_applied",
                             f"{len(pending)} follow-up instruction(s) arrived during the run and were queued as "
                             f"task {followup['task_id']}.", agent_id=task["agent_id"], task_id=task_id,
                             project_id=task["project_id"])

    def _mirror(self, task: dict[str, Any], status: TaskStatus) -> None:
        if not task.get("runtime_task_id"):
            return
        try:
            with MultiAgentRuntimeStore(self.runtime_path) as runtime:
                current = runtime.get_task(task["runtime_task_id"]).status
                if current is status:
                    return
                runtime.update_task_status(task_id=task["runtime_task_id"], actor_id=task["agent_id"], status=status,
                                           idempotency_key=f"hub-{status.value}-{uuid.uuid4()}")
        except MultiAgentRuntimeError:
            pass  # the control-plane mirror is best-effort; hub_tasks is the execution record

    def _record_artifacts(self, task: dict[str, Any], before: dict[str, dict[str, Any]]) -> None:
        workspace = self._project_root(task["project_id"])
        after = snapshot_workspace(workspace)
        changes: list[tuple[str, str]] = []
        for rel, info in after.items():
            old = before.get(rel)
            if old is None:
                changes.append((rel, "created"))
            elif (old["size"], old["mtime"]) != (info["size"], info["mtime"]):
                changes.append((rel, "modified"))
        changes.extend((rel, "deleted") for rel in before if rel not in after)
        if not changes:
            return
        with self.store.tx() as db:
            for rel, change in sorted(changes)[:200]:
                path = workspace / rel
                data = None
                if change != "deleted":
                    try:
                        data = path.read_bytes() if path.stat().st_size <= MAX_STORED_BYTES else None
                    except OSError:
                        data = None
                sha = hashlib.sha256(data).hexdigest() if data is not None else None
                if change == "modified" and data is not None and before[rel].get("text") is not None:
                    try:
                        if data.decode("utf-8") == before[rel]["text"]:
                            continue  # touched but unchanged
                    except UnicodeDecodeError:
                        pass
                if data is not None:
                    blob = self.artifact_dir / sha[:2] / sha
                    if not blob.exists():
                        blob.parent.mkdir(exist_ok=True)
                        blob.write_bytes(data)
                diff = None
                old_text = (before.get(rel) or {}).get("text")
                if data is not None and _is_text_name(rel) and len(data) <= MAX_DIFF_BYTES:
                    try:
                        new_text = data.decode("utf-8")
                        diff = "".join(difflib.unified_diff(
                            (old_text or "").splitlines(keepends=True), new_text.splitlines(keepends=True),
                            fromfile=f"a/{rel}" if old_text is not None else "/dev/null", tofile=f"b/{rel}"))[:200_000]
                    except UnicodeDecodeError:
                        diff = None
                elif change == "deleted" and old_text is not None:
                    diff = "".join(difflib.unified_diff(old_text.splitlines(keepends=True), [],
                                                        fromfile=f"a/{rel}", tofile="/dev/null"))[:200_000]
                version = int(db.execute("SELECT COALESCE(MAX(version),0)+1 FROM hub_artifacts WHERE project_id=? AND path=?",
                                         (task["project_id"], rel)).fetchone()[0])
                artifact_id = _id("art")
                db.execute("INSERT INTO hub_artifacts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (artifact_id, task["task_id"], task["agent_id"], task["project_id"], rel, version, change,
                            sha, len(data) if data is not None else None, _mime(rel), int(data is not None), diff, _now()))
                self.store.event(db, "artifact.recorded", f"{change.title()} {rel} (version {version})",
                                 agent_id=task["agent_id"], task_id=task["task_id"], project_id=task["project_id"],
                                 detail={"artifact_id": artifact_id, "path": rel, "change": change})


def _classify_provider_failure(text: str) -> str | None:
    lowered = text.lower()
    if "not authenticated" in lowered or "sign in" in lowered or "login" in lowered or "oauth" in lowered:
        return "login"
    if "usage limit" in lowered or "rate limit" in lowered or "quota" in lowered or "capacity" in lowered:
        return "capacity"
    if "temporarily unavailable" in lowered or "provider unavailable" in lowered:
        return "availability"
    return None


# --------------------------------------------------------------------------- event recorder

_TOOL_SUMMARY_KEYS = ("path", "paths", "program", "arguments", "cwd", "query", "url", "pattern", "name")


class _EventRecorder:
    """Turns the agent's runtime events and real tool executions into readable Hub events."""

    def __init__(self, runtime: AgentRuntime, task: dict[str, Any]) -> None:
        self.runtime, self.task = runtime, task
        self.tool_calls = 0
        self._last_progress = 0.0

    def _event(self, kind: str, summary: str, detail: dict[str, Any] | None = None) -> None:
        with self.runtime.store.tx() as db:
            self.runtime.store.event(db, kind, summary, agent_id=self.task["agent_id"], task_id=self.task["task_id"],
                                     project_id=self.task["project_id"], detail=detail)

    def on_event(self, text: str) -> None:
        text = str(text)
        head = text.split(" - ", 1)[0].strip().lower()
        if head == "tool":
            return  # recorded with its real outcome by the ToolBox wrapper
        if head == "processing":
            if time.monotonic() - self._last_progress < 2.0:
                return
            self._last_progress = time.monotonic()
            self._event("progress", text.replace(" - ", " · "))
        elif head == "model":
            self._event("model.selected", text.replace(" - ", " · "))
        elif head == "failover":
            self._event("model.failover", "Provider fallback: " + text.split(" - ", 1)[-1].replace(" - ", " · "))
        elif head == "recovery":
            self._event("recovery", text.split(" - ", 1)[-1])
        elif head.startswith("verif"):
            self._event("verify", text.replace(" - ", " · "))
        else:
            self._event("progress", text.replace(" - ", " · "))

    def on_stream(self, _delta: str) -> None:
        # Streamed text is shown as the result when the task finishes; nothing to persist per chunk.
        return

    def wrap(self, toolbox: Any) -> None:
        original = toolbox.execute

        def execute(name: str, arguments: dict[str, Any]) -> str:
            started = time.monotonic()
            raw = original(name, arguments)
            self.tool_calls += 1
            ok, note = _tool_outcome(name, raw)
            shown = {k: _bounded(arguments.get(k), 160) for k in _TOOL_SUMMARY_KEYS if isinstance(arguments, dict) and arguments.get(k) not in (None, "", [])}
            target = shown.get("path") or shown.get("program") or shown.get("query") or shown.get("url") or shown.get("pattern") or ""
            self._event("tool", f"{name}{(' ' + target) if target else ''} → {'ok' if ok else 'error'}{(': ' + note) if note else ''}",
                        {"tool": name, "arguments": shown, "ok": ok, "note": note,
                         "seconds": round(time.monotonic() - started, 2)})
            return raw

        toolbox.execute = execute


def _tool_outcome(name: str, raw: str) -> tuple[bool, str]:
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return True, ""
    if not isinstance(payload, dict):
        return True, ""
    ok = bool(payload.get("ok", True)) and not payload.get("error")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if not ok:
        return False, _bounded(payload.get("error") or payload.get("message") or "failed", 160)
    if name == "run_process" and isinstance(data, dict) and "exit_code" in data:
        return data.get("exit_code") == 0, f"exit code {data.get('exit_code')}" + (" (timed out)" if data.get("timed_out") else "")
    if name == "web_search" and isinstance(data, dict) and isinstance(data.get("results"), list):
        return True, f"{len(data['results'])} results"
    return True, ""
