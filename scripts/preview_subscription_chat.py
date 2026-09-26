"""Disposable synthetic live-subscription acceptance server; no private workspace roots."""
import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jarvis.command_center import CommandCenterService, CommandCenterHTTPServer
from jarvis.subscription_chat import subscription_providers

parser = argparse.ArgumentParser()
parser.add_argument('--port', type=int, default=8767)
parser.add_argument('--directory', type=Path)
args = parser.parse_args()
directory = args.directory or Path(tempfile.mkdtemp(prefix='jarvis-subscription-preview-'))
directory.mkdir(parents=True, exist_ok=True)
service = CommandCenterService(directory / 'runtime.db', directory / 'conversation.db',
                               providers=subscription_providers(), simulator_interval=.15)
if not service.list_agents():
    for provider, name in [('claude-cli', 'Claude Chat'), ('codex-cli', 'Codex Chat')]:
        agent = service.create_agent({'name': name, 'role': 'Text assistant', 'purpose': '',
                                      'provider': provider, 'model': 'default', 'project_id': 'synthetic'})
        service.set_lifecycle(agent['agent_id'], 'start')
server = CommandCenterHTTPServer(('127.0.0.1', args.port), service)
print(f'Data: {directory}', flush=True)
print(f'http://127.0.0.1:{server.server_port}/#token={server.token}', flush=True)
try:
    server.serve_forever()
except KeyboardInterrupt:
    pass
finally:
    server.server_close()
    service.close()
