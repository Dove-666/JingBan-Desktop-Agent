"""Independent camera consent and end-to-end analysis with synthetic pixels.

No physical camera is opened.  The single HTTP integration test targets only
the existing loopback fixture, with a temporary archive and memory credentials.
"""

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest

from PySide6.QtCore import QEvent, QObject, QThreadPool, Signal
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from desktop_pet.animation_controller import AnimationState
from desktop_pet.chat_controller import ChatController
from desktop_pet.conversations import ConversationStore
from desktop_pet.perception.context_analyzer import ContextEvent
from desktop_pet.perception.controller import PerceptionController
from desktop_pet.perception.screen_observer import ForegroundInfo, Observation
from desktop_pet.perception.settings import CameraSettings, PerceptionSettings
from desktop_pet.settings_manager import ApiSettings
from tests.mock_api import MockApiServer
from tests.test_application import MemoryCredentialBackend, isolated_settings, spin_until
from tests.test_perception_permissions import QuietChat


def synthetic_frame():
    result = QImage(320, 180, QImage.Format.Format_RGB32)
    result.fill(QColor("#627f96"))
    return result


class CameraStore:
    def __init__(self):
        self.camera = CameraSettings()
        # Start with deliberately denied screen consent to test independence;
        # this fixture must not depend on the application's release defaults.
        self.screen = PerceptionSettings(enabled=False, allow_remote=False)
        self.saved_camera = []
        self.saved_screen = []
        self.fail = False

    def load_camera_settings(self):
        return self.camera

    def save_camera_settings(self, settings):
        self.saved_camera.append(settings)
        if self.fail:
            raise OSError("SYNTHETIC_PRIVATE_PERSISTENCE_ERROR")
        self.camera = settings

    def load_perception_settings(self):
        return self.screen

    def save_perception_settings(self, settings):
        self.saved_screen.append(settings)
        self.screen = settings

    def load_api_settings(self):
        return ApiSettings("http://127.0.0.1:9/v1", "synthetic-camera-model", 2)

    def get_api_key(self):
        return "synthetic-camera-test-token"


class SyntheticCameraObserver(QObject):
    error_changed = Signal(str)
    status_changed = Signal(str)
    active_changed = Signal(bool)
    frame_ready = Signal(QImage)

    def __init__(self):
        super().__init__()
        self.starts, self.stops = [], 0
        self.running = self.closed = self.fail_start = False
        self.device_id = "camera-a"
        self.generation = 0
        self.image = None
        self.status = "摄像头识别已关闭。"
        self.error = ""

    @property
    def active(self):
        return self.running

    def available_devices(self):
        return [{"id": "camera-a", "description": "合成测试镜头 A", "is_default": True},
                {"id": "camera-b", "description": "合成测试镜头 B", "is_default": False}]

    def start(self, device_id=""):
        selected = device_id or "camera-a"
        if self.running and selected == self.device_id:
            return True
        if self.running:
            self.stop()
        self.starts.append(device_id)
        if self.closed or self.fail_start:
            self.running = False
            self.error = "无法打开摄像头，请检查系统相机权限。"
            self.error_changed.emit(self.error)
            return False
        self.device_id = selected
        self.generation += 1
        self.running = True
        self.active_changed.emit(True)
        return True

    def stop(self):
        self.stops += 1
        self.running = False
        self.image = None
        self.active_changed.emit(False)

    def shutdown(self):
        self.stop()
        self.closed = True

    def produce(self):
        self.image = synthetic_frame()
        self.frame_ready.emit(QImage(self.image))

    def info(self):
        return ForegroundInfo(hwnd=98765, pid=self.generation, process_name="camera:" + self.device_id,
                              title="摄像头 · 合成测试镜头", idle_seconds=0, available=self.running)

    def observe(self, settings):
        if not settings.enabled or not self.running or self.image is None:
            return Observation(self.info(), blocked_code="camera_no_frame", blocked_reason="尚无摄像头预览。", source="camera")
        return Observation(self.info(), QImage(self.image), capture_method="合成摄像头画面", source="camera")

    def current_blocked_code(self, settings):
        return "" if settings.enabled and self.running and self.image is not None else "camera_no_frame"

    def current_safety(self, settings):
        return "" if not self.current_blocked_code(settings) else "尚无摄像头预览。"

    def is_current(self, observation, settings):
        return (not self.current_blocked_code(settings)
                and observation.info.process_name == self.info().process_name
                and observation.info.pid == self.generation)


class HeldCameraAnalyzer:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.closed = False
        self.observations = []

    def analyze(self, observation, *_args, **kwargs):
        self.observations.append((observation, kwargs))
        self.started.set()
        self.release.wait(5)
        return ContextEvent("interesting", "合成镜头中有一个蓝色水杯。", "这个蓝色水杯看起来很清爽。", "happy", .9, .95, "camera-cup")

    def shutdown(self):
        self.closed = True


def camera_controller(*args, **kwargs):
    from desktop_pet.perception.camera_controller import CameraController
    return CameraController(*args, **kwargs)


class CameraControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.store, self.chat = CameraStore(), QuietChat()
        self.observer, self.analyzer = SyntheticCameraObserver(), HeldCameraAnalyzer()
        self.responses = []
        self.pet = SimpleNamespace(
            pet=SimpleNamespace(animation=SimpleNamespace(state=AnimationState.IDLE)),
            respond=lambda *args, **kwargs: self.responses.append((args, kwargs)),
            play_motion=lambda *args: None,
        )
        self.controller = camera_controller(self.store, self.pet, self.chat, observer=self.observer,
                                            analyzer_factory=lambda: self.analyzer, clock=lambda: 1000.)
        self.controllers = [self.controller]
        self.analyzers = [self.analyzer]

    def tearDown(self):
        for analyzer in self.analyzers:
            analyzer.release.set()
        for controller in self.controllers:
            controller.shutdown()
        spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
        for controller in self.controllers:
            controller.deleteLater()
        self.observer.deleteLater()
        self.chat.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def enable_remote_analysis(self):
        self.assertTrue(self.controller.set_enabled(True, allow_remote=True))
        self.observer.produce()
        self.assertTrue(self.controller.poll())
        spin_until(self.analyzer.started.is_set)
        self.assertTrue(self.controller.busy)

    def test_default_off_does_not_start_camera_or_enable_screen(self):
        self.assertFalse(self.controller.settings.enabled)
        self.assertFalse(self.controller.settings.allow_remote)
        self.assertFalse(self.controller._timer.isActive())
        self.assertEqual(self.observer.starts, [])
        self.assertFalse(self.controller.poll())
        self.assertFalse(self.store.screen.enabled)
        self.assertEqual(self.store.saved_screen, [])

    def test_local_enable_previews_frames_without_remote_permission_or_model(self):
        self.assertTrue(self.controller.set_enabled(True))
        self.assertTrue(self.observer.running)
        self.assertFalse(self.controller.settings.allow_remote)
        self.observer.produce()
        self.assertFalse(self.controller.preview.isNull())
        self.assertTrue(self.controller.observe_now())
        self.assertFalse(self.analyzer.started.is_set())
        self.assertEqual(self.chat.comments, [])
        self.assertIn("没有调用 API", self.controller.status)
        self.assertEqual(self.store.saved_screen, [])

    def test_remote_switch_is_separate_from_local_camera_switch(self):
        self.assertTrue(self.controller.set_enabled(True))
        self.observer.produce()
        self.assertTrue(self.controller.poll())
        self.assertFalse(self.analyzer.started.is_set())
        self.assertTrue(self.controller.set_remote_enabled(True))
        self.observer.produce()
        self.assertTrue(self.controller.poll())
        spin_until(self.analyzer.started.is_set)
        self.assertEqual(self.analyzer.observations[0][0].source, "camera")
        self.assertFalse(self.store.screen.allow_remote)

    def test_turning_off_on_save_failure_still_stops_hardware_and_clears_preview(self):
        self.assertTrue(self.controller.set_enabled(True))
        self.observer.produce()
        self.store.fail = True
        self.assertFalse(self.controller.set_enabled(False))
        self.assertFalse(self.controller.settings.enabled)
        self.assertFalse(self.observer.running)
        self.assertTrue(self.controller.preview.isNull())
        self.observer.produce()  # A callback already queued before revocation.
        self.assertTrue(self.controller.preview.isNull())
        self.assertNotIn("SYNTHETIC_PRIVATE", self.controller.status)
        starts = len(self.observer.starts)
        self.controller.set_paused(True)
        self.controller.set_paused(False)
        self.assertEqual(len(self.observer.starts), starts)

    def test_device_change_closes_old_camera_and_invalidates_old_observation(self):
        self.assertTrue(self.controller.set_enabled(True))
        self.observer.produce()
        self.controller.observe_now()
        previous = self.controller._last_observation
        stopped = self.observer.stops
        self.assertTrue(self.controller.select_device("camera-b"))
        self.assertGreater(self.observer.stops, stopped)
        self.assertEqual(self.observer.device_id, "camera-b")
        self.assertTrue(self.controller.preview.isNull())
        self.observer.produce()
        self.assertFalse(self.observer.is_current(previous, self.controller.settings))

    def test_start_error_keeps_hardware_closed_and_reports_without_opening_screen(self):
        self.observer.fail_start = True
        self.assertFalse(self.controller.set_enabled(True))
        self.assertFalse(self.observer.running)
        self.assertTrue(self.controller.preview.isNull())
        self.assertIn("摄像头", self.controller.status)
        self.assertEqual(self.store.saved_screen, [])

    def cancellation_case(self, action):
        self.enable_remote_analysis()
        if action == "disable":
            self.controller.set_enabled(False)
        elif action == "pause":
            self.controller.set_paused(True)
        else:
            self.controller.set_remote_enabled(False)
        self.assertFalse(self.controller.busy)
        self.assertTrue(self.analyzer.closed)
        if action == "remote":
            self.assertTrue(self.observer.running)
            self.assertTrue(self.controller.settings.enabled)
            self.assertFalse(self.controller.settings.allow_remote)
        else:
            self.assertFalse(self.observer.running)
            self.assertTrue(self.controller.preview.isNull())
        self.analyzer.release.set()
        spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
        self.assertEqual(self.chat.comments, [])
        self.assertEqual(self.responses, [])

    def test_disable_cancels_worker_and_discards_late_result(self):
        self.cancellation_case("disable")

    def test_pause_cancels_worker_releases_camera_and_discards_late_result(self):
        self.cancellation_case("pause")

    def test_remote_revocation_cancels_worker_but_preserves_local_camera(self):
        self.cancellation_case("remote")

    def test_hardware_error_cancels_worker_and_late_frames_stay_hidden(self):
        self.enable_remote_analysis()
        self.observer.running = False
        self.observer.error_changed.emit("摄像头已断开，请重新连接。")
        self.assertFalse(self.controller.busy)
        self.assertTrue(self.controller.preview.isNull())
        self.observer.produce()
        self.assertTrue(self.controller.preview.isNull())
        self.analyzer.release.set()
        spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
        self.assertEqual(self.chat.comments, [])

    def test_screen_and_camera_settings_independent_and_remote_workers_do_not_overlap(self):
        screen_observer = SyntheticCameraObserver()
        screen_observer.start()
        screen_observer.produce()
        screen_analyzer = HeldCameraAnalyzer()
        self.analyzers.append(screen_analyzer)
        self.store.screen = PerceptionSettings(enabled=True, allow_remote=True)
        screen = PerceptionController(self.store, self.pet, self.chat, observer=screen_observer,
                                      analyzer_factory=lambda: screen_analyzer, clock=lambda: 1000.)
        self.controllers.append(screen)
        screen.other_visual_controller = self.controller
        self.controller.other_visual_controller = screen
        self.enable_remote_analysis()
        screen.poll()
        self.assertFalse(screen.busy)
        self.assertFalse(screen_analyzer.started.is_set())
        self.controller.set_remote_enabled(False)
        self.analyzer.release.set()
        spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
        self.assertTrue(self.store.screen.enabled)
        self.assertTrue(self.store.screen.allow_remote)
        self.assertEqual(self.store.saved_screen, [])
        self.assertTrue(screen.comment_current_preview())
        spin_until(screen_analyzer.started.is_set)
        self.assertTrue(self.controller.set_remote_enabled(True))
        self.observer.produce()
        self.controller.observe_now()
        self.assertFalse(self.controller.comment_current_preview())
        self.assertFalse(self.controller.busy)
        self.assertTrue(screen.busy)
        screen_observer.shutdown()


class CameraHttpArchiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def test_explicit_remote_comment_uses_camera_prompt_and_persists_camera_source(self):
        with tempfile.TemporaryDirectory(prefix="aura-camera-http-") as directory, MockApiServer() as server:
            path = Path(directory)
            settings = isolated_settings(path / "settings.ini", MemoryCredentialBackend())
            settings.save_api_settings(ApiSettings(server.base_url, "synthetic-camera-model", 5))
            settings.save_api_key("loopback-camera-token")
            chat = ChatController(settings, store=ConversationStore(path / "archive.sqlite3"))
            observer = SyntheticCameraObserver()
            responses = []
            pet = SimpleNamespace(pet=SimpleNamespace(animation=SimpleNamespace(state=AnimationState.IDLE)),
                                  respond=lambda *args, **kwargs: responses.append((args, kwargs)),
                                  play_motion=lambda *args: None)
            controller = camera_controller(settings, pet, chat, observer=observer, clock=lambda: 1000.)
            try:
                self.assertTrue(controller.set_enabled(True, allow_remote=True))
                observer.produce()
                self.assertTrue(controller.observe_now())
                self.assertEqual(server.requests, [])
                server.enqueue(json.dumps({"kind": "interesting", "summary": "合成镜头里有一个蓝色杯子。",
                                           "reply": "这个蓝色杯子看起来很清爽。", "emotion": "happy",
                                           "importance": .5, "confidence": .95, "event_key": "synthetic-camera-cup"}, ensure_ascii=False))
                self.assertTrue(controller.comment_current_preview())
                spin_until(lambda: not controller.busy and len(chat.messages) == 1)
                request = server.requests[0]["body"]
                system = request["messages"][0]["content"]
                self.assertIn("摄像头画面分析器", system)
                self.assertNotIn("屏幕情境分析器", system)
                text = request["messages"][1]["content"][0]["text"]
                self.assertIn('"source_kind": "camera"', text)
                self.assertTrue(request["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,"))
                message = chat.messages[0]
                self.assertEqual(message["source"], "proactive_camera")
                self.assertEqual(message["screen_event"]["context_summary"], "合成镜头里有一个蓝色杯子。")
                archived = chat.store.load_messages(chat.current_conversation_id)
                self.assertEqual(archived[0]["source"], "proactive_camera")
                self.assertNotIn("data:image", json.dumps(archived, ensure_ascii=False))
                self.assertEqual(len(responses), 1)
            finally:
                controller.shutdown()
                chat.shutdown()
                spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
                controller.deleteLater()
                chat.deleteLater()
                observer.deleteLater()
                self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
