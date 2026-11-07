"""Agent completion requires actual observations, using the real SDK worker.

Only synthetic loopback HTTP and an owned empty workspace are used. Planner
metadata and optimistic model prose must never prove a file was created.
"""
from pathlib import Path
from contextlib import closing
import sqlite3
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
from context_store import ObservationStore


@unittest.skipUnless(PYTHON.is_file(), 'isolated nanobot SDK environment missing')
class AgentCompletionEvidenceTests(unittest.TestCase):
    def run_case(self, steps):
        workspace_root = (REPO / 'agent_workspace').resolve()
        workspace_root.mkdir(exist_ok=True)
        model = ScriptedModel(steps)
        client = Client()
        try:
            with tempfile.TemporaryDirectory(prefix='agent-completion-evidence-', dir=workspace_root) as owned:
                workspace = Path(owned).resolve()
                self.assertTrue(workspace.is_relative_to(workspace_root))
                client.send({'type': 'run', 'id': 'completion-evidence-case', 'kind': 'agent',
                    'conversation_id': str(uuid4()), 'session_history': [],
                    'task': 'Create report.txt in the delegated workspace, containing exactly HELLO. Read it back before confirming success.',
                    'context': '', 'config': {'base_url': model.base_url,
                        'api_key': 'synthetic-completion-evidence-key', 'model': 'offline-fixture'},
                    'options': {'workspace': str(workspace), 'max_steps': 0, 'timeout_seconds': 20,
                        'tools_enabled': True, 'computer_enabled': False, 'checkpoint_required': False,
                        'include_activity': True}})
                result = client.terminal()
                self.assertEqual(result['type'], 'final', result)
                self.assertEqual(result['status'], 'needs_user', result)
                self.assertIn('Created report.txt with content HELLO.', result['reply'])
                self.assertFalse((workspace / 'report.txt').exists())
                effects = [frame for frame in client.all_frames if frame['type'] == 'activity'
                           and frame.get('name') not in {'update_plan', 'finish_task'}]
                self.assertEqual(effects, [], 'This model used no actual effect or observation tool.')
                self.assertEqual(list(workspace.iterdir()), [], 'Planner metadata cannot write a user artifact.')
                self.assertEqual(model.errors, [])
                return result, len(model.calls)
        finally:
            self.assertEqual(client.close(), '')
            model.close()

    def test_plain_agent_claim_without_tools_preserves_reply_but_is_not_completed(self):
        result, calls = self.run_case(['Created report.txt with content HELLO.'])
        self.assertEqual(result['tools_used'], [])
        self.assertEqual(calls, 1)

    def test_completed_plan_and_finish_declaration_without_actual_tools_are_not_evidence(self):
        result, calls = self.run_case([
            tool('update_plan', steps=[{'content': 'Create report.txt and read it back', 'status': 'completed'}]),
            tool('finish_task', status='completed', summary='All requested work finished'),
            'Created report.txt with content HELLO.',
        ])
        self.assertTrue(set(result['tools_used']).issubset({'update_plan', 'finish_task'}))
        self.assertEqual(calls, 3)

    def test_late_worker_cannot_reactivate_revoked_persistent_observations(self):
        workspace_root = (REPO / 'agent_workspace').resolve()
        workspace_root.mkdir(exist_ok=True)
        conversation = str(uuid4())
        namespace = conversation + ':' + 'a' * 64
        model = ScriptedModel(['This provider must never be reached.'])
        client = None
        try:
            with tempfile.TemporaryDirectory(prefix='late-worker-context-') as archived, \
                    tempfile.TemporaryDirectory(prefix='late-worker-owned-', dir=workspace_root) as owned:
                store = ObservationStore(archived, namespace)
                store.activate()  # Trusted host, before conversation deletion.
                store.put_text('old private observation')
                ObservationStore.clear_conversation(archived, conversation)
                client = Client()  # Simulate a worker that starts after deletion.
                client.send({'type': 'run', 'id': 'late-worker-case', 'kind': 'agent',
                    'conversation_id': conversation,
                    'session_history': [
                        {'role': 'user', 'content': 'A prior task.'},
                        {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'prior-read', 'type': 'function',
                            'function': {'name': 'read_file', 'arguments': '{"path":"prior.txt"}'}}]},
                        {'role': 'tool', 'tool_call_id': 'prior-read', 'name': 'read_file',
                         'content': 'A prior observed body. ' * 1000},
                        {'role': 'assistant', 'content': 'Prior work stopped.'}],
                    'task': 'Review the earlier observation without executing anything.', 'context': '',
                    'config': {'base_url': model.base_url, 'api_key': 'synthetic-late-worker-key', 'model': 'offline-fixture'},
                    'options': {'workspace': str(Path(owned).resolve()), 'max_steps': 0, 'timeout_seconds': 20,
                        'tools_enabled': True, 'computer_enabled': False, 'checkpoint_required': False,
                        'context_store_root': archived, 'context_store_namespace': namespace}})
                result = client.terminal()
                self.assertEqual(result['type'], 'error', result)
                self.assertEqual(model.calls, [], 'A revoked archive must fail closed before a model request.')
                with closing(sqlite3.connect(store.path)) as connection:
                    self.assertEqual(connection.execute('SELECT revoked FROM namespaces WHERE namespace=?', (namespace,)).fetchone()[0], 1)
                    self.assertEqual(connection.execute('SELECT count(*) FROM observations WHERE namespace=?', (namespace,)).fetchone()[0], 0)
                self.assertEqual(list(Path(owned).iterdir()), [])
                self.assertEqual(client.close(), '')
                client = None
        finally:
            if client is not None:
                self.assertEqual(client.close(), '')
            model.close()


if __name__ == '__main__':
    unittest.main()
