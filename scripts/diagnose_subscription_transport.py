"""Synthetic CLI protocol diagnostics; never emits auth records or raw system metadata."""
import json
import subprocess
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jarvis.subscription_chat import ClaudeSubscription, safe_environment, SYSTEM
from jarvis.redaction import contains_secret
adapter = ClaudeSubscription()
with tempfile.TemporaryDirectory(prefix='jarvis-protocol-check-') as cwd:
    args=[adapter.executable, '--safe-mode', '--setting-sources', '', '--tools', '',
          '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}', '--disable-slash-commands',
          '--permission-mode', 'dontAsk', '--no-chrome', '--no-session-persistence',
          '--system-prompt', SYSTEM, '--output-format', 'stream-json', '--verbose', '-p']
    result=subprocess.run(args,input='Reply with hello only.',capture_output=True,text=True,
                          env=safe_environment(),cwd=cwd,timeout=45)
    print('exit',result.returncode)
    for line in result.stdout.splitlines():
        event=json.loads(line)
        if event.get('type')=='system':
            print(json.dumps({k:event.get(k) for k in ['type','subtype','tools','mcp_servers','model','permissionMode']}))
        elif event.get('type')=='result':
            out={k:event.get(k) for k in ['type','subtype','is_error','result','errors']}
            rendered=json.dumps(out)
            print(rendered if not contains_secret(rendered) else 'Error diagnostic withheld by secret screen')
