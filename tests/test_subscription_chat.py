import io
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
import urllib.request
from pathlib import Path
from unittest.mock import patch

from jarvis.command_center import CommandCenterService
from jarvis.subscription_chat import ClaudeSubscription, CodexSubscription, ChatTransportError, safe_environment, release_text


class RecordingChat:
    label, simulated, status, reason = 'Test text transport', False, 'UNTESTED', 'Test only'
    def __init__(self):
        self.calls = []
        self.gate = threading.Event()
        self.gate.set()
    def chat(self, model, messages, cancel, progress):
        self.calls.append((model, messages))
        progress('Partial synthetic response')
        while not self.gate.wait(.01):
            if cancel.is_set():
                raise ChatTransportError('Cancelled')
        return 'Synthetic answer', {'input_tokens': 10, 'output_tokens': 2}


class LiveConversationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.adapter = RecordingChat()
        self.start()
        self.agent = self.service.create_agent({'name':'Test','role':'Assistant','purpose':'DO NOT EXPORT PURPOSE',
            'provider':'claude-cli','model':'default','project_id':'test'})['agent_id']
        self.service.set_lifecycle(self.agent, 'start')
    def start(self):
        self.service = CommandCenterService(self.root/'r.db', self.root/'e.db', providers={'claude-cli':self.adapter}, simulator_interval=.01)
    def tearDown(self):
        self.adapter.gate.set()
        self.service.close()
        self.temp.cleanup()
    def send(self, body='Hello', **fields):
        return self.service.conversation(self.agent,'test',{'body':body,'request_id':str(uuid.uuid4()),**fields})
    def snapshot(self, chat=None):
        return self.service.conversation(self.agent,'test',chat_id=chat)
    def await_state(self, expected='COMPLETED'):
        deadline=time.monotonic()+4
        while time.monotonic()<deadline:
            turns=self.snapshot()['live_turns']
            if turns and turns[-1]['state']==expected:
                return turns[-1]
            time.sleep(.01)
        self.fail(f'Expected {expected}: {self.snapshot()["live_turns"]}')
    def test_multiturn_only_authorized_history_and_usage(self):
        self.send('The color is amber');self.await_state()
        self.send('What color?');turn=self.await_state()
        self.assertEqual([m['role'] for m in self.adapter.calls[-1][1]],['user','assistant','user'])
        self.assertNotIn('DO NOT EXPORT PURPOSE',json.dumps(self.adapter.calls))
        self.assertTrue(turn['release_digest'])
        self.assertEqual(self.snapshot()['live_usage'][-1]['input_tokens'],10)
    def test_model_switch_back_never_replays_previous_segment(self):
        self.send('Old segment');self.await_state()
        for model in ['second','default']:
            self.service.update_model(self.agent,{'provider':'claude-cli','model':model})
            self.send('New segment');self.await_state()
            self.assertEqual(len(self.adapter.calls[-1][1]),1)
    def test_chat_isolation(self):
        self.send('General context');self.await_state()
        chat=self.service.composer_control(self.agent,'test','chats',{'title':'Separate'})['chat_id']
        self.send('Separate context',chat_id=chat)
        deadline=time.monotonic()+3
        while time.monotonic()<deadline and len(self.adapter.calls)<2:time.sleep(.01)
        self.assertEqual(self.adapter.calls[-1][1],[{'role':'user','content':'Separate context'}])
    def test_idempotency_and_conflicting_reuse(self):
        key=str(uuid.uuid4())
        first=self.send(request_id=key)
        second=self.send(request_id=key)
        self.assertEqual(first['message_id'],second['message_id'])
        with self.assertRaises(ValueError):self.send('Changed',request_id=key)
        self.await_state();self.assertEqual(len(self.adapter.calls),1)
    def test_secret_private_path_and_attachments_rejected(self):
        for body in ['Read ' + '\\'.join(('C:', 'Users', 'example', 'private.txt')),
                     'password=supersecretpassword123']:
            with self.assertRaises(PermissionError):self.send(body)
        with self.assertRaises(PermissionError):self.send(attachments=['attachment'])
        self.assertEqual(self.adapter.calls,[])
    def test_restart_history_retained_and_no_automatic_replay(self):
        self.send();self.await_state()
        self.service.close();self.start()
        self.assertEqual(self.snapshot()['live_turns'][0]['state'],'COMPLETED')
        self.send('Continue');self.await_state()
        self.assertEqual(len(self.adapter.calls[-1][1]),3)
    def test_stop_prevents_late_completion_and_queued_work(self):
        self.adapter.gate.clear();self.send();self.await_state('RUNNING')
        self.send('Queued')
        self.service.set_lifecycle(self.agent,'stop')
        self.adapter.gate.set();time.sleep(.1)
        self.assertTrue(all(t['state']=='CANCELLED' for t in self.snapshot()['live_turns']))
    def test_pause_resume_restarts_turn_explicitly(self):
        self.adapter.gate.clear();self.send();self.await_state('RUNNING')
        self.service.set_lifecycle(self.agent,'pause');self.await_state('PAUSED')
        time.sleep(.05);self.adapter.gate.set()
        self.service.set_lifecycle(self.agent,'resume');self.await_state()
    def test_steering_waits_for_target_then_replays_its_answer(self):
        self.adapter.gate.clear();message=self.send(kind='work');self.await_state('RUNNING')
        self.send('Make it shorter',kind='steer',run_id=message['run_id'])
        self.assertEqual(len(self.adapter.calls),1)
        self.adapter.gate.set();self.await_state()
        self.assertEqual(len(self.adapter.calls[-1][1]),3)
    def test_stale_steering_and_legacy_execution_denied(self):
        with self.assertRaises(ValueError):self.send(kind='steer',run_id='absent')
        with self.assertRaises(ValueError):self.service.assign_task(self.agent,{'title':'No','prompt':'No'})
    def test_failed_target_does_not_release_orphan_steering(self):
        self.service._conversation_stop.set()
        self.service._conversation_thread.join(1)
        target=self.send(kind='work')
        self.send('Followup',kind='steer',run_id=target['run_id'])
        with self.service.conversations.db() as db:
            db.execute("UPDATE cc_live_turns SET state='FAILED' WHERE turn_id=?",(target['run_id'],))
        self.service.live.tick(self.service.list_agents())
        self.await_state('FAILED')
        self.assertEqual(self.adapter.calls,[])
    def test_malformed_message_fields_fail_closed(self):
        for fields in [{'kind':[]},{'attachments':{}},{'body':''}]:
            with self.assertRaises(ValueError):self.send(**fields)
        self.assertEqual(self.adapter.calls,[])
    def test_provider_exception_does_not_leak(self):
        with patch.object(self.adapter,'chat',side_effect=RuntimeError('private host detail')):
            self.send();self.await_state('FAILED')
        self.assertNotIn('private host detail',json.dumps(self.snapshot()))
    def test_interrupted_turn_requires_resume_after_restart(self):
        self.adapter.gate.clear();self.send();self.await_state('RUNNING')
        # Emulate a persisted process interruption without importing another live database.
        self.service.set_lifecycle(self.agent,'pause');time.sleep(.05)
        self.service.close()
        with self.service.conversations.db() as db:db.execute("UPDATE cc_live_turns SET state='RUNNING'")
        self.start();self.assertEqual(self.snapshot()['live_turns'][0]['state'],'INTERRUPTED')
        count=len(self.adapter.calls);time.sleep(.05);self.assertEqual(len(self.adapter.calls),count)


class FakeProcess:
    def __init__(self, events):
        self.stdin=io.StringIO();self.stdout=io.StringIO('\n'.join(json.dumps(e) for e in events)+'\n')
        self.returncode=0
    def poll(self):return 0
    def wait(self,timeout=None):return 0
    def kill(self):pass


class TransportTests(unittest.TestCase):
    def test_cancelled_transport_never_probes_or_launches(self):
        cancelled=threading.Event();cancelled.set()
        with patch('jarvis.subscription_chat.subprocess.run') as run, patch('jarvis.subscription_chat.subprocess.Popen') as launch:
            for adapter in [ClaudeSubscription(),CodexSubscription()]:
                with self.assertRaisesRegex(ChatTransportError,'Cancelled'):
                    adapter.chat('default',[],cancelled,lambda _:None)
        run.assert_not_called();launch.assert_not_called()
    def test_cancel_during_claude_auth_prevents_launch(self):
        cancelled=threading.Event()
        def auth(*args,**kwargs):
            cancelled.set()
            return subprocess.CompletedProcess([],0,json.dumps({'loggedIn':True,'authMethod':'claude.ai','apiProvider':'firstParty'}),'')
        adapter=ClaudeSubscription();adapter.executable='fake'
        with patch('jarvis.subscription_chat.subprocess.run',side_effect=auth),patch('jarvis.subscription_chat.subprocess.Popen') as launch:
            with self.assertRaisesRegex(ChatTransportError,'Cancelled'):
                adapter.chat('default',[{'role':'user','content':'Hi'}],cancelled,lambda _:None)
        launch.assert_not_called()
    def test_environment_drops_api_keys_and_parent_agent_context(self):
        with patch.dict(os.environ,{'ANTHROPIC_API_KEY':'fake','OPENAI_API_KEY':'fake','CODEX_THREAD_ID':'private','CLAUDE_CONFIG_DIR':'private'}):
            env=safe_environment()
        for key in ['ANTHROPIC_API_KEY','OPENAI_API_KEY','CODEX_THREAD_ID','CLAUDE_CONFIG_DIR']:self.assertNotIn(key,env)
    def run_transport(self, events, auth=None):
        adapter=ClaudeSubscription();adapter.executable='test-executable'
        auth=auth or {'loggedIn':True,'authMethod':'claude.ai','apiProvider':'firstParty'}
        self.process=FakeProcess(events)
        with patch('jarvis.subscription_chat.subprocess.run',return_value=subprocess.CompletedProcess([],0,json.dumps(auth))), \
             patch('jarvis.subscription_chat.subprocess.Popen',return_value=self.process) as launch:
            result=adapter.chat('sonnet',[{'role':'user','content':'Synthetic hello'}],threading.Event(),lambda _:None)
        return result,launch.call_args
    def test_tool_free_arguments_and_protocol_response(self):
        result,call=self.run_transport([{'type':'system','subtype':'init','tools':[],'mcp_servers':[]},
            {'type':'result','subtype':'success','is_error':False,'result':'Hello','usage':{'input_tokens':3}}])
        self.assertEqual(result,('Hello',{'input_tokens':3}))
        args=call.args[0]
        self.assertEqual(args[args.index('--tools')+1],'')
        for flag in ['--safe-mode','--strict-mcp-config','--no-session-persistence','--system-prompt']:self.assertIn(flag,args)
    def test_exposed_tools_fail_closed(self):
        with self.assertRaisesRegex(ChatTransportError,'isolation'):
            self.run_transport([{'type':'system','subtype':'init','tools':['Bash']}])
    def test_expired_oauth_is_actionable(self):
        with self.assertRaisesRegex(ChatTransportError,'OAuth session expired'):
            self.run_transport([{'type':'result','subtype':'success','is_error':True,'result':'Failed to authenticate: OAuth session expired and could not be refreshed'}])
    def test_paid_api_auth_rejected_before_launch(self):
        with self.assertRaisesRegex(ChatTransportError,'API-key'):
            self.run_transport([],{'loggedIn':True,'authMethod':'api_key','apiProvider':'firstParty'})
    def test_release_limit(self):
        with self.assertRaises(ValueError):release_text('a'*80001)


class CodexTransportTests(unittest.TestCase):
    def test_cancel_during_codex_auth_prevents_launch(self):
        cancelled=threading.Event();adapter=CodexSubscription()
        def auth(*args,**kwargs):
            cancelled.set()
            return subprocess.CompletedProcess([],0,'Logged in using ChatGPT','')
        with patch('jarvis.subscription_chat.shutil.which',return_value='fake'), \
             patch.object(adapter,'verify_isolation',return_value=Path('catalog.json')), \
             patch('jarvis.subscription_chat.subprocess.run',side_effect=auth), \
             patch('jarvis.subscription_chat.subprocess.Popen') as launch:
            with self.assertRaisesRegex(ChatTransportError,'Cancelled'):
                adapter.chat('default',[{'role':'user','content':'Hi'}],cancelled,lambda _:None)
        launch.assert_not_called()
    def verify(self, *, tools=None, version='codex-cli 0.146.1', mode='code_mode_only', context=None):
        def invoke(args, **kwargs):
            if '--version' in args:return subprocess.CompletedProcess(args,0,version,'')
            if 'models' in args:
                return subprocess.CompletedProcess(args,0,json.dumps({'models':[{'slug':'gpt-5.6-sol','tool_mode':mode}]}),'')
            base=next(arg for arg in args if arg.startswith('model_providers.offline_probe.base_url='))
            url=json.loads(base.split('=',1)[1])+'/responses'
            body={'model':'gpt-5.6-sol','tools':[] if tools is None else tools,'input':context or []}
            request=urllib.request.Request(url,data=json.dumps(body).encode(),headers={'Content-Type':'application/json'})
            try:urllib.request.urlopen(request,timeout=5)
            except urllib.error.HTTPError as exc:self.assertEqual(exc.code,400)
            return subprocess.CompletedProcess(args,1,'','')
        with tempfile.TemporaryDirectory() as directory,patch('jarvis.subscription_chat.subprocess.run',side_effect=invoke):
            return CodexSubscription().verify_isolation('fake-codex',directory,safe_environment(),'gpt-5.6-sol')
    def test_exact_version_catalog_and_empty_tools_pass(self):
        self.verify()
    def test_tool_exposure_blocks_user_payload(self):
        with self.assertRaisesRegex(ChatTransportError,'tool-registry'):
            self.verify(tools=[{'type':'function','name':'view_image'}])
    def test_new_version_requires_reverification(self):
        with self.assertRaisesRegex(ChatTransportError,'version changed'):
            self.verify(version='codex-cli 0.999.0')
    def test_non_host_model_mode_rejected(self):
        with self.assertRaisesRegex(ChatTransportError,'no verified tool-free'):
            self.verify(mode='default')
    def test_ambient_context_rejected(self):
        with self.assertRaisesRegex(ChatTransportError,'ambient-context'):
            self.verify(context=[{'text':'<environment_context>private</environment_context>'}])
    def test_real_command_has_no_mock_provider_override_and_keeps_isolation(self):
        args=CodexSubscription().command('codex',Path('catalog.json'),'gpt-5.6-sol')
        self.assertFalse(any('offline_probe' in item for item in args))
        for item in ['--ignore-user-config','--ignore-rules','--ephemeral','--strict-config',
                     'project_doc_max_bytes=0','skills.include_instructions=false','code_mode_host']:
            self.assertIn(item,args)
    def test_subscription_jsonl_response(self):
        process=FakeProcess([{'type':'item.completed','item':{'type':'agent_message','text':'amber'}},
                             {'type':'turn.completed','usage':{'input_tokens':5,'output_tokens':1}}])
        adapter=CodexSubscription()
        with patch('jarvis.subscription_chat.shutil.which',return_value='fake-codex'), \
             patch.object(adapter,'verify_isolation',return_value=Path('catalog.json')), \
             patch('jarvis.subscription_chat.subprocess.run',return_value=subprocess.CompletedProcess([],0,'Logged in using ChatGPT','')), \
             patch('jarvis.subscription_chat.subprocess.Popen',return_value=process):
            result=adapter.chat('default',[{'role':'user','content':'color'}],threading.Event(),lambda _:None)
        self.assertEqual(result[0],'amber')
        self.assertEqual(adapter.status,'CONNECTED')


if __name__=='__main__':unittest.main()
