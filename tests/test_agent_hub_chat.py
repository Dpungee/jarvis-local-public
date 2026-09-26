"""Chat is the interface to the permission-enforced agent executor."""
import json
import time
import uuid
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from tests.test_agent_hub_recovery import HubRecoveryTests
from tests.test_subscription_chat import RecordingChat
from jarvis.subscription_chat import release_text


class FakeMemory:
    """The agent-memory calls the Hub makes for a chat's native conversation."""

    def __init__(self):
        self.conversations = {}

    def conversation_exists(self, conversation_id):
        return conversation_id in self.conversations

    def new_conversation(self, title):
        conversation_id = len(self.conversations) + 1
        self.conversations[conversation_id] = []
        return conversation_id

    def add_message(self, conversation_id, role, content):
        self.conversations[conversation_id].append((role, content))


class HubChatTests(HubRecoveryTests):
    def setUp(self):
        super().setUp()
        self.memory = FakeMemory()
        self.runs = []
        self.adapter = RecordingChat()
        self.service.command_center.live.providers['codex-cli'] = self.adapter
        self.agent_id = self.agent(tool_groups=['web_research'])['agent_id']
        self.prompts = []
        self.base = '/api/agents/' + self.agent_id
        code, self.chat = self.request(self.base+'/chats', {'title': 'Regular chat'})
        self.assertEqual(code, 200)

    def send(self, body, **extra):
        return self.request(self.base+'/messages', {
            'chat_id': self.chat['chat_id'], 'body': body, 'request_id': str(uuid.uuid4()), **extra})

    def completed(self, count):
        deadline = time.monotonic()+6
        while time.monotonic() < deadline:
            code, data = self.request(self.base+'/chat?chat='+self.chat['chat_id'])
            self.assertEqual(code, 200)
            if len(data['agent_turns']) == count and all(t['state']=='COMPLETED' for t in data['agent_turns']):
                return data
            time.sleep(.02)
        self.fail(str(data))

    def execute(self):
        runtime = self.service.runtime
        case = self
        class Result(str):
            status = 'complete'
            model = 'codex-cli:gpt-5.6-sol'
            tool_calls = 1
        class Toolbox:
            def __init__(self):
                self.tools = {'web_search':object(), 'run_process':object()}
            def execute(self, name, arguments):
                return json.dumps({'ok':name in self.tools,'results':['Synthetic news source'] if name in self.tools else []})
        class Runner:
            toolbox = Toolbox()
            operator_brief = None
            def run(self, prompt, **kwargs):
                case.prompts.append(prompt)
                case.runs.append(dict(kwargs, operator_brief=self.operator_brief))
                case.assertNotIn('run_process', self.toolbox.tools)
                case.assertTrue(json.loads(self.toolbox.execute('web_search', {'query':'synthetic test'}))['ok'])
                return Result('Synthetic researched answer')
        with patch.object(runtime, '_agent_config', return_value=SimpleNamespace()), patch.object(runtime,'_open_memory',return_value=self.memory), patch.object(runtime,'_make_client',return_value=SimpleNamespace()), patch.object(runtime,'_make_agent',return_value=Runner()):
            runtime.dispatch_once()
            deadline=time.monotonic()+6
            while runtime.running_ids() and time.monotonic()<deadline:
                time.sleep(.01)
            self.assertFalse(runtime.running_ids())

    def test_chat_executes_tools_and_keeps_multiturn_context(self):
        self.assertEqual(self.send('yo whats good')[0], 200)
        self.execute()
        self.completed(1)
        self.assertEqual(self.send('Find some news from today')[0], 200)
        self.execute()
        data = self.completed(2)
        self.assertEqual(len(data['messages']), 4)
        # The agent receives only the operator's words; the earlier turn is conversation
        # history in the agent's own memory, continued by both turns of this chat.
        self.assertEqual(self.prompts, ['yo whats good', 'Find some news from today'])
        conversation = self.runs[0]['conversation_id']
        self.assertIsNotNone(conversation)
        self.assertEqual(self.runs[1]['conversation_id'], conversation)
        self.assertEqual(len(data['agent_turns']), 2)
        self.assertEqual(data['agent_turns'][-1]['tool_calls'], 1)
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual([e['detail']['tool'] for e in self.service.runtime.latest_events(agent_id=self.agent_id) if e['kind']=='tool'], ['web_search','web_search'])

    def test_closed_chat_schema_and_scope(self):
        for extra in [{'kind':'work'}, {'attachments':['file']}, {'tools':True}, {'run_id':'fake'}]:
            self.assertEqual(self.send('Hello', **extra)[0], 400)
        other = self.agent(name='Other')['agent_id']
        code, refused = self.request('/api/agents/'+other+'/chat?chat='+self.chat['chat_id'])
        self.assertEqual(code, 400)
        self.assertIn('does not belong', refused['error'])
        self.assertEqual(self.adapter.calls, [])

    def test_chat_uses_executor_not_text_only_adapter(self):
        self.service.command_center.live.providers.clear()
        self.assertEqual(self.send('Hello')[0], 200)
        self.execute()
        self.completed(1)

    def test_idempotent_request_and_conflicting_reuse(self):
        request_id = str(uuid.uuid4())
        first = self.send('Hello',request_id=request_id)[1]
        second = self.send('Hello',request_id=request_id)[1]
        self.assertEqual(first['task_id'],second['task_id'])
        self.assertEqual(self.send('Different',request_id=request_id)[0],400)
        self.assertEqual(len(self.service.runtime.chat_tasks(self.agent_id,self.chat['chat_id'])),1)

    def test_other_chat_context_is_not_replayed(self):
        self.send('Secret unrelated topic')
        self.execute()
        self.chat = self.request(self.base+'/chats',{'title':'Separate'})[1]
        self.send('Hello in new chat')
        self.execute()
        self.assertNotIn('Secret unrelated topic',self.prompts[-1])
        self.assertNotEqual(self.runs[0]['conversation_id'], self.runs[1]['conversation_id'])
        self.assertEqual(self.memory.conversations[self.runs[1]['conversation_id']], [])

    def test_history_survives_restart_without_resending(self):
        self.send('Remember the amber color')
        self.execute()
        self.service.close()
        self.service = self.open_service()
        self.server.service = self.service
        data = self.completed(1)
        self.assertEqual(data['messages'][0]['body'],'Remember the amber color')
        self.assertEqual(len(self.service.runtime.chat_tasks(self.agent_id,self.chat['chat_id'])),1)


class ConversationPrivacyTests(unittest.TestCase):
    def test_long_ordinary_prose_and_public_urls_are_not_identifiers(self):
        value = 'Tell me some interesting news and explain the context. ' * 200
        self.assertEqual(release_text(value),value)
        release_text(json.dumps([{'role':'assistant','content':value+' https://example.com/news'}]))

    def test_private_data_is_detected_across_window_edges(self):
        for offset in [0,240,255,256,490,511,512,1023,4090]:
            for sensitive in ['person@example.com','10.0.0.7',r'C:\Users\Example\private.txt','api_key=sk-'+('x'*32)]:
                with self.subTest(offset=offset,sensitive=sensitive):
                    with self.assertRaises(PermissionError):
                        release_text(('ordinary words '*400)[:offset] + sensitive + ' more harmless words '*60)

    def test_opaque_values_and_total_bound_remain_closed(self):
        with self.assertRaises(PermissionError): release_text('A'*600)
        with self.assertRaises(ValueError): release_text('word '*16001)
