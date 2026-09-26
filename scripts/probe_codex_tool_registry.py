"""Offline wire-shape test. Fake local endpoint, no credentials or cloud request.

Never count its output as a real model response. It reports tool names only.
"""
import json
import os
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

captured=[]
class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_POST(self):
        raw=self.rfile.read(int(self.headers.get('Content-Length','0')))
        try:
            body=json.loads(raw)
            captured.append({'model':body.get('model'),'tools':body.get('tools',[]), 'input':body.get('input',[])})
        except ValueError:
            captured.append({'decode_error':True})
        reply=json.dumps({'error':{'message':'Offline isolation probe complete','type':'invalid_request_error'}}).encode()
        self.send_response(400);self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(reply)));self.end_headers();self.wfile.write(reply)

server=HTTPServer(('127.0.0.1',0),Handler)
threading.Thread(target=server.serve_forever,daemon=True).start()
try:
 with tempfile.TemporaryDirectory(prefix='jarvis-codex-wire-') as directory:
    env={k:v for k,v in os.environ.items() if k.upper() in {'SYSTEMROOT','WINDIR','PATH','PATHEXT','TEMP','TMP'}}
    env.update(CODEX_HOME=directory,HOME=directory,USERPROFILE=directory)
    args=['codex','exec','--ignore-user-config','--ignore-rules','--skip-git-repo-check','--ephemeral','--json']
    settings=['model_provider="offline_probe"',
              'model_providers.offline_probe.name="Offline local test"',
              f'model_providers.offline_probe.base_url="http://127.0.0.1:{server.server_port}/v1"',
              'model_providers.offline_probe.wire_api="responses"',
              'model_providers.offline_probe.requires_openai_auth=false',
              'model="'+os.environ.get('JARVIS_PROBE_MODEL','gpt-5.6-sol')+'"',
              'project_doc_max_bytes=0','include_environment_context=false',
              'include_apps_instructions=false','skills.include_instructions=false',
              'web_search="disabled"','agents.enabled=false','sandbox_mode="read-only"']
    features=['shell_tool','shell_snapshot','unified_exec','apps','plugins','hooks','memories',
              'multi_agent','multi_agent_v2','browser_use','in_app_browser','computer_use',
              'code_mode_host','image_generation','skill_search','skill_mcp_dependency_install',
              'workspace_dependencies','goals','remote_plugin','tool_suggest','enable_request_compression']
    for value in settings:args.extend(['-c',value])
    for feature in features:args.extend(['--disable',feature])
    args.append('Synthetic offline probe. No real model is connected.')
    try:
        result=subprocess.run(args,cwd=directory,env=env,capture_output=True,text=True,timeout=20)
        print('CLI exit:',result.returncode)
    except subprocess.TimeoutExpired:
        print('CLI timed out; offline request capture follows.')
    for request in captured:
        tools=request.get('tools',[])
        def strings(value):
            if isinstance(value,str):return [value]
            if isinstance(value,dict):return [s for v in value.values() for s in strings(v)]
            if isinstance(value,list):return [s for v in value for s in strings(v)]
            return []
        print(json.dumps({'model':request.get('model'),'tool_names':[t.get('name',t.get('type')) for t in tools],
                          'private_windows_path_present':any('C:\\Users\\' in s for s in strings(request.get('input',[]))),
                          'cwd_present':any(directory in s for s in strings(request.get('input',[]))),
                          'environment_context_present':'<environment_context>' in json.dumps(request.get('input')),
                          'skills_context_present':'<skills_instructions>' in json.dumps(request.get('input'))}))
    if not captured:
        print('No request captured; no tool-registry conclusion available.')
        print(result.stdout[-1200:].replace(directory,'<isolated>'))
        print(result.stderr[-1200:].replace(directory,'<isolated>'))
finally:
    server.shutdown();server.server_close()
