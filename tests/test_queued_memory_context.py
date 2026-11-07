"""Context export must not use an undispatched job as its memory query."""
from types import SimpleNamespace
import unittest

from tests import test_chat_images as fixture


class QueuedMemoryContextTests(unittest.TestCase):
    setUpClass = classmethod(fixture.ChatImageTests.setUpClass.__func__)
    setUp = fixture.ChatImageTests.setUp
    tearDown = fixture.ChatImageTests.tearDown
    rpc = fixture.ChatImageTests.rpc

    def test_default_memory_query_uses_only_completed_or_legacy_archive_turn(self):
        queries = []
        self.chat._memory = SimpleNamespace(
            load_settings=lambda: SimpleNamespace(enabled=True, relevance_threshold=.6),
            search=lambda query, **kwargs: queries.append(query) or [])
        try:
            self.chat.store.append_message(self.cid, "user", "Completed archive fact", "user",
                                           metadata={"request_status": "complete"})
            for status in ("queued", "pending", "failed", "cancelled", "interrupted", "unknown"):
                self.chat.store.append_message(self.cid, "user", "Unusable future " + status, "user",
                                               metadata={"request_status": status})
            envelope = self.chat.context_envelope_for(self.cid)
            self.assertEqual("available", envelope["memory_status"])
            self.assertEqual(["Completed archive fact"], queries)
            self.chat.context_envelope_for(self.cid, query="Explicit phone query")
            self.assertEqual("Explicit phone query", queries[-1])
            self.assertEqual(7, len(self.chat.store.load_messages(self.cid)))
        finally:
            self.chat._memory = None

    def test_queue_only_archive_does_not_trigger_memory_search(self):
        queries = []
        self.chat._memory = SimpleNamespace(
            load_settings=lambda: SimpleNamespace(enabled=True, relevance_threshold=.6),
            search=lambda query, **kwargs: queries.append(query) or [])
        try:
            self.chat.store.append_message(self.cid, "user", "Never dispatched", "user",
                                           metadata={"request_status": "queued"})
            self.chat.context_envelope_for(self.cid)
            self.assertEqual([], queries)
            self.assertEqual("Never dispatched", self.chat.store.load_messages(self.cid)[0]["content"])
        finally:
            self.chat._memory = None


if __name__ == "__main__": unittest.main()
