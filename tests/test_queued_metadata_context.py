"""Queued instructions cannot become existing facts in shared chat metadata."""
import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from desktop_pet.conversation_metadata import context_summary, update_session_metadata
from desktop_pet.conversations.store import ConversationStore


class QueuedMetadataContextTests(unittest.TestCase):
    def test_future_queued_user_text_and_old_contaminated_summary_do_not_reach_metadata_model(self):
        with tempfile.TemporaryDirectory(prefix="aura-queued-metadata-") as owned:
            store = ConversationStore(Path(owned) / "history.sqlite3")
            cid = str(uuid4())
            store.append_message(cid, "user", "Completed topic", "user")
            store.append_message(cid, "assistant", "Actual completed answer", "assistant")
            queued = store.append_message(cid, "user", "FUTURE_QUEUED_TASK", "user",
                                          metadata={"request_status": "queued", "execution_mode": "agent"})
            store.update_metadata(cid, summary="OLD_SUMMARY_CONTAINING_FUTURE_QUEUED_TASK")
            before = store.load_messages(cid)
            safe = context_summary(store.get_conversation(cid), before)
            self.assertNotIn("FUTURE_QUEUED_TASK", safe)
            calls = []
            class Client:
                def request_text(self, settings, key, messages):
                    calls.append(messages)
                    return '{"title":"Completed topic","summary":"Actual completed answer"}'
            update_session_metadata(store, cid, Client(), None, "")
            self.assertEqual(len(calls), 1)
            self.assertNotIn("FUTURE_QUEUED_TASK", json.dumps(calls))
            self.assertEqual(next(row for row in store.load_messages(cid) if row["id"] == queued["id"])["content"],
                             "FUTURE_QUEUED_TASK")


if __name__ == "__main__":
    unittest.main()
