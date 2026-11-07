"""Real SDK stdin/stdout with synthetic escaped credentials and local HTTP."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from uuid import uuid4

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parents[1]
sys.path.insert(0, str(BACKEND / 'tests'))
sys.path.insert(0, str(BACKEND))
from test_harness_worker import ScriptedModel, tool
from test_worker import Client, PYTHON


@unittest.skipUnless(PYTHON.is_file(), 'isolated nanobot SDK environment missing')
class AgentRedactionWireTests(unittest.TestCase):
    def test_worker_redacts_encoded_reply_and_published_native_tool_arguments(self):
        secret = 'synthetic-wire-\\-"-credential'
        reply = {'private': secret, 'safe': 'visible reply body'}
        for index in range(5):
            reply = json.dumps(reply, ensure_ascii=index % 2 == 0)
        model = ScriptedModel([tool('read_file', path='owned-' + secret + '.txt'),
                               'MODEL_FIXTURE_JSON:' + reply])
        client = Client()
        try:
            workspace_root = (REPO / 'agent_workspace').resolve()
            workspace_root.mkdir(exist_ok=True)
            with tempfile.TemporaryDirectory(prefix='redaction-wire-owned-', dir=workspace_root) as owned:
                self.assertTrue(Path(owned).resolve().is_relative_to(workspace_root))
                client.send({'type': 'run', 'id': 'redaction-wire-case', 'kind': 'agent',
                    'conversation_id': str(uuid4()), 'session_history': [],
                    'task': 'Read only the owned fixture. Do not write or change any file. Explain the observed result.',
                    'context': '', 'config': {'base_url': model.base_url, 'api_key': secret, 'model': 'offline-fixture'},
                    'options': {'workspace': str(Path(owned).resolve()), 'max_steps': 0, 'timeout_seconds': 20,
                        'tools_enabled': True, 'computer_enabled': False, 'checkpoint_required': False}})
                result = client.terminal()
                self.assertEqual(result['type'], 'final')
                projected = result['reply'].split('MODEL_FIXTURE_JSON:', 1)[1]
                for _ in range(5):
                    projected = json.loads(projected)
                self.assertEqual(projected, {'private': '[redacted]', 'safe': 'visible reply body'})
                arguments = [call['function']['arguments'] for frame in client.all_frames
                    if frame['type'] == 'session' for call in frame['message'].get('tool_calls', [])]
                self.assertEqual(len(arguments), 1)
                self.assertEqual(json.loads(arguments[0])['path'], 'owned-[redacted].txt')
                self.assertEqual(list(Path(owned).iterdir()), [])
                self.assertEqual(model.errors, [])
                self.assertEqual(client.close(), '')
                client = None
        finally:
            if client is not None:
                self.assertEqual(client.close(), '')
            model.close()


if __name__ == '__main__':
    unittest.main()
