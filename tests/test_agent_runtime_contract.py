"""Offline contracts for the preserved (non-Hub) runtime, using real SQLite.

No dispatch loop, provider discovery, model transport, or host tool is started.
"""
import hashlib
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from jarvis.agent import AgentResult, AgentRunCancelled
from jarvis.agent_runtime import (
    TOOL_GROUPS,
    AgentRuntime,
    HubStore,
    TaskError,
    _classify_provider_failure,
    _EventRecorder,
    _Running,
    _tool_outcome,
    snapshot_workspace,
)
from jarvis.config import Config


class RuntimeContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = Config(
            root=self.root, workspace=self.root / 'workspace', data_dir=self.root / 'data',
            soul_path=self.root / 'soul.md', model='fixture', fast_model='fixture',
            reasoning_model='fixture', coding_model='fixture', ollama_url='',
            ollama_api_key=None, max_steps=3, context_length=4096,
            command_timeout=1, autonomy='readonly',
        )
        self.registry = Mock()
        self.registry.snapshot.return_value = {}
        self.registry.ready.return_value = (True, 'offline fixture')
        self.client = Mock()
        self.client_factory = Mock(return_value=self.client)
        self.runtime = self.open_runtime()
        self.project = self.runtime.create_project('Fixture project', 'tester')
        self.workspace = self.runtime._project_root(self.project['project_id'])

    def open_runtime(self):
        runtime = AgentRuntime(
            state_dir=self.root / 'state', runtime_path=self.root / 'runtime.db',
            provider_profile_dir=self.root / 'profile', base_config=self.config,
            registry=self.registry, client_factory=self.client_factory, start=False,
        )
        self.addCleanup(runtime.close)
        self.assertFalse(runtime._thread.is_alive())
        return runtime

    def agent(self, **kwargs):
        payload = {
            'name': 'Fixture', 'role': 'Assistant', 'provider': 'codex-cli', 'model': 'fixture-model',
            'project_id': self.project['project_id'], 'enable': True, 'tool_groups': [],
        }
        payload.update(kwargs)
        return self.runtime.create_agent(payload, 'tester')

    def task(self, agent=None, **kwargs):
        agent = agent or self.agent()
        return self.runtime.submit_task(agent['agent_id'], {'request': 'Test request', **kwargs}, 'tester')

    def set_state(self, task, state):
        with self.runtime.store.tx() as db:
            db.execute('UPDATE hub_tasks SET state=? WHERE task_id=?', (state, task['task_id']))

    def test_project_registry_and_durable_default(self):
        self.assertTrue(self.project['managed'])
        self.assertNotIn('root', self.project)
        self.assertTrue(self.workspace.is_dir())
        changed = self.runtime.set_default_model('codex-cli', 'fixture-model', 'tester')
        self.assertEqual(changed['model'], 'fixture-model')
        reopened = self.open_runtime()
        self.assertEqual(reopened.default_model(), changed)
        self.assertEqual(reopened.projects(), self.runtime.projects())
        moved = self.root / 'replacement'
        self.runtime.register_project(self.project['project_id'], 'Renamed', moved, managed=False)
        self.assertEqual(self.runtime._project_root(self.project['project_id']), moved)
        self.assertEqual(self.runtime.project(self.project['project_id'])['name'], 'Renamed')
        for provider, model in [('paid-api', 'valid'), ('codex-cli', '../bad')]:
            with self.assertRaises(TaskError):
                self.runtime.set_default_model(provider, model, 'tester')
        with self.assertRaises(TaskError):
            self.runtime.project('missing')

    def test_closed_group_schema_and_tool_union(self):
        for invalid in ['files_read', None, [1], ['unknown']]:
            with self.subTest(invalid=invalid), self.assertRaises(TaskError):
                self.runtime._groups(invalid)
        self.assertEqual(self.runtime._groups(['memory', 'files_read', 'memory']), ['files_read', 'memory'])
        self.assertEqual(self.runtime._allowed_tools([]), set())
        for group, spec in TOOL_GROUPS.items():
            self.assertEqual(self.runtime._allowed_tools([group]), set(spec['tools']))

    def test_agent_configuration_lifecycle_and_archived_history(self):
        agent = self.agent()
        aid = agent['agent_id']
        changed = self.runtime.update_agent(aid, {'instructions': 'Only fixtures', 'tool_groups': ['files_read']}, 'tester')
        self.assertEqual(changed['instructions'], 'Only fixtures')
        self.assertEqual(changed['tool_groups'], ['files_read'])
        for action, state in [('pause', 'PAUSED'), ('resume', 'RUNNING'), ('disable', 'STOPPED'), ('enable', 'RUNNING')]:
            self.assertEqual(self.runtime.set_lifecycle(aid, action, 'tester')['lifecycle'], state)
        original = self.task(agent)
        self.assertTrue(self.runtime.set_lifecycle(aid, 'archive', 'tester')['archived'])
        with self.assertRaises(TaskError):
            self.task(agent)
        self.assertEqual(self.runtime.task(original['task_id'])['request'], 'Test request')
        self.assertFalse(self.runtime.set_lifecycle(aid, 'enable', 'tester')['archived'])
        self.assertEqual(len(self.runtime.agents()), 1)
        for payload in [{'actor': 'forged'}, {'project_id': 'other'}, {'tool_groups': ['unknown']}]:
            with self.assertRaises(TaskError):
                self.runtime.update_agent(aid, payload, 'tester')
        with self.assertRaises(TaskError):
            self.runtime.set_lifecycle(aid, 'delete-everything', 'tester')

    def test_agent_and_request_validation(self):
        for payload in [{'name': ''}, {'name': 'x\x00'}, {'name': 'x' * 81}, {'provider': 'api'}, {'model': '../x'}, {'extra': 1}]:
            base = {'name': 'Fixture', 'project_id': self.project['project_id']}
            base.update(payload)
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.runtime.create_agent(base, 'tester')
        agent = self.agent()
        for payload in [{'request': ''}, {'request': 'x' * 20001}, {'request': 'ok', 'chat_id': 'foreign'},
                        {'request': 'ok', 'model_override': '../x'}, {'request': 'ok', 'tool_groups': ['run_commands']}]:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.runtime.submit_task(agent['agent_id'], payload, 'tester')
        for value in [None, 'x' * 81, 'agt_' + '0' * 32]:
            with self.assertRaises(TaskError):
                self.runtime.agent(value)
        with self.assertRaises(TaskError):
            self.runtime._agent_dir('../escape')

    def test_task_idempotence_and_configuration_snapshot(self):
        agent = self.agent()
        task = self.task(agent, request_id='fixture-request', model_override='override-model')
        self.assertEqual(self.task(agent, request_id='fixture-request')['task_id'], task['task_id'])
        with self.assertRaises(TaskError):
            self.runtime.submit_task(agent['agent_id'], {'request': 'different', 'request_id': 'fixture-request'}, 'tester')
        self.runtime.update_agent(agent['agent_id'], {'tool_groups': ['files_read']}, 'tester')
        saved = self.runtime.task(task['task_id'])
        self.assertEqual(saved['tool_groups'], [])
        self.assertEqual(saved['model_requested'], 'override-model')
        self.assertEqual(len(self.runtime.tasks(agent_id=agent['agent_id'], project_id=self.project['project_id'], states={'QUEUED'})), 1)
        self.assertEqual(self.runtime.tasks(states={'FAILED'}), [])
        with self.assertRaises(TaskError):
            self.runtime.task('missing')

    def test_request_retry_cannot_cross_agents_or_change_explicit_configuration(self):
        first, second = self.agent(), self.agent(name='Other fixture')
        task = self.task(first, request_id='bound-request')
        with self.assertRaises(TaskError):
            self.task(second, request_id='bound-request')
        for change in ({'title': 'Changed'}, {'model_override': 'different-model'}, {'chat_id': 'foreign'}):
            with self.subTest(change=change), self.assertRaises(TaskError):
                self.task(first, request_id='bound-request', **change)
        self.assertEqual(self.task(first, request_id='bound-request')['task_id'], task['task_id'])
        self.assertEqual(len(self.runtime.tasks()), 1)

    def test_oversized_event_detail_remains_valid_and_bounded(self):
        import json
        with self.runtime.store.tx() as db:
            event_id = self.runtime.store.event(db, 'fixture.large', 'Synthetic event', detail={'body': 'x' * 8100})
        with self.runtime.store.read() as db:
            row = db.execute('SELECT detail_json FROM hub_events WHERE seq=?', (event_id,)).fetchone()
        self.assertLessEqual(len(row[0]), 8000)
        self.assertEqual(json.loads(row[0]), {'truncated': True, 'original_chars': 8112})
        self.assertTrue(self.runtime.events())

    def test_task_pause_resume_cancel_retry_and_steering(self):
        task = self.task()
        tid = task['task_id']
        self.assertEqual(self.runtime.steer(tid, 'Check the result', 'tester')['steering'][0]['state'], 'PENDING')
        for action, state in [('pause', 'PAUSED'), ('pause', 'PAUSED'), ('resume', 'QUEUED'), ('cancel', 'CANCELLED')]:
            self.assertEqual(self.runtime.control(tid, action, 'tester')['state'], state)
        self.assertEqual(self.runtime.task(tid)['steering'][0]['state'], 'DISCARDED')
        retried = self.runtime.control(tid, 'retry', 'tester')
        self.assertEqual(retried['retry_of'], tid)
        self.assertIn('Inspect the workspace first', retried['request'])
        self.assertEqual(self.runtime.task(tid)['related'][0]['task_id'], retried['task_id'])
        for action in ['cancel', 'pause', 'resume', 'invalid']:
            with self.assertRaises(TaskError):
                self.runtime.control(tid, action, 'tester')
        with self.assertRaises(TaskError):
            self.runtime.steer(tid, 'too late', 'tester')
        with self.assertRaises(TaskError):
            self.runtime.control(retried['task_id'], 'retry', 'tester')

    def test_restart_interrupts_only_started_work_without_execution(self):
        running, queued = self.task(), self.task()
        self.set_state(running, 'RUNNING')
        reopened = self.open_runtime()
        self.assertEqual(reopened.task(running['task_id'])['state'], 'INTERRUPTED')
        self.assertEqual(reopened.task(queued['task_id'])['state'], 'QUEUED')
        self.client_factory.assert_not_called()
        retried = reopened.control(running['task_id'], 'retry', 'tester')
        self.assertEqual(reopened.task(running['task_id'])['state'], 'CANCELLED')
        self.assertEqual(retried['retry_of'], running['task_id'])

    def test_provider_wait_backoff_is_bounded_and_explicit_resume_resets(self):
        task = self.task()
        for count, backoff in enumerate([30, 90, 240], 1):
            with patch('jarvis.agent_runtime._now', return_value=1000):
                self.runtime._wait_for_provider(task, 'Sign in needed', kind='login')
            task = self.runtime.task(task['task_id'])
            self.assertEqual((task['state'], task['provider_waits'], task['not_before']), ('WAITING_PROVIDER', count, 1000 + backoff))
        self.runtime._wait_for_provider(task, 'still unavailable', kind='login')
        self.assertEqual(self.runtime.task(task['task_id'])['state'], 'FAILED')
        second = self.task()
        self.runtime._wait_for_provider(second, 'busy', kind='capacity')
        resumed = self.runtime.control(second['task_id'], 'resume', 'tester')
        self.assertEqual(resumed['provider_waits'], 0)
        self.assertIsNone(resumed['not_before'])

    def test_dispatch_refuses_unready_provider_and_requeues_when_ready(self):
        task = self.task()
        self.registry.ready.return_value = (False, 'not authenticated')
        with patch.object(self.runtime, '_start') as start:
            self.runtime.dispatch_once()
            self.assertEqual(self.runtime.task(task['task_id'])['state'], 'WAITING_PROVIDER')
            start.assert_not_called()
            self.registry.ready.return_value = (True, '')
            self.runtime.dispatch_once()
            start.assert_not_called()
            with self.runtime.store.tx() as db:
                db.execute('UPDATE hub_tasks SET not_before=0')
            self.runtime.dispatch_once()
            self.assertEqual(self.runtime.task(task['task_id'])['state'], 'QUEUED')
            self.runtime.dispatch_once()
            start.assert_called_once()
        self.client_factory.assert_not_called()

    def test_queue_serializes_agent_and_project_writers(self):
        task = self.task()
        self.assertEqual(self.runtime.queue_reason(task), 'Starting.')
        holder = _Running('held', task['agent_id'], task['project_id'], True, threading.Event(), Mock())
        self.runtime._running['held'] = holder
        self.addCleanup(self.runtime._running.clear)
        self.assertIn("agent's current task", self.runtime.queue_reason(task))
        holder.agent_id = 'other'
        task['tool_groups'] = ['files_write']
        self.assertIn('write lock', self.runtime.queue_reason(task))
        task['tool_groups'] = []
        self.runtime.max_concurrent = 1
        self.assertIn('free slot', self.runtime.queue_reason(task))
        self.runtime._running.clear()
        task['not_before'] = 99999999999
        self.assertIn('before retrying', self.runtime.queue_reason(task))
        self.runtime.set_lifecycle(task['agent_id'], 'pause', 'tester')
        self.assertIn('not enabled', self.runtime.queue_reason(task))

    def test_scoped_config_disables_unrequested_channels(self):
        task = self.task()
        for groups, autonomy, execution in [([], 'readonly', 'disabled'), (['files_write'], 'autonomous', 'disabled'), (['run_commands'], 'autonomous', 'trusted-host')]:
            task['tool_groups'] = groups
            config = self.runtime.agent_config(task, self.workspace, 'codex-cli', 'fixture-model')
            self.assertEqual((config.autonomy, config.execution_mode), (autonomy, execution))
            self.assertEqual(config.model, 'codex-cli:fixture-model')
            for field in ['network_access', 'computer_access', 'external_access', 'self_repair', 'self_inspect', 'initiative']:
                self.assertEqual(getattr(config, field), 'disabled')
            self.assertFalse(config.openai_api_enabled)
            self.assertFalse(config.anthropic_api_enabled)
            self.assertFalse(config.ollama_enabled)
            self.assertTrue(config.codex_cli_enabled)

    def test_injected_execution_persists_conversation_steering_tools_and_artifacts(self):
        agent = self.agent()
        self.runtime.update_agent(agent['agent_id'], {'tool_groups': ['files_read'], 'instructions': 'Stay local'}, 'tester')
        task = self.task(agent)
        self.runtime.steer(task['task_id'], 'Inspect first', 'tester')
        memory = Mock()
        toolbox = SimpleNamespace(tools={'read_file': object(), 'run_process': object()}, execute=lambda name, args: '{"ok":true}')
        def run(prompt, **kwargs):
            self.assertIn('Stay local', prompt)
            self.assertIn('Inspect first', prompt)
            self.assertEqual(set(toolbox.tools), {'read_file'})
            self.assertFalse(kwargs['cancellation_guard']())
            self.assertIsNone(kwargs['conversation_id'])
            toolbox.execute('read_file', {'path': 'example.txt'})
            (self.workspace / 'result.txt').write_bytes(b'fixture result\n')
            kwargs['stream_callback']('fixture')
            return AgentResult('Done', conversation_id=123, model='codex-cli:fixture-model')
        self.runtime._agent_factory = lambda *args: SimpleNamespace(toolbox=toolbox, run=run)
        with patch.object(self.runtime, '_open_memory', return_value=memory):
            self.runtime._execute(task['task_id'], threading.Event())
        saved = self.runtime.task(task['task_id'])
        self.assertEqual((saved['state'], saved['result'], saved['tool_calls']), ('COMPLETED', 'Done', 1))
        self.assertEqual(saved['steering'][0]['state'], 'APPLIED')
        self.assertEqual(self.runtime.agent(agent['agent_id'])['chats'][0]['conversation_id'], 123)
        self.client_factory.assert_called_once_with('codex-cli', 'fixture-model')
        self.client.close.assert_called_once()
        memory.close.assert_called_once()
        artifacts = self.runtime.artifacts(task_id=task['task_id'])
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(self.runtime.artifact_content(artifacts[0]['artifact_id'])[0], b'fixture result\n')

    def test_finish_failure_refusal_and_control_outcomes(self):
        cases = [({'exception': RuntimeError('fixture failure')}, None, 'FAILED'),
                 ({'exception': AgentRunCancelled()}, None, 'CANCELLED'),
                 ({}, 'pause', 'PAUSED'), ({}, 'shutdown', 'INTERRUPTED'), ({}, 'cancel', 'CANCELLED'),
                 ({'result': AgentResult('refused', status='incomplete', reason='Denied')}, None, 'FAILED'),
                 ({'result': AgentResult('login needed', status='incomplete')}, None, 'WAITING_PROVIDER'),
                 ({'result': AgentResult('approval', waiting_for_approval=True, approval_id=7)}, None, 'WAITING_APPROVAL')]
        for outcome, control, expected in cases:
            with self.subTest(expected=expected, control=control):
                task = self.task()
                self.runtime._finish(task, outcome, control)
                self.assertEqual(self.runtime.task(task['task_id'])['state'], expected)

    def test_pending_followup_becomes_linked_task_only_after_completion(self):
        task = self.task()
        self.runtime.steer(task['task_id'], 'Second step', 'tester')
        self.runtime._queue_followups(task['task_id'])
        self.assertEqual(self.runtime.task(task['task_id'])['related'], [])
        self.runtime._finish(task, {'result': AgentResult('done')}, None)
        saved = self.runtime.task(task['task_id'])
        followup = self.runtime.task(saved['related'][0]['task_id'])
        self.assertEqual(followup['followup_of'], task['task_id'])
        self.assertIn('Second step', followup['request'])
        self.assertEqual(saved['steering'][0]['applied_task_id'], followup['task_id'])
        self.runtime._queue_followups(task['task_id'])
        self.assertEqual(len(self.runtime.task(task['task_id'])['related']), 1)

    def test_artifact_versions_deletion_and_integrity_failure(self):
        task = self.task()
        path = self.workspace / 'example.txt'
        path.write_bytes(b'first\n')
        self.runtime._record_artifacts(task, {})
        before = snapshot_workspace(self.workspace)
        path.write_bytes(b'second, longer\n')
        self.runtime._record_artifacts(task, before)
        before = snapshot_workspace(self.workspace)
        path.unlink()
        self.runtime._record_artifacts(task, before)
        rows = sorted(self.runtime.artifacts(agent_id=task['agent_id'], project_id=task['project_id']), key=lambda row: row['version'])
        self.assertEqual([r['change'] for r in rows], ['created', 'modified', 'deleted'])
        self.assertEqual([r['version'] for r in rows], [1, 2, 3])
        self.assertEqual(len(self.runtime.artifact(rows[0]['artifact_id'])['versions']), 3)
        self.assertIn('+second, longer', self.runtime.artifact(rows[1]['artifact_id'])['diff'])
        with self.assertRaises(TaskError):
            self.runtime.artifact_content(rows[2]['artifact_id'])
        sha = rows[0]['sha256']
        self.assertEqual(sha, hashlib.sha256(b'first\n').hexdigest())
        (self.runtime.artifact_dir / sha[:2] / sha).write_bytes(b'tampered')
        with self.assertRaisesRegex(TaskError, 'integrity'):
            self.runtime.artifact_content(rows[0]['artifact_id'])
        with self.assertRaises(TaskError):
            self.runtime.artifact('missing')

    def test_events_filters_audit_and_transaction_rollback(self):
        task = self.task()
        last = self.runtime.last_event_seq()
        self.runtime.steer(task['task_id'], 'More detail', 'tester')
        events = self.runtime.events(after=last, agent_id=task['agent_id'], task_id=task['task_id'], project_id=task['project_id'], kinds={'task'})
        self.assertEqual([e['kind'] for e in events], ['task.steer_received'])
        self.assertEqual(events[0]['detail']['body'], 'More detail')
        self.assertEqual(self.runtime.events(after=last, kinds={'artifact'}), [])
        self.runtime.record_audit('tester', 'fixture', None, 'ok')
        self.assertEqual(self.runtime.audit(1)[0]['action'], 'fixture')
        before = self.runtime.last_event_seq()
        with self.assertRaises(RuntimeError), self.runtime.store.tx() as db:
            self.runtime.store.event(db, 'rolled.back', 'must disappear')
            raise RuntimeError('rollback')
        self.assertEqual(self.runtime.last_event_seq(), before)
        reopened = HubStore(self.runtime.store.path)
        with reopened.read() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM hub_tasks').fetchone()[0], 1)

    def test_event_recorder_normalizes_progress_and_records_real_outcomes(self):
        task = self.task()
        recorder = _EventRecorder(self.runtime, task)
        with patch('jarvis.agent_runtime.time.monotonic', return_value=10):
            for message in ['Processing - step 1', 'Processing - step 2', 'Tool - ignored', 'Model - fixture',
                            'Failover - fixture', 'Recovery - retried', 'Verified - yes', 'Other - detail']:
                recorder.on_event(message)
        toolbox = SimpleNamespace(execute=lambda name, arguments: '{"ok":false,"error":"denied"}')
        recorder.wrap(toolbox)
        self.assertIn('denied', toolbox.execute('read_file', {'path': 'fixture.txt', 'secret': 'not recorded'}))
        rows = self.runtime.events(task_id=task['task_id'])
        self.assertEqual(sum(e['kind'] == 'progress' for e in rows), 2)
        event = next(e for e in rows if e['kind'] == 'tool')
        self.assertFalse(event['detail']['ok'])
        self.assertEqual(event['detail']['arguments'], {'path': 'fixture.txt'})
        self.assertEqual(recorder.tool_calls, 1)

    def test_tool_outcomes_and_provider_classification(self):
        for raw in ['plain', '[]', 'null']:
            self.assertEqual(_tool_outcome('tool', raw), (True, ''))
        self.assertEqual(_tool_outcome('tool', '{"error":"denied"}'), (False, 'denied'))
        self.assertEqual(_tool_outcome('run_process', '{"data":{"exit_code":2,"timed_out":true}}'), (False, 'exit code 2 (timed out)'))
        self.assertEqual(_tool_outcome('web_search', '{"data":{"results":[1,2]}}'), (True, '2 results'))
        for text, kind in [('OAuth required', 'login'), ('quota exceeded', 'capacity'), ('provider unavailable', 'availability'), ('policy refusal', None)]:
            self.assertEqual(_classify_provider_failure(text), kind)

    def test_real_approval_store_queues_only_matching_waiter_and_refuses_replay(self):
        for approve in [True, False]:
            agent = self.agent()
            task = self.task(agent)
            memory = self.runtime._open_memory(agent['agent_id'])
            try:
                authorized, approval_id = memory.authorize_or_request(
                    'write_file', 'fixture.txt', 'Operator decision required', approval_scope='foreground')
                self.assertFalse(authorized)
            finally:
                memory.close()
            self.runtime._finish(task, {'result': AgentResult('Waiting', waiting_for_approval=True, approval_id=approval_id)}, None)
            rows = self.runtime.approvals(agent['agent_id'])
            self.assertEqual(rows[0]['status'], 'pending')
            self.assertEqual(rows[0]['approval_id'], approval_id)
            self.assertEqual(self.runtime.decide_approval(agent['agent_id'], approval_id, approve, 'tester'),
                             {'approval_id': approval_id, 'approved': approve})
            self.assertEqual(self.runtime.task(task['task_id'])['state'], 'QUEUED' if approve else 'CANCELLED')
            with self.assertRaises(TaskError):
                self.runtime.decide_approval(agent['agent_id'], approval_id, approve, 'tester')
        self.client_factory.assert_not_called()

    def test_start_claim_is_once_and_passes_cancellation_to_worker(self):
        task = self.task()
        with patch('jarvis.agent_runtime.threading.Thread') as thread_type:
            self.runtime._start(task)
            self.runtime._start(task)
            thread_type.assert_called_once()
            thread_type.return_value.start.assert_called_once()
            saved = self.runtime.task(task['task_id'])
            self.assertEqual((saved['state'], saved['attempt']), ('RUNNING', 1))
            running = self.runtime._running[task['task_id']]
            self.assertIs(thread_type.call_args.kwargs['args'][1], running.cancel)
            self.runtime.control(task['task_id'], 'pause', 'tester')
            self.assertTrue(running.cancel.is_set())
            self.assertEqual(self.runtime.task(task['task_id'])['control'], 'pause')
            self.runtime.close()
            self.assertEqual(running.control, 'shutdown')
            self.assertEqual(thread_type.return_value.join.call_count, 1)
        self.runtime._running.clear()
        self.client_factory.assert_not_called()

    def test_execution_failure_closes_resources_and_survives_artifact_error(self):
        task = self.task()
        memory = Mock()
        memory.close.side_effect = RuntimeError('fixture close failure')
        self.client.close.side_effect = RuntimeError('fixture close failure')
        self.runtime._agent_factory = Mock(side_effect=RuntimeError('scripted constructor failure'))
        with patch.object(self.runtime, '_open_memory', return_value=memory), patch.object(self.runtime, '_record_artifacts', side_effect=OSError('fixture artifact failure')):
            self.runtime._execute(task['task_id'], threading.Event())
        saved = self.runtime.task(task['task_id'])
        self.assertEqual(saved['state'], 'FAILED')
        self.assertEqual(saved['error_kind'], 'runtime')
        self.assertIn('scripted constructor failure', saved['error'])
        memory.close.assert_called_once()
        self.client.close.assert_called_once()
        self.assertEqual(len(self.runtime.events(task_id=task['task_id'], kinds={'artifact.error'})), 1)

    def test_model_update_changes_future_tasks_without_changing_queued_binding(self):
        agent = self.agent()
        first = self.task(agent)
        changed = self.runtime.update_agent(agent['agent_id'], {'provider': 'claude-cli', 'model': 'fixture-next'}, 'tester')
        self.assertEqual((changed['provider'], changed['model']), ('claude-cli', 'fixture-next'))
        second = self.task(agent)
        self.assertEqual((first['provider'], first['model_configured']), ('codex-cli', 'fixture-model'))
        self.assertEqual((second['provider'], second['model_configured']), ('claude-cli', 'fixture-next'))
        with self.assertRaises(TaskError):
            self.runtime.update_agent(agent['agent_id'], {'provider': 'paid-api'}, 'tester')
        self.runtime.set_lifecycle(agent['agent_id'], 'pause', 'tester')
        with self.assertRaises(TaskError):
            self.task(agent)
        self.runtime.control(first['task_id'], 'pause', 'tester')
        with self.assertRaises(TaskError):
            self.runtime.control(first['task_id'], 'resume', 'tester')

    def test_workspace_snapshot_filters_generated_dirs_and_records_binary_versions(self):
        self.assertEqual(snapshot_workspace(self.root / 'missing'), {})
        (self.workspace / 'node_modules').mkdir()
        (self.workspace / 'node_modules' / 'hidden.txt').write_bytes(b'ignore')
        (self.workspace / 'README').write_bytes(b'readable')
        (self.workspace / 'raw.bin').write_bytes(b'\xff\x00')
        before = snapshot_workspace(self.workspace)
        self.assertEqual(set(before), {'README', 'raw.bin'})
        self.assertEqual(before['README']['text'], 'readable')
        self.assertNotIn('text', before['raw.bin'])
        task = self.task()
        self.runtime._record_artifacts(task, {})
        rows = self.runtime.artifacts(task_id=task['task_id'])
        raw = next(row for row in rows if row['path'] == 'raw.bin')
        self.assertFalse(raw['has_diff'])
        self.assertEqual(self.runtime.artifact_content(raw['artifact_id'])[0], b'\xff\x00')
        count = len(rows)
        self.runtime._record_artifacts(task, before)
        self.assertEqual(len(self.runtime.artifacts(task_id=task['task_id'])), count)


if __name__ == '__main__':
    unittest.main()
