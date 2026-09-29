"""Hub agents that talk to each other: asks between agents, and moderated team rooms.

Why the other agent answers *inline*: the dispatcher runs one task per agent and one writer per
project. If agent A asked agent B by queueing a normal task for B, A would hold its task (and
its project's write lock) while waiting, and B's task could never start. So B answers inside
A's task, the way helper agents run, but as itself: its own runner, configuration, model,
effort, memory and **only its own permissions** (``AgentRuntime._inline_turn``). While B
answers it is registered in the runtime's running set, so the dispatcher treats B as busy and
its project's write lock as held.

Waiting and deadlocks:

* If B is busy with its own work (or its project is locked by unrelated work), the ask waits,
  with no time limit, until B is free; Stop cancels it.
* Every wait is an edge in a wait-for graph (``waiter -> agent it waits for``). An ask that would
  close a cycle is refused, which is how back-and-forth works: B does not ask A while A waits
  for B; B puts its question in its reply and A answers with another ask.

Threads: each pair of agents has one open thread; each side of it is a conversation in that
agent's own memory, so asking again continues with context (``new_thread`` starts a fresh one).

Rooms: a group discussion on a topic. It runs as a task of the chair (or inside the task of an
agent that started it); each member speaks in turn as an inline run with its own tools, the
chair last in each round, and the chair ends it with ``end_discussion(summary)``. The operator
can post into a room, stop it, and resume it after a stop or a restart. There is no round
limit; a room whose last two rounds added nothing new is asked once for the chair's summary
and then stops as ``stalled``.

Everything is stored in hub.db (threads, rooms, members, messages) and every ask, reply, room
message, refusal and stall is a Hub event.
"""

from __future__ import annotations

import difflib
import json
import re
import threading
import time
import uuid
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

POLL_SECONDS = 0.25
MAX_MESSAGE_CHARS = 12_000
MAX_REPLY_CHARS = 12_000
MAX_STORED_CHARS = 20_000
MAX_TOPIC_CHARS = 2_000
MAX_TITLE_CHARS = 120
MAX_SUMMARY_CHARS = 8_000
MAX_ROOM_MEMBERS = 8
MAX_VIEW_MESSAGES = 500
MAX_PROMPT_CHARS = 16_000
STALL_ROUNDS = 2
ROOM_STATES = ("running", "done", "stopped", "stalled", "interrupted")

SCHEMA = """
    -- Agent-to-agent threads: one open thread per pair; each side is a conversation in that
    -- agent's own memory (conversation_a / conversation_b).
    CREATE TABLE IF NOT EXISTS hub_team_threads(
        thread_id TEXT PRIMARY KEY, agent_a TEXT NOT NULL, agent_b TEXT NOT NULL,
        conversation_a INTEGER, conversation_b INTEGER, created_at REAL NOT NULL,
        updated_at REAL NOT NULL, closed_at REAL);
    CREATE INDEX IF NOT EXISTS hub_team_threads_a ON hub_team_threads(agent_a, closed_at);
    CREATE INDEX IF NOT EXISTS hub_team_threads_b ON hub_team_threads(agent_b, closed_at);
    -- Team rooms. An operator room runs as the chair's task (task_id); an agent-started room
    -- runs inside the task of the agent that started it.
    CREATE TABLE IF NOT EXISTS hub_team_rooms(
        room_id TEXT PRIMARY KEY, title TEXT NOT NULL, topic TEXT NOT NULL,
        goal TEXT NOT NULL DEFAULT '', chair_id TEXT NOT NULL,
        origin TEXT NOT NULL CHECK(origin IN ('operator','agent')), task_id TEXT,
        state TEXT NOT NULL CHECK(state IN ('running','done','stopped','stalled','interrupted')),
        round INTEGER NOT NULL DEFAULT 0, stall_rounds INTEGER NOT NULL DEFAULT 0, summary TEXT,
        speaking TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL, finished_at REAL);
    CREATE INDEX IF NOT EXISTS hub_team_rooms_task ON hub_team_rooms(task_id);
    CREATE TABLE IF NOT EXISTS hub_team_members(
        room_id TEXT NOT NULL, agent_id TEXT NOT NULL, position INTEGER NOT NULL,
        conversation_id INTEGER, last_seen INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(room_id, agent_id));
    CREATE TABLE IF NOT EXISTS hub_team_messages(
        seq INTEGER PRIMARY KEY AUTOINCREMENT, message_id TEXT NOT NULL UNIQUE,
        thread_id TEXT, room_id TEXT,
        sender_kind TEXT NOT NULL CHECK(sender_kind IN ('agent','operator','system')),
        sender_agent_id TEXT, body TEXT NOT NULL, task_id TEXT, round INTEGER,
        state TEXT NOT NULL, created_at REAL NOT NULL);
    CREATE INDEX IF NOT EXISTS hub_team_messages_thread ON hub_team_messages(thread_id, seq);
    CREATE INDEX IF NOT EXISTS hub_team_messages_room ON hub_team_messages(room_id, seq);
"""

LIST_AGENTS_DESCRIPTION = (
    "List the operator's other agents you can talk to: name, id, role, purpose, model, what each may do, "
    "and whether it is idle or busy right now."
)
ASK_AGENT_DESCRIPTION = (
    "Send a message to another of the operator's agents (by name or id) and wait for its reply. It answers "
    "with its own tools, access and memory; asking it again continues the same conversation (new_thread=true "
    "starts a fresh one). If it is busy with its own work this waits until it is free. Use it when another "
    "agent has the information, access or skills the job needs. If its reply asks you something, answer "
    "with another ask_agent."
)
ASK_AGENT_PARAMETERS = {"type": "object", "properties": {
    "agent": {"type": "string", "maxLength": 120, "description": "The other agent's name or id (see list_agents)."},
    "message": {"type": "string", "maxLength": MAX_MESSAGE_CHARS},
    "new_thread": {"type": "boolean", "description": "Start a fresh conversation instead of continuing."}},
    "required": ["agent", "message"], "additionalProperties": False}
START_DISCUSSION_DESCRIPTION = (
    "Hold a moderated team discussion with other agents (names or ids) on a topic, with you as chair: each "
    "speaks in turn with its own tools, you speak last in each round and close it with a summary. Returns "
    "the summary and the room id. Use it when a decision needs several agents' knowledge or views."
)
START_DISCUSSION_PARAMETERS = {"type": "object", "properties": {
    "members": {"type": "array", "minItems": 1, "maxItems": MAX_ROOM_MEMBERS - 1,
                "items": {"type": "string", "maxLength": 120}},
    "topic": {"type": "string", "maxLength": MAX_TOPIC_CHARS},
    "goal": {"type": "string", "maxLength": MAX_TOPIC_CHARS}},
    "required": ["members", "topic"], "additionalProperties": False}
END_DISCUSSION_DESCRIPTION = (
    "Close this team discussion (you chair it) with the final summary: decisions, answers, open questions "
    "and who does what next."
)
END_DISCUSSION_PARAMETERS = {"type": "object", "properties": {
    "summary": {"type": "string", "maxLength": MAX_SUMMARY_CHARS}}, "required": ["summary"],
    "additionalProperties": False}

_AGREEMENT = re.compile(
    r"\b(?:agree[sd]?|sounds good|looks good|lgtm|makes sense|same here|concur|fine by me|good plan|"
    r"nothing (?:new|more|further|else)|nothing to add|no (?:further|more|new) (?:input|comments?|points?|thoughts?)|"
    r"thanks?|thank you|okay|ok)\b")
_SUBSTANCE = re.compile(r"\d|https?://|`|\?|[\\/][\w.-]+\.\w{1,5}\b")


class TeamError(ValueError):
    """A refused ask or room request, in words the agent or operator can act on."""


class TeamStopped(RuntimeError):
    """The wait or turn was stopped (the operator, or the task's own Stop)."""


class TurnResult(str):
    """What a room run hands back to the task executor: its text plus a status."""

    def __new__(cls, text: str, status: str = "complete") -> TurnResult:
        value = super().__new__(cls, text)
        value.status = status
        value.tool_calls = 0
        return value


def _now() -> float:
    return time.time()


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _clip(text: Any, limit: int) -> str:
    value = str(text or "")
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _one_line(text: Any, limit: int = 160) -> str:
    return _clip(" ".join(str(text or "").split()), limit)


def _normal(text: Any) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", str(text or "").casefold()).split())


def is_new(text: str, earlier: list[str]) -> bool:
    """False for an empty reply, a short agreement with no substance (no numbers, links, code,
    file names or questions), or a near-duplicate of an earlier message."""
    body = _normal(text)
    if not body:
        return False
    if len(body) <= 300 and _AGREEMENT.search(body) and not _SUBSTANCE.search(str(text)):
        return False
    for previous in earlier[-40:]:
        other = _normal(previous)
        if not other:
            continue
        matcher = difflib.SequenceMatcher(None, body[:1200], other[:1200], autojunk=False)
        if matcher.real_quick_ratio() >= 0.85 and matcher.quick_ratio() >= 0.85 and matcher.ratio() >= 0.85:
            return False
    return True


class TeamService:
    """Asks, threads and rooms. Registries are guarded by the runtime's lock."""

    def __init__(self, runtime: Any) -> None:
        self.rt = runtime
        self._edges: dict[str, str] = {}        # waiting agent -> the agent it waits for
        self._pending: dict[str, dict[str, Any]] = {}  # root task id -> approval raised inside it
        self._asks: dict[str, dict[str, Any]] = {}     # thread id -> the ask in flight
        self._rooms: dict[str, dict[str, Any]] = {}    # room id -> running room controls

    # ------------------------------------------------------------------ storage
    @staticmethod
    def recover(db: Any, event_in: Callable[..., Any]) -> None:
        """At start-up: a room that was running when the Hub stopped is interrupted (resumable)."""
        for row in db.execute("SELECT room_id, chair_id, title FROM hub_team_rooms WHERE state='running'").fetchall():
            db.execute("UPDATE hub_team_rooms SET state='interrupted', speaking=NULL, updated_at=? WHERE room_id=?",
                       (_now(), row["room_id"]))
            event_in(db, row["chair_id"], None, None, "team", "warn",
                     f"Team room “{_one_line(row['title'], 80)}” interrupted by a restart — resume it to continue",
                     {"room_id": row["room_id"]})

    def _add(self, db: Any, *, thread_id: str | None = None, room_id: str | None = None, kind: str = "agent",
             sender: str | None = None, body: str, task_id: str | None = None, round_no: int | None = None,
             state: str = "sent") -> tuple[str, int]:
        message_id = _id("tmsg")
        cursor = db.execute(
            "INSERT INTO hub_team_messages(message_id, thread_id, room_id, sender_kind, sender_agent_id, body,"
            " task_id, round, state, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (message_id, thread_id, room_id, kind, sender, _clip(body, MAX_STORED_CHARS), task_id, round_no, state,
             _now()))
        if thread_id:
            db.execute("UPDATE hub_team_threads SET updated_at=? WHERE thread_id=?", (_now(), thread_id))
        if room_id:
            db.execute("UPDATE hub_team_rooms SET updated_at=? WHERE room_id=?", (_now(), room_id))
        return message_id, int(cursor.lastrowid)

    def add_message(self, **fields: Any) -> tuple[str, int]:
        with self.rt.db() as db:
            return self._add(db, **fields)

    # ------------------------------------------------------------------- agents
    def _agents(self) -> dict[str, Any]:
        from .multi_agent_runtime import MultiAgentRuntimeStore

        with MultiAgentRuntimeStore(self.rt.runtime_path) as store:
            rows = store.db.execute("SELECT * FROM runtime_agents ORDER BY created_at, agent_id").fetchall()
            return {row["agent_id"]: store._agent_from_row(row) for row in rows}

    def _busy(self, agent_id: str) -> bool:
        with self.rt._lock:
            return any(r["agent_id"] == agent_id for r in self.rt._running.values())

    def _refusal(self, agent: Any) -> str | None:
        from .multi_agent_runtime import AgentLifecycle

        settings = self.rt.agent_settings(agent.agent_id)
        if settings["archived"]:
            return f"{agent.display_name} is archived."
        if agent.lifecycle is not AgentLifecycle.RUNNING:
            return f"{agent.display_name} is not enabled right now."
        if not settings["permissions"].get("team"):
            return f"{agent.display_name} does not take part in team work (its Team ability is off)."
        return None

    def _resolve(self, value: Any, agents: dict[str, Any], caller_id: str | None) -> Any:
        if not isinstance(value, str) or not value.strip() or len(value) > 120:
            raise TeamError("Name the other agent by its name or id (see list_agents).")
        wanted = " ".join(value.split())
        found = agents.get(wanted)
        if found is None:
            matches = [a for a in agents.values() if a.display_name.casefold() == wanted.casefold()]
            if len(matches) > 1:
                raise TeamError(f"More than one agent is called {wanted}; use its id from list_agents.")
            found = matches[0] if matches else None
        if found is None:
            names = ", ".join(sorted(a.display_name for a in agents.values() if a.agent_id != caller_id))[:400]
            raise TeamError(f"There is no agent called {_one_line(wanted, 60)}. The others are: {names or 'none'}.")
        if found.agent_id == caller_id:
            raise TeamError("That is you; name another agent.")
        return found

    def list_agents(self, caller_id: str) -> list[dict[str, Any]]:
        from .agent_hub_runtime import PERMISSION_LABELS
        from .multi_agent_runtime import AgentLifecycle

        result = []
        for agent in self._agents().values():
            if agent.agent_id == caller_id:
                continue
            settings = self.rt.agent_settings(agent.agent_id)
            if settings["archived"] or agent.lifecycle is AgentLifecycle.STOPPED:
                continue
            permissions = settings["permissions"]
            status = ("busy" if self._busy(agent.agent_id) else "idle") if agent.lifecycle is AgentLifecycle.RUNNING \
                else "paused" if agent.lifecycle is AgentLifecycle.PAUSED else "not enabled"
            result.append({"agent_id": agent.agent_id, "name": agent.display_name, "role": agent.role,
                           "purpose": _one_line(agent.purpose, 300), "model": f"{agent.model_provider} · {agent.model_name}",
                           "abilities": [PERMISSION_LABELS[k] for k, v in permissions.items() if v and k in PERMISSION_LABELS],
                           "status": status, "accepts_messages": bool(permissions.get("team"))
                           and agent.lifecycle is AgentLifecycle.RUNNING})
        return result

    # ---------------------------------------------------------- waits and cycles
    def _reaches(self, start: str, goal: str) -> bool:
        node, seen = start, set()
        while node not in seen:
            if node == goal:
                return True
            seen.add(node)
            if node not in self._edges:
                return False
            node = self._edges[node]
        return False

    def _acquire(self, waiter: Any, speaker: Any, ctx: dict[str, Any], guard: Callable[[], bool],
                 on_wait: Callable[[str], None], label: str) -> tuple[str, dict[str, Any]]:
        """Wait (no time limit, Stop cancels) until the speaker is free and its project's write lock
        is free for this chain; then register the inline run so dispatch sees the speaker busy."""
        from .agent_hub_runtime import WRITE_PERMISSIONS

        rt, chain = self.rt, ctx["chain_id"]
        target, waiting = speaker.agent_id, waiter.agent_id
        project = speaker.project_id or "command-center"
        writes = any(rt.agent_settings(target)["permissions"].get(k) for k in WRITE_PERMISSIONS)
        announced = None
        try:
            while True:
                with rt._lock:
                    if self._reaches(target, waiting):
                        raise TeamError(f"{speaker.display_name} is waiting for your answer: put your question in "
                                        "your reply instead.")
                    busy = [r for r in rt._running.values() if r["agent_id"] == target]
                    holders = [r for key, r in rt._running.items() if writes and r.get("writes")
                               and r["project_id"] == project and (r.get("chain") or key) != chain]
                    blocker = busy[0]["agent_id"] if busy else holders[0]["agent_id"] if holders else target
                    if blocker != target and self._reaches(blocker, waiting):
                        raise TeamError(f"{speaker.display_name}'s project is locked by work that is waiting for "
                                        "you; waiting now would deadlock. Put your question in your reply.")
                    self._edges[waiting] = blocker
                    if not busy and not holders:
                        key = f"{chain}~{target}~{uuid.uuid4().hex[:8]}"
                        record = {"task_id": ctx["root"]["task_id"], "agent_id": target, "project_id": project,
                                  "writes": writes, "cancel": ctx["record"]["cancel"], "intent": None,
                                  "chain": chain, "inline": True, "label": label,
                                  "thread": threading.current_thread()}
                        self._edges[waiting] = target
                        rt._running[key] = record
                        return key, record
                reason = "busy" if busy else "locked"
                if reason != announced:
                    on_wait(reason)
                    announced = reason
                if guard():
                    raise TeamStopped("Stopped while waiting.")
                time.sleep(POLL_SECONDS)
        except BaseException:
            with rt._lock:
                self._edges.pop(waiting, None)
            raise

    def _release(self, key: str | None, waiter_id: str) -> None:
        with self.rt._lock:
            if key:
                self.rt._running.pop(key, None)
            self._edges.pop(waiter_id, None)

    def inline_label(self, agent_id: str) -> str | None:
        with self.rt._lock:
            return next((r.get("label") for r in self.rt._running.values()
                         if r.get("inline") and r["agent_id"] == agent_id), None)

    # ------------------------------------------------------------------ turns
    def _turn(self, *, waiter: Any, speaker: Any, prompt: str, brief: str, conversation_id: int | None,
              title: str, ctx: dict[str, Any], guard: Callable[[], bool], where: dict[str, str],
              own: bool = False, memory: Any = None, extra_tools: dict[str, Any] | None = None,
              label: str = "") -> dict[str, Any]:
        """One inline turn of ``speaker`` inside the chain's task, with its own permissions."""
        root = ctx["root"]
        key = None

        def on_wait(reason: str) -> None:
            what = (f"Waiting for {speaker.display_name} to finish its current task" if reason == "busy" else
                    f"Waiting for the write lock on {speaker.display_name}'s project")
            self.rt._set(root["task_id"], progress=_one_line(what, 120))
            self.rt.event(waiter.agent_id, root["task_id"], root["project_id"], "team", what,
                          detail={**where, "peer_agent_id": speaker.agent_id, "waiting": reason})

        try:
            if own:
                record = ctx["record"]
            else:
                key, record = self._acquire(waiter, speaker, ctx, guard, on_wait, label)
            child = {"root": root, "record": record, "chain_id": ctx["chain_id"], "cancel": guard}
            return self.rt._inline_turn(
                root_task=root, record=record, agent_id=speaker.agent_id, prompt=prompt, brief=brief,
                conversation_id=conversation_id, title=title, cancel=guard, team=child,
                event_detail={**where, "as_agent": speaker.agent_id}, extra_tools=extra_tools, memory=memory,
                approval=lambda approval_id, conversation: self._await_approval(
                    ctx, speaker, approval_id, conversation, where, guard))
        except TeamStopped:
            return {"status": "stopped", "reply": "", "conversation_id": conversation_id, "changes": 0}
        finally:
            if key is not None:
                self._release(key, waiter.agent_id)

    def _await_approval(self, ctx: dict[str, Any], speaker: Any, approval_id: Any, conversation_id: Any,
                        where: dict[str, str], guard: Callable[[], bool]) -> bool | None:
        """A sensitive step of an inline turn: wait for the operator's decision (True/False), or None if stopped."""
        if not isinstance(approval_id, int):
            return False
        root = ctx["root"]
        decided = threading.Event()
        entry = {"agent_id": speaker.agent_id, "approval_id": approval_id, "conversation_id": conversation_id,
                 "event": decided, "approved": None, **where}
        with self.rt._lock:
            self._pending[root["task_id"]] = entry
        what = f"{speaker.display_name} needs your approval (#{approval_id})"
        with self.rt.db() as db:
            self._add(db, thread_id=where.get("thread_id"), room_id=where.get("room_id"), kind="system",
                      body=what + ". Approve or deny it to continue.", task_id=root["task_id"], state="approval")
            self.rt._event_in(db, speaker.agent_id, root["task_id"], root["project_id"], "approval", "warn", what,
                              {**where, "approval_id": approval_id, "agent_id": speaker.agent_id})
        self.rt._set(root["task_id"], progress=_one_line(f"Waiting for your approval of {speaker.display_name}'s "
                                                          f"action (#{approval_id})", 120))
        try:
            while not decided.wait(POLL_SECONDS):
                if guard():
                    return None
            # Set without a decision when the thread or room was stopped.
            return None if entry["approved"] is None or guard() else bool(entry["approved"])
        finally:
            with self.rt._lock:
                if self._pending.get(root["task_id"]) is entry:
                    self._pending.pop(root["task_id"], None)

    def pending(self, task_id: str) -> dict[str, Any] | None:
        with self.rt._lock:
            entry = self._pending.get(task_id)
            return None if entry is None else {k: v for k, v in entry.items() if k not in {"event", "approved"}}

    def decide(self, task_id: str, approve: bool) -> dict[str, Any]:
        """The operator's decision on an approval raised inside an ask or a room turn."""
        from .agent_hub_runtime import TaskError

        with self.rt._lock:
            entry = self._pending.get(task_id)
        if entry is None:
            raise TaskError("Nothing in this task is waiting for an approval.")
        path = self.rt.state_dir / "agents" / entry["agent_id"] / "data"
        memory = self.rt._open_memory(SimpleNamespace(data_dir=path))
        try:
            approval = next((r for r in memory.list_approvals(limit=200)
                             if int(r.get("id", -1)) == int(entry["approval_id"])), None)
            memory.decide_approval(int(entry["approval_id"]), bool(approve))
        finally:
            closer = getattr(memory, "close", None)
            if callable(closer):
                closer()
        tool = ""
        if approve and approval and approval.get("action") == "after_web_content" and entry.get("conversation_id"):
            try:
                tool = str(json.loads(str(approval.get("resource") or "{}")).get("tool") or "")
            except (TypeError, ValueError):
                tool = ""
        with self.rt.db() as db:
            if tool:
                db.execute("INSERT OR REPLACE INTO hub_taint_grants VALUES (?,?,?,?,?)",
                           (entry["agent_id"], int(entry["conversation_id"]), tool, _now(), _now() + 24 * 3600))
            self.rt._event_in(db, entry["agent_id"], task_id, None, "approval", "info" if approve else "warn",
                              f"Approval #{entry['approval_id']} {'granted' if approve else 'denied'}",
                              {k: entry.get(k) for k in ("thread_id", "room_id", "agent_id")})
        entry["approved"] = bool(approve)
        entry["event"].set()
        return {"task_id": task_id, "approval_id": entry["approval_id"], "approved": bool(approve)}

    def approval_detail(self, task_id: str) -> dict[str, Any] | None:
        entry = self.pending(task_id)
        if entry is None:
            return None
        path = self.rt.state_dir / "agents" / entry["agent_id"] / "data"
        memory = self.rt._open_memory(SimpleNamespace(data_dir=path))
        try:
            rows = memory.list_approvals(limit=200)
        finally:
            closer = getattr(memory, "close", None)
            if callable(closer):
                closer()
        match = next((r for r in rows if int(r.get("id", -1)) == int(entry["approval_id"])), None) or {}
        agent = self._agents().get(entry["agent_id"])
        return {"id": entry["approval_id"], "action": _clip(match.get("action") or "unknown", 100),
                "resource": _clip(match.get("resource") or "", 600), "reason": _clip(match.get("reason") or "", 400),
                "status": match.get("status"), "inline": True, "agent_id": entry["agent_id"],
                "agent_name": agent.display_name if agent else entry["agent_id"],
                "thread_id": entry.get("thread_id"), "room_id": entry.get("room_id")}

    # -------------------------------------------------------------------- tools
    def install_tools(self, toolbox: Any, *, agent_id: str, memory: Any, team: dict[str, Any]) -> None:
        from .tools import Tool

        toolbox.tools["list_agents"] = Tool(
            "list_agents", LIST_AGENTS_DESCRIPTION, {"type": "object", "properties": {}, "additionalProperties": False},
            lambda: {"agents": self.list_agents(agent_id)})
        toolbox.tools["ask_agent"] = Tool(
            "ask_agent", ASK_AGENT_DESCRIPTION, ASK_AGENT_PARAMETERS,
            lambda agent, message, new_thread=False: self.ask(caller_id=agent_id, target=agent, message=message,
                                                              new_thread=new_thread, memory=memory, ctx=team))
        toolbox.tools["start_team_discussion"] = Tool(
            "start_team_discussion", START_DISCUSSION_DESCRIPTION, START_DISCUSSION_PARAMETERS,
            lambda members, topic, goal=None: self.start_discussion(chair_id=agent_id, members=members, topic=topic,
                                                                    goal=goal, memory=memory, ctx=team))

    @staticmethod
    def _text(value: Any, what: str, limit: int, *, allow_empty: bool = False) -> str:
        if value is None and allow_empty:
            return ""
        if not isinstance(value, str):
            raise TeamError(f"The {what} must be text.")
        text = value.replace("\r\n", "\n").strip()
        if (not text and not allow_empty) or len(text) > limit:
            raise TeamError(f"The {what} must be 1-{limit:,} characters.")
        if any(ord(c) < 32 and c not in "\n\t" for c in text):
            raise TeamError(f"The {what} must not contain control characters.")
        if text:
            from .subscription_chat import release_operator_text

            try:
                release_operator_text(text)
            except PermissionError:
                raise TeamError(f"The {what} looks like it contains a password, key or other secret; it was not "
                                "sent.") from None
        return text

    @staticmethod
    def _screened(text: str) -> str:
        """Text passed from one agent to another (a reply, a room message) goes through the same
        secret screen as the operator's messages: agents may use different model providers."""
        if not text.strip():
            return text
        from .subscription_chat import release_operator_text

        try:
            release_operator_text(text)
        except PermissionError:
            return "[Withheld: this message looked like it contained a password, key or other secret.]"
        except ValueError:
            return _clip(text, MAX_STORED_CHARS)
        return text

    # --------------------------------------------------------------------- asks
    def _open_thread(self, a: str, b: str, fresh: bool) -> dict[str, Any]:
        now = _now()
        with self.rt.db() as db:
            row = db.execute("SELECT * FROM hub_team_threads WHERE closed_at IS NULL AND ((agent_a=? AND agent_b=?)"
                             " OR (agent_a=? AND agent_b=?)) ORDER BY created_at DESC LIMIT 1", (a, b, b, a)).fetchone()
            if row is not None and fresh:
                db.execute("UPDATE hub_team_threads SET closed_at=? WHERE thread_id=?", (now, row["thread_id"]))
                row = None
            if row is None:
                thread_id = _id("thread")
                db.execute("INSERT INTO hub_team_threads(thread_id, agent_a, agent_b, created_at, updated_at)"
                           " VALUES (?,?,?,?,?)", (thread_id, a, b, now, now))
                row = db.execute("SELECT * FROM hub_team_threads WHERE thread_id=?", (thread_id,)).fetchone()
            return dict(row)

    @staticmethod
    def _side(thread: dict[str, Any], agent_id: str) -> str:
        return "conversation_a" if thread["agent_a"] == agent_id else "conversation_b"

    def ask(self, *, caller_id: str, target: Any, message: Any, new_thread: Any, memory: Any,
            ctx: dict[str, Any]) -> dict[str, Any]:
        agents = self._agents()
        caller = agents.get(caller_id)
        if caller is None:
            raise TeamError("Unknown asking agent.")
        peer = self._resolve(target, agents, caller_id)
        if not isinstance(new_thread, bool):
            raise TeamError("new_thread must be true or false.")
        root = ctx["root"]
        refusal = self._refusal(peer)
        if refusal is None:
            with self.rt._lock:
                if self._reaches(peer.agent_id, caller_id):
                    refusal = f"{peer.display_name} is waiting for your answer: put your question in your reply instead."
        if refusal:
            self.rt.event(caller_id, root["task_id"], root["project_id"], "team",
                          f"Ask to {peer.display_name} refused: {refusal}", level="warn",
                          detail={"peer_agent_id": peer.agent_id, "refused": True})
            raise TeamError(refusal)
        text = self._text(message, "message", MAX_MESSAGE_CHARS)
        thread = self._open_thread(caller_id, peer.agent_id, new_thread)
        thread_id = thread["thread_id"]
        where = {"thread_id": thread_id}
        with self.rt.db() as db:
            self._add(db, thread_id=thread_id, sender=caller_id, body=text, task_id=root["task_id"], state="sent")
            self.rt._event_in(db, caller_id, root["task_id"], root["project_id"], "team", "info",
                              f"{caller.display_name} asked {peer.display_name}: {_one_line(text, 200)}",
                              {**where, "peer_agent_id": peer.agent_id})
        stop = threading.Event()
        with self.rt._lock:
            self._asks[thread_id] = {"stop": stop, "task_id": root["task_id"], "asker": caller_id, "peer": peer.agent_id}

        def guard() -> bool:
            return stop.is_set() or bool(ctx["cancel"]())

        brief = (f"This turn is a message from your fellow agent “{caller.display_name}” ({caller.role}), not from "
                 "the operator. Answer it for them: your reply is returned to them as the result of their "
                 "ask_agent call, and the operator can read this thread. Use your own tools and knowledge. If you need "
                 f"something from {caller.display_name}, ask it in your reply; they are waiting for you, so do not "
                 "call ask_agent back to them.")
        try:
            outcome = self._turn(waiter=caller, speaker=peer, prompt=text, brief=brief,
                                 conversation_id=thread[self._side(thread, peer.agent_id)],
                                 title=f"Thread with {caller.display_name}", ctx=ctx, guard=guard, where=where,
                                 label=f"Answering {caller.display_name}")
        except TeamError as exc:
            with self.rt.db() as db:
                self._add(db, thread_id=thread_id, kind="system", body=str(exc), task_id=root["task_id"], state="refused")
                self.rt._event_in(db, caller_id, root["task_id"], root["project_id"], "team", "warn",
                                  f"Ask to {peer.display_name} refused: {_one_line(exc, 200)}",
                                  {**where, "peer_agent_id": peer.agent_id, "refused": True})
            raise
        finally:
            with self.rt._lock:
                if self._asks.get(thread_id, {}).get("stop") is stop:
                    self._asks.pop(thread_id, None)
        reply = self._screened(_clip(outcome.get("reply") or "", MAX_REPLY_CHARS))
        status = outcome.get("status") or "failed"
        if status == "stopped":
            reply = reply or "The ask was stopped before an answer."
        elif status == "denied":
            reply = reply or f"You denied {peer.display_name}'s requested action."
        mine = self._record_side(memory, thread, caller_id, peer, text, reply)
        with self.rt.db() as db:
            side = self._side(thread, peer.agent_id)
            db.execute(f"UPDATE hub_team_threads SET {side}=COALESCE(?, {side}), "
                       f"{self._side(thread, caller_id)}=COALESCE(?, {self._side(thread, caller_id)}) WHERE thread_id=?",
                       (outcome.get("conversation_id"), mine, thread_id))
            self._add(db, thread_id=thread_id, sender=peer.agent_id, body=reply or "(no reply)",
                      task_id=root["task_id"], state={"complete": "answered"}.get(status, status))
            self.rt._event_in(db, peer.agent_id, root["task_id"], root["project_id"], "team",
                              "info" if status == "complete" else "warn",
                              f"{peer.display_name} replied to {caller.display_name}: {_one_line(reply, 200)}"
                              if status == "complete" else f"{peer.display_name}'s answer {status}: {_one_line(reply, 160)}",
                              {**where, "peer_agent_id": caller_id, "status": status})
        return {"agent": peer.display_name, "agent_id": peer.agent_id, "thread_id": thread_id,
                "status": {"complete": "answered"}.get(status, status), "reply": reply,
                "note": "This is the other agent's own answer; check key facts before relying on them."}

    @staticmethod
    def _record_side(memory: Any, thread: dict[str, Any], caller_id: str, peer: Any, question: str,
                     reply: str) -> int | None:
        """The asker's side of the thread in its own memory: its question and the reply."""
        if memory is None or not all(hasattr(memory, m) for m in ("new_conversation", "add_message",
                                                                     "conversation_exists")):
            return None
        side = "conversation_a" if thread["agent_a"] == caller_id else "conversation_b"
        conversation = thread.get(side)
        try:
            if conversation is None or not memory.conversation_exists(int(conversation)):
                conversation = int(memory.new_conversation(f"Thread with {peer.display_name}"))
            memory.add_message(int(conversation), "assistant", f"(to {peer.display_name}) {question}")
            memory.add_message(int(conversation), "user", f"({peer.display_name} replied) {reply}")
        except Exception:  # noqa: BLE001 - the asker's own record is best effort; the thread is stored
            return None
        return int(conversation)

    # ------------------------------------------------------------------ threads
    def threads(self, agent_id: str) -> list[dict[str, Any]]:
        agents = self._agents()
        with self.rt.db() as db:
            rows = db.execute("SELECT * FROM hub_team_threads WHERE agent_a=? OR agent_b=? ORDER BY updated_at DESC"
                              " LIMIT 200", (agent_id, agent_id)).fetchall()
            stats = {}
            for row in rows:
                last = db.execute("SELECT body, created_at FROM hub_team_messages WHERE thread_id=? ORDER BY seq DESC"
                                  " LIMIT 1", (row["thread_id"],)).fetchone()
                count = db.execute("SELECT COUNT(*) FROM hub_team_messages WHERE thread_id=?",
                                   (row["thread_id"],)).fetchone()[0]
                stats[row["thread_id"]] = (last, count)
        result = []
        with self.rt._lock:
            active = set(self._asks)
        for row in rows:
            peer_id = row["agent_b"] if row["agent_a"] == agent_id else row["agent_a"]
            last, count = stats[row["thread_id"]]
            peer = agents.get(peer_id)
            result.append({"thread_id": row["thread_id"],
                           "peer": {"agent_id": peer_id, "name": peer.display_name if peer else peer_id},
                           "last_at": last["created_at"] if last else row["updated_at"],
                           "last_preview": _one_line(last["body"] if last else "", 160), "count": count,
                           "active": row["thread_id"] in active, "closed": row["closed_at"] is not None})
        return result

    def thread(self, thread_id: str) -> dict[str, Any]:
        from .agent_hub_runtime import TaskError

        with self.rt.db() as db:
            row = db.execute("SELECT * FROM hub_team_threads WHERE thread_id=?", (str(thread_id),)).fetchone()
            if row is None:
                raise TaskError("Unknown thread.")
            messages = db.execute("SELECT * FROM (SELECT * FROM hub_team_messages WHERE thread_id=? ORDER BY seq DESC"
                                  " LIMIT ?) ORDER BY seq", (row["thread_id"], MAX_VIEW_MESSAGES)).fetchall()
        agents = self._agents()

        def who(agent_id: str) -> dict[str, str]:
            agent = agents.get(agent_id)
            return {"agent_id": agent_id, "name": agent.display_name if agent else agent_id}

        with self.rt._lock:
            ask = self._asks.get(row["thread_id"])
        approval = None
        if ask is not None and (self.pending(ask["task_id"]) or {}).get("thread_id") == row["thread_id"]:
            approval = self.approval_detail(ask["task_id"])
        return {"thread_id": row["thread_id"], "a": who(row["agent_a"]), "b": who(row["agent_b"]),
                "messages": [{"message_id": m["message_id"], "sender_agent_id": m["sender_agent_id"],
                              "kind": m["sender_kind"], "body": m["body"], "task_id": m["task_id"],
                              "at": m["created_at"], "state": m["state"]} for m in messages],
                "active": ask is not None, "task_id": ask["task_id"] if ask else None,
                "closed": row["closed_at"] is not None, "approval": approval}

    def stop_thread(self, thread_id: str) -> dict[str, Any]:
        from .agent_hub_runtime import TaskError

        with self.rt._lock:
            ask = self._asks.get(str(thread_id))
        if ask is None:
            raise TaskError("Nothing is in flight on this thread.")
        ask["stop"].set()
        with self.rt._lock:
            entry = self._pending.get(ask["task_id"])
        if entry is not None and entry.get("thread_id") == str(thread_id):
            entry["event"].set()  # an approval wait ends too (as stopped, not approved)
        self.rt.event(ask["asker"], ask["task_id"], None, "team", "You stopped an ask between agents", level="warn",
                      detail={"thread_id": str(thread_id)})
        return {"thread_id": str(thread_id), "stopping": True}

    def counts(self, agent_id: str) -> dict[str, int]:
        with self.rt.db() as db:
            threads = db.execute("SELECT COUNT(*) FROM hub_team_threads WHERE agent_a=? OR agent_b=?",
                                 (agent_id, agent_id)).fetchone()[0]
            rooms = db.execute("SELECT COUNT(*) FROM hub_team_rooms r JOIN hub_team_members m ON m.room_id=r.room_id"
                               " WHERE m.agent_id=? AND r.state='running'", (agent_id,)).fetchone()[0]
        return {"threads": int(threads), "rooms_active": int(rooms)}

    # -------------------------------------------------------------------- rooms
    def _members(self, value: Any, agents: dict[str, Any], *, minimum: int) -> list[Any]:
        if not isinstance(value, list) or not minimum <= len(value) <= MAX_ROOM_MEMBERS:
            raise TeamError(f"A room has {minimum}-{MAX_ROOM_MEMBERS} members.")
        members = [self._resolve(v, agents, None) for v in value]
        if len({m.agent_id for m in members}) != len(members):
            raise TeamError("Each agent can join a room once.")
        for member in members:
            refusal = self._refusal(member)
            if refusal:
                raise TeamError(refusal)
        return members

    def create_room(self, payload: dict[str, Any]) -> dict[str, Any]:
        """The operator starts a room: it runs as a task of the chair (default: the first member)."""
        if not isinstance(payload, dict) or set(payload) - {"title", "topic", "goal", "member_ids", "chair_id"}:
            raise TeamError("A room takes topic, member_ids and optional title, goal and chair_id.")
        agents = self._agents()
        topic = self._text(payload.get("topic"), "topic", MAX_TOPIC_CHARS)
        goal = self._text(payload.get("goal"), "goal", MAX_TOPIC_CHARS, allow_empty=True)
        title = " ".join(self._text(payload.get("title"), "title", MAX_TITLE_CHARS, allow_empty=True).split()) \
            or _one_line(topic, 60)
        members = self._members(payload.get("member_ids"), agents, minimum=2)
        chair_id = payload.get("chair_id") or members[0].agent_id
        if chair_id not in {m.agent_id for m in members}:
            raise TeamError("The chair must be one of the members.")
        room_id = _id("room")
        ordered = [m for m in members if m.agent_id != chair_id] + [agents[chair_id]]

        def link(db: Any, task_id: str) -> None:
            now = _now()
            db.execute("INSERT INTO hub_team_rooms(room_id, title, topic, goal, chair_id, origin, task_id, state,"
                       " created_at, updated_at) VALUES (?,?,?,?,?,'operator',?,'running',?,?)",
                       (room_id, title, topic, goal, chair_id, task_id, now, now))
            for position, member in enumerate(ordered):
                db.execute("INSERT INTO hub_team_members(room_id, agent_id, position) VALUES (?,?,?)",
                           (room_id, member.agent_id, position))
            self.rt._event_in(db, chair_id, task_id, agents[chair_id].project_id, "team", "info",
                              f"Team room “{_one_line(title, 80)}” started with "
                              + ", ".join(m.display_name for m in ordered), {"room_id": room_id})

        self.rt.create_task(chair_id, title=f"Team room · {title}",
                            request=f"Chair the team discussion “{title}”: {_one_line(topic, 400)}", _after_insert=link)
        return self.room(room_id)

    def room_for_task(self, task_id: str) -> str | None:
        with self.rt.db() as db:
            row = db.execute("SELECT room_id FROM hub_team_rooms WHERE task_id=? AND origin='operator'",
                             (task_id,)).fetchone()
        return row["room_id"] if row else None

    def run_room_task(self, task: dict[str, Any], record: dict[str, Any], room_id: str) -> TurnResult:
        """The chair's task for an operator room."""
        ctx = {"root": task, "record": record, "chain_id": task["task_id"], "cancel": record["cancel"].is_set}
        return self._run_room(room_id, ctx, chair_memory=None)

    def start_discussion(self, *, chair_id: str, members: Any, topic: Any, goal: Any, memory: Any,
                         ctx: dict[str, Any]) -> dict[str, Any]:
        """An agent-started room, run inline in the caller's task with the caller as chair."""
        agents = self._agents()
        chair = agents.get(chair_id)
        if chair is None:
            raise TeamError("Unknown chair.")
        chosen = self._members(members, agents, minimum=1)
        if any(m.agent_id == chair_id for m in chosen):
            raise TeamError("You chair the discussion; list only the other members.")
        with self.rt._lock:
            waiting = [m.display_name for m in chosen if self._reaches(m.agent_id, chair_id)]
        if waiting:
            raise TeamError(f"{', '.join(waiting)} {'is' if len(waiting) == 1 else 'are'} waiting for your answer, "
                            "so cannot join a discussion you run now.")
        text = self._text(topic, "topic", MAX_TOPIC_CHARS)
        aim = self._text(goal, "goal", MAX_TOPIC_CHARS, allow_empty=True)
        root, room_id, now = ctx["root"], _id("room"), _now()
        title = _one_line(text, 60)
        with self.rt.db() as db:
            db.execute("INSERT INTO hub_team_rooms(room_id, title, topic, goal, chair_id, origin, task_id, state,"
                       " created_at, updated_at) VALUES (?,?,?,?,?,'agent',?,'running',?,?)",
                       (room_id, title, text, aim, chair_id, root["task_id"], now, now))
            for position, member in enumerate([*chosen, chair]):
                db.execute("INSERT INTO hub_team_members(room_id, agent_id, position) VALUES (?,?,?)",
                           (room_id, member.agent_id, position))
            self.rt._event_in(db, chair_id, root["task_id"], root["project_id"], "team", "info",
                              f"{chair.display_name} started a team discussion “{title}” with "
                              + ", ".join(m.display_name for m in chosen), {"room_id": room_id})
        result = self._run_room(room_id, ctx, chair_memory=memory)
        room = self.room(room_id)
        return {"room_id": room_id, "state": room["state"], "rounds": room["round"],
                "summary": room.get("summary") or str(result)}

    def _room_row(self, room_id: str) -> dict[str, Any]:
        from .agent_hub_runtime import TaskError

        with self.rt.db() as db:
            row = db.execute("SELECT * FROM hub_team_rooms WHERE room_id=?", (str(room_id),)).fetchone()
            if row is None:
                raise TaskError("Unknown room.")
            members = [dict(m) for m in db.execute("SELECT * FROM hub_team_members WHERE room_id=? ORDER BY position",
                                                   (row["room_id"],))]
        return dict(row) | {"_members": members}

    def _update_room(self, room_id: str, **fields: Any) -> None:
        fields["updated_at"] = _now()
        with self.rt.db() as db:
            db.execute(f"UPDATE hub_team_rooms SET {', '.join(f'{k}=?' for k in fields)} WHERE room_id=?",
                       (*fields.values(), room_id))

    def _run_room(self, room_id: str, ctx: dict[str, Any], *, chair_memory: Any) -> TurnResult:
        room = self._room_row(room_id)
        if room["state"] in {"done", "stalled"}:
            return TurnResult(room.get("summary") or "This discussion has already ended.")
        agents = self._agents()
        chair = agents.get(room["chair_id"])
        members = [agents[m["agent_id"]] for m in room["_members"] if m["agent_id"] in agents]
        if chair is None or len(members) < 2:
            self._update_room(room_id, state="stopped", finished_at=_now(), speaking=None)
            return TurnResult("The room's agents are no longer available.", "incomplete")
        order = [m for m in members if m.agent_id != chair.agent_id] + [chair]
        stop = threading.Event()
        with self.rt._lock:
            self._rooms[room_id] = {"stop": stop, "speaking": None, "task_id": ctx["root"]["task_id"]}
        self._update_room(room_id, state="running", task_id=ctx["root"]["task_id"], finished_at=None)
        round_no, stalls = int(room["round"]), int(room["stall_rounds"])
        ended: str | None = None
        final = None

        def cancelled() -> bool:
            return stop.is_set() or bool(ctx["cancel"]())

        try:
            while not cancelled():
                round_no += 1
                self._update_room(room_id, round=round_no)
                with self.rt.db() as db:
                    start_seq = db.execute("SELECT COALESCE(MAX(seq),0) FROM hub_team_messages").fetchone()[0]
                novel = False
                for speaker in order:
                    if cancelled():
                        break
                    turn = self._room_turn(room, speaker, chair, order, round_no, ctx, stop, chair_memory)
                    novel = novel or turn["new"]
                    if turn["ended"] is not None:
                        ended = turn["ended"]
                        break
                if ended is not None or cancelled():
                    break
                with self.rt.db() as db:
                    operator_posted = db.execute("SELECT 1 FROM hub_team_messages WHERE room_id=? AND seq>? AND"
                                                 " sender_kind='operator'", (room_id, start_seq)).fetchone() is not None
                stalls = 0 if (novel or operator_posted) else stalls + 1
                self._update_room(room_id, stall_rounds=stalls)
                if stalls >= STALL_ROUNDS:
                    self.rt.event(chair.agent_id, ctx["root"]["task_id"], ctx["root"]["project_id"], "team",
                                  f"Team room “{_one_line(room['title'], 80)}” stalled: {STALL_ROUNDS} rounds added "
                                  "nothing new; asking the chair for the summary", level="warn", detail={"room_id": room_id})
                    self.add_message(room_id=room_id, kind="system", round_no=round_no, task_id=ctx["root"]["task_id"],
                                     body=f"The last {STALL_ROUNDS} rounds added nothing new. The chair now closes the "
                                          "discussion with a summary.", state="stalled")
                    turn = self._room_turn(room, chair, chair, order, round_no, ctx, stop, chair_memory, stalled=True)
                    ended = turn["ended"] if turn["ended"] is not None else (turn["reply"] or "No summary was given.")
                    final = "stalled"
                    break
        finally:
            with self.rt._lock:
                self._rooms.pop(room_id, None)
        record = ctx["record"]
        if ended is not None:
            state, summary = final or "done", _clip(ended, MAX_SUMMARY_CHARS)
        elif record.get("intent") in {"shutdown", "pause"}:
            state, summary = "interrupted", None
        else:
            state, summary = "stopped", None
        fields: dict[str, Any] = {"state": state, "speaking": None}
        if summary is not None:
            fields["summary"] = summary
        if state in {"done", "stalled", "stopped"}:
            fields["finished_at"] = _now()
        self._update_room(room_id, **fields)
        words = {"done": "ended by the chair", "stalled": "stopped after stalling", "stopped": "stopped",
                 "interrupted": "interrupted"}[state]
        self.rt.event(chair.agent_id, ctx["root"]["task_id"], ctx["root"]["project_id"], "team",
                      f"Team room “{_one_line(room['title'], 80)}” {words} after {round_no - int(room['round'])} "
                      "round(s)", level="info" if state == "done" else "warn", detail={"room_id": room_id, "state": state})
        if summary is not None:
            return TurnResult(summary)
        return TurnResult("The discussion was stopped." if state == "stopped" else "The discussion was interrupted.")

    def _room_turn(self, room: dict[str, Any], speaker: Any, chair: Any, order: list[Any], round_no: int,
                   ctx: dict[str, Any], stop: threading.Event, chair_memory: Any, *,
                   stalled: bool = False) -> dict[str, Any]:
        from .tools import Tool

        room_id, root = room["room_id"], ctx["root"]
        is_chair = speaker.agent_id == chair.agent_id
        with self.rt.db() as db:
            member = db.execute("SELECT * FROM hub_team_members WHERE room_id=? AND agent_id=?",
                                (room_id, speaker.agent_id)).fetchone()
            new = db.execute("SELECT * FROM hub_team_messages WHERE room_id=? AND seq>? AND"
                             " NOT (sender_kind='agent' AND sender_agent_id=?) ORDER BY seq",
                             (room_id, member["last_seen"], speaker.agent_id)).fetchall()
            earlier = [r["body"] for r in db.execute("SELECT body FROM hub_team_messages WHERE room_id=? AND"
                                                     " sender_kind='agent' ORDER BY seq DESC LIMIT 40", (room_id,))]
            # What this speaker has now seen; anything posted while it speaks reaches it next turn.
            seen = new[-1]["seq"] if new else member["last_seen"]
            db.execute("UPDATE hub_team_rooms SET speaking=?, updated_at=? WHERE room_id=?",
                       (speaker.agent_id, _now(), room_id))
        with self.rt._lock:
            if room_id in self._rooms:
                self._rooms[room_id]["speaking"] = speaker.agent_id
        agents = {a.agent_id: a for a in order}
        lines, used = [], 0
        for message in reversed(new):
            name = ("Operator" if message["sender_kind"] == "operator" else "Note" if message["sender_kind"] == "system"
                    else agents[message["sender_agent_id"]].display_name if message["sender_agent_id"] in agents
                    else "Agent")
            entry = f"**{name}:** {self._screened(_clip(message['body'], 4000))}"
            if used + len(entry) > MAX_PROMPT_CHARS:
                lines.append("(earlier messages omitted)")
                break
            lines.append(entry)
            used += len(entry)
        lines.reverse()
        if stalled:
            prompt = ("The discussion has stalled: the last rounds added nothing new. Close it now: call "
                      "end_discussion with the final summary.")
        elif lines:
            prompt = f"Round {round_no}. New in the room since your last turn:\n\n" + "\n\n".join(lines) + "\n\nYour turn."
        else:
            prompt = f"Round {round_no}. You speak first. Topic: {room['topic']}\n\nYour turn."
        others = ", ".join(a.display_name for a in order if a.agent_id != speaker.agent_id)
        brief = (f"You are in a team discussion, room “{room['title']}”, with the operator's agents {others}. "
                 f"Topic: {room['topic']}" + (f"\nGoal: {room['goal']}" if room.get("goal") else "") + "\n"
                 "Messages from the other agents and the operator reach you as the user's message, each labelled with "
                 "who wrote it. Contribute what you know or can find out with your own tools, build on and correct "
                 "what others said, and keep it short and concrete. Do not repeat points already made; if you have "
                 "nothing new, say so in one line.\n"
                 + ("You chair this discussion and speak last in each round. When the goal is met or the discussion "
                    "has run its course, call end_discussion with the final summary (decisions, answers, open "
                    "questions, who does what next); otherwise say briefly what the team should cover next round."
                    if is_chair else f"{chair.display_name} chairs it and will close it."))
        ended: dict[str, str] = {}

        def end_discussion(summary: str) -> dict[str, Any]:
            text = self._text(summary, "summary", MAX_SUMMARY_CHARS)
            ended["summary"] = text
            return {"ended": True, "room_id": room_id}

        extra = {"end_discussion": Tool("end_discussion", END_DISCUSSION_DESCRIPTION, END_DISCUSSION_PARAMETERS,
                                        end_discussion)} if is_chair else None
        where = {"room_id": room_id}

        def guard() -> bool:
            return stop.is_set() or bool(ctx["cancel"]())

        self.rt.event(speaker.agent_id, root["task_id"], root["project_id"], "team",
                      f"{speaker.display_name} is speaking in “{_one_line(room['title'], 60)}” (round {round_no})",
                      detail={**where, "round": round_no})
        own = is_chair and ctx["record"]["agent_id"] == chair.agent_id
        outcome = self._turn(waiter=chair, speaker=speaker, prompt=prompt, brief=brief,
                             conversation_id=member["conversation_id"],
                             title=f"Team room: {_one_line(room['title'], 60)}", ctx=ctx, guard=guard, where=where,
                             own=own, memory=chair_memory if own else None, extra_tools=extra,
                             label=f"Speaking in “{_one_line(room['title'], 40)}”")
        reply = outcome.get("reply") or ""
        status = outcome.get("status") or "failed"
        if "summary" in ended and not reply.strip():
            reply = ended["summary"]
        with self.rt.db() as db:
            # A stopped turn leaves no message, and its speaker sees the same messages again on resume.
            if status != "stopped":
                self._add(db, room_id=room_id, sender=speaker.agent_id, body=reply or "(no reply)",
                          task_id=root["task_id"], round_no=round_no, state={"complete": "said"}.get(status, status))
            db.execute("UPDATE hub_team_members SET last_seen=?, conversation_id=COALESCE(?, conversation_id)"
                       " WHERE room_id=? AND agent_id=?", (member["last_seen"] if status == "stopped" else seen,
                                                          outcome.get("conversation_id"), room_id, speaker.agent_id))
            db.execute("UPDATE hub_team_rooms SET speaking=NULL WHERE room_id=?", (room_id,))
            if status != "stopped":
                self.rt._event_in(db, speaker.agent_id, root["task_id"], root["project_id"], "team",
                                  "info" if status == "complete" else "warn",
                                  f"{speaker.display_name} in “{_one_line(room['title'], 60)}”: {_one_line(reply, 180)}",
                                  {**where, "round": round_no, "status": status})
        with self.rt._lock:
            if room_id in self._rooms:
                self._rooms[room_id]["speaking"] = None
        novel = status == "complete" and (is_new(reply, earlier) or int(outcome.get("changes") or 0) > 0)
        return {"reply": reply, "status": status, "new": novel, "ended": ended.get("summary")}

    def room_view(self, row: dict[str, Any], agents: dict[str, Any], members: list[dict[str, Any]],
                  last_at: float | None) -> dict[str, Any]:
        def who(agent_id: str) -> dict[str, str]:
            agent = agents.get(agent_id)
            return {"agent_id": agent_id, "name": agent.display_name if agent else agent_id}

        with self.rt._lock:
            live = self._rooms.get(row["room_id"])
        return {"room_id": row["room_id"], "title": row["title"], "topic": row["topic"], "goal": row["goal"] or None,
                "members": [who(m["agent_id"]) for m in members], "chair_id": row["chair_id"], "state": row["state"],
                "origin": row["origin"], "task_id": row["task_id"], "round": row["round"],
                "created_at": row["created_at"], "last_at": last_at or row["updated_at"],
                "summary": row["summary"], "speaking": (live or {}).get("speaking") or row["speaking"]}

    def rooms(self) -> list[dict[str, Any]]:
        agents = self._agents()
        with self.rt.db() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM hub_team_rooms ORDER BY updated_at DESC LIMIT 200")]
            members = {}
            last = {}
            for row in rows:
                members[row["room_id"]] = [dict(m) for m in db.execute(
                    "SELECT agent_id FROM hub_team_members WHERE room_id=? ORDER BY position", (row["room_id"],))]
                last[row["room_id"]] = db.execute("SELECT MAX(created_at) FROM hub_team_messages WHERE room_id=?",
                                                  (row["room_id"],)).fetchone()[0]
        return [self.room_view(r, agents, members[r["room_id"]], last[r["room_id"]]) for r in rows]

    def room(self, room_id: str) -> dict[str, Any]:
        row = self._room_row(room_id)
        agents = self._agents()
        with self.rt.db() as db:
            messages = db.execute("SELECT * FROM (SELECT * FROM hub_team_messages WHERE room_id=? ORDER BY seq DESC"
                                  " LIMIT ?) ORDER BY seq", (row["room_id"], MAX_VIEW_MESSAGES)).fetchall()
        view = self.room_view(row, agents, row["_members"], messages[-1]["created_at"] if messages else None)

        def sender(message: Any) -> dict[str, Any]:
            if message["sender_kind"] == "agent":
                agent = agents.get(message["sender_agent_id"])
                return {"kind": "agent", "agent_id": message["sender_agent_id"],
                        "name": agent.display_name if agent else message["sender_agent_id"]}
            return {"kind": message["sender_kind"], "name": "You" if message["sender_kind"] == "operator" else "Hub"}

        approval = None
        pending = self.pending(row["task_id"]) if row["task_id"] else None
        if pending and pending.get("room_id") == row["room_id"]:
            approval = self.approval_detail(row["task_id"])
        return view | {"messages": [{"message_id": m["message_id"], "sender": sender(m), "body": m["body"],
                                     "at": m["created_at"], "round": m["round"], "state": m["state"]} for m in messages],
                       "approval": approval}

    def post(self, room_id: str, body: Any) -> dict[str, Any]:
        row = self._room_row(room_id)
        if row["state"] in {"done", "stalled"}:
            raise TeamError("This discussion has ended; start a new room to continue.")
        text = self._text(body, "message", MAX_MESSAGE_CHARS)
        with self.rt.db() as db:
            message_id, _seq = self._add(db, room_id=row["room_id"], kind="operator", body=text, task_id=row["task_id"],
                                         round_no=row["round"], state="posted")
            self.rt._event_in(db, row["chair_id"], row["task_id"], None, "team", "info",
                              f"You posted in “{_one_line(row['title'], 60)}”: {_one_line(text, 160)}",
                              {"room_id": row["room_id"]})
        return {"room_id": row["room_id"], "message_id": message_id, "queued": row["state"] == "running"}

    def stop_room(self, room_id: str) -> dict[str, Any]:
        row = self._room_row(room_id)
        with self.rt._lock:
            live = self._rooms.get(row["room_id"])
        if live is not None:
            live["stop"].set()
            with self.rt._lock:
                entry = self._pending.get(live["task_id"])
            if entry is not None and entry.get("room_id") == row["room_id"]:
                entry["event"].set()
            return {"room_id": row["room_id"], "stopping": True}
        if row["state"] not in {"running", "interrupted"}:
            raise TeamError("This room is not running.")
        self._update_room(row["room_id"], state="stopped", finished_at=_now(), speaking=None)
        if row["task_id"] and row["origin"] == "operator":
            try:
                self.rt.cancel(row["task_id"])  # a queued or interrupted chair task will not start it again
            except ValueError:
                pass
        self.rt.event(row["chair_id"], row["task_id"], None, "team", f"Team room “{_one_line(row['title'], 80)}” stopped",
                      level="warn", detail={"room_id": row["room_id"]})
        return {"room_id": row["room_id"], "stopping": False, "state": "stopped"}

    def resume_room(self, room_id: str) -> dict[str, Any]:
        """Continue an interrupted or stopped room from where it was, as a task of the chair."""
        from .agent_hub_runtime import TaskError

        row = self._room_row(room_id)
        if row["state"] not in {"interrupted", "stopped"}:
            raise TeamError("Only an interrupted or stopped room can be resumed.")
        with self.rt._lock:
            if row["room_id"] in self._rooms:
                raise TeamError("This room is still running.")
        agents = self._agents()
        for member in row["_members"]:
            agent = agents.get(member["agent_id"])
            refusal = self._refusal(agent) if agent else "A member of this room no longer exists."
            if refusal:
                raise TeamError(refusal)
        task = None
        if row["task_id"] and row["origin"] == "operator":
            try:
                task = self.rt.task(row["task_id"])
            except TaskError:
                task = None
        if task is not None and task["state"] in {"INTERRUPTED", "PAUSED", "WAITING_PROVIDER", "WAITING_INPUT"}:
            self._update_room(row["room_id"], state="interrupted", finished_at=None)
            self.rt.resume(task["task_id"])
        else:
            def link(db: Any, task_id: str) -> None:
                db.execute("UPDATE hub_team_rooms SET task_id=?, origin='operator', state='interrupted',"
                           " finished_at=NULL, updated_at=? WHERE room_id=?", (task_id, _now(), row["room_id"]))

            self.rt.create_task(row["chair_id"], title=f"Team room · {row['title']}",
                                request=f"Continue chairing the team discussion “{row['title']}”.", _after_insert=link)
        self.rt.event(row["chair_id"], None, None, "team", f"Team room “{_one_line(row['title'], 80)}” resumed",
                      detail={"room_id": row["room_id"]})
        return self.room(row["room_id"])

    def room_task(self, room_id: str) -> str:
        from .agent_hub_runtime import TaskError

        row = self._room_row(room_id)
        with self.rt._lock:
            live = self._rooms.get(row["room_id"])
        task_id = (live or {}).get("task_id") or row["task_id"]
        if not task_id or (self.pending(task_id) or {}).get("room_id") != row["room_id"]:
            raise TaskError("Nothing in this room is waiting for an approval.")
        return task_id

    def thread_task(self, thread_id: str) -> str:
        from .agent_hub_runtime import TaskError

        with self.rt._lock:
            ask = self._asks.get(str(thread_id))
        if ask is None or (self.pending(ask["task_id"]) or {}).get("thread_id") != str(thread_id):
            raise TaskError("Nothing on this thread is waiting for an approval.")
        return ask["task_id"]
