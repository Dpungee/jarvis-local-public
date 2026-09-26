"""Complete original-schema Hub contract; no provider calls or operator state."""
import json
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jarvis.agent_hub import HubAuth, HubHTTPServer, HubService
from jarvis.agent_hub_runtime import AgentRuntime, TaskError, allowed_tools


class HubRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.service = self.open_service()
        self.auth = HubAuth(self.root)
        self.server = HubHTTPServer(('127.0.0.1', 0), self.service, self.auth)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def open_service(self):
        return HubService(state_dir=self.root, provider_profile_dir=self.root/'profile',
                          runtime_kwargs={'autostart': False, 'provider_probe': lambda _: {
                              'installed': True, 'authenticated': True, 'detail': 'Synthetic test', 'version': 'test'}})

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.service.close()
        self.tmp.cleanup()

    def request(self, path, body=None, authenticated=True, origin=None):
        headers = {'Content-Type': 'application/json'}
        if authenticated:
            headers['Authorization'] = 'Bearer '+self.auth.operator_token
        if origin:
            headers['Origin'] = origin
        req = urllib.request.Request(f'http://127.0.0.1:{self.server.server_port}'+path,
                                     data=None if body is None else json.dumps(body).encode(), headers=headers)
        try:
            with urllib.request.urlopen(req) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    def agent(self, **overrides):
        status, result = self.request('/api/agents', {'name':'Atlas','role':'Research',
            'provider':'codex-cli','model':'gpt-5.6-sol','enable':True,'tool_groups':[], **overrides})
        self.assertEqual(status, 200, result)
        return result

    def test_authentication_and_origin_stay_closed(self):
        self.assertEqual(self.request('/api/overview',authenticated=False)[0],401)
        self.assertEqual(self.request('/api/projects',{'name':'No'},origin='https://untrusted.invalid')[0],403)
        self.assertEqual(self.request('/api/session')[1]['actor'],'local-operator')

    def test_complete_read_surface(self):
        agent = self.agent()
        for path in ['/api/overview','/api/providers','/api/projects','/api/audit','/api/sessions','/api/events',
                     '/api/agents/'+agent['agent_id']]:
            self.assertEqual(self.request(path)[0],200,path)

    def test_explicit_groups_do_not_restore_defaults(self):
        agent = self.agent(tool_groups=['files_read'])
        self.assertEqual([k for k,v in agent['permissions'].items() if v],['files_read'])
        self.assertNotIn('write_file',allowed_tools(agent['permissions']))
        status, changed = self.request('/api/agents/'+agent['agent_id']+'/config', {'tool_groups':[]})
        self.assertEqual(status,200)
        self.assertFalse(any(changed['permissions'].values()))

    def test_ambiguous_or_unrepresentable_grants_refused(self):
        for payload in [{'tool_groups':['documents']},{'tool_groups':'files_read'},
                        {'tool_groups':[], 'permissions':{}},{'actor':'forged'}]:
            self.assertEqual(self.request('/api/agents', {'name':'No','role':'No',**payload})[0],400)

    def test_authenticated_actor_is_audited(self):
        self.agent()
        rows=self.request('/api/audit')[1]
        self.assertEqual(rows[0]['actor'],'local-operator')
        self.assertEqual(rows[0]['outcome'],'ok')

    def test_lifecycle_and_project_contract(self):
        status, project = self.request('/api/projects',{'name':'Folder'})
        self.assertEqual(status,200)
        agent = self.agent(project_id=project['project_id'])
        for action in ['pause','start','stop','start']:
            self.assertEqual(self.request('/api/agents/'+agent['agent_id']+'/lifecycle',{'action':action})[0],200)
        for archived in [True,False]:
            self.assertEqual(self.request('/api/agents/'+agent['agent_id']+'/archive',{'archived':archived})[0],200)

    def test_task_controls_events_and_saved_restart(self):
        agent = self.agent()
        status, task = self.request('/api/agents/'+agent['agent_id']+'/tasks',{'request':'Synthetic request','title':''})
        self.assertEqual(status,200,task)
        path='/api/tasks/'+task['task_id']
        self.assertEqual(self.request(path+'/steer',{'body':'Synthetic follow-up'})[0],200)
        for action in ['pause','resume','cancel','retry']:
            self.assertEqual(self.request(path+'/'+action,{})[0],200,action)
        self.service.runtime._set(task['task_id'],state='RUNNING')
        self.service.close()
        self.service=self.open_service()
        self.server.service=self.service
        detail=self.request(path)[1]
        self.assertEqual(detail['state'],'INTERRUPTED')
        self.assertEqual(detail['request'],'Synthetic request')
        self.assertEqual(len(detail['steering']),1)
        self.assertEqual(self.request('/api/agents/'+agent['agent_id'])[0],200)

    def test_shared_profile_provider_client_contract(self):
        sentinel=object()
        with patch('jarvis.agent_providers.build_agent_client',return_value=sentinel) as build:
            self.assertIs(self.service.runtime._make_client(SimpleNamespace(model='codex-cli:gpt-5.6-sol')),sentinel)
        build.assert_called_once_with(self.root/'profile','codex-cli','gpt-5.6-sol')

    def test_model_verification_default_and_actual_override_contract(self):
        with patch.object(self.service.runtime, '_verify_claude',
                          return_value=(True,'Synthetic verification',['claude-opus-5-5'])):
            status, result = self.request('/api/providers/verify',
                                         {'provider':'claude-cli','model':'claude-opus-5-5'})
        self.assertEqual(status,200)
        self.assertTrue(result['verified'])
        self.assertEqual(self.request('/api/settings/default-model',
                                     {'provider':'claude-cli','model':'claude-opus-5-5'})[0],200)
        agent = self.agent()
        status, task = self.request('/api/agents/'+agent['agent_id']+'/tasks',
                                   {'request':'Synthetic only','model':'gpt-5.5'})
        self.assertEqual(status,200,task)
        self.assertEqual(task['model_override'],'codex-cli:gpt-5.5')
        self.assertIsNone(task['model_used'])

    def test_other_schema_is_refused_without_conversion(self):
        other=self.root/'other'
        other.mkdir()
        with closing(sqlite3.connect(other/'hub.db')) as db:
            db.execute('CREATE TABLE hub_tasks(task_id TEXT, groups_json TEXT)')
            db.commit()
        with self.assertRaisesRegex(TaskError,'Incompatible'):
            AgentRuntime(state_dir=other,runtime_path=other/'runtime.db',provider_profile_dir=other,
                         project_root=lambda _:other,autostart=False)
        with closing(sqlite3.connect(other/'hub.db')) as db:
            self.assertEqual([r[1] for r in db.execute('PRAGMA table_info(hub_tasks)')],['task_id','groups_json'])

    def test_provider_refresh_contract(self):
        status, result=self.request('/api/providers/refresh',{})
        self.assertEqual(status,200)
        self.assertTrue(result['providers']['codex-cli']['authenticated'])

    def test_dispatch_permissions_result_and_artifact_integrity(self):
        agent = self.agent(tool_groups=['files_write'])
        runtime = self.service.runtime
        runtime.refresh_providers()
        workspace = self.service.project_root(agent['project_id'])

        class Result(str):
            status = 'complete'
            model = 'codex-cli:gpt-5.6-sol'
            tool_calls = 2

        class Toolbox:
            def __init__(inner):
                inner.tools = {'write_file': object(), 'run_process': object()}

            def execute(inner, name, arguments):
                if name not in inner.tools:
                    return json.dumps({'ok': False, 'error': 'Unknown tool'})
                (workspace / 'result.txt').write_text('Synthetic execution result', encoding='utf-8')
                return json.dumps({'ok': True})

        class Runner:
            toolbox = Toolbox()

            def run(inner, prompt, **kwargs):
                denied = json.loads(inner.toolbox.execute('run_process', {}))
                self.assertFalse(denied['ok'])
                inner.toolbox.execute('write_file', {'path':'result.txt'})
                return Result('Synthetic completed result')

        task = runtime.create_task(agent['agent_id'], title='', request='Synthetic test only')
        with patch.object(runtime, '_agent_config', return_value=SimpleNamespace()), \
             patch.object(runtime, '_open_memory', return_value=SimpleNamespace()), \
             patch.object(runtime, '_make_client', return_value=SimpleNamespace()), \
             patch.object(runtime, '_make_agent', return_value=Runner()):
            runtime.dispatch_once()
            deadline = time.monotonic() + 5
            while runtime.running_ids() and time.monotonic() < deadline:
                time.sleep(.01)
        detail = runtime.task(task['task_id'])
        self.assertEqual(detail['state'],'COMPLETED',detail.get('blocker'))
        self.assertEqual(detail['result'],'Synthetic completed result')
        artifacts = runtime.artifacts(task_id=task['task_id'])
        self.assertEqual(len(artifacts),1)
        self.assertEqual(runtime.artifact_bytes(artifacts[0]['artifact_id'])[0],b'Synthetic execution result')
        with runtime.db() as db:
            db.execute('UPDATE hub_blobs SET data=?', (b'corrupted',))
        with self.assertRaisesRegex(TaskError,'integrity'):
            runtime.artifact_bytes(artifacts[0]['artifact_id'])


if __name__ == '__main__':
    unittest.main()
