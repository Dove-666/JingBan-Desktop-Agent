"""Qt stream identity/cancellation behavior with an actual worker thread."""
import threading
import unittest

from PySide6.QtCore import QThreadPool
from PySide6.QtTest import QTest
from desktop_pet.api_client import ApiClient, ApiError, ChatReply
from tests import test_chat_controller as support


class SlowStreamingClient(ApiClient):
    def __init__(self):
        super().__init__()
        self.release = threading.Event()
        self.seen = threading.Event()
        self.calls = 0

    def complete_context(self, settings, key, messages, *, on_delta=None, cancellation_event=None, **kwargs):
        self.calls += 1
        on_delta("鲸鱼娘正在回复")
        self.seen.set()
        self.release.wait(3)
        if cancellation_event.is_set():
            raise ApiError("cancelled", "cancelled", partial_reply="鲸鱼娘正在回复")
        on_delta("鲸鱼娘正在回复。完成")
        return ChatReply("鲸鱼娘正在回复。完成")


class ChatStreamingControllerTests(unittest.TestCase):
    setUpClass = classmethod(support.ChatControllerTests.setUpClass.__func__)
    setUp = support.ChatControllerTests.setUp
    tearDown = support.ChatControllerTests.tearDown
    make_controller = support.ChatControllerTests.make_controller
    spin_until = support.ChatControllerTests.spin_until
    def test_stream_shows_before_completion_and_commits_exactly_once(self):
        client = SlowStreamingClient()
        controller = self.make_controller(client=client)
        self.assertTrue(controller.send("hello"))
        self.spin_until(lambda: controller.streaming_reply is not None)
        self.assertTrue(controller.busy)
        self.assertEqual(controller.streaming_reply["text"], "鲸鱼娘正在回复")
        self.assertFalse(any(row["role"] == "assistant" for row in controller.messages))
        client.release.set()
        self.spin_until(lambda: not controller.busy)
        self.assertIsNone(controller.streaming_reply)
        self.assertEqual(len([row for row in controller.messages if row["role"] == "assistant"]), 1)
        self.assertEqual(client.calls, 1)

    def test_stop_retains_partial_as_interrupted_and_rejects_late_delta(self):
        client = SlowStreamingClient()
        controller = self.make_controller(client=client)
        self.assertTrue(controller.send("hello"))
        self.spin_until(lambda: controller.streaming_reply is not None)
        old = controller._request_id
        cid = controller.current_conversation_id
        self.assertTrue(controller.stop_request())
        client.release.set()
        controller._stream_delta.emit(old, cid, "late reply must not be shown")
        QTest.qWait(60)
        self.assertEqual(controller.streaming_reply["state"], "interrupted")
        self.assertEqual(controller.streaming_reply["text"], "鲸鱼娘正在回复")
        self.assertFalse(any(row["role"] == "assistant" for row in controller.messages))
        self.assertEqual(client.calls, 1)


if __name__ == "__main__":
    unittest.main()
