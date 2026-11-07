"""Real Controller notifications keep the final canonical and guarded trace."""
from copy import deepcopy
from datetime import datetime, timedelta
import json
import unittest

from desktop_pet.execution_trace import ExecutionTrace, ExecutionViewUpdate
from tests import test_agent_shutdown_completion as owned


class ExecutionViewNotificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        owned.AgentShutdownCompletionTests.setUpClass()

    def setUp(self):
        self.case = owned.AgentShutdownCompletionTests('runTest')
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.chat = self.case.chat
        self.cid = self.case.cid
        self.message = self.case.store.append_message(self.cid, 'user', 'OWNED public trace check', 'user',
            metadata={'request_status': 'complete'})
        self.assertTrue(self.chat.open_conversation(self.cid))
        self.case.wait(lambda: not self.chat.history_loading and not self.chat._runner._tasks)
        self.updates, self.legacy = [], []
        self.chat.messages_view_changed.connect(self.updates.append)
        self.chat.messages_changed.connect(lambda: self.legacy.append(True))

    def accepted(self, value):
        self.chat._accept_execution(self.cid, self.message['id'], value)
        update = self.updates[-1]
        self.assertIsInstance(update, ExecutionViewUpdate)
        return json.loads(update.execution_json)

    def test_late_running_update_publishes_the_existing_terminal_snapshot(self):
        trace = ExecutionTrace('owned')
        running = trace.snapshot()
        cancelled = trace.finish('cancelled')
        self.assertEqual(self.accepted(cancelled)['state'], 'cancelled')
        running['duration_ms'] = cancelled['duration_ms'] + 1000
        running['events'][0]['label'] = 'LATE OWNED RUNNING'
        result = self.accepted(running)
        self.assertEqual(result['state'], 'cancelled')
        self.assertEqual(result, self.chat.messages[0]['metadata']['execution'])
        self.assertEqual(len(self.legacy), 2)

    def test_unconfirmed_commit_guard_is_applied_before_building_the_view_payload(self):
        trace = ExecutionTrace('owned')
        blocked = trace.finish('interrupted')
        self.chat._unconfirmed_reply_commits[(self.cid, self.message['id'])] = deepcopy(blocked)
        late = deepcopy(blocked)
        late['state'] = 'completed'
        late['duration_ms'] += 1000
        late['events'][0]['label'] = 'LATE OWNED COMPLETION'
        result = self.accepted(late)
        self.assertEqual(result, blocked)
        self.assertEqual(self.chat.messages[0]['metadata']['execution'], blocked)
        self.assertEqual(len(self.legacy), 1)

    def test_new_conversation_is_a_full_notification_and_keeps_legacy_subscribers(self):
        original = self.chat.current_conversation_id
        self.chat.new_conversation()
        self.assertNotEqual(self.chat.current_conversation_id, original)
        self.assertTrue(self.updates)
        self.assertTrue(all(update is None for update in self.updates))
        self.assertEqual(len(self.updates), len(self.legacy))

    def test_fresh_attempt_replaces_the_old_trace_with_its_own_identity(self):
        trace = ExecutionTrace('owned')
        previous = trace.finish('completed')
        self.accepted(previous)
        fresh = trace.snapshot()
        fresh['state'] = 'running'
        fresh['started_at'] = (datetime.fromisoformat(previous['started_at']) + timedelta(seconds=1)).isoformat()
        fresh['duration_ms'] = 0
        result = self.accepted(fresh)
        self.assertEqual(result['started_at'], fresh['started_at'])
        self.assertEqual(result['state'], 'running')
        self.assertEqual(len(self.legacy), 2)


if __name__ == '__main__':
    unittest.main()
