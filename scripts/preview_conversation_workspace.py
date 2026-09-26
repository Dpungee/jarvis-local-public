"""Disposable, synthetic-only command-center preview (no configured providers)."""
import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jarvis.command_center import CommandCenterHTTPServer, CommandCenterService, OfflineDemoProvider


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8766)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='jarvis-conversation-preview-') as directory:
        root = Path(directory)
        files = root/'synthetic-project'
        files.mkdir()
        (files/'brief.md').write_text('# Example brief\n\nSynthetic project for conversational interaction testing.\n', encoding='utf-8')
        service = CommandCenterService(root/'runtime.db',root/'execution.db',
                                      providers={'offline-demo':OfflineDemoProvider()},
                                      workspace_roots={'example':files})
        service.conversations.ensure_project('example','Design Lab')
        service.conversations.ensure_project('second','Research Studio')
        for name, project in [('Atlas','example'),('Nova','second')]:
            agent = service.create_agent({'name':name,'role':'Conversation simulator','provider':'offline-demo',
                                          'model':'offline-checkpoints','project_id':project})
            service.set_lifecycle(agent['agent_id'],'start')
        server = CommandCenterHTTPServer(('127.0.0.1',args.port),service)
        print(f'http://127.0.0.1:{server.server_port}/#token={server.token}',flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
            service.close()


if __name__ == '__main__':
    main()
