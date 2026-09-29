"""Agents that talk to each other: ask_agent, list_agents, team rooms and their routes.

Every model is a fake runner scripted per agent; no provider is called. The fakes keep the
Hub's real paths: dispatch, the inline turn of the other agent (its own runner, grants and
memory), approvals, events and the HTTP routes."""
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jarvis import hub_team
from jarvis.agent_hub import HubAuth, HubHTTPServer, HubService
from jarvis.agent_hub_runtime import (
    DEFAULT_PERMISSIONS,
    PERMISSION_LABELS,
    allowed_tools,
)
from jarvis.tools import Tool, _serialize_tool_response

TOOL_NAMES = ("read_file", "write_file", "run_process", "web_search", "remember")


class Result(str):
    model = "claude-cli:claude-sonnet-5"
    tool_calls = 0

    def __new__(cls, text, status="complete", approval_id=None):
        value = super().__new__(cls, text)
        value.status = status
        value.waiting_for_approval = approval_id is not None
        value.approval_id = approval_id
        return value


class FakeMemory:
    def __init__(self):
        self.conversations = {}
        self.approvals = []
        self.decisions = []
        self.closed = 0

    def conversation_exists(self, conversation_id):
        return conversation_id in self.conversations

    def new_conversation(self, title):
        conversation_id = 100 + len(self.conversations)
        self.conversations[conversation_id] = [("title", title)]
        return conversation_id

    def add_message(self, conversation_id, role, content):
        self.conversations[conversation_id].append((role, content))

    def list_approvals(self, limit=200):
        return self.approvals

    def decide_approval(self, approval_id, approve):
        self.decisions.append((approval_id, approve))

    def authorize_or_request(self, *args, **kwargs):
        return True, 1

    def close(self):
        self.closed += 1


class FakeToolbox:
    def __init__(self):
        self.tools = {name: Tool(name, name, {"type": "object", "properties": {}}, lambda _n=name: {"did": _n})
                      for name in TOOL_NAMES}

    def execute(self, name, arguments):
        tool = self.tools.get(name)
        if tool is None:
            return _serialize_tool_response(False, "error", f"Unknown tool: {name}")
        try:
            return _serialize_tool_response(True, "result", tool.function(**arguments))
        except Exception as exc:  # noqa: BLE001 - mirrors ToolBox.execute
            return _serialize_tool_response(False, "error", f"{type(exc).__name__}: {exc}")


class TeamBase(unittest.TestCase):
    """A Hub with fake agents. ``self.behaviors[agent_id](runner, call)`` scripts each agent."""

    serve = True

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.memories = {}
        self.calls = []
        self.behaviors = {}
        self.service = self.open_service()
        if self.serve:
            self.auth = HubAuth(self.root)
            self.server = HubHTTPServer(("127.0.0.1", 0), self.service, self.auth)
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()
            self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def open_service(self):
        service = HubService(state_dir=self.root, provider_profile_dir=self.root / "profile",
                             runtime_kwargs={"autostart": False, "provider_probe": lambda _: {
                                 "installed": True, "authenticated": True, "detail": "t", "version": "t"}})
        self.addCleanup(service.close)
        runtime = service.runtime
        case = self

        class Runner:
            def __init__(self, agent_id):
                self.agent_id = agent_id
                self.toolbox = FakeToolbox()
                self.operator_brief = None
                self.open_toolset = False
                self.conversational_clarifications = False

            def run(self, prompt, conversation_id=None, cancellation_guard=None, **kwargs):
                call = {"agent": self.agent_id, "prompt": prompt, "conversation_id": conversation_id,
                        "tools": sorted(self.toolbox.tools), "brief": self.operator_brief,
                        "guard": cancellation_guard or (lambda: False)}
                case.calls.append(call)
                behaviour = case.behaviors.get(self.agent_id)
                return behaviour(self, call) if behaviour else Result(f"{self.agent_id} answered: {prompt[:40]}")

        def memory_for(config):
            key = Path(config.data_dir).parent.name
            return self.memories.setdefault(key, FakeMemory())

        for name, value in (
                ("_agent_config", lambda *, agent_id, project_root, permissions, reference: SimpleNamespace(
                    data_dir=self.root / "agents" / agent_id / "data")),
                ("_open_memory", memory_for),
                ("_make_client", lambda *a, **k: SimpleNamespace()),
                ("_make_agent", lambda config, memory, on_event, client: Runner(Path(config.data_dir).parent.name))):
            patcher = patch.object(runtime, name, side_effect=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        return service

    @property
    def runtime(self):
        return self.service.runtime

    def agent(self, name, groups=None, **extra):
        payload = {"name": name, "role": f"{name} role", "provider": "claude-cli", "model": "claude-sonnet-5",
                   "enable": True, **extra}
        if groups is not None:
            payload["tool_groups"] = groups
        return self.service.create_agent(payload)["agent_id"]

    def task(self, agent_id, request="Do the job"):
        return self.runtime.create_task(agent_id, title="", request=request)["task_id"]

    def dispatch(self):
        return self.runtime.dispatch_once()

    def wait(self, predicate, timeout=15.0, message="condition"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.02)
        self.fail(f"Timed out waiting for {message}")

    def idle(self):
        self.wait(lambda: not self.runtime.running_ids() and not self.runtime._running, message="idle runtime")

    def state(self, task_id):
        return self.runtime.task(task_id)["state"]

    def calls_of(self, agent_id):
        return [c for c in self.calls if c["agent"] == agent_id]

    @staticmethod
    def tool(runner, name, arguments):
        return json.loads(runner.toolbox.execute(name, arguments))

    # HTTP
    def request(self, path, body=None, authenticated=True):
        headers = {"Content-Type": "application/json"}
        if authenticated:
            headers["Authorization"] = "Bearer " + self.auth.operator_token
        req = urllib.request.Request(f"http://127.0.0.1:{self.server.server_port}" + path,
                                     data=None if body is None else json.dumps(body).encode(), headers=headers)
        try:
            with urllib.request.urlopen(req) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    def ok(self, path, body=None):
        code, data = self.request(path, body)
        self.assertEqual(code, 200, data)
        return data


# ================================================================== asks
class AskTests(TeamBase):
    def test_ask_runs_the_other_agent_inline_with_its_own_grants_and_continues_the_thread(self):
        atlas = self.agent("Atlas")  # every ability
        coder = self.agent("Coder", ["files_read", "team"])
        outputs = []

        def atlas_turn(runner, call):
            outputs.append(self.tool(runner, "ask_agent", {"agent": "coder", "message": "What is in notes.md?"}))
            outputs.append(self.tool(runner, "ask_agent", {"agent": coder, "message": "And the second line?"}))
            outputs.append(self.tool(runner, "ask_agent", {"agent": "Coder", "message": "Fresh start",
                                                           "new_thread": True}))
            return Result("Coder says it is about apples.")

        self.behaviors[atlas] = atlas_turn
        self.behaviors[coder] = lambda runner, call: Result(f"Coder reply to: {call['prompt']}")
        task_id = self.task(atlas, "Find out what is in notes.md")
        self.dispatch()
        self.idle()
        self.assertEqual(self.state(task_id), "COMPLETED")
        first, second, third = (o["result"] for o in outputs)
        self.assertEqual((first["status"], first["reply"]), ("answered", "Coder reply to: What is in notes.md?"))
        self.assertEqual(first["thread_id"], second["thread_id"])
        self.assertNotEqual(first["thread_id"], third["thread_id"])
        atlas_run, *coder_runs = [self.calls_of(atlas)[0], *self.calls_of(coder)]
        # Coder ran with its own grants only: no write or program tools, no helper, no browser.
        self.assertIn("write_file", atlas_run["tools"])
        self.assertIn("run_process", atlas_run["tools"])
        for run in coder_runs:
            self.assertEqual(set(run["tools"]), {"read_file", "read_document", "list_agents", "ask_agent",
                                                 "start_team_discussion"})
            self.assertIn("fellow agent “Atlas”", run["brief"])
            self.assertIn("Coder role", run["brief"])
        # Continuity: the same conversation in Coder's own memory; a new thread gets a new one.
        self.assertEqual(coder_runs[0]["conversation_id"], coder_runs[1]["conversation_id"])
        self.assertNotEqual(coder_runs[1]["conversation_id"], coder_runs[2]["conversation_id"])
        # Atlas's side of the thread is in Atlas's own memory.
        sides = [c for c in self.memories[atlas].conversations.values() if c[0] == ("title", "Thread with Coder")]
        self.assertEqual([m[0] for m in sides[0][1:]], ["assistant", "user", "assistant", "user"])
        thread = self.ok(f"/api/threads/{first['thread_id']}")
        self.assertEqual([(m["sender_agent_id"], m["state"]) for m in thread["messages"]],
                         [(atlas, "sent"), (coder, "answered"), (atlas, "sent"), (coder, "answered")])
        self.assertEqual((thread["a"]["name"], thread["b"]["name"], thread["active"]), ("Atlas", "Coder", False))
        listing = self.ok(f"/api/agents/{coder}/threads")["threads"]
        self.assertEqual(len(listing), 2)
        self.assertEqual(listing[1]["peer"], {"agent_id": atlas, "name": "Atlas"})
        self.assertEqual(listing[1]["count"], 4)
        # Tool rows (contract item 7) and Coder's own events.
        events = self.runtime.events(task_id=task_id)
        asks = [e["detail"] for e in events if e["kind"] == "tool" and e["detail"]["tool"] == "ask_agent"]
        self.assertEqual({k: asks[0][k] for k in ("peer_agent_id", "thread_id", "args")},
                         {"peer_agent_id": coder, "thread_id": first["thread_id"], "args": "coder: What is in notes.md?"})
        self.assertIn("Coder reply to", asks[0]["reply_preview"])
        team = [e for e in events if e["kind"] == "team"]
        self.assertTrue(any(e["summary"].startswith("Atlas asked Coder") for e in team))
        self.assertTrue(any(e["summary"].startswith("Coder replied to Atlas") for e in team))
        # Agent payload (contract item 8).
        detail = self.ok(f"/api/agents/{coder}")
        self.assertEqual(detail["team"], {"threads": 2, "rooms_active": 0})
        self.assertEqual(detail["permission_labels"]["team"], "Talk to your other agents and join team discussions")

    def test_list_agents_and_refusals(self):
        atlas = self.agent("Atlas")
        coder = self.agent("Coder", ["files_read", "team"])
        quiet = self.agent("Quiet", ["files_read"])
        gone = self.agent("Gone")
        self.service.archive(gone, True)
        outputs = []

        def atlas_turn(runner, call):
            outputs.append(self.tool(runner, "list_agents", {}))
            for arguments in ({"agent": "Nobody", "message": "hi"}, {"agent": "Atlas", "message": "hi"},
                              {"agent": "Quiet", "message": "hi"}, {"agent": "Gone", "message": "hi"},
                              {"agent": "Coder", "message": ""}, {"agent": "Coder", "message": "x" * 12_001},
                              {"agent": "Coder", "message": "my key is sk-or-" + "z" * 40},
                              {"agent": "Coder", "message": "hi", "new_thread": "yes"}):
                outputs.append(self.tool(runner, "ask_agent", arguments))
            return Result("done")

        self.behaviors[atlas] = atlas_turn
        self.dispatch_and_finish(atlas)
        agents = outputs[0]["result"]["agents"]
        self.assertEqual(sorted(a["name"] for a in agents), ["Coder", "Quiet"])
        coder_entry = next(a for a in agents if a["name"] == "Coder")
        self.assertEqual((coder_entry["status"], coder_entry["accepts_messages"]), ("idle", True))
        self.assertIn(PERMISSION_LABELS["files_read"], coder_entry["abilities"])
        self.assertFalse(next(a for a in agents if a["name"] == "Quiet")["accepts_messages"])
        errors = [o["error"] for o in outputs[1:]]
        self.assertTrue(all(not o["ok"] for o in outputs[1:]))
        for words, error in zip(("no agent called Nobody", "That is you", "Team ability is off", "archived",
                                 "1-12,000", "1-12,000", "secret", "true or false"), errors):
            self.assertIn(words, error)
        self.assertEqual(self.calls_of(coder), [])
        self.assertEqual(self.calls_of(quiet), [])

    def dispatch_and_finish(self, agent_id, request="Do the job"):
        task_id = self.task(agent_id, request)
        self.dispatch()
        self.idle()
        return task_id

    def test_waits_while_the_other_agent_is_busy_then_runs(self):
        atlas = self.agent("Atlas")
        coder = self.agent("Coder", ["files_read", "team"])
        release = threading.Event()
        outputs = []

        def coder_turn(runner, call):
            if call["prompt"] == "Coder's own work":
                release.wait(10)
                return Result("own work done")
            return Result("answer after own work")

        self.behaviors[coder] = coder_turn
        self.behaviors[atlas] = lambda runner, call: (
            outputs.append(self.tool(runner, "ask_agent", {"agent": "Coder", "message": "Quick question"}))
            or Result("got it"))
        own = self.task(coder, "Coder's own work")
        self.dispatch()
        self.wait(lambda: self.calls_of(coder), message="Coder's own task")
        ask = self.task(atlas, "Ask Coder")
        self.dispatch()
        self.wait(lambda: "Waiting for Coder to finish its current task" in (self.runtime.task(ask)["progress"] or ""),
                  message="waiting progress")
        self.assertEqual(len(self.calls_of(coder)), 1)  # the ask never interrupts Coder's own work
        release.set()
        self.idle()
        self.assertEqual((self.state(own), self.state(ask)), ("COMPLETED", "COMPLETED"))
        self.assertEqual(outputs[0]["result"]["reply"], "answer after own work")
        waits = [e for e in self.runtime.events(task_id=ask) if e["kind"] == "team" and "Waiting for" in e["summary"]]
        self.assertEqual(len(waits), 1)

    def test_the_answering_agent_counts_as_busy_for_dispatch(self):
        atlas = self.agent("Atlas")
        coder = self.agent("Coder", ["files_read", "team"])
        answering, finish = threading.Event(), threading.Event()

        def coder_turn(runner, call):
            if call["prompt"] == "queued for Coder":
                return Result("queued work done")
            answering.set()
            finish.wait(10)
            return Result("answer")

        self.behaviors[coder] = coder_turn
        self.behaviors[atlas] = lambda runner, call: self.tool(runner, "ask_agent", {
            "agent": "Coder", "message": "Q"}) and Result("ok")
        self.task(atlas)
        self.dispatch()
        answering.wait(10)
        queued = self.task(coder, "queued for Coder")
        self.assertEqual(self.dispatch(), [])
        self.assertEqual(self.state(queued), "QUEUED")
        self.assertIn("Answering Atlas", self.service.overview()["agents"][1]["status"]["detail"])
        self.assertEqual(len(self.runtime.running_ids()), 1)  # Coder's inline turn is not a task of its own
        finish.set()
        self.idle()
        self.dispatch()
        self.idle()
        self.assertEqual(self.state(queued), "COMPLETED")

    def test_cycles_are_refused_in_the_same_chain_and_across_tasks(self):
        atlas = self.agent("Atlas")
        # Coder writes nothing, so both agents' own tasks can run at once in the shared project.
        coder = self.agent("Coder", ["files_read", "team"])
        refused = []

        def coder_turn(runner, call):
            if call["prompt"] == "Question from Atlas":
                refused.append(self.tool(runner, "ask_agent", {"agent": "Atlas", "message": "back at you"}))
                return Result("I need the file name: which file?")
            return Result("fine")

        self.behaviors[coder] = coder_turn
        self.behaviors[atlas] = lambda runner, call: self.tool(runner, "ask_agent", {
            "agent": "Coder", "message": "Question from Atlas"}) and Result("ok")
        self.dispatch_and_finish(atlas)
        self.assertFalse(refused[0]["ok"])
        self.assertIn("Atlas is waiting for your answer: put your question in your reply", refused[0]["error"])
        # Across tasks: Atlas waits for busy Coder; Coder's own task then asks Atlas.
        release = threading.Event()
        crossed = []

        def coder_own(runner, call):
            if call["prompt"] == "Coder's own work":
                self.wait(lambda: self.runtime.team._edges.get(atlas) == coder, message="Atlas waiting")
                crossed.append(self.tool(runner, "ask_agent", {"agent": "Atlas", "message": "Are you free?"}))
                release.set()
                return Result("own work done")
            return Result("answer to Atlas")

        self.behaviors[coder] = coder_own
        own = self.task(coder, "Coder's own work")
        self.dispatch()
        self.wait(lambda: any(c["prompt"] == "Coder's own work" for c in self.calls_of(coder)), message="Coder busy")
        ask = self.task(atlas, "Ask Coder")
        self.dispatch()
        self.assertTrue(release.wait(10))
        self.idle()
        self.assertFalse(crossed[0]["ok"])
        self.assertIn("waiting for your answer", crossed[0]["error"])
        self.assertEqual((self.state(own), self.state(ask)), ("COMPLETED", "COMPLETED"))
        self.assertEqual(self.runtime.team._edges, {})

    def test_stop_cancels_the_other_agents_turn_and_the_thread_can_be_stopped(self):
        atlas = self.agent("Atlas")
        coder = self.agent("Coder", ["files_read", "team"])
        started = threading.Event()
        seen = []

        def coder_turn(runner, call):
            started.set()
            while not call["guard"]():
                time.sleep(0.01)
            seen.append("stopped")
            return Result("partial")

        outputs = []
        self.behaviors[coder] = coder_turn
        self.behaviors[atlas] = lambda runner, call: (outputs.append(self.tool(runner, "ask_agent", {
            "agent": "Coder", "message": "Long job"})) or Result("after ask"))
        task_id = self.task(atlas)
        self.dispatch()
        started.wait(10)
        self.runtime.cancel(task_id)
        self.idle()
        self.assertEqual(seen, ["stopped"])
        self.assertEqual(self.state(task_id), "CANCELLED")
        self.assertEqual(outputs[0]["result"]["status"], "stopped")
        # The operator stops just the ask; the asking agent carries on.
        started.clear()
        second = self.task(atlas)
        self.dispatch()
        started.wait(10)
        thread_id = self.ok(f"/api/agents/{atlas}/threads")["threads"][0]["thread_id"]
        self.assertTrue(self.ok(f"/api/threads/{thread_id}")["active"])
        self.assertEqual(self.ok(f"/api/threads/{thread_id}/stop", {})["stopping"], True)
        self.idle()
        self.assertEqual(self.state(second), "COMPLETED")
        self.assertEqual(outputs[1]["result"]["status"], "stopped")
        self.assertEqual(self.request(f"/api/threads/{thread_id}/stop", {})[0], 400)

    def test_an_approval_inside_an_ask_waits_for_the_operator_then_continues(self):
        atlas = self.agent("Atlas")
        coder = self.agent("Coder", ["files_read", "accounts", "team"])
        attempts = []

        def coder_turn(runner, call):
            attempts.append(call["prompt"])
            if len(attempts) == 1:
                return Result("I need your OK to send the email.", status="incomplete", approval_id=7)
            if len(attempts) == 3:
                return Result("Needs OK again", status="incomplete", approval_id=8)
            return Result("Email sent.")

        self.memories.setdefault(coder, FakeMemory()).approvals = [
            {"id": 7, "action": "communicate_external", "resource": "send mail to team", "reason": "sends", "status": "pending"},
            {"id": 8, "action": "communicate_external", "resource": "again", "reason": "sends", "status": "pending"}]
        outputs = []
        self.behaviors[coder] = coder_turn
        self.behaviors[atlas] = lambda runner, call: (outputs.append(self.tool(runner, "ask_agent", {
            "agent": "Coder", "message": "Email the team"})) or Result("done"))
        task_id = self.task(atlas)
        self.dispatch()
        chat_approval = self.wait(lambda: self.runtime.approval_detail(self.runtime.task(task_id)), message="approval")
        self.assertEqual((chat_approval["id"], chat_approval["agent_name"], chat_approval["inline"]), (7, "Coder", True))
        thread_id = chat_approval["thread_id"]
        self.assertEqual(self.ok(f"/api/threads/{thread_id}")["approval"]["id"], 7)
        self.assertEqual(self.state(task_id), "RUNNING")
        self.ok(f"/api/threads/{thread_id}/approval", {"decision": "approve"})
        self.idle()
        self.assertEqual(self.memories[coder].decisions, [(7, True)])
        self.assertEqual(attempts, ["Email the team", "Email the team"])  # the same message runs again
        self.assertEqual(outputs[0]["result"]["reply"], "Email sent.")
        # Denied through the task's own approval control: the ask reports it.
        second = self.task(atlas)
        self.dispatch()
        self.wait(lambda: self.runtime.approval_detail(self.runtime.task(second)), message="second approval")
        self.assertEqual(self.request(f"/api/tasks/{second}/approval", {"decision": "maybe"})[0], 400)
        self.ok(f"/api/tasks/{second}/approval", {"decision": "deny"})
        self.idle()
        self.assertEqual(self.memories[coder].decisions[-1], (8, False))
        self.assertEqual(outputs[1]["result"]["status"], "denied")
        self.assertEqual(self.request(f"/api/threads/{thread_id}/approval", {"decision": "approve"})[0], 400)


# ================================================================== rooms
class RoomTests(TeamBase):
    def setUp(self):
        super().setUp()
        self.atlas = self.agent("Atlas")
        self.coder = self.agent("Coder", ["files_read", "team"])

    def start(self, **extra):
        body = {"topic": "Plan the launch", "member_ids": [self.atlas, self.coder], **extra}
        room = self.ok("/api/rooms", body)
        self.dispatch()
        return room

    def test_rounds_interjection_and_the_chair_ending_it(self):
        started, posted = threading.Event(), threading.Event()
        coder_turns = []

        def coder_turn(runner, call):
            coder_turns.append(call)
            if len(coder_turns) == 1:
                started.set()
                posted.wait(10)
                return Result("Launch needs 3 beta testers and the pricing page.")
            return Result("Pricing page draft is in pricing.md with 2 tiers.")

        chair_turns = []

        def atlas_turn(runner, call):
            chair_turns.append(call)
            if len(chair_turns) == 2:
                self.tool(runner, "end_discussion", {"summary": "Ship on Friday with 2 pricing tiers."})
                return Result("Closing the discussion.")
            return Result("Next round: settle the pricing tiers.")

        self.behaviors[self.coder], self.behaviors[self.atlas] = coder_turn, atlas_turn
        room = self.start(title="Launch")
        self.assertEqual((room["state"], room["chair_id"], room["title"]), ("running", self.atlas, "Launch"))
        started.wait(10)
        self.ok(f"/api/rooms/{room['room_id']}/messages", {"body": "Please keep costs under 500."})
        self.assertEqual(self.ok(f"/api/rooms/{room['room_id']}")["speaking"], self.coder)
        posted.set()
        self.idle()
        view = self.ok(f"/api/rooms/{room['room_id']}")
        self.assertEqual((view["state"], view["summary"], view["round"]),
                         ("done", "Ship on Friday with 2 pricing tiers.", 2))
        # The operator posted while Coder was speaking, so the post comes first in the transcript.
        self.assertEqual([(m["sender"]["kind"], m["sender"].get("name"), m["round"]) for m in view["messages"]],
                         [("operator", "You", 1), ("agent", "Coder", 1), ("agent", "Atlas", 1),
                          ("agent", "Coder", 2), ("agent", "Atlas", 2)])
        self.assertTrue(all("end_discussion" in c["tools"] for c in chair_turns))  # only the chair can close it
        self.assertFalse(any("end_discussion" in c["tools"] for c in coder_turns))
        # The operator's post joined the transcript before the next speaker (the chair).
        self.assertIn("**Operator:** Please keep costs under 500.", chair_turns[0]["prompt"])
        self.assertIn("**Coder:** Launch needs 3 beta testers", chair_turns[0]["prompt"])
        self.assertIn("**Atlas:** Next round", coder_turns[1]["prompt"])
        self.assertIn("**Operator:**", coder_turns[1]["prompt"])  # Coder had not seen the post yet
        self.assertEqual(coder_turns[0]["conversation_id"], coder_turns[1]["conversation_id"])
        chair_task = self.runtime.task(view["task_id"])
        self.assertEqual((chair_task["state"], chair_task["result"]), ("COMPLETED", "Ship on Friday with 2 pricing tiers."))
        listing = self.ok("/api/rooms")["rooms"]
        self.assertEqual([(r["room_id"], r["state"]) for r in listing], [(room["room_id"], "done")])
        self.assertEqual(self.request(f"/api/rooms/{room['room_id']}/messages", {"body": "late"})[0], 400)
        self.assertEqual(self.request(f"/api/rooms/{room['room_id']}/resume", {})[0], 400)

    def test_two_rounds_with_nothing_new_ask_the_chair_once_then_stop(self):
        self.behaviors[self.coder] = lambda runner, call: Result("Agreed, sounds good.")
        chair = []

        def atlas_turn(runner, call):
            chair.append(call["prompt"])
            if "stalled" in call["prompt"]:
                self.tool(runner, "end_discussion", {"summary": "No new information; plan stays as is."})
                return Result("Summary given.")
            return Result("Okay, agreed.")

        self.behaviors[self.atlas] = atlas_turn
        room = self.start()
        self.idle()
        view = self.ok(f"/api/rooms/{room['room_id']}")
        self.assertEqual((view["state"], view["round"], view["summary"]),
                         ("stalled", 2, "No new information; plan stays as is."))
        self.assertEqual(len(chair), 3)
        self.assertEqual(sum("stalled" in p for p in chair), 1)
        self.assertEqual(len(self.calls_of(self.coder)), 2)
        self.assertEqual(view["messages"][-2]["sender"]["kind"], "system")
        stalls = [e for e in self.runtime.events(agent_id=self.atlas) if "stalled" in e["summary"]]
        self.assertTrue(stalls)

    def test_novelty_heuristic(self):
        self.assertFalse(hub_team.is_new("", []))
        self.assertFalse(hub_team.is_new("I agree, nothing to add.", []))
        self.assertTrue(hub_team.is_new("Agreed, but should we test on Android?", []))
        self.assertTrue(hub_team.is_new("Agreed; the budget is 450.", []))
        earlier = ["The launch needs three beta testers and a finished pricing page before Friday."]
        self.assertFalse(hub_team.is_new("The launch needs three beta testers and a finished pricing page before Friday!",
                                         earlier))
        self.assertTrue(hub_team.is_new("Marketing copy is ready for review in docs/copy.md.", earlier))

    def test_stop_and_resume_after_a_stop(self):
        started = threading.Event()

        def coder_turn(runner, call):
            started.set()
            while not call["guard"]():
                time.sleep(0.01)
            return Result("cut off")

        self.behaviors[self.coder] = coder_turn
        room = self.start()
        started.wait(10)
        self.assertEqual(self.request(f"/api/rooms/{room['room_id']}/resume", {})[0], 400)
        self.ok(f"/api/rooms/{room['room_id']}/stop", {})
        self.idle()
        view = self.ok(f"/api/rooms/{room['room_id']}")
        self.assertEqual(view["state"], "stopped")
        self.assertEqual(view["messages"], [])  # a stopped turn leaves no message
        self.assertEqual(self.runtime.task(view["task_id"])["state"], "COMPLETED")
        self.behaviors[self.coder] = lambda runner, call: Result("Now with feeling: 4 testers.")
        self.behaviors[self.atlas] = lambda runner, call: (
            self.tool(runner, "end_discussion", {"summary": "Resumed and closed."}) and Result("closed"))
        resumed = self.ok(f"/api/rooms/{room['room_id']}/resume", {})
        self.assertNotEqual(resumed["task_id"], view["task_id"])
        self.dispatch()
        self.idle()
        final = self.ok(f"/api/rooms/{room['room_id']}")
        self.assertEqual((final["state"], final["summary"], final["round"]), ("done", "Resumed and closed.", 2))
        # Coder saw the start of the room again (its stopped turn had not answered it).
        self.assertIn("You speak first", self.calls_of(self.coder)[-1]["prompt"])

    def test_an_agent_can_start_and_chair_a_discussion(self):
        outputs = []

        def atlas_turn(runner, call):
            if call["prompt"].startswith("Round"):
                self.tool(runner, "end_discussion", {"summary": "Coder will write the tests."})
                return Result("Closed.")
            outputs.append(self.tool(runner, "start_team_discussion", {"members": ["Coder"], "topic": "Who writes tests?"}))
            return Result("Discussed.")

        self.behaviors[self.atlas] = atlas_turn
        self.behaviors[self.coder] = lambda runner, call: Result("I can write the tests by Thursday.")
        task_id = self.task(self.atlas, "Decide who writes the tests")
        self.dispatch()
        self.idle()
        result = outputs[0]["result"]
        self.assertEqual((result["state"], result["summary"], result["rounds"]), ("done", "Coder will write the tests.", 1))
        room = self.ok(f"/api/rooms/{result['room_id']}")
        self.assertEqual(room["origin"], "agent")
        self.assertEqual(self.state(task_id), "COMPLETED")
        detail = next(e["detail"] for e in self.runtime.events(task_id=task_id)
                      if e["kind"] == "tool" and e["detail"]["tool"] == "start_team_discussion")
        self.assertEqual((detail["room_id"], detail["reply_preview"]), (result["room_id"], "Coder will write the tests."))
        # The chair's own memory is reused for its room turns (no second memory opened for it).
        self.assertEqual(self.memories[self.atlas].closed, 1)

    def test_room_validation_and_auth(self):
        quiet = self.agent("Quiet", ["files_read"])
        for body in ({"member_ids": [self.atlas, self.coder]}, {"topic": "", "member_ids": [self.atlas, self.coder]},
                     {"topic": "x" * 2001, "member_ids": [self.atlas, self.coder]},
                     {"topic": "t", "member_ids": [self.atlas]}, {"topic": "t", "member_ids": [self.atlas, self.atlas]},
                     {"topic": "t", "member_ids": [self.atlas, "agt_nobody"]},
                     {"topic": "t", "member_ids": [self.atlas, quiet]},
                     {"topic": "t", "member_ids": [self.atlas, self.coder], "chair_id": quiet},
                     {"topic": "t", "member_ids": [self.atlas, self.coder], "extra": 1},
                     {"topic": "t", "member_ids": "atlas,coder"},
                     {"topic": "t", "member_ids": [self.atlas, self.coder], "title": "x" * 121}):
            code, refused = self.request("/api/rooms", body)
            self.assertEqual(code, 400, (body, refused))
        room = self.ok("/api/rooms", {"topic": "Plan", "member_ids": [self.atlas, self.coder], "chair_id": self.coder})
        self.assertEqual(room["chair_id"], self.coder)
        rid = room["room_id"]
        for path, body in ((f"/api/rooms/{rid}/messages", {"body": ""}), (f"/api/rooms/{rid}/messages", {"text": "x"}),
                           (f"/api/rooms/{rid}/messages", {"body": "x" * 12_001}), (f"/api/rooms/{rid}/stop", {"x": 1}),
                           (f"/api/rooms/{rid}/approval", {"decision": "approve"}), (f"/api/rooms/{rid}/fly", {}),
                           ("/api/rooms/room_missing/stop", {}), ("/api/threads/thread_missing/stop", {}),
                           ("/api/threads/thread_missing/fly", {})):
            self.assertEqual(self.request(path, body)[0], 400, path)
        for path in ("/api/rooms/room_missing", "/api/threads/thread_missing", "/api/agents/agt_nobody/threads"):
            self.assertEqual(self.request(path)[0], 400, path)
        for path in ("/api/rooms", f"/api/rooms/{rid}", f"/api/agents/{self.atlas}/threads", "/api/threads/x"):
            self.assertEqual(self.request(path, authenticated=False)[0], 401, path)
        for path in ("/api/rooms", f"/api/rooms/{rid}/messages", f"/api/rooms/{rid}/stop", f"/api/rooms/{rid}/resume",
                     f"/api/rooms/{rid}/approval", "/api/threads/x/stop", "/api/threads/x/approval"):
            self.assertEqual(self.request(path, {"body": "x"}, authenticated=False)[0], 401, path)
        # A queued room's chair task never starts once the room is stopped.
        self.ok(f"/api/rooms/{rid}/stop", {})
        self.assertEqual(self.runtime.task(room["task_id"])["state"], "CANCELLED")
        self.assertEqual(self.dispatch(), [])

    def test_team_permission_requires_explicit_grant_for_saved_agents(self):
        self.assertTrue(DEFAULT_PERMISSIONS["team"])
        self.assertEqual(allowed_tools({"team": True}), frozenset({"list_agents", "ask_agent", "start_team_discussion"}))
        limited = self.agent("Limited", ["files_read"])
        with self.runtime.db() as db:
            for agent_id in (self.atlas, limited):
                saved = json.loads(db.execute("SELECT permissions FROM hub_agent_settings WHERE agent_id=?",
                                              (agent_id,)).fetchone()[0])
                saved.pop("team")
                db.execute("UPDATE hub_agent_settings SET permissions=? WHERE agent_id=?", (json.dumps(saved), agent_id))
        self.assertFalse(self.runtime.agent_settings(self.atlas)["permissions"]["team"])
        self.assertFalse(self.runtime.agent_settings(limited)["permissions"]["team"])
        grants = self.runtime.agent_settings(self.atlas)["permissions"]
        grants["team"] = True
        self.runtime.save_agent_settings(self.atlas, permissions=grants)
        self.assertTrue(self.runtime.agent_settings(self.atlas)["permissions"]["team"])


class RestartTests(TeamBase):
    serve = False

    def test_a_running_room_is_interrupted_by_a_restart_and_can_be_resumed(self):
        atlas = self.agent("Atlas")
        coder = self.agent("Coder", ["files_read", "team"])
        started = threading.Event()

        def coder_turn(runner, call):
            started.set()
            while not call["guard"]():
                time.sleep(0.01)
            return Result("cut off by shutdown")

        self.behaviors[coder] = coder_turn
        room = self.runtime.team.create_room({"topic": "Plan", "member_ids": [atlas, coder]})
        self.runtime.dispatch_once()
        started.wait(10)
        self.runtime.close(timeout=10)  # a shutdown: the running room becomes interrupted
        self.assertEqual(self.runtime.team.room(room["room_id"])["state"], "interrupted")
        self.assertEqual(self.runtime.task(room["task_id"])["state"], "INTERRUPTED")
        # A crash leaves it marked running; the next start marks it interrupted.
        with self.runtime.db() as db:
            db.execute("UPDATE hub_team_rooms SET state='running' WHERE room_id=?", (room["room_id"],))
        self.service = self.open_service()
        restarted = self.runtime.team.room(room["room_id"])
        self.assertEqual(restarted["state"], "interrupted")
        self.assertTrue(any("interrupted by a restart" in e["summary"] for e in self.runtime.events(kinds=["team"])))
        self.behaviors[coder] = lambda runner, call: Result("Back again with 2 new facts.")
        self.behaviors[atlas] = lambda runner, call: (
            self.tool(runner, "end_discussion", {"summary": "Finished after the restart."}) and Result("closed"))
        resumed = self.runtime.team.resume_room(room["room_id"])
        self.assertEqual(resumed["task_id"], room["task_id"])  # the interrupted chair task runs again
        self.runtime.dispatch_once()
        self.wait(lambda: self.runtime.team.room(room["room_id"])["state"] == "done", message="room done")
        self.idle()
        final = self.runtime.team.room(room["room_id"])
        self.assertEqual((final["summary"], final["round"]), ("Finished after the restart.", 2))


if __name__ == "__main__":
    unittest.main()
