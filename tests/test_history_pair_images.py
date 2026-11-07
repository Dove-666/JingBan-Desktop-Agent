"""Comparing two already-sent photos must not require attaching one again."""
import unittest
from uuid import uuid4
from tests import test_explicit_image_comparison as comparison
from tests import test_chat_images as fixture
from tests.mock_api import MockApiServer

images = comparison.images


class HistoryPairImageTests(unittest.TestCase):
    setUpClass = classmethod(comparison.ExplicitImageComparisonTests.setUpClass.__func__)
    setUp = comparison.ExplicitImageComparisonTests.setUp
    tearDown = comparison.ExplicitImageComparisonTests.tearDown
    rpc = comparison.ExplicitImageComparisonTests.rpc
    configure = comparison.ExplicitImageComparisonTests.configure
    upload = comparison.ExplicitImageComparisonTests.upload
    send = comparison.ExplicitImageComparisonTests.send
    def test_explicit_pair_reference_without_new_attachment_sends_both_original_images(self):
        old, old_data = self.upload()
        latest, latest_data = self.upload("#f47b75")
        with MockApiServer() as server:
            self.configure(server)
            self.send(server, "第一张图片", image_id=old)
            self.send(server, "第二张图片", image_id=latest)
            self.send(server, "对比这两张图片，有什么区别？")
            actual = images(server.requests[-1])
            self.assertEqual([old_data, latest_data], [url.split(",", 1)[1] for _, url in actual])
            users = [message for message in self.chat.store.load_messages(self.cid) if message["role"] == "user"]
            self.assertNotIn("image_id", users[-1]["metadata"])
            self.assertEqual(["第一张图片", "第二张图片"], [message["content"][0]["text"] for message, _ in actual])

    def test_insufficient_pair_reference_without_attachment_is_rejected_before_http(self):
        old, _ = self.upload()
        with MockApiServer() as server:
            self.configure(server)
            self.send(server, "第一张图片", image_id=old)
            self.send(server, "对比这两张图片，有什么区别？")
            self.assertEqual(1, len(server.requests))

    def test_retry_retains_original_pair_after_later_image(self):
        old, old_data = self.upload(); latest, latest_data = self.upload("#f47b75")
        later, later_data = self.upload("#36ab54")
        with MockApiServer() as server:
            self.configure(server)
            self.send(server, "第一张", image_id=old)
            self.send(server, "第二张", image_id=latest)
            failed, _ = self.send(server, "对比这两张图片", status=500)
            self.send(server, "后来新话题", image_id=later)
            server.enqueue("比较已完成")
            self.assertTrue(self.chat.retry(self.cid, failed["result"]["message_id"]))
            fixture.fixtures.spin_until(lambda: not self.chat.busy)
            self.assertEqual([old_data, latest_data], [url.split(",", 1)[1] for _, url in images(server.requests[-1])])
            self.assertNotIn(later_data, str(server.requests[-1]["body"]))

    def test_historical_pair_does_not_cross_conversations_or_read_missing_pixels(self):
        old, _ = self.upload(); latest, _ = self.upload("#f47b75")
        with MockApiServer() as server:
            self.configure(server)
            self.send(server, "第一张", image_id=old)
            self.send(server, "第二张", image_id=latest)
            self.send(server, "对比这两张图片", cid=str(uuid4()))
            self.assertEqual(2, len(server.requests))
            self.remote.images.delete(old)
            self.send(server, "对比这两张图片")
            self.assertEqual(2, len(server.requests))
            users = [m for m in self.chat.store.load_messages(self.cid) if m["role"] == "user"]
            self.assertEqual("image_missing", users[-1]["metadata"]["image_error_code"])
            self.assertNotIn("image_id", users[-1]["metadata"])

    def test_historical_pair_ignores_unfinished_images_and_respects_recent_window(self):
        old, old_data = self.upload(); latest, latest_data = self.upload("#f47b75")
        queued, _ = self.upload("#36ab54")
        with MockApiServer() as server:
            self.configure(server)
            self.send(server, "第一张", image_id=old)
            self.send(server, "第二张", image_id=latest)
            self.chat.store.append_message(self.cid, "user", "尚未执行的图", "user",
                metadata={"image_id": queued, "request_status": "queued"})
            self.send(server, "compare these two images")
            self.assertEqual([old_data, latest_data], [url.split(",", 1)[1] for _, url in images(server.requests[-1])])
            for i in range(22): self.chat.store.append_message(self.cid, "user", f"无关消息{i}", "user")
            self.send(server, "对比这两张图片")
            self.assertEqual(3, len(server.requests))

    def test_pinned_absence_is_not_rebound_on_retry_after_later_uploads(self):
        old, _ = self.upload(); latest, _ = self.upload("#f47b75")
        with MockApiServer() as server:
            self.configure(server)
            failed, _ = self.send(server, "对比这两张图片")
            self.assertEqual(0, len(server.requests))
            self.send(server, "后来第一张", image_id=old)
            self.send(server, "后来第二张", image_id=latest)
            self.assertTrue(self.chat.retry(self.cid, failed["result"]["message_id"]))
            fixture.fixtures.spin_until(lambda: not self.chat.busy)
            self.assertEqual(2, len(server.requests))

