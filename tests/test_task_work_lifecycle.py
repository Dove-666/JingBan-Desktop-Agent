"""Agent-only work lifecycle with temporary data and in-process doubles."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from PySide6.QtCore import QEvent, QThreadPool
from PySide6.QtTest import QSignalSpy

from desktop_pet.api_client import ApiError
from tests import test_agent_chat_routing as routing
from tests import test_remote_desktop_assist as remote


def transitions(spy):
    return [spy.at(index)[0] for index in range(spy.count())]


class WorkApi(routing.FakeApi):
    def complete_context(self, settings, key, messages, *, on_delta=None, on_context_exceeded=None, **kwargs):
        return super().complete_context(settings, key, messages, **kwargs)


class TaskWorkLifecycleTests(unittest.TestCase):
    setUpClass = classmethod(routing.AgentChatRoutingTests.setUpClass.__func__)
    tearDown = routing.AgentChatRoutingTests.tearDown
    controller = routing.AgentChatRoutingTests.controller
    spin = routing.AgentChatRoutingTests.spin
    finish = routing.AgentChatRoutingTests.finish

    def setUp(self):
        with patch.object(routing, "FakeApi", WorkApi):
            routing.AgentChatRoutingTests.setUp(self)

    def test_ordinary_turn_never_starts_typing_while_generating_or_saving(self):
        self.api.route = None
        self.api.hold = True
        spy = QSignalSpy(self.chat.work_active_changed)
        self.assertFalse(self.chat.work_active)
        self.assertTrue(self.chat.send("你好，鲸鱼娘"))
        self.assertFalse(self.chat.work_active)
        self.assertFalse(self.chat.send("重复发送"))
        self.spin(self.api.started.is_set)
        self.chat._refresh_work_active()
        self.assertEqual(transitions(spy), [])
        self.api.release.set()
        self.finish()
        self.assertFalse(self.chat.work_active)
        self.assertEqual(transitions(spy), [])
        self.assertEqual(self.chat.messages[-1]["role"], "assistant")

    def test_ordinary_model_failure_does_not_start_typing(self):
        spy = QSignalSpy(self.chat.work_active_changed)
        with patch.object(self.api, "complete_context", side_effect=ApiError("network", "fixture failure")):
            self.assertTrue(self.chat.send("你好"))
            self.finish()
        self.assertFalse(self.chat.work_active)
        self.assertEqual(transitions(spy), [])
        self.assertTrue(self.chat.error)

    def test_routed_agent_starts_only_after_routing_and_stays_active_through_tools(self):
        self.api.hold = True
        self.agent.hold = True
        spy = QSignalSpy(self.chat.work_active_changed)
        self.assertTrue(self.chat.send("帮我创建一个测试文件"))
        self.spin(self.api.started.is_set)
        self.assertFalse(self.chat.work_active)
        self.assertEqual(transitions(spy), [])
        self.assertEqual(self.agent.calls, [])
        self.api.release.set()
        self.spin(lambda: self.agent.started.is_set() and self.chat.work_active)
        for phase in ("computer", "verifying", "finishing"):
            self.agent.progress(phase)
        self.app.processEvents()
        self.assertTrue(self.chat.work_active)
        self.assertEqual(transitions(spy), [True])
        self.agent.release.set()
        self.finish()
        self.assertEqual(transitions(spy), [True, False])

    def test_direct_agent_keeps_work_state_when_switching_conversations(self):
        self.agent.hold = True
        spy = QSignalSpy(self.chat.work_active_changed)
        self.assertTrue(self.chat.send("执行合成测试任务", agent_mode=True))
        self.spin(self.agent.started.is_set)
        new_id = self.chat.new_conversation()
        self.assertTrue(self.chat.work_active)
        self.assertEqual(transitions(spy), [True])
        self.assertEqual(self.api.calls, [])
        self.agent.release.set()
        self.finish()
        self.assertEqual(self.chat.current_conversation_id, new_id)
        self.assertEqual(transitions(spy), [True, False])

    def test_stopped_old_agent_callback_cannot_start_typing_for_ordinary_chat(self):
        self.agent.hold = self.agent.ignore_cancellation = True
        spy = QSignalSpy(self.chat.work_active_changed)
        self.assertTrue(self.chat.send("帮我创建一个测试文件"))
        self.spin(lambda: self.agent.started.is_set() and self.chat.work_active)
        old_request = self.chat._active_request
        old_target = (old_request["conversation_id"], old_request["message_id"])
        self.assertTrue(self.chat.stop_request())
        self.assertFalse(self.chat.work_active)
        self.assertIn(old_target, self.chat._requests)
        self.chat.new_conversation()
        self.api.route = None
        self.api.hold = True
        self.api.started.clear()
        self.api.release.clear()
        self.assertTrue(self.chat.send("新的普通聊天"))
        self.spin(self.api.started.is_set)
        self.assertFalse(self.chat.work_active)
        self.assertEqual(transitions(spy), [True, False])
        self.agent.release.set()
        self.spin(lambda: old_target not in self.chat._requests)
        self.assertFalse(self.chat.work_active)
        self.assertEqual(transitions(spy), [True, False])
        self.api.release.set()
        self.finish()
        self.assertEqual(transitions(spy), [True, False])

    def test_stopped_old_agent_callback_cannot_stop_a_new_agent(self):
        self.agent.hold = self.agent.ignore_cancellation = True
        spy = QSignalSpy(self.chat.work_active_changed)
        self.assertTrue(self.chat.send("执行旧合成任务", agent_mode=True))
        self.spin(self.agent.started.is_set)
        old_request = self.chat._active_request
        old_target = (old_request["conversation_id"], old_request["message_id"])
        self.assertTrue(self.chat.stop_request())
        self.assertFalse(self.chat.work_active)
        next_agent = routing.FakeAgent()
        next_agent.hold = True
        self.chat._agent_service = next_agent
        self.chat.new_conversation()
        self.assertTrue(self.chat.send("执行新的合成任务", agent_mode=True))
        self.spin(next_agent.started.is_set)
        self.assertEqual(transitions(spy), [True, False, True])
        self.agent.release.set()
        self.spin(lambda: old_target not in self.chat._requests)
        self.assertTrue(self.chat.work_active)
        self.assertEqual(transitions(spy), [True, False, True])
        next_agent.release.set()
        self.finish()
        self.assertEqual(transitions(spy), [True, False, True, False])

    def test_model_declining_agent_routing_does_not_start_typing(self):
        self.api.route = None
        spy = QSignalSpy(self.chat.work_active_changed)
        self.assertTrue(self.chat.send("帮我创建一个测试文件"))
        self.finish()
        self.assertTrue(self.api.calls[-1]["allow_agent_routing"])
        self.assertEqual(self.agent.calls, [])
        self.assertEqual(transitions(spy), [])

    def test_connection_checks_and_model_lists_do_not_start_work(self):
        spy = QSignalSpy(self.chat.work_active_changed)
        with patch.object(self.api, "test_connection", return_value="fixture connection ready"):
            self.assertTrue(self.chat.test_connection(self.settings.load_api_settings(), routing.KEY))
            self.assertTrue(self.chat.busy)
            self.assertFalse(self.chat.work_active)
            self.finish()
        fetched = []
        with patch.object(self.api, "list_models", return_value=["fixture-model"]):
            self.assertTrue(self.chat.fetch_models(self.settings.load_api_settings(), routing.KEY,
                lambda models, error: fetched.append((models, error))))
            self.spin(lambda: bool(fetched))
        self.assertEqual(transitions(spy), [])
        self.assertFalse(self.chat.work_active)

    def test_saved_screen_comments_do_not_start_work(self):
        spy = QSignalSpy(self.chat.work_active_changed)
        result = self.chat.record_screen_comment(self.chat.current_conversation_id, "合成屏幕评论",
            event_id=str(uuid4()), app_name="fixture", context_summary="合成屏幕摘要", event_type="fixture")
        self.assertIsNotNone(result)
        self.spin(lambda: not self.chat._runner._tasks)
        self.assertEqual(transitions(spy), [])
        self.assertFalse(self.chat.work_active)

    def test_shutdown_stops_work_before_discarded_callbacks(self):
        self.agent.hold = True
        spy = QSignalSpy(self.chat.work_active_changed)
        self.assertTrue(self.chat.send("执行合成测试任务", agent_mode=True))
        self.spin(self.agent.started.is_set)
        self.chat.shutdown()
        self.assertFalse(self.chat.work_active)
        self.assertEqual(transitions(spy), [True, False])
        self.spin(lambda: not QThreadPool.globalInstance().activeThreadCount())
        self.app.processEvents()
        self.assertEqual(transitions(spy), [True, False])


class RemoteTaskWorkLifecycleTests(unittest.TestCase):
    setUpClass = classmethod(remote.RemoteDesktopAssistTests.setUpClass.__func__)
    setUp = remote.RemoteDesktopAssistTests.setUp
    tearDown = remote.RemoteDesktopAssistTests.tearDown
    rpc = remote.RemoteDesktopAssistTests.rpc
    configure = remote.RemoteDesktopAssistTests.configure
    configure_agent = remote.RemoteDesktopAssistTests.configure_agent

    def test_authenticated_remote_agent_uses_the_same_work_lifecycle(self):
        from tests.test_remote_controller import spin_until

        self.configure(SimpleNamespace(base_url="http://127.0.0.1:1/v1"))
        self.chat._metadata_factory = lambda: None
        agent = self.configure_agent()
        agent.hold = True
        spy = QSignalSpy(self.chat.work_active_changed)
        desktop_id = self.chat.new_conversation()
        result = self.rpc("agent.send", {"conversation_id": self.cid, "text": "执行合成测试任务"})
        self.assertTrue(result["ok"], result)
        spin_until(agent.started.is_set)
        self.assertTrue(self.chat.work_active)
        self.assertEqual(self.chat.current_conversation_id, desktop_id)
        self.assertEqual(transitions(spy), [True])
        agent.release.set()
        spin_until(lambda: not self.chat.busy and not self.chat._runner._tasks)
        self.assertFalse(self.chat.work_active)
        self.assertEqual(transitions(spy), [True, False])


class ApplicationWorkWiringTests(unittest.TestCase):
    setUpClass = classmethod(routing.AgentChatRoutingTests.setUpClass.__func__)

    def test_application_forwards_dedicated_work_signal_separately_from_busy(self):
        from pathlib import Path
        import tempfile

        from desktop_pet.application_controller import ApplicationController
        from desktop_pet.config import PetConfig
        from desktop_pet.pet_controller import PetController
        from tests.test_application import MemoryCredentialBackend, isolated_settings, spin_until

        with tempfile.TemporaryDirectory(prefix="aura-work-wiring-") as folder:
            settings = isolated_settings(Path(folder) / "preferences.ini", MemoryCredentialBackend())
            calls = []
            with patch.object(PetController, "set_task_working",
                              lambda controller, active: calls.append((controller, active))):
                session = ApplicationController(PetConfig(), settings=settings, api_client=WorkApi())
                try:
                    session.chat.busy_changed.emit(True)
                    self.assertEqual(calls, [])
                    session.chat.work_active_changed.emit(True)
                    session.chat.work_active_changed.emit(False)
                    self.assertEqual(calls, [(session.pet_controller, True), (session.pet_controller, False)])
                finally:
                    session.shutdown()
                    spin_until(lambda: not QThreadPool.globalInstance().activeThreadCount())
                    session.pet.deleteLater()
                    session.deleteLater()
                    self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                    self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
