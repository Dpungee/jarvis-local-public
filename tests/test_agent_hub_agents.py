"""Hub agents behave like personal agents: natural conversation and a truthful capability picture."""
import dataclasses
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jarvis.agent import Agent
from jarvis.config import Config
from jarvis.memory import Memory


def contract(*keys):
    return SimpleNamespace(missing_inputs=tuple(SimpleNamespace(key=key) for key in keys))


class ConversationalClarificationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "ws").mkdir()
        (root / "data").mkdir()
        config = dataclasses.replace(Config.load(), workspace=root / "ws", data_dir=root / "data",
                                     ollama_enabled=False, claude_cli_enabled=False, codex_cli_enabled=False)
        self.memory = Memory(root / "data" / "jarvis.db")
        self.addCleanup(self.memory.close)
        client = SimpleNamespace(models=lambda refresh=True: ["claude-cli:claude-opus-5-5"],
                                 chat=lambda *a, **k: None)
        self.agent = Agent(config, self.memory, client=client, record_training=False)
        self.calls = []

    def reply(self, message=None, error=None):
        def fake_chat(messages, tools, route, **kwargs):
            self.calls.append((messages, tools, kwargs))
            if error:
                raise error
            return message, route
        self.agent._chat = fake_chat
        return self.agent._conversational_clarification(
            operator_prompt="trade crypto for me", task_contract=contract("trading_account", "risk_limits"),
            route=SimpleNamespace(model="claude-cli:x", profile="general"),
            recent_messages=[{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}])

    def test_model_writes_the_reply_with_the_real_tool_list_and_no_tools(self):
        self.assertEqual(self.reply({"role": "assistant", "content": "I can't trade; I can research."}),
                         "I can't trade; I can research.")
        messages, tools, kwargs = self.calls[0]
        self.assertEqual(tools, [])
        self.assertIs(kwargs.get("think_override"), False)
        system = messages[0]["content"]
        self.assertIn("trading account, risk limits", system)
        self.assertIn("<available_tools>", system)
        self.assertIn("read_file", system)
        self.assertIn("Never ask for, accept or repeat passwords, private keys", system)
        self.assertEqual([m["role"] for m in messages[1:]], ["user", "assistant", "user"])

    def test_falls_back_to_the_fixed_question(self):
        self.assertIsNone(self.reply(error=RuntimeError("provider down")))
        self.assertIsNone(self.reply({"role": "assistant", "content": ""}))
        self.assertIsNone(self.reply({"role": "assistant", "content": "x", "tool_calls": [{"function": {}}]}))

    def test_off_by_default(self):
        self.assertFalse(self.agent.conversational_clarifications)


class OperatorTextScreenTests(unittest.TestCase):
    def test_personal_details_pass_and_secrets_do_not(self):
        from jarvis.subscription_chat import release_operator_text, release_text
        for allowed in ("email sam@example.com saying hi", "call me at 555-123-4567",
                        "open " + "\\".join(("C:", "Users", "example", "report.docx")),
                        "my solana address is 7EcDhSYGxXyscszYEp35KHN8vvw3svAuLKTzXwCFLtV",
                        "send 0.1 ETH to 0x52908400098527886E0F7030069857D2E4169EE7"):
            self.assertEqual(release_operator_text(allowed), allowed)
        for secret in ("api_key=sk-" + "x" * 40, "-----BEGIN RSA PRIVATE KEY-----",
                       "solana private key 5J3mBbAH58CpQ3Y5RNJpUKPE62SQ5tfcvU2JpbnkeyhfsYB1Jcn5",
                       "here: 4wBqpZM9xaSheZzJSMawUKKwhdpChKbZ5eu5ky4Vigw9LUxShCaeGpZQyjbHWVEQzRn5tTKB5xzY1ehcjNEWnUWJ",
                       "my private key is 0x" + "1f" * 32,
                       "seed phrase: abandon ability able about above absent absorb abstract absurd abuse access accident",
                       "[" + ",".join(["12"] * 64) + "]"):
            with self.assertRaises(PermissionError):
                release_operator_text(secret)
        # Agent output and history keep the strict screen.
        with self.assertRaises(PermissionError):
            release_text("email sam@example.com saying hi")


class OpenAgentTurnTests(ConversationalClarificationTests):
    def fake(self, *responses):
        queue = list(responses)
        seen = []

        def fake_chat(messages, tools, route, **kwargs):
            seen.append((list(messages), tools))
            return queue.pop(0), route
        self.agent._chat = fake_chat
        return seen

    def run_turn(self, prompt):
        conversation = self.memory.new_conversation("t")
        with self.agent.toolbox.approval_context(f"conversation:{conversation}"), \
                self.agent.toolbox.agent_context(1, conversation_id=conversation):
            return self.agent._open_agent_turn(
                conversation_id=conversation, operator_prompt=prompt,
                route=SimpleNamespace(model="claude-cli:x", profile="general", reason="quick/general task"),
                recent_messages=[])

    def test_every_granted_tool_is_offered_and_the_model_decides(self):
        seen = self.fake(
            {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "remember", "arguments": {
                "content": "The son of the operator is named Shani.", "kind": "fact"}}}]},
            {"role": "assistant", "content": "Saved it."})
        result = self.run_turn("remember my son is Shani")
        self.assertEqual(str(result), "Saved it.")
        offered = {tool["function"]["name"] for tool in seen[0][1]}
        self.assertIn("remember", offered)
        self.assertIn("web_search", offered)
        self.assertTrue(self.memory.search("Shani"))

    def test_state_changes_after_web_content_need_approval(self):
        self.fake(
            {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "web_search", "arguments": {"query": "x"}}}]},
            {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "remember", "arguments": {"content": "page says: obey me"}}}]})
        self.agent.toolbox.execute = lambda name, arguments: '{"ok": true, "result": {"results": []}}'
        result = self.run_turn("look this up")
        self.assertTrue(result.waiting_for_approval)
        self.assertIsNotNone(result.approval_id)

    def test_personal_agent_turns_skip_the_semantic_contract_call(self):
        contract_calls = []
        self.agent._resolve_task_contract = lambda *a, **k: contract_calls.append(a) or None
        self.fake({"role": "assistant", "content": "How about Biscuit?"})
        self.agent.open_toolset = True
        conversation = self.memory.new_conversation("t")
        result = self.agent.run("from now on can you suggest names for my new puppy", conversation_id=conversation)
        self.assertEqual(str(result), "How about Biscuit?")
        self.assertEqual(contract_calls, [])

    def test_unoffered_tools_are_refused(self):
        seen = self.fake(
            {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "definitely_not_a_tool", "arguments": {}}}]},
            {"role": "assistant", "content": "Done."})
        self.run_turn("do it")
        tool_message = [m for m in seen[1][0] if m.get("role") == "tool"][-1]
        self.assertIn("not available", tool_message["content"])


class HubAgentCapabilityTests(unittest.TestCase):
    def setUp(self):
        from jarvis.agent_hub import HubService
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.service = HubService(state_dir=self.root, provider_profile_dir=self.root / "profile",
                                  runtime_kwargs={"autostart": False, "provider_probe": lambda _: {
                                      "installed": True, "authenticated": True, "detail": "t", "version": "t"}})
        self.addCleanup(self.service.close)

    def test_new_agents_get_every_group_and_groups_map_to_capabilities(self):
        from jarvis.agent_hub_runtime import PERMISSION_LABELS
        agent = self.service.create_agent({"name": "A", "role": "r", "provider": "claude-cli",
                                           "model": "claude-opus-5-5", "enable": True})
        self.assertTrue(all(agent["permissions"][key] for key in PERMISSION_LABELS))
        runtime = self.service.runtime
        config = runtime._agent_config(agent_id=agent["agent_id"], project_root=self.root,
                                       reference="claude-cli:x", permissions=agent["permissions"])
        self.assertEqual((config.computer_access, config.external_access, config.execution_mode),
                         ("trusted-desktop", "trusted-external", "trusted-host"))
        off = runtime._agent_config(agent_id=agent["agent_id"], project_root=self.root, reference="claude-cli:x",
                                    permissions={**agent["permissions"], "computer": False, "accounts": False})
        self.assertEqual((off.computer_access, off.external_access), ("disabled", "disabled"))

    def test_full_access_is_an_explicit_operator_action(self):
        limited = {"files_read": True, "files_write": False, "web_research": False, "run_commands": False,
                   "memory": False, "computer": False, "accounts": False, "schedules": False, "skills": False}
        agent = self.service.create_agent({"name": "B", "role": "r", "provider": "claude-cli",
                                           "model": "claude-opus-5-5", "permissions": limited})
        self.assertFalse(self.service.runtime.agent_settings(agent["agent_id"])["permissions"]["computer"])
        result = self.service.grant_full_access()
        self.assertIn(agent["agent_id"], result["updated"])
        self.assertTrue(all(self.service.runtime.agent_settings(agent["agent_id"])["permissions"].values()))

    def test_schedules_run_in_their_chat_and_quiet_watches_stay_hidden(self):
        from datetime import datetime
        # A daily 08:00 job is tomorrow, independent of host timezone or wall clock.
        base = datetime(2026, 9, 26, 9, 0).timestamp()
        fixed_clock = patch("jarvis.agent_hub_runtime._now", return_value=base)
        fixed_clock.start()
        self.addCleanup(fixed_clock.stop)
        agent = self.service.create_agent({"name": "C", "role": "r", "provider": "claude-cli",
                                           "model": "claude-opus-5-5", "enable": True})
        chat = self.service.create_chat(agent["agent_id"], {"title": "Watch"})["chat_id"]
        task = self.service.chat(agent["agent_id"], chat, {"body": "watch bitcoin", "request_id": "r1"})
        toolbox = SimpleNamespace(tools={})
        self.service.runtime._install_schedule_tools(toolbox, task)
        create = toolbox.tools["schedule_create"].function
        created = create(name="BTC watch", instructions="Check the bitcoin price.", every_minutes=5,
                         notify_when="the price is below $80,000")
        self.assertEqual(created["kind"], "interval")
        with self.assertRaises(ValueError):
            create(name="x", instructions="y", every_minutes=5, daily_at="08:00")
        with self.assertRaises(ValueError):
            create(name="x", instructions="y", daily_at="25:00")
        daily = create(name="News", instructions="Crypto news.", daily_at="08:00")
        self.assertIn("T08:00", daily["next_run"])
        queued = self.service.runtime.run_due_schedules(now=base + 400)
        self.assertEqual(len(queued), 1)
        run = self.service.runtime.task(queued[0])
        self.assertIn("Only report if this is true: the price is below $80,000", run["request"])
        self.assertIn(queued[0], [t["task_id"] for t in self.service.runtime.chat_tasks(agent["agent_id"], chat)])
        self.service.runtime._set(queued[0], state="COMPLETED", result="NO_UPDATE")
        view = self.service.chat(agent["agent_id"], chat)
        self.assertNotIn(queued[0], [t["task_id"] for t in view["agent_turns"]])
        # The same due slot is never queued twice.
        self.assertEqual(self.service.runtime.run_due_schedules(now=base + 400), [])


class ChatScopedTaintGrantTests(OpenAgentTurnTests):
    def test_an_allowed_kind_of_step_runs_without_a_new_approval(self):
        self.agent.tainted_tools_allowed = frozenset({"remember"})
        calls = []

        def execute(name, arguments):
            calls.append(name)
            return '{"ok": true, "result": {"results": []}}'
        self.agent.toolbox.execute = execute
        self.fake(
            {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "web_search", "arguments": {"query": "x"}}}]},
            {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "remember", "arguments": {"content": "a fact"}}}]},
            {"role": "assistant", "content": "Done."})
        result = self.run_turn("look this up and remember it")
        self.assertFalse(result.waiting_for_approval)
        self.assertEqual(calls, ["web_search", "remember"])

    def test_the_approval_note_says_exactly_what_will_run_and_what_is_done(self):
        self.fake(
            {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "web_search", "arguments": {"query": "sol prices"}}}]},
            {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "remember", "arguments": {"content": "keep this lesson"}}}]})
        self.agent.toolbox.execute = lambda name, arguments: '{"ok": true, "result": {"results": []}}'
        result = self.run_turn("paper trade")
        self.assertTrue(result.waiting_for_approval)
        self.assertIn("remember keep this lesson", str(result))
        self.assertEqual(self.agent._describe_step("run_process", {"program": "python", "arguments": ["bot.py", "--test"]}),
                         "run `python bot.py --test`")
        self.assertIn("web search sol prices", str(result))


class ScheduleTimingTests(HubAgentCapabilityTests):
    def test_daily_and_interval_overlap_queue_each_once_at_the_due_boundary(self):
        from datetime import datetime

        base = datetime(2026, 9, 26, 7, 55).timestamp()
        with patch("jarvis.agent_hub_runtime._now", return_value=base):
            tools = self.tools()
            create = tools["schedule_create"].function
            interval = create(name="Interval", instructions="interval check", every_minutes=5)
            daily = create(name="Daily", instructions="daily check", daily_at="08:00")
            self.assertEqual(interval["next_run"], daily["next_run"])
            self.assertEqual(self.service.runtime.run_due_schedules(now=base + 299.999), [])
            queued = self.service.runtime.run_due_schedules(now=base + 300)
            self.assertEqual(len(queued), 2)
            self.assertEqual(len(set(queued)), 2)
            self.assertEqual({self.service.runtime.task(task_id)["request"] for task_id in queued}, {
                "⏰ Scheduled job “Interval”: interval check",
                "⏰ Scheduled job “Daily”: daily check",
            })
            self.assertEqual(self.service.runtime.run_due_schedules(now=base + 300), [])
            self.assertEqual(self.service.runtime.run_due_schedules(now=base + 400), [])
            with self.service.runtime.db() as db:
                rows = list(db.execute("SELECT last_task_id, next_run_at FROM hub_schedules"))
            self.assertEqual({row["last_task_id"] for row in rows}, set(queued))
            self.assertTrue(all(row["next_run_at"] > base + 400 for row in rows))

    def tools(self):
        from types import SimpleNamespace as NS
        agent = self.service.create_agent({"name": "S", "role": "r", "provider": "claude-cli",
                                           "model": "claude-opus-5-5", "enable": True})
        chat = self.service.create_chat(agent["agent_id"], {"title": "T"})["chat_id"]
        task = self.service.chat(agent["agent_id"], chat, {"body": "report later", "request_id": "t1"})
        toolbox = NS(tools={})
        self.service.runtime._install_schedule_tools(toolbox, self.service.runtime.task(task["task_id"])
                                                     if hasattr(self.service.runtime, "task") else task)
        return toolbox.tools

    def test_relative_one_time_runs_and_the_result_shows_the_wait(self):
        tools = self.tools()
        self.assertIn("in_minutes", tools["schedule_create"].parameters["properties"])
        self.assertIn("local time now is", tools["schedule_create"].description)
        made = tools["schedule_create"].function(name="final report", instructions="report", in_minutes=190)
        self.assertEqual(made["kind"], "once")
        self.assertEqual(made["first_run_in"], "3 h 10 min")
        self.assertIn("now", made)

    def test_an_offset_time_is_converted_not_read_as_local(self):
        from datetime import datetime, timedelta, timezone
        tools = self.tools()
        when = (datetime.now(timezone.utc) + timedelta(hours=2)).replace(second=0, microsecond=0)
        made = tools["schedule_create"].function(name="r", instructions="x", at=when.isoformat())
        self.assertIn(made["first_run_in"], {"1 h 59 min", "2 h 0 min"})


class HubTaintGrantTests(HubAgentCapabilityTests):
    def test_approving_a_step_after_web_content_allows_that_kind_for_the_chat(self):
        import json as _json
        agent = self.service.create_agent({"name": "D", "role": "r", "provider": "claude-cli",
                                           "model": "claude-opus-5-5", "enable": True})
        runtime = self.service.runtime
        chat = self.service.create_chat(agent["agent_id"], {"title": "Grant"})["chat_id"]
        task = self.service.chat(agent["agent_id"], chat, {"body": "run it", "request_id": "g1"})
        config = runtime._agent_config(agent_id=agent["agent_id"], project_root=self.root, reference="claude-cli:x",
                                       permissions=agent["permissions"])
        memory = runtime._open_memory(config)
        conversation = memory.new_conversation("c")
        _ok, approval_id = memory.authorize_or_request(
            "after_web_content", _json.dumps({"tool": "run_process", "arguments": {"program": "python"}}),
            "reason", approval_scope=f"conversation:{conversation}")
        memory.close()
        runtime._set(task["task_id"], state="WAITING_APPROVAL", approval_id=approval_id, conversation_id=conversation)
        runtime.decide_approval(task["task_id"], True)
        with runtime.db() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM hub_taint_grants")]
        self.assertEqual([(r["agent_id"], r["conversation_id"], r["tool"]) for r in rows],
                         [(agent["agent_id"], conversation, "run_process")])


class OpenRoutingTests(unittest.TestCase):
    def test_open_ended_jobs_are_not_specialised_lanes(self):
        from jarvis import agent as agent_module
        from jarvis.natural_language import operator_action_text
        prompt = ("are you able to paper trade memecoins on solana for the next 5hrs and tell me if you "
                  "made money or lost money (paper money) and learn from every mistake")
        action = operator_action_text(prompt)
        self.assertFalse(agent_module._requires_coding(action))
        self.assertFalse(bool(agent_module._CURRENT_EVENT_INFO_INTENT.search(prompt)))


class _StubDevTools:
    def __init__(self):
        self.calls = []

    def call(self, method, params=None, session=None, timeout=15.0):
        self.calls.append((method, params))
        return {}

    def pump(self, seconds):
        return None


class BrowserSessionGuardTests(unittest.TestCase):
    def session(self, element):
        from jarvis.web_browser import BrowserSession
        devtools = _StubDevTools()
        session = BrowserSession(Path(tempfile.gettempdir()) / "unused-browser-profile")
        session._session = lambda agent_id: (devtools, "s1")
        session._settle = lambda *a, **k: None
        session._read = lambda d, s, a=None: {"url": "https://shop.example/cart", "elements": []}
        session._until_rendered = lambda *a, **k: None

        def evaluate(d, s, expression):
            return None if "scrollIntoView" in expression else dict(element, x=5, y=5, in_view=True)
        session._evaluate = evaluate
        return session, devtools

    def clicks(self, devtools):
        return [c for c in devtools.calls if c[0] == "Input.dispatchMouseEvent"]

    def test_commit_buttons_need_the_confirmed_path(self):
        from jarvis.web_browser import BrowserError
        for label in ("Place order", "Book now", "Send", "Complete reservation", "Pay $42.10", "Delete"):
            with self.subTest(label=label):
                session, devtools = self.session({"tag": "button", "type": "button", "text": label,
                                                  "form_fields": "", "name": "", "id": "", "autocomplete": ""})
                with self.assertRaises(BrowserError):
                    session.click("a", 3)
                self.assertEqual(self.clicks(devtools), [])
                session.click("a", 3, confirmed=True)
                self.assertEqual(len(self.clicks(devtools)), 3)

    def test_committing_links_require_approval_even_with_long_labels(self):
        from jarvis.web_browser import needs_confirmation
        slot = {"tag": "a", "href": "https://www.opentable.com/booking/details?t=1900",
                "text": "7:00 PM Reserve table at Quattro Gatti Ristorante e Pizzeria restaurant"}
        self.assertTrue(needs_confirmation(slot))
        self.assertTrue(needs_confirmation({"tag": "a", "href": "", "text": "7:00 PM Reserve table at Cipollina restaurant"}))
        self.assertTrue(needs_confirmation({"tag": "a", "href": "", "text": "Reserve now"}))
        self.assertTrue(needs_confirmation({**slot, "text": "Pay now"}))
        self.assertTrue(needs_confirmation({"tag": "button", "text": "Complete reservation"}))
        self.assertTrue(needs_confirmation({"tag": "button", "text": "Reserve"}))
        self.assertFalse(needs_confirmation({"tag": "button", "text": "Add to cart"}))
        self.assertFalse(needs_confirmation({"tag": "div", "role": "button", "text": (
            "Show more times near 7:00 PM for booking a table for two people this Friday")}))
        self.assertTrue(needs_confirmation({"tag": "button", "type": "submit", "text": "Continue",
                                            "form_fields": "text cardnumber  cc-number"}))

    def test_a_re_rendered_page_never_clicks_a_different_element(self):
        import re as _re
        from jarvis.web_browser import BrowserError, BrowserSession
        devtools = _StubDevTools()
        session = BrowserSession(Path(tempfile.gettempdir()) / "unused-browser-profile")
        session._session = lambda agent_id: (devtools, "s1")
        session._settle = lambda *a, **k: None
        session._until_rendered = lambda *a, **k: None
        session._read = lambda d, s, a=None: {"elements": []}
        # The agent was shown "Next" as 3; the page re-rendered and 3 is now "Delete account".
        dom = {3: {"tag": "button", "label": "Delete account", "text": "Delete account"},
               7: {"tag": "button", "label": "Next", "text": "Next"}}
        session._shown["a"] = {3: {"ref": 3, "tag": "button", "label": "Next"}}

        def evaluate(d, s, expression):
            if "scrollIntoView" in expression:
                return None
            if expression.startswith("(() => { const el = document.querySelector("):  # re-tag
                new, old = (int(n) for n in _re.findall(r"(\d+)", expression)[:2])
                dom[old] = dom.pop(new)
                return None
            if "location.href" == expression:
                return "https://shop.example/"
            match = _re.search(r"\((\d+)\)$", expression)
            if match:
                item = dom.get(int(match.group(1)))
                return None if item is None else dict(item, type="button", form_fields="", name="", id="",
                                                      autocomplete="", href="", x=1, y=1)
            return {"elements": [dict(v, ref=k, href=None) for k, v in dom.items()]}
        session._evaluate = evaluate
        page = session.click("a", 3)
        self.assertEqual(page["clicked"], "Next")
        self.assertEqual(dom[3]["label"], "Next")
        dom.clear()
        with self.assertRaises(BrowserError):
            session.click("a", 3)

    def test_ordinary_links_click_without_asking(self):
        session, devtools = self.session({"tag": "a", "type": "", "text": "Italian restaurants near me",
                                          "form_fields": "", "name": "", "id": "", "autocomplete": ""})
        self.assertEqual(session.click("a", 1)["clicked"], "Italian restaurants near me")
        self.assertEqual(len(self.clicks(devtools)), 3)

    def test_password_payment_and_code_fields_are_never_typed_into(self):
        from jarvis.web_browser import BrowserError
        for field in ({"type": "password", "name": "pw"}, {"type": "text", "name": "cardnumber"},
                      {"type": "text", "name": "x", "autocomplete": "cc-number"},
                      {"type": "text", "name": "otp"}, {"type": "tel", "name": "cvc"}):
            with self.subTest(field=field):
                element = {"tag": "input", "text": "", "form_fields": "", "id": "", "autocomplete": "", **field}
                session, devtools = self.session(element)
                with self.assertRaises(BrowserError):
                    session.type("a", 2, "secret")
                self.assertFalse(any(c[0] == "Input.insertText" for c in devtools.calls))

    def test_ordinary_fields_are_typed(self):
        session, devtools = self.session({"tag": "input", "type": "search", "text": "Search", "name": "q",
                                          "form_fields": "", "id": "", "autocomplete": ""})
        session.type("a", 2, "tacos near 10001", submit=True)
        self.assertIn(("Input.insertText", {"text": "tacos near 10001"}), devtools.calls)

    def test_only_public_web_addresses_open(self):
        from jarvis.web_browser import BrowserError, check_public_url
        for url in ("http://127.0.0.1:8790/", "http://localhost/", "http://192.168.1.1/", "http://10.0.0.5/",
                    "file:///C:/Windows/win.ini", "javascript:alert(1)", "https://user:pw@8.8.8.8/",
                    "http://[::1]/", "http://169.254.169.254/latest"):
            with self.subTest(url=url), self.assertRaises(BrowserError):
                check_public_url(url)
        self.assertEqual(check_public_url("8.8.8.8/dns"), "https://8.8.8.8/dns")


class HubBrowserToolTests(HubAgentCapabilityTests):
    def toolbox(self, button):
        from jarvis.tools import ToolBox
        agent = self.service.create_agent({"name": "Br", "role": "r", "provider": "claude-cli",
                                           "model": "claude-opus-5-5", "enable": True})
        runtime = self.service.runtime
        config = runtime._agent_config(agent_id=agent["agent_id"], project_root=self.root,
                                       reference="claude-cli:x", permissions=agent["permissions"])
        memory = runtime._open_memory(config)
        self.addCleanup(memory.close)
        clicked = []
        page = {"button": button}

        class FakeSession:
            def describe(self, agent_id, ref):
                import hashlib
                return {"page": "https://shop.example/checkout?session=abc", "text": page["button"],
                        "tag": "button", "payload_sha256": hashlib.sha256(page["button"].encode()).hexdigest()}

            def click(self, agent_id, ref, confirmed=False, expected_payload_sha256=None):
                assert expected_payload_sha256 == self.describe(agent_id, ref)["payload_sha256"]
                clicked.append((agent_id, ref, confirmed))
                return {"url": "https://shop.example/done", "elements": []}
        runtime.browser = lambda: FakeSession()
        toolbox = ToolBox(config, memory)
        from jarvis.agent_hub_runtime import BROWSER_TOOLS, allowed_tools
        runtime._install_browser_tools(toolbox, agent["agent_id"], allowed_tools(agent["permissions"]))
        self.assertTrue(BROWSER_TOOLS <= set(toolbox.tools))
        return toolbox, memory, clicked, page, agent["agent_id"]

    def test_committing_click_asks_once_for_that_exact_page_and_button(self):
        import json as _json
        toolbox, memory, clicked, page, agent_id = self.toolbox("Place order")
        with toolbox.approval_context("conversation:7"):
            blocked = _json.loads(toolbox.execute("browser_confirm_click", {"ref": 12}))
        self.assertTrue(blocked["approval_required"])
        self.assertEqual(clicked, [])
        request = next(a for a in memory.list_approvals() if a["id"] == blocked["approval_id"])
        self.assertIn("https://shop.example/checkout", request["resource"])
        self.assertIn("Place order", request["resource"])
        self.assertNotIn("session=abc", request["resource"])
        memory.decide_approval(blocked["approval_id"], True)
        # The element number may differ after a re-read; the approval is for the page and button.
        with toolbox.approval_context("conversation:7"):
            done = _json.loads(toolbox.execute("browser_confirm_click", {"ref": 30}))
        self.assertTrue(done["ok"], done)
        self.assertEqual(clicked, [(agent_id, 30, True)])
        with toolbox.approval_context("conversation:7"):
            again = _json.loads(toolbox.execute("browser_confirm_click", {"ref": 30}))
        self.assertTrue(again["approval_required"])  # one use
        self.assertEqual(len(clicked), 1)

    def test_a_different_button_is_not_covered_by_the_approval(self):
        import json as _json
        toolbox, memory, clicked, page, _agent = self.toolbox("Place order")
        with toolbox.approval_context("conversation:8"):
            blocked = _json.loads(toolbox.execute("browser_confirm_click", {"ref": 4}))
        memory.decide_approval(blocked["approval_id"], True)
        page["button"] = "Place order and subscribe"
        with toolbox.approval_context("conversation:8"):
            other = _json.loads(toolbox.execute("browser_confirm_click", {"ref": 4}))
        self.assertTrue(other["approval_required"])
        self.assertEqual(clicked, [])

    def test_browser_permission_off_offers_no_browser_tools(self):
        from jarvis.agent_hub_runtime import BROWSER_TOOLS, allowed_tools
        self.assertFalse(allowed_tools({"browser": False, "web_research": True}) & BROWSER_TOOLS)

    def test_the_brief_tells_the_agent_to_let_the_operator_sign_in(self):
        from types import SimpleNamespace as NS
        brief = self.service.runtime._operator_brief(
            {"attempt": 1}, NS(display_name="R", role="", purpose=""), "", {"browser": True})
        self.assertIn("sign in in the agent browser window themselves", brief)
        self.assertIn("browser_confirm_click", brief)


if __name__ == "__main__":
    unittest.main()
