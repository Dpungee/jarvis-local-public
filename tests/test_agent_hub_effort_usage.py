"""Per-agent effort, real context usage, model windows, history budget and compaction."""
import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from jarvis import model_client as mc
from jarvis.agent_hub_runtime import AgentRuntime
from jarvis.memory import Memory
from tests.test_agent_hub_recovery import HubRecoveryTests


def cli_result(**extra):
    return json.dumps({"type": "result", "is_error": False, "result": "ok",
                       "structured_output": {"content": "ok", "tool_calls": []},
                       "usage": {"input_tokens": 5, "cache_read_input_tokens": 40_000,
                                 "cache_creation_input_tokens": 3_000, "output_tokens": 7},
                       "modelUsage": {"claude-opus-5-5": {"contextWindow": 1_000_000}}, **extra})


class ClaudeEffortTests(unittest.TestCase):
    def client(self, stderr=""):
        calls = []

        def runner(args, **options):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, cli_result(), stderr)
        return mc.ClaudeCLIClient("claude.exe", working_directory=".", runner=runner,
                                  unthinking_effort="low"), calls

    @staticmethod
    def effort(args):
        return args[args.index("--effort") + 1] if "--effort" in args else None

    def test_pinned_effort_replaces_the_route_mapping(self):
        client, calls = self.client()
        client.set_fixed_effort("ultracode")
        for think in (False, True, "high", None):
            client.chat([{"role": "user", "content": "hi"}], [], "claude-opus-5-5", think=think,
                        response_format={"type": "object"})
        self.assertEqual([self.effort(a) for a in calls], ["ultracode"] * 4)
        client.set_fixed_effort(None)
        client.chat([{"role": "user", "content": "hi"}], [], "claude-opus-5-5", think=False,
                    response_format={"type": "object"})
        self.assertEqual(self.effort(calls[-1]), "low")

    def test_unknown_efforts_are_refused(self):
        client, _ = self.client()
        for bad in ("ultra", "bogus", "XHIGH"):
            with self.assertRaises(ValueError):
                client.set_fixed_effort(bad)
        codex = mc.CodexCLIClient.__new__(mc.CodexCLIClient)
        self.assertIn("ultra", codex.FIXED_EFFORTS)
        self.assertNotIn("ultracode", codex.FIXED_EFFORTS)

    def test_an_ignored_pinned_effort_fails_instead_of_running_at_the_default(self):
        client, _ = self.client(stderr="Warning: Unknown --effort value 'max' — ignoring it and using the default effort.")
        client.set_fixed_effort("max")
        with self.assertRaises(mc.ModelProviderError) as caught:
            client.chat([{"role": "user", "content": "hi"}], [], "claude-opus-5-5",
                        response_format={"type": "object"})
        self.assertIn("did not apply the chosen effort 'max'", str(caught.exception))
        auto, _ = self.client(stderr="Warning: Unknown --effort value 'x'")
        auto.chat([{"role": "user", "content": "hi"}], [], "claude-opus-5-5",
                  response_format={"type": "object"})  # auto keeps the old tolerance

    def test_usage_counts_cached_input_and_the_reported_window(self):
        client, _ = self.client()
        client.chat([{"role": "user", "content": "hi"}], [], "claude-opus-5-5", response_format={"type": "object"})
        entry = client.call_usage[-1]
        self.assertEqual((entry["context_tokens"], entry["output_tokens"], entry["context_window"]),
                         (43_005, 7, 1_000_000))

    def test_rate_limit_event_shape(self):
        client, _ = self.client()
        client._note_rate_limits({"status": "allowed", "resetsAt": 1790383200, "rateLimitType": "five_hour",
                                  "unifiedWindows": {"five_hour": {"utilization": 0.11, "resetsAt": 1790383200},
                                                     "seven_day": {"utilization": 0.09, "resetsAt": 1790884800}}})
        self.assertEqual(client.rate_limits["windows"]["five_hour"], {"utilization": 0.11, "resets_at": 1790383200})
        self.assertEqual(set(client.rate_limits["windows"]), {"five_hour", "seven_day"})


class HubEffortUsageTests(HubRecoveryTests):
    def setUp(self):
        super().setUp()
        self.runtime = self.service.runtime
        catalogue = self.runtime.provider_profile_dir / "codex-cli-home"
        catalogue.mkdir(parents=True, exist_ok=True)
        (catalogue / "models_cache.json").write_text(json.dumps({"models": [
            {"slug": "gpt-5.6-sol", "context_window": 272_000, "effective_context_window_percent": 95,
             "supported_reasoning_levels": [{"effort": e} for e in ("low", "medium", "high", "xhigh", "max", "ultra")]},
            {"slug": "gpt-5.5", "context_window": 272_000,
             "supported_reasoning_levels": [{"effort": e} for e in ("low", "medium", "high", "xhigh")]}]}),
            encoding="utf-8")
        self.agent_id = self.agent()["agent_id"]  # codex-cli gpt-5.6-sol

    def test_effort_levels_follow_provider_and_catalogue(self):
        self.assertEqual(self.runtime.effort_levels("claude-cli", "claude-opus-5-5")[-1], "ultracode")
        self.assertEqual(self.runtime.effort_levels("codex-cli", "gpt-5.6-sol")[-2:], ["max", "ultra"])
        self.assertNotIn("ultra", self.runtime.effort_levels("codex-cli", "gpt-5.5"))

    def test_set_effort_validates_and_falls_back_when_the_model_changes(self):
        code, result = self.request(f"/api/agents/{self.agent_id}/effort", {"effort": "ultra"})
        self.assertEqual((code, result["effort"]), (200, "ultra"))
        self.assertEqual(self.request(f"/api/agents/{self.agent_id}/effort", {"effort": "ultracode"})[0], 400)
        self.assertEqual(self.runtime._pinned_effort(self.agent_id, "codex-cli:gpt-5.6-sol"), "ultra")
        # gpt-5.5 has no "ultra": the run uses auto rather than an effort the model lacks.
        self.assertIsNone(self.runtime._pinned_effort(self.agent_id, "codex-cli:gpt-5.5"))
        overview = self.request("/api/overview")[1]
        agent = next(a for a in overview["agents"] if a["agent_id"] == self.agent_id)
        self.assertEqual(agent["effort"], "ultra")
        self.assertEqual(overview["effort_labels"]["xhigh"], "Extra high")

    def test_model_windows(self):
        self.assertEqual(self.runtime.model_window("codex-cli", "gpt-5.6-sol")["tokens"], 258_400)
        self.assertEqual(self.runtime.model_window("claude-cli", "claude-opus-5-5")["tokens"], 1_000_000)
        self.assertEqual(self.runtime.model_window("claude-cli", "claude-sonnet-5")["source"],
                         "assumed until the first reply reports it")
        self.runtime._put_setting("window:claude-cli:claude-sonnet-5", 1_000_000)
        self.assertEqual(self.runtime.model_window("claude-cli", "claude-sonnet-5")["source"], "reported by the Claude CLI")
        self.assertEqual(AgentRuntime.history_budget_chars(1_000_000), 2_000_000)

    def test_turn_usage_and_limits_reach_the_chat_footer(self):
        chat = self.request(f"/api/agents/{self.agent_id}/chats", {"title": "t"})[1]
        task = self.request(f"/api/agents/{self.agent_id}/messages", {
            "chat_id": chat["chat_id"], "body": "hi", "request_id": "r1"})[1]
        cli = SimpleNamespace(call_usage=[
            {"context_tokens": 30_000, "output_tokens": 10, "context_window": None},
            {"context_tokens": 52_000, "output_tokens": 90, "context_window": None}],
            rate_limits={"status": "allowed", "observed_at": time.time(),
                         "windows": {"five_hour": {"utilization": 0.12, "resets_at": time.time() + 600}}})
        memory = SimpleNamespace(recent_messages=lambda cid, limit: [{"role": "user", "content": "x" * 400}])
        self.runtime._record_turn_usage(self.runtime.task(task["task_id"]), SimpleNamespace(codex_cli=cli),
                                        "codex-cli:gpt-5.6-sol", memory, 7, 2_067_200)
        usage = self.request(f"/api/agents/{self.agent_id}/chat?chat={chat['chat_id']}")[1]["usage"]
        self.assertEqual(usage["last_turn"]["context_tokens"], 52_000)
        self.assertEqual(usage["last_turn"]["peak_context_tokens"], 52_000)
        self.assertEqual(usage["last_turn"]["context_window"], 258_400)
        self.assertEqual(usage["history"]["chars"], 400)
        self.assertEqual(usage["limits"]["windows"]["five_hour"]["utilization"], 0.12)
        self.assertEqual(self.request(f"/api/agents/{self.agent_id}/usage")[1]["window"]["tokens"], 258_400)


class HistoryAndCompactionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.runtime = AgentRuntime(state_dir=self.root, runtime_path=self.root / "runtime.db",
                                    provider_profile_dir=self.root / "profile",
                                    project_root=lambda _: self.root, autostart=False)
        self.addCleanup(self.runtime.close)
        data = self.root / "agents" / "agt_x" / "data"
        data.mkdir(parents=True)
        self.memory = Memory(data / "jarvis.db")
        self.addCleanup(lambda: self.memory.close())
        self.conversation = self.memory.new_conversation("chat")
        for turn in range(30):
            self.memory.add_message(self.conversation, "user", f"question {turn} " + "q" * 900)
            self.memory.add_message(self.conversation, "assistant", f"answer {turn} " + "a" * 900)

    def test_history_budget_keeps_the_newest_messages_that_fit(self):
        from jarvis.agent import Agent

        agent = Agent.__new__(Agent)
        agent.memory = self.memory
        agent.history_message_limit, agent.history_char_budget = 24, None
        self.assertEqual(len(agent._recent_history(self.conversation)), 24)
        agent.history_char_budget = 4_000
        kept = agent._recent_history(self.conversation)
        self.assertEqual(len(kept), 4)
        self.assertTrue(kept[-1]["content"].startswith("answer 29"))
        agent.history_char_budget = 10_000_000
        self.assertEqual(len(agent._recent_history(self.conversation)), 60)

    def test_manual_compaction_keeps_recent_turns_and_condenses_the_rest(self):
        with self.runtime.db() as db:
            db.execute("INSERT INTO hub_chat_conversations VALUES ('agt_x','chat_1',?,?)", (self.conversation, time.time()))
        self.memory.close()
        outcome = self.runtime.compact_chat("agt_x", "chat_1")
        self.assertTrue(outcome["compacted"], outcome)
        self.assertGreater(outcome["messages"], 40)
        self.memory = Memory(self.root / "agents" / "agt_x" / "data" / "jarvis.db")
        remaining = self.memory.recent_messages(self.conversation, limit=1_000)
        self.assertLess(len(remaining), 60)
        self.assertTrue(remaining[-1]["content"].startswith("answer 29"))

    def test_auto_compaction_runs_only_past_the_threshold(self):
        task = {"agent_id": "agt_x", "task_id": "task_1", "project_id": "p"}
        self.runtime._maybe_auto_compact(task, self.memory, self.conversation, budget=1_000_000)
        self.assertEqual(len(self.memory.recent_messages(self.conversation, limit=1_000)), 60)
        self.runtime._maybe_auto_compact(task, self.memory, self.conversation, budget=40_000)
        self.assertLess(len(self.memory.recent_messages(self.conversation, limit=1_000)), 60)


def _only_own_tests(cls):
    own = set(vars(cls))
    for name in dir(cls):
        if name.startswith("test") and name not in own:
            setattr(cls, name, None)
    return cls


_only_own_tests(HubEffortUsageTests)

if __name__ == "__main__":
    unittest.main()
