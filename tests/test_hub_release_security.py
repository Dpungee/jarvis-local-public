"""Offline adversarial regression tests for Agent Hub release boundaries."""
import io
import json
import shutil
import socket
import subprocess
import threading
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from jarvis import web_browser as browser
from jarvis.agent_hub_runtime import AgentRuntime
from jarvis.web_check import WebCheckError
from tests import test_agent_hub_agents as browser_fixtures


class BrowserReleaseSecurityTests(unittest.TestCase):
    def session(self, **changes):
        element = dict(tag='textarea', type='textarea', text='Message', name='message',
                       form_fields='textarea message', id='message', autocomplete='')
        element.update(changes)
        return browser_fixtures.BrowserSessionGuardTests().session(element)

    def test_message_submit_needs_confirmation_before_any_input(self):
        session, devtools = self.session()
        with self.assertRaises(browser.BrowserError):
            session.type('agent', 1, 'A synthetic message', submit=True)
        self.assertEqual(devtools.calls, [])

    def test_search_still_works_without_committing_approval(self):
        session, devtools = self.session(type='search', name='q', form_fields='search q')
        session.type('agent', 1, 'synthetic search', submit=True)
        self.assertTrue(any(method == 'Input.insertText' for method, _ in devtools.calls))

    def test_long_submit_and_link_commit_labels_require_confirmation(self):
        for tag, label in [('button', 'Publish this message to everyone now'),
                           ('button', 'Submit my new profile to the service'),
                           ('a', 'Send this message to everyone now')]:
            with self.subTest(label=label):
                self.assertTrue(browser.needs_confirmation(dict(tag=tag, type='submit', text=label,
                                                               href='https://example.com/send')))
        self.assertTrue(browser.needs_confirmation(dict(tag='a', type='',
            text='Delete this old account and its files', href='https://example.com/remove')))

    def test_changed_payload_blocks_mouse_press_even_after_hover(self):
        session, devtools = self.session(tag='button', type='button', text='Send')
        evaluate = session._evaluate
        session._evaluate = lambda d, s, expression: ('b' * 64 if browser._CONFIRM_STATE_JS in expression
                                                       else evaluate(d, s, expression))
        with self.assertRaisesRegex(browser.BrowserError, 'form changed'):
            session.click('agent', 1, confirmed=True, expected_payload_sha256='a' * 64)
        self.assertFalse(any(method == 'Input.dispatchMouseEvent' and data['type'] == 'mousePressed'
                             for method, data in devtools.calls))

    @unittest.skipUnless(shutil.which('node'), 'Node is required for offline snapshot JavaScript evaluation')
    def test_confirmation_digest_binds_query_hidden_fields_and_destination(self):
        script = r"""
global.crypto = require('crypto').webcrypto;
const form = {action:'https://example.com/send',method:'post'};
const field = {tagName:'INPUT',type:'hidden',name:'recipient',id:'',value:'first',checked:false};
const target = {tagName:'BUTTON',type:'submit',innerText:'Send',href:'',form,
  getAttribute(){return null},closest(){return form}};
global.document = {querySelector(){return target}, querySelectorAll(){return [field]}};
global.location = {href:'https://example.com/send?recipient=first'};
""" + 'const snapshot = ' + browser._CONFIRM_STATE_JS + r""";
(async () => {
  const digests = [await snapshot(1)];
  field.value='second'; digests.push(await snapshot(1));
  location.href='https://example.com/send?recipient=second'; digests.push(await snapshot(1));
  form.action='https://example.com/other'; digests.push(await snapshot(1));
  console.log(JSON.stringify(digests));
})().catch(e=>{console.error(e);process.exit(1)});
"""
        # Allow cold process/WebCrypto startup on loaded hosted Windows runners;
        # all four payload-binding assertions still have to pass without retries.
        result = subprocess.run([shutil.which('node'), '-e', script], check=True,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(len(set(json.loads(result.stdout))), 4)

    def test_boundary_javascript_runs_in_isolated_world(self):
        session, _ = self.session()
        devtools = Mock()
        devtools.call.side_effect = [dict(frameTree=dict(frame=dict(id='frame'))),
                                    dict(executionContextId=12), dict(result=dict(value='result'))]
        result = browser.BrowserSession._evaluate(session, devtools, 'session', 'document.title')
        self.assertEqual(result, 'result')
        self.assertEqual(devtools.call.call_args_list[1].args[0], 'Page.createIsolatedWorld')
        self.assertEqual(devtools.call.call_args_list[2].args[1]['contextId'], 12)

    def test_confirmed_click_is_never_replayed_after_ambiguous_timeout(self):
        session, devtools = self.session(tag='button', type='button', text='Send')
        session._settle = Mock(side_effect=WebCheckError('timed out'))
        with self.assertRaisesRegex(browser.BrowserError, 'not retried'):
            session.click('agent', 1, confirmed=True)
        self.assertEqual(sum(method == 'Input.dispatchMouseEvent' for method, _ in devtools.calls), 3)

    def test_type_is_never_replayed_after_ambiguous_timeout(self):
        session, devtools = self.session()
        session._settle = Mock(side_effect=WebCheckError('connection closed'))
        with self.assertRaisesRegex(browser.BrowserError, 'not retried'):
            session.type('agent', 1, 'synthetic')
        self.assertEqual(sum(method == 'Input.insertText' for method, _ in devtools.calls), 1)

    @unittest.skipUnless(shutil.which('node'), 'Node is required for offline snapshot JavaScript evaluation')
    def test_snapshot_never_returns_sensitive_field_values_or_value_labels(self):
        script = r"""
const specs = [
  ['password','', '', ''], ['text','otp','','one-time-code'],
  ['text','payment','','cc-number'], ['text','secret','',''], ['text','query','','']
];
const nodes = specs.map(([type,name,id,autocomplete], i) => ({
  type,name,id,autocomplete,value:i===4?'ordinary search':'SYNTHETIC_VALUE_'+i,
  tagName:'INPUT',labels:[],innerText:'',disabled:false,
  getAttribute(k) {return k==='autocomplete'?autocomplete:k==='name'?name:null},
  getBoundingClientRect() {return {width:100,height:20,top:0,bottom:20,left:0,right:100}},
  setAttribute(){},removeAttribute(){}
}));
global.getComputedStyle=()=>({visibility:'visible',display:'block'});
global.location={href:'https://example.com/'};
global.document={body:{innerText:'Ordinary page'}, title:'Fixture', querySelectorAll:()=>nodes};
""" + 'const result = ' + browser._READ_JS + '; console.log(JSON.stringify(result));'
        completed = subprocess.run([shutil.which('node'), '-e', script], check=True,
                                   capture_output=True, text=True, timeout=10)
        self.assertNotIn('SYNTHETIC_VALUE_', completed.stdout)
        self.assertIn('ordinary search', completed.stdout)
        self.assertNotIn('el.value', browser._LABEL_JS)
        self.assertNotIn('el.value', browser._DESCRIBE_JS)

    def test_proxy_refuses_private_and_mixed_dns_without_connecting(self):
        public = (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))
        for address in ('127.0.0.1', '10.0.0.1', '169.254.169.254', '::1'):
            private = (socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, 443))
            with self.subTest(address=address), patch.object(browser.socket, 'getaddrinfo', return_value=[public, private]), \
                    patch.object(browser.socket, 'socket') as create:
                with self.assertRaises(browser.BrowserError):
                    browser._public_connection('fixture.example', 443)
                create.assert_not_called()

    def test_proxy_pins_connection_to_validated_ip_without_second_resolution(self):
        resolved = (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))
        with patch.object(browser.socket, 'getaddrinfo', return_value=[resolved]) as resolve, \
                patch.object(browser.socket, 'socket') as create:
            result = browser._public_connection('fixture.example', 443)
        self.assertIs(result, create.return_value)
        resolve.assert_called_once()
        create.return_value.connect.assert_called_once_with(resolved[4])

    def test_proxy_checks_connect_and_absolute_subresource_requests(self):
        for request in (b'CONNECT private.example:443 HTTP/1.1\r\n\r\n',
                        b'GET http://private.example/image HTTP/1.1\r\n\r\n'):
            handler = object.__new__(browser._PublicProxyHandler)
            handler.connection = Mock()
            handler.rfile = io.BytesIO(request)
            handler.wfile = io.BytesIO()
            with patch.object(browser, '_public_connection', side_effect=browser.BrowserError('blocked')) as connect:
                handler.handle()
            connect.assert_called_once()
            self.assertIn(b'403 Forbidden', handler.wfile.getvalue())

    def test_browser_launch_disables_direct_and_loopback_proxy_bypasses(self):
        with tempfile.TemporaryDirectory() as directory:
            session = browser.BrowserSession(Path(directory))
            proxy = Mock(server_address=('127.0.0.1', 12345))
            def started(*_args, **_kwargs):
                (Path(directory) / 'DevToolsActivePort').write_text('12346\n/devtools/test', encoding='utf-8')
                return Mock()
            with patch.object(browser, '_PublicProxy', return_value=proxy), \
                    patch.object(browser, 'browser_executable', return_value=Path('browser')), \
                    patch.object(browser.subprocess, 'Popen', side_effect=started) as launch, \
                    patch.object(session, '_attach', return_value=Mock()), patch.object(browser.time, 'sleep'):
                session._connect()
            args = launch.call_args.args[0]
            self.assertIn('--proxy-server=http://127.0.0.1:12345', args)
            self.assertIn('--proxy-bypass-list=<-loopback>', args)
            self.assertIn('--disable-quic', args)
            self.assertIn('--force-webrtc-ip-handling-policy=disable_non_proxied_udp', args)

    def test_unknown_existing_browser_is_not_attached_without_proxy_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / 'DevToolsActivePort').write_text('12346\n/devtools/test', encoding='utf-8')
            session = browser.BrowserSession(Path(directory))
            with patch.object(browser, 'browser_executable', return_value=Path('browser')), \
                    patch.object(browser.socket, 'create_connection', return_value=Mock()), \
                    patch.object(session, '_attach') as attach:
                with self.assertRaisesRegex(browser.BrowserError, 'Close the existing'):
                    session._connect()
                attach.assert_not_called()

    def test_public_proxy_forwards_public_http_without_proxy_credentials(self):
        handler = object.__new__(browser._PublicProxyHandler)
        handler.connection = Mock()
        handler.rfile = io.BytesIO(b'GET http://example.com/page?q=1 HTTP/1.1\r\n'
                                  b'Proxy-Authorization: placeholder\r\nHost: forged.example\r\n\r\n')
        handler.wfile = io.BytesIO()
        upstream = Mock()
        with patch.object(browser, '_public_connection', return_value=upstream) as connect, \
                patch.object(browser.select, 'select', return_value=([], [], [])):
            handler.handle()
        connect.assert_called_once_with('example.com', 80)
        request = upstream.sendall.call_args.args[0]
        self.assertIn(b'GET /page?q=1 HTTP/1.1', request)
        self.assertIn(b'Host: example.com', request)
        self.assertNotIn(b'placeholder', request)
        self.assertNotIn(b'forged.example', request)


class RuntimeReleaseSecurityTests(unittest.TestCase):
    def runtime(self):
        runtime = object.__new__(AgentRuntime)
        runtime._lock = threading.RLock()
        runtime._permission_locks = {}
        self.settings = {'permissions': {'files_write': True}, 'archived': False}
        runtime.agent_settings = lambda _agent: self.settings
        runtime._save_agent_settings = lambda _agent, **values: self.settings.update(
            permissions=values['permissions'])
        return runtime

    def test_permissions_are_checked_again_on_each_call(self):
        runtime = self.runtime()
        execute = Mock(return_value='{"ok":true}')
        runtime._authorized_tool('agent', 'write_file', {}, execute)
        runtime.save_agent_settings('agent', permissions={'files_write': False})
        result = json.loads(runtime._authorized_tool('agent', 'write_file', {}, execute))
        self.assertFalse(result['ok'])
        self.assertEqual(execute.call_count, 1)

    def test_acknowledged_revocation_fences_inflight_work(self):
        runtime = self.runtime()
        entered, finish, saved = threading.Event(), threading.Event(), threading.Event()
        def execute(_name, _arguments):
            entered.set()
            finish.wait(3)
            return '{"ok":true}'
        worker = threading.Thread(target=runtime._authorized_tool, args=('agent', 'write_file', {}, execute))
        def revoke():
            runtime.save_agent_settings('agent', permissions={'files_write': False})
            saved.set()
        worker.start()
        self.assertTrue(entered.wait(1))
        revoker = threading.Thread(target=revoke)
        revoker.start()
        try:
            self.assertFalse(saved.wait(.05))
        finally:
            finish.set()
            worker.join(3)
            revoker.join(3)
        self.assertTrue(saved.is_set())
        next_call = Mock()
        self.assertFalse(json.loads(runtime._authorized_tool('agent', 'write_file', {}, next_call))['ok'])
        next_call.assert_not_called()

    def test_archived_agent_cannot_use_retained_tool(self):
        runtime = self.runtime()
        self.settings['archived'] = True
        execute = Mock()
        self.assertFalse(json.loads(runtime._authorized_tool('agent', 'write_file', {}, execute))['ok'])
        execute.assert_not_called()

    def test_file_and_command_changes_invalidate_preview_verification(self):
        for tool in ('write_file', 'edit_file', 'move_path', 'trash_path', 'run_process', 'start_process'):
            for result in ({'written': True}, 'written'):
                with self.subTest(tool=tool, result=result):
                    launch = {'processes': {}, 'verified': [('http://localhost:8765', 'process')]}
                    AgentRuntime._note_launch_evidence(launch, tool, {}, json.dumps({'ok': True, 'result': result}))
                    self.assertEqual(launch['verified'], [])

    def test_read_only_tools_do_not_invalidate_preview_verification(self):
        launch = {'processes': {}, 'verified': [('http://localhost:8765', 'process')]}
        AgentRuntime._note_launch_evidence(launch, 'read_file', {}, json.dumps({'ok': True, 'result': 'text'}))
        self.assertEqual(len(launch['verified']), 1)

    def test_browser_approval_binds_payload_and_rechecks_before_dispatch(self):
        runtime = object.__new__(AgentRuntime)
        page = dict(page='https://example.com/send?recipient=first', text='Send', tag='button',
                    payload_sha256='a' * 64)
        session = Mock()
        session.describe.side_effect = lambda _agent, _ref: dict(page)
        runtime.browser = lambda: session
        toolbox = SimpleNamespace(tools={}, _approved_arguments_for=lambda _name: approved)
        runtime._install_browser_tools(toolbox, 'agent', {'browser_confirm_click'})
        approved = toolbox.browser_snapshot({'ref': 1})
        self.assertNotIn('recipient=first', json.dumps(approved))
        self.assertEqual(approved['payload_sha256'], 'a' * 64)
        page['payload_sha256'] = 'b' * 64
        with self.assertRaisesRegex(PermissionError, 'form contents changed'):
            toolbox.tools['browser_confirm_click'].function(ref=1)
        session.click.assert_not_called()
        page['payload_sha256'] = 'a' * 64
        toolbox.tools['browser_confirm_click'].function(ref=1)
        session.click.assert_called_once_with('agent', 1, confirmed=True, expected_payload_sha256='a' * 64)
