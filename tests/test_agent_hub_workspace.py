"""The Hub workspace: archive and delete, goals, search, artifacts and schedule controls."""
import time
import unittest
import urllib.error
import urllib.request
from types import SimpleNamespace

import tests.test_agent_hub_chat as chat_tests
from jarvis.agent_hub_runtime import AgentRuntime, artifact_kind
from jarvis.memory import Memory


class ErasingMemory(chat_tests.FakeMemory):
    def __init__(self):
        super().__init__()
        self.deleted = []
        self.issued = 0

    def new_conversation(self, title):
        self.issued += 1
        self.conversations[self.issued] = []
        return self.issued

    def delete_conversation(self, conversation_id):
        self.deleted.append(conversation_id)
        self.conversations.pop(conversation_id, None)
        return {"deleted": True}


class WorkspaceTests(chat_tests.HubChatTests):
    def setUp(self):
        super().setUp()
        self.memory = ErasingMemory()
        # The Hub erases a chat's conversation only from an agent memory file that exists.
        data = self.service.state_dir / "agents" / self.agent_id / "data"
        data.mkdir(parents=True, exist_ok=True)
        (data / "jarvis.db").write_bytes(b"")
        self.service.runtime._open_memory = lambda config: self.memory

    def detail(self):
        code, data = self.request(self.base)
        self.assertEqual(code, 200, data)
        return data

    def rows(self, sql, *params):
        with self.service.runtime.db() as db:
            return db.execute(sql, params).fetchall()

    def raw(self, path):
        request = urllib.request.Request(f"http://127.0.0.1:{self.server.server_port}{path}",
                                         headers={"Authorization": "Bearer " + self.auth.operator_token})
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, response.headers.get("Content-Type"), response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers.get("Content-Type"), exc.read()

    def schedule(self, chat_id):
        now = time.time()
        with self.service.runtime.db() as db:
            db.execute("INSERT INTO hub_schedules VALUES (?,?,?,?,?,?,?,?,?,?,1,?,NULL,NULL,?,?)",
                       ("sched_test", self.agent_id, chat_id, "command-center", "check-in", "check",
                        "interval", 30, None, None, now + 1800, now, now))

    def chat_path(self, verb):
        return f"{self.base}/chats/{self.chat['chat_id']}/{verb}"

    # ------------------------------------------------------------- archive
    def test_archive_hides_and_a_new_message_restores(self):
        self.assertEqual(self.request(self.chat_path("archive"), {"archived": True})[0], 200)
        chat = next(c for c in self.detail()["chats"] if c["chat_id"] == self.chat["chat_id"])
        self.assertTrue(chat["archived"])
        self.assertEqual(self.request(self.chat_path("archive"), {"archived": "yes"})[0], 400)
        self.send("back again")
        chat = next(c for c in self.detail()["chats"] if c["chat_id"] == self.chat["chat_id"])
        self.assertFalse(chat["archived"])
        self.assertEqual(chat["turns"], 1)
        self.assertTrue(chat["active"])

    def test_archiving_a_chat_of_another_agent_is_refused(self):
        other = self.agent(name="Other")["agent_id"]
        code, _ = self.request(f"/api/agents/{other}/chats/{self.chat['chat_id']}/archive", {"archived": True})
        self.assertEqual(code, 400)

    def test_task_archive_flag(self):
        task = self.send("hello")[1]
        self.execute()
        self.completed(1)
        self.assertEqual(self.request(f"/api/tasks/{task['task_id']}/archive", {"archived": True})[0], 200)
        view = next(t for t in self.detail()["tasks"] if t["task_id"] == task["task_id"])
        self.assertTrue(view["archived"])
        self.assertEqual(view["chat_id"], self.chat["chat_id"])

    # -------------------------------------------------------------- delete
    def test_delete_chat_is_refused_while_work_is_open(self):
        self.send("still queued")
        code, refused = self.request(self.chat_path("delete"), {})
        self.assertEqual(code, 400)
        self.assertIn("in progress", refused["error"])
        self.assertEqual(len(self.service.runtime.chat_tasks(self.agent_id, self.chat["chat_id"])), 1)

    def test_delete_chat_removes_records_memory_and_its_schedules(self):
        self.send("first message")
        self.execute()
        self.completed(1)
        conversation = self.runs[0]["conversation_id"]
        self.schedule(self.chat["chat_id"])
        task_ids = [t["task_id"] for t in self.service.runtime.chat_tasks(self.agent_id, self.chat["chat_id"])]
        code, result = self.request(self.chat_path("delete"), {})
        self.assertEqual(code, 200, result)
        self.assertEqual((result["messages"], result["schedules_removed"]), (1, 1))
        self.assertEqual(self.memory.deleted, [conversation])
        marks = ",".join("?" * len(task_ids))
        for table in ("hub_tasks", "hub_chat_turns", "hub_events"):
            self.assertEqual(self.rows(f"SELECT * FROM {table} WHERE task_id IN ({marks})", *task_ids), [], table)
        self.assertEqual(self.rows("SELECT * FROM hub_schedules"), [])
        self.assertEqual(self.rows("SELECT * FROM hub_chat_conversations"), [])
        self.assertNotIn(self.chat["chat_id"], [c["chat_id"] for c in self.detail()["chats"]])
        self.assertEqual(self.request(self.base + "/chat?chat=" + self.chat["chat_id"])[0], 400)

    def test_delete_task_rebuilds_the_chat_conversation_from_what_remains(self):
        first = self.send("keep this one")[1]
        self.execute()
        second = self.send("delete this one")[1]
        self.execute()
        self.completed(2)
        old_conversation = self.runs[0]["conversation_id"]
        self.assertEqual(self.request(f"/api/tasks/{second['task_id']}/delete", {})[0], 200)
        self.assertEqual(self.memory.deleted, [old_conversation])
        remaining = [t["task_id"] for t in self.service.runtime.chat_tasks(self.agent_id, self.chat["chat_id"])]
        self.assertEqual(remaining, [first["task_id"]])
        self.send("next message")
        self.execute()
        self.assertNotEqual(self.runs[-1]["conversation_id"], old_conversation)
        # The new conversation is seeded from the turn that remains, not the deleted one.
        seeded = self.memory.conversations[self.runs[-1]["conversation_id"]]
        self.assertIn(("user", "keep this one"), seeded)
        self.assertNotIn(("user", "delete this one"), seeded)

    def test_delete_open_task_is_refused(self):
        task = self.send("queued work")[1]
        code, refused = self.request(f"/api/tasks/{task['task_id']}/delete", {})
        self.assertEqual(code, 400)
        self.assertIn("Stop it first", refused["error"])

    # --------------------------------------------------------------- goals
    def test_goals_crud_and_brief(self):
        code, goal = self.request(self.base + "/goals", {"title": "Run a 5K by March", "category": "health"})
        self.assertEqual(code, 200, goal)
        self.assertEqual((goal["category_label"], goal["state"]), ("Health", "ACTIVE"))
        self.assertEqual(self.request(self.base + "/goals", {"title": " "})[0], 400)
        self.assertEqual(self.request(self.base + "/goals", {"title": "x", "owner": "y"})[0], 400)
        code, updated = self.request(f"/api/goals/{goal['goal_id']}/update", {"progress": "Ran 2K twice this week"})
        self.assertEqual(code, 200, updated)
        brief = self.service.runtime._goals_brief(self.agent_id)
        self.assertIn(goal["goal_id"], brief)
        self.assertIn("Ran 2K twice this week", brief)
        self.assertEqual(self.request(f"/api/goals/{goal['goal_id']}/update", {"state": "DONE"})[1]["state"], "DONE")
        self.assertNotIn(goal["goal_id"], self.service.runtime._goals_brief(self.agent_id))
        self.assertEqual(self.detail()["goals"][0]["goal_id"], goal["goal_id"])
        self.assertEqual(self.request(f"/api/goals/{goal['goal_id']}/delete", {})[0], 200)
        self.assertEqual(self.detail()["goals"], [])

    def test_goal_tools_write_to_this_agent_only(self):
        toolbox = SimpleNamespace(tools={})
        task = self.send("set a goal")[1]
        self.service.runtime._install_goal_tools(toolbox, task)
        self.assertEqual(set(toolbox.tools), {"goal_list", "goal_create", "goal_update"})
        created = toolbox.tools["goal_create"].function(title="Save $5,000", category="finance")
        self.assertEqual(created["category"], "finance")
        toolbox.tools["goal_update"].function(goal_id=created["goal_id"], progress="Opened a savings account")
        self.assertEqual(toolbox.tools["goal_list"].function()[0]["progress"], "Opened a savings account")
        stored = self.service.runtime.goal(created["goal_id"])
        self.assertEqual((stored["created_by"], stored["chat_id"]), ("agent", self.chat["chat_id"]))
        other = self.agent(name="Other")["agent_id"]
        foreign = self.service.runtime.create_goal(other, title="Theirs")
        with self.assertRaises(Exception):
            toolbox.tools["goal_update"].function(goal_id=foreign["goal_id"], progress="hijack")
        self.assertEqual(self.service.runtime.goal(foreign["goal_id"])["progress"], "")

    def test_brief_asks_for_proactivity(self):
        agent = SimpleNamespace(display_name="Atlas", role="", purpose="")
        brief = AgentRuntime._operator_brief({"attempt": 1}, agent, "", {"schedules": True})
        self.assertIn("Be proactive", brief)
        self.assertIn("schedule a check-in", brief)
        self.assertIn("approval still ask first", brief)

    # -------------------------------------------------------------- search
    def test_search_finds_messages_and_titles(self):
        self.send("Plan the amber festival")
        self.execute()
        self.completed(1)
        code, found = self.request(self.base + "/search?q=amber")
        self.assertEqual(code, 200, found)
        self.assertEqual(found["messages"][0]["chat_id"], self.chat["chat_id"])
        self.assertIn("amber", found["messages"][0]["snippet"])
        self.assertEqual(self.request(self.base + "/search?q=%25")[1]["messages"], [])
        self.assertEqual(self.request(self.base + "/search?q=Regular")[1]["chats"][0]["chat_id"], self.chat["chat_id"])

    # ----------------------------------------------------------- artifacts
    def test_artifacts_gallery_and_file_serving(self):
        root = self.service.project_root("command-center")
        (root / "report.md").write_text("# Report\nhello", encoding="utf-8")
        (root / "page.html").write_text("<script>alert(1)</script>", encoding="utf-8")
        (root / "logs").mkdir()
        (root / "logs" / "trades.csv").write_text("a,b\n1,2\n", encoding="utf-8")
        runtime = self.service.runtime
        task = self.send("make files")[1]
        before = {}
        runtime._record_artifacts(runtime.task(task["task_id"]), before, runtime._snapshot(root))
        (root / "later.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
        code, gallery = self.request(self.base + "/artifacts")
        self.assertEqual(code, 200, gallery)
        made = {item["path"]: item["kind"] for item in gallery["made"]}
        self.assertEqual(made, {"report.md": "documents", "page.html": "web", "logs/trades.csv": "documents"})
        self.assertEqual([(i["path"], i["kind"]) for i in gallery["folder"]], [("later.png", "images")])
        status, kind, body = self.raw(self.base + "/files/content?path=page.html")
        self.assertEqual((status, kind), (200, "text/plain; charset=utf-8"))
        self.assertEqual(body, b"<script>alert(1)</script>")
        self.assertEqual(self.raw(self.base + "/files/content?path=later.png")[1], "image/png")
        for escape in ("../hub.db", "..%2Fhub.db", "logs", "", "C:/Windows/win.ini"):
            self.assertEqual(self.raw(self.base + "/files/content?path=" + escape)[0], 400, escape)
        self.assertEqual(self.raw(self.base + "/files/content?path=report.md&download=1")[1],
                         "application/octet-stream")

    def test_artifact_kinds(self):
        for path, kind in (("a.MP4", "videos"), ("b.mp3", "audio"), ("c.svg", "images"), ("d.py", "code"),
                           ("e.pdf", "documents"), ("f.bin", "other"), ("g.htm", "web")):
            self.assertEqual(artifact_kind(path), kind, path)

    # ----------------------------------------------------------- schedules
    def test_schedule_controls(self):
        self.schedule(self.chat["chat_id"])
        self.assertEqual(self.detail()["schedules"][0]["schedule_id"], "sched_test")
        self.assertFalse(self.request("/api/schedules/sched_test/pause", {})[1]["enabled"])
        self.assertTrue(self.request("/api/schedules/sched_test/resume", {})[1]["enabled"])
        self.assertEqual(self.request("/api/schedules/sched_test/delete", {})[0], 200)
        self.assertEqual(self.request("/api/schedules/sched_test/delete", {})[0], 400)
        self.assertEqual(self.detail()["schedules"], [])

    def test_overview_advertises_features(self):
        overview = self.request("/api/overview")[1]
        self.assertTrue(overview["features"]["archive"])
        self.assertEqual(overview["goal_categories"]["other"], "Something else")


class RealMemoryEraseTests(unittest.TestCase):
    def test_erase_removes_the_transcript_from_agent_memory(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            runtime = AgentRuntime(state_dir=Path(tmp), runtime_path=Path(tmp) / "runtime.db",
                                   provider_profile_dir=Path(tmp) / "profile", project_root=lambda _: Path(tmp),
                                   autostart=False)
            data = Path(tmp) / "agents" / "agt_x" / "data"
            data.mkdir(parents=True)
            memory = Memory(data / "jarvis.db")
            conversation = memory.new_conversation("chat")
            memory.add_message(conversation, "user", "the violet password hint")
            memory.close()
            runtime._erase_conversations("agt_x", {conversation})
            memory = Memory(data / "jarvis.db")
            try:
                self.assertFalse(memory.conversation_exists(conversation))
                rows = memory.db.execute("SELECT COUNT(*) FROM messages WHERE conversation_id=?",
                                         (conversation,)).fetchone()[0]
                self.assertEqual(rows, 0)
            finally:
                memory.close()
                runtime.close()


def _only_own_tests(cls):
    own = set(vars(cls))
    for name in dir(cls):
        if name.startswith("test") and name not in own:
            setattr(cls, name, None)
    return cls


_only_own_tests(WorkspaceTests)

if __name__ == "__main__":
    unittest.main()
