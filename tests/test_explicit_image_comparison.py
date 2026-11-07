"""Actual local HTTP image payloads over owned synthetic conversations only."""
import base64
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from tests import test_chat_images as fixture
from tests.mock_api import MockApiServer
from desktop_pet import chat_images as policy


def images(request):
    values=[]
    for message in request["body"]["messages"]:
        if isinstance(message.get("content"),list):
            values.extend((message,part["image_url"]["url"]) for part in message["content"] if part.get("type")=="image_url")
    return values


class ExplicitImageComparisonTests(unittest.TestCase):
    setUpClass=classmethod(fixture.ChatImageTests.setUpClass.__func__)
    setUp=fixture.ChatImageTests.setUp
    tearDown=fixture.ChatImageTests.tearDown
    rpc=fixture.ChatImageTests.rpc
    configure=fixture.ChatImageTests.configure
    upload=fixture.ChatImageTests.upload
    send=fixture.ChatImageTests.send

    def test_reference_and_comparison_policy_accepts_only_explicit_context(self):
        for text in ("图片压缩算法怎么做", "如何选择照片格式", "how does image compression work", "photography basics", "?", "你好"):
            self.assertFalse(policy.references_previous_image(text),text)
            self.assertFalse(policy.compares_previous_image(text),text)
        for text in ("这张和上一张照片哪里不一样？", "跟上面那张图比较一下", "对比这两张图片",
                     "Compare this photo with the previous image", "what changed from the last picture?"):
            self.assertTrue(policy.compares_previous_image(text),text)
        for text in ("这张图片是什么？", "对比这张图片", "JPEG和PNG有什么区别", "compare image formats", "上一张照片",
                     "比较这两张表的数据", "对比这两张车票的价格", "比较这两张图表的数据", "比较这两张"):
            self.assertFalse(policy.compares_previous_image(text),text)
        self.assertTrue(policy.references_previous_image("上一张是什么"))

    def test_new_explicit_comparison_keeps_old_and_current_pixels_on_their_own_turns(self):
        old,old_data=self.upload();current,current_data=self.upload("#f47b75")
        with MockApiServer() as server:
            self.configure(server);first,_=self.send(server,"这是第一张图片",image_id=old)
            text="这张和上一张照片哪里不一样？"
            second,_=self.send(server,text,image_id=current)
            actual=images(server.requests[-1])
            self.assertEqual(2,len(actual))
            self.assertEqual([old_data,current_data],[url.split(",",1)[1] for _,url in actual])
            self.assertEqual("这是第一张图片",actual[0][0]["content"][0]["text"])
            self.assertEqual(text,actual[1][0]["content"][0]["text"])
            users=[row for row in self.chat.store.load_messages(self.cid) if row["role"]=="user"]
            self.assertEqual([first["result"]["message_id"],second["result"]["message_id"]],[row["id"] for row in users])
            self.assertEqual([old,current],[row["metadata"]["image_id"] for row in users])
            self.assertEqual(old,users[-1]["metadata"]["context_image_id"])
            self.send(server,"明天有什么安排")
            self.assertEqual([],images(server.requests[-1]))

    def test_general_image_topic_and_ordinary_new_image_do_not_send_old_private_pixels(self):
        old,old_data=self.upload();current,current_data=self.upload("#f47b75")
        with MockApiServer() as server:
            self.configure(server);self.send(server,"第一张图片",image_id=old)
            for text in ("图片压缩算法怎么做", "如何选择照片格式", "how does image compression work", "photography basics"):
                self.send(server,text)
                self.assertEqual([],images(server.requests[-1]),text)
            for text in ("这张图是什么", "对比这张图片", "JPEG和PNG有什么区别", "比较这两张表的数据",
                         "对比这两张车票的价格", "比较这两张图表的数据", "比较这两张"):
                self.send(server,text,image_id=current)
                self.assertEqual([current_data],[url.split(",",1)[1] for _,url in images(server.requests[-1])])
                self.assertNotIn(old_data,str(server.requests[-1]["body"]))

    def test_retry_of_comparison_reuses_the_original_pair_after_a_later_new_image(self):
        old,old_data=self.upload();current,current_data=self.upload("#f47b75");later,later_data=self.upload("#36ab54")
        with MockApiServer() as server:
            self.configure(server);self.send(server,"第一张",image_id=old)
            failed,_=self.send(server,"比较这张和上一张照片",image_id=current,status=500)
            self.send(server,"新话题的新图",image_id=later)
            server.enqueue('{"reply":"比较已完成","emotion":"neutral"}')
            self.assertTrue(self.chat.retry(self.cid,failed["result"]["message_id"]))
            fixture.fixtures.spin_until(lambda:not self.chat.busy)
            self.assertEqual([old_data,current_data],[url.split(",",1)[1] for _,url in images(server.requests[-1])])
            self.assertNotIn(later_data,str(server.requests[-1]["body"]))

    def test_absent_current_conversation_image_and_expired_context_reject_comparison_before_http(self):
        old,old_data=self.upload();current,current_data=self.upload("#f47b75")
        with MockApiServer() as server:
            self.configure(server);self.send(server,"第一张",image_id=old)
            other=str(uuid4());text="跟上一张照片比较"
            failed,_=self.send(server,text,image_id=current,cid=other)
            self.assertEqual(1,len(server.requests))
            archived=next(row for row in self.chat.store.load_messages(other) if row["id"]==failed["result"]["message_id"])
            self.assertEqual(text,archived["content"])
            self.assertEqual(current,archived["metadata"]["image_id"])
            self.assertIsNone(archived["metadata"]["context_image_id"])
            self.assertEqual("image_unavailable",archived["metadata"]["image_error_code"])
            for index in range(22):self.chat.store.append_message(self.cid,"user",f"无关文字{index}","user")
            text="这张和上一张图片有什么变化"
            failed,_=self.send(server,text,image_id=current)
            self.assertEqual(1,len(server.requests))
            archived=next(row for row in self.chat.store.load_messages(self.cid) if row["id"]==failed["result"]["message_id"])
            self.assertEqual(text,archived["content"])
            self.assertEqual(current,archived["metadata"]["image_id"])
            self.assertIsNone(archived["metadata"]["context_image_id"])
            self.assertEqual("image_unavailable",archived["metadata"]["image_error_code"])

    def test_missing_pinned_old_cache_does_not_silently_send_only_the_new_image(self):
        old,_=self.upload();current,current_data=self.upload("#f47b75")
        with MockApiServer() as server:
            self.configure(server);self.send(server,"第一张",image_id=old)
            self.remote.images.delete(old)
            failed,_=self.send(server,"比较这张和上一张照片",image_id=current)
            self.assertEqual(1,len(server.requests))
            archived=next(row for row in self.chat.store.load_messages(self.cid) if row["id"]==failed["result"]["message_id"])
            self.assertEqual(current,archived["metadata"]["image_id"])
            self.assertEqual(old,archived["metadata"]["context_image_id"])
            self.assertEqual("image_missing",archived["metadata"]["image_error_code"])
            self.send(server,"只看本次新图",image_id=current)
            self.assertEqual([current_data],[url.split(",",1)[1] for _,url in images(server.requests[-1])])

    def test_queued_image_is_not_the_previous_completed_user_image(self):
        old,old_data=self.upload();current,current_data=self.upload("#f47b75");queued,queued_data=self.upload("#36ab54")
        with MockApiServer() as server:
            self.configure(server);self.send(server,"已经完成的一张图片",image_id=old)
            self.chat.store.append_message(self.cid,"user","排队但未执行的图片","user",
                metadata={"image_id":queued,"request_status":"queued"})
            self.send(server,"这张和上一张照片有什么变化",image_id=current)
            self.assertEqual([old_data,current_data],[url.split(",",1)[1] for _,url in images(server.requests[-1])])
            self.assertNotIn(queued_data,str(server.requests[-1]["body"]))


if __name__=="__main__":unittest.main()
