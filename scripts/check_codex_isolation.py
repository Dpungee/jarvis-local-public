"""Offline prompt-shape probe, with empty temporary home and no real credentials.

This does not prove the model-visible tool registry is empty; it never sends a turn.
"""
import json
import subprocess
import tempfile
from pathlib import Path

with tempfile.TemporaryDirectory(prefix='jarvis-codex-empty-') as directory:
    import os
    env={k:v for k,v in os.environ.items() if k.upper() in {'SYSTEMROOT','WINDIR','PATH','PATHEXT','TEMP','TMP'}}
    env.update(CODEX_HOME=directory,HOME=directory,USERPROFILE=directory)
    args=['codex','debug','prompt-input']
    settings=['project_doc_max_bytes=0','include_environment_context=false',
              'include_apps_instructions=false','skills.include_instructions=false',
              'web_search="disabled"','agents.enabled=false',
              'developer_instructions="Text-only isolation probe."']
    features=['shell_tool','shell_snapshot','unified_exec','apps','plugins','hooks','memories',
              'multi_agent','multi_agent_v2','browser_use','in_app_browser','computer_use',
              'code_mode_host','image_generation','skill_search','skill_mcp_dependency_install',
              'workspace_dependencies','goals','remote_plugin','tool_suggest']
    for setting in settings:args.extend(['-c',setting])
    for feature in features:args.extend(['--disable',feature])
    args.append('Synthetic hello.')
    result=subprocess.run(args,cwd=directory,env=env,capture_output=True,text=True,timeout=30)
    print('exit',result.returncode)
    if result.returncode:
        print(result.stderr[-1200:].replace(directory,'<isolated-directory>').replace(str(Path.home()),'<user-home>'))
    else:
        data=json.loads(result.stdout)
        rendered=json.dumps(data)
        print(json.dumps({'shape':type(data).__name__,'fields':list(data) if isinstance(data,dict) else None,
              'environment_context_present':'<environment_context>' in rendered,
              'skills_present':'<skills_instructions>' in rendered,
              'private_user_path_present':str(Path.home()) in rendered,
              'tool_registry_proven_empty':False}))
    for candidate in [['-c','tools.enabled=false'], ['--disable','view_image']]:
        probe=['codex','exec','--strict-config','--ignore-user-config','--ignore-rules',
               '--skip-git-repo-check','--ephemeral',*candidate,'Synthetic hello.']
        checked=subprocess.run(probe,cwd=directory,env=env,capture_output=True,text=True,timeout=15)
        # Report only known option/config diagnostics, never complete CLI output.
        print(json.dumps({'candidate':candidate,'exit':checked.returncode,
              'unknown_feature':'Unknown feature' in checked.stderr or 'unknown feature' in checked.stderr,
              'unknown_field':'unknown field' in checked.stderr,
              'config_error':'Error loading configuration' in checked.stderr}))
        print('\n'.join(line for line in checked.stderr.splitlines() if not line.startswith('WARNING:'))[-700:])
