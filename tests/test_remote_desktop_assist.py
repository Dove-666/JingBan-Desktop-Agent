"""Authenticated explicit desktop reads/tasks, using synthetic pixels only."""

from dataclasses import replace
import json
from types import SimpleNamespace
from uuid import uuid4
import unittest
from unittest.mock import patch

from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from desktop_pet.perception.screen_observer import ForegroundInfo, ScreenObserver
from desktop_pet.perception.settings import PerceptionSettings
from desktop_pet.remote.desktop_capture import DesktopCaptureUnavailable, capture_desktop_image
from tests import test_chat_images as image_fixtures
from tests import test_remote_controller as fixtures
from tests.mock_api import MockApiServer
from tests.test_agent_chat_routing import FakeAgent


class RemoteDesktopAssistTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    setUp = image_fixtures.ChatImageTests.setUp
    tearDown = image_fixtures.ChatImageTests.tearDown
    rpc = image_fixtures.ChatImageTests.rpc
    configure = image_fixtures.ChatImageTests.configure
    upload = image_fixtures.ChatImageTests.upload

    def allow_synthetic_screen(self, *, title="Public synthetic QA", process_name="fixture.exe", idle_seconds=0):
        self.captures = 0
        info = ForegroundInfo(123, 123456, process_name, title, idle_seconds, True)

        def capture(_info):
            self.captures += 1
            image = QImage(80, 60, QImage.Format.Format_RGB32)
            image.fill(QColor("#759df4"))
            return image

        observer = ScreenObserver(metadata_provider=lambda: info, capture=capture, own_pid=-1)
        self.session.perception = SimpleNamespace(observer=observer, paused=False, source_kind="screen",
            settings=replace(PerceptionSettings(), enabled=True, allow_remote=True))

    def configure_agent(self):
        agent = FakeAgent()
        self.chat._agent_service = agent
        self.settings.save_agent_settings(replace(self.settings.load_agent_settings(),
            enabled=True, computer_enabled=True, gui_model="synthetic-gui-model"))
        self.settings.save_agent_gui_api_key("synthetic-gui-key")
        self.chat.refresh_agent_settings()
        return agent

    def allow_own_window(self):
        self.allow_synthetic_screen(title="和鲸鱼娘聊天", process_name="python.exe")

    def test_snapshot_permissions_authorization_and_durable_receipt_without_model(self):
        status = self.rpc("status")["result"]
        self.assertIn("desktop.snapshot", status["capabilities"])
        self.assertFalse(status["desktop_context"]["available"])
        self.assertEqual(self.rpc("desktop.snapshot")["error"]["code"], "desktop_permission_denied")
        self.allow_synthetic_screen()
        self.assertTrue(self.rpc("status")["result"]["desktop_context"]["available"])
        unauthorized = self.remote.handle_request("f" * 64,
            {"id": str(uuid4()), "method": "desktop.snapshot", "params": {}})
        self.assertEqual(unauthorized["error"]["code"], "unauthorized")
        self.assertEqual(self.captures, 0)
        request_id = str(uuid4())
        first = self.rpc("desktop.snapshot", request_id=request_id)
        self.assertTrue(first["ok"], first)
        metadata = first["result"]
        self.assertEqual((metadata["width"], metadata["height"]), (80, 60))
        self.assertTrue(self.rpc("image.get", {"id": metadata["id"]})["ok"])
        self.assertTrue(self.rpc("desktop.snapshot", request_id=request_id)["result"]["duplicate"])
        self.assertEqual(self.captures, 1)
        receipt = self.rpc("operation.get", {"request_id": request_id})["result"]
        self.assertEqual(receipt["state"], "complete")
        self.assertNotIn("data_base64", json.dumps(receipt))
        self.assertEqual(self.chat.store.load_messages(self.cid), [])

    def test_privacy_and_revoked_consent_discard_without_storing_or_model(self):
        self.allow_synthetic_screen(title="密码管理器")
        self.assertEqual(self.rpc("desktop.snapshot")["error"]["code"], "desktop_capture_unavailable")
        self.assertEqual(self.captures, 0)
        self.allow_synthetic_screen()
        observer = self.session.perception.observer
        original = observer._capture

        def revoke(info):
            image = original(info)
            self.session.perception.paused = True
            return image

        observer._capture = revoke
        self.assertEqual(self.rpc("desktop.snapshot")["error"]["code"], "desktop_permission_denied")
        self.assertEqual(self.remote.images.list()["items"], [])

    def test_explicit_task_uses_fresh_screen_without_fake_attachment_or_followup_leak(self):
        self.allow_synthetic_screen()
        with MockApiServer() as server:
            self.configure(server)
            request_id = str(uuid4())
            params = {"text": "请解释当前电脑画面", "conversation_id": self.cid, "desktop_context": True}
            server.enqueue("这是蓝色的测试窗口。")
            result = self.rpc("chat.send", params, request_id)
            self.assertTrue(result["ok"], result)
            fixtures.spin_until(lambda: not self.chat.busy)
            self.assertEqual(self.captures, 1)
            messages = self.chat.store.load_messages(self.cid)
            self.assertTrue(messages[0]["metadata"]["desktop_context"])
            self.assertNotIn("image_id", messages[0]["metadata"])
            self.assertIn("desktop_image_id", messages[0]["metadata"])
            content = server.requests[0]["body"]["messages"][-1]["content"]
            self.assertEqual(content[0]["text"], params["text"])
            self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/png;base64,"))
            self.assertTrue(self.rpc("chat.send", params, request_id)["result"]["duplicate"])
            self.assertEqual((self.captures, len(server.requests)), (1, 1))
            server.enqueue("普通聊天回复")
            self.assertTrue(self.rpc("chat.send", {"text": "今天心情怎么样", "conversation_id": self.cid})["ok"])
            fixtures.spin_until(lambda: not self.chat.busy)
            self.assertTrue(all(isinstance(message["content"], str) for message in server.requests[1]["body"]["messages"]))
            self.assertNotIn("desktop_image_id", json.dumps(self.remote.sync.snapshot()))

    def test_explicit_control_routes_existing_agent_and_is_not_replayed(self):
        self.allow_synthetic_screen()
        agent = self.configure_agent()
        with MockApiServer() as server:
            self.configure(server)
            server.enqueue(json.dumps({"reply": "准备执行明确的电脑任务。", "emotion": "neutral",
                "agent_request": {"kind": "computer", "task": "do not replace actual user instruction"}}))
            request_id = str(uuid4())
            params = {"text": "帮我打开测试窗口", "conversation_id": self.cid, "desktop_context": True}
            self.assertTrue(self.rpc("chat.send", params, request_id)["ok"])
            fixtures.spin_until(lambda: not self.chat.busy)
            self.assertEqual(len(agent.calls), 1)
            self.assertEqual(agent.calls[0]["request"].kind, "computer")
            self.assertEqual(agent.calls[0]["request"].task, params["text"])
            self.assertTrue(self.rpc("chat.send", params, request_id)["result"]["duplicate"])
            self.assertEqual((self.captures, len(agent.calls), len(server.requests)), (1, 1, 1))
            user = self.chat.store.load_messages(self.cid)[0]
            self.assertFalse(self.chat.retry(self.cid, user["id"]))

    def test_own_window_task_reaches_existing_agent_without_image_or_replay(self):
        self.allow_own_window()
        agent = self.configure_agent()
        image_id, _ = self.upload()
        # A previously sent phone image must never become this turn's context.
        self.chat.store.append_message(self.cid, "user", "上一张测试图", "user",
            metadata={"image_id": image_id, "request_status": "complete"})
        self.chat.store.append_message(self.cid, "assistant", "先前图片的文字回复", "assistant")
        with MockApiServer() as server:
            self.configure(server)
            server.enqueue(json.dumps({"reply": "先切换到桌面并验证画面。", "emotion": "neutral",
                "agent_request": {"kind": "computer", "task": "untrusted rewritten instruction"}}))
            request_id = str(uuid4())
            params = {"text": "切换到电脑桌面，查看桌面有哪些软件图标", "conversation_id": self.cid,
                      "desktop_context": True}
            result = self.rpc("chat.send", params, request_id)
            self.assertTrue(result["ok"], result)
            fixtures.spin_until(lambda: not self.chat.busy)
            self.assertEqual(self.captures, 0)
            self.assertEqual(len(agent.calls), 1)
            self.assertEqual(agent.calls[0]["request"].task, params["text"])
            self.assertTrue(all(isinstance(item["content"], str) for item in server.requests[0]["body"]["messages"]))
            content = server.requests[0]["body"]["messages"][-1]["content"]
            self.assertTrue(content.startswith(params["text"]))
            self.assertIn("本轮未提供截图", content)
            self.assertIn("不能声称看到了", content)
            self.assertNotIn("data:image", json.dumps(agent.calls[0]["context"]["messages"]))
            user = next(item for item in self.chat.store.load_messages(self.cid)
                        if item["content"] == params["text"])
            self.assertEqual(user["content"], params["text"])
            self.assertTrue(user["metadata"]["desktop_context"])
            self.assertIsNone(user["metadata"]["desktop_image_id"])
            self.assertEqual(user["metadata"]["desktop_capture_unavailable_reason"], "own_window")
            self.assertNotIn("context_image_id", user["metadata"])
            self.assertNotIn("desktop_capture_unavailable_reason", json.dumps(self.remote.sync.snapshot()))
            self.assertTrue(self.rpc("chat.send", params, request_id)["result"]["duplicate"])
            self.assertEqual((len(agent.calls), len(server.requests)), (1, 1))
            self.assertFalse(self.chat.retry(self.cid, user["id"]))

    def test_own_window_question_stays_text_and_snapshot_remains_strict(self):
        self.allow_own_window()
        agent = self.configure_agent()
        snapshot = self.rpc("desktop.snapshot")
        self.assertEqual(snapshot["error"]["code"], "desktop_capture_unavailable")
        self.assertIn("AURA", snapshot["error"]["message"])
        with MockApiServer() as server:
            self.configure(server)
            server.enqueue(json.dumps({"reply": "本轮没有截图，请明确说明需要我做什么。", "emotion": "neutral",
                "agent_request": {"kind": "computer", "task": "hostile unsolicited route"}}))
            self.assertTrue(self.rpc("chat.send", {"text": "?", "conversation_id": self.cid,
                                                  "desktop_context": True})["ok"])
            fixtures.spin_until(lambda: not self.chat.busy)
            self.assertEqual(agent.calls, [])
            self.assertIn("本轮未提供截图", server.requests[0]["body"]["messages"][-1]["content"])
            server.enqueue("这是普通聊天。")
            self.assertTrue(self.rpc("chat.send", {"text": "你好", "conversation_id": self.cid})["ok"])
            fixtures.spin_until(lambda: not self.chat.busy)
            self.assertEqual(server.requests[1]["body"]["messages"][-1]["content"], "你好")
        self.assertEqual(self.captures, 0)
        self.assertEqual(self.remote.images.list()["items"], [])

    def test_privacy_and_capture_failures_never_admit_no_image_chat(self):
        for title, process in (("密码管理器", "fixture.exe"), ("Public synthetic QA", "1password.exe"),
                               ("", "fixture.exe")):
            with self.subTest(title=title, process=process):
                self.allow_synthetic_screen(title=title, process_name=process)
                result = self.rpc("chat.send", {"text": "切换到电脑桌面", "conversation_id": self.cid,
                                                "desktop_context": True})
                self.assertEqual(result["error"]["code"], "desktop_capture_unavailable")
        self.allow_synthetic_screen()
        self.session.perception.observer._capture = lambda _: QImage()
        result = self.rpc("chat.send", {"text": "切换到电脑桌面", "conversation_id": self.cid,
                                       "desktop_context": True})
        self.assertEqual(result["error"]["code"], "desktop_capture_unavailable")
        self.assertEqual(self.chat.store.load_messages(self.cid), [])
        self.assertEqual(self.remote.images.list()["items"], [])

    def test_own_window_fallback_rechecks_consent_and_foreground_before_admission(self):
        for change in ("pause", "exclude", "revoke"):
            with self.subTest(change=change):
                self.allow_own_window()
                def blocked(_observer, _settings):
                    if change == "pause":
                        self.session.perception.paused = True
                    elif change == "exclude":
                        self.session.perception.observer._metadata = lambda: ForegroundInfo(
                            456, 123456, "1password.exe", "Private window", 0, True)
                    else:
                        self.remote.preferences.revoke(fixtures.PEER)
                    raise DesktopCaptureUnavailable("own_window")
                with patch("desktop_pet.remote.controller.capture_desktop_image", blocked):
                    result = self.rpc("chat.send", {"text": "切换到电脑桌面", "conversation_id": self.cid,
                                                   "desktop_context": True})
                self.assertEqual(result["error"]["code"], {
                    "pause": "desktop_permission_denied", "exclude": "desktop_capture_unavailable",
                    "revoke": "unauthorized"}[change])
        self.assertEqual(self.chat.store.load_messages(self.cid), [])

    def test_client_cannot_forge_private_desktop_fallback_or_bypass_consent(self):
        for params in ({"desktop_capture_unavailable_reason": "own_window"},
                       {"desktop_image_id": "a" * 64},
                       {"desktop_capture_unavailable_reason": "privacy"}):
            result = self.rpc("chat.send", {"text": "切换到电脑桌面", "conversation_id": self.cid,
                                           "desktop_context": True, **params})
            self.assertEqual(result["error"]["code"], "invalid_request")
        self.assertEqual(self.rpc("chat.send", {"text": "切换到电脑桌面", "conversation_id": self.cid,
            "desktop_context": True})["error"]["code"], "desktop_permission_denied")
        self.assertEqual(self.chat.store.load_messages(self.cid), [])

    def test_chat_rejects_unknown_or_inconsistent_private_fallback(self):
        with MockApiServer() as server:
            self.configure(server)
            for options in ({"desktop_context": True},
                            {"desktop_context": True, "desktop_capture_unavailable_reason": "privacy"},
                            {"desktop_capture_unavailable_reason": "own_window"},
                            {"desktop_context": True, "desktop_image_id": "a" * 64,
                             "desktop_capture_unavailable_reason": "own_window"},
                            {"desktop_context": True, "desktop_capture_unavailable_reason": []}):
                with self.subTest(options=options):
                    self.assertFalse(self.chat.send("测试", **options))
            self.assertEqual(server.requests, [])
        self.assertEqual(self.chat.store.load_messages(self.cid), [])

    def test_explicit_capture_ignores_local_idle_but_retains_legacy_observer_contract(self):
        self.allow_synthetic_screen(idle_seconds=3600)
        self.assertTrue(self.rpc("desktop.snapshot")["ok"])
        observer = self.session.perception.observer
        settings = self.session.perception.settings
        legacy = SimpleNamespace(observe=lambda value: observer.observe_explicit(value),
                                 is_current=lambda observation, value: observer.is_current_explicit(observation, value))
        payload = capture_desktop_image(legacy, settings)
        self.assertTrue(payload["data_base64"])

    def test_mixed_attachment_and_invalid_desktop_flag_never_capture(self):
        self.allow_synthetic_screen()
        for params in ({"desktop_context": "yes"}, {"desktop_context": True, "image_id": "a" * 64}):
            result = self.rpc("chat.send", {"text": "测试", "conversation_id": self.cid, **params})
            self.assertEqual(result["error"]["code"], "invalid_request")
        self.assertEqual(self.captures, 0)


if __name__ == "__main__":
    unittest.main()
