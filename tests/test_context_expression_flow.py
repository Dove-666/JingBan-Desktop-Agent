"""Context-selected faces through real Qt, isolated settings and loopback HTTP.

The screen and camera observers supply synthetic pixels.  No physical camera,
user desktop, production archive, credential vault or external API is accessed.
"""

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtCore import QEvent, QThreadPool
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication

from desktop_pet.animation_controller import AnimationState
from desktop_pet.application_controller import ApplicationController
from desktop_pet.config import PetConfig
from desktop_pet.expression_manager import EXPRESSIONS
from desktop_pet.memory.settings import MemorySettings
from desktop_pet.perception.camera_controller import CameraController
from desktop_pet.perception.context_analyzer import ContextEvent, parse_context
from desktop_pet.perception.proactive_agent import ProactiveAgent
from desktop_pet.perception.settings import CameraSettings, PerceptionSettings
from desktop_pet.settings_manager import ApiSettings
from tests.mock_api import MockApiServer
from tests.test_application import (
    LOCAL_TEST_KEY, MemoryCredentialBackend, isolated_settings, spin_until,
)
from tests.test_camera_controller import SyntheticCameraObserver
from tests.test_perception_integration import AdjustableClock, SyntheticScene


class _DisabledMemory:
    def load_settings(self):
        return MemorySettings()

    def credential_status(self):
        return "Isolated expression tests; long-term memory is disabled."

    def search(self, *args, **kwargs):
        return []

    def extract(self, *args, **kwargs):
        return []

    def shutdown(self):
        pass


def _visual_json(*, emotion="happy", kind="none", reply="", importance=0.0,
                 confidence=0.95, express=True, key="synthetic-expression"):
    return json.dumps({
        "kind": kind, "summary": "A synthetic scene used only for a face selection.",
        "reply": reply, "emotion": emotion, "importance": importance,
        "confidence": confidence, "event_key": key, "express": express,
    })


class ContextExpressionDecisionTests(unittest.TestCase):
    def setUp(self):
        self.settings = PerceptionSettings(enabled=True, allow_remote=True,
                                           commentary_mode="quiet")

    def test_explicit_expression_handles_none_empty_reply_and_low_importance(self):
        for kind, reply, importance in (
            ("none", "", 0.0), ("interesting", "", 0.9),
            ("work", "An optional comment", 0.1),
        ):
            with self.subTest(kind=kind, importance=importance):
                agent = ProactiveAgent()
                event = ContextEvent(kind, "A synthetic scene", reply, "thinking",
                                     importance, 0.60, "test-expression", True)
                before = agent.quota_snapshot(1000, self.settings)
                decision = agent.decide(event, 1000, self.settings)
                self.assertEqual(decision.action, "expression")
                agent.record(decision, 1000)
                self.assertEqual(agent.quota_snapshot(1000, self.settings), before)

    def test_expression_cannot_bypass_confidence_busy_idle_or_consent(self):
        event = ContextEvent("none", "A synthetic scene", "", "happy",
                             0, .95, "synthetic", True)
        cases = (
            (replace(event, confidence=.5999), self.settings, False, 0),
            (event, self.settings, True, 0),
            (event, self.settings, False, self.settings.idle_pause_seconds),
            (event, replace(self.settings, enabled=False), False, 0),
            (replace(event, express=False), self.settings, False, 0),
        )
        for value, settings, busy, idle in cases:
            with self.subTest(event=value, busy=busy, idle=idle):
                decision = ProactiveAgent().decide(value, 1000, settings, busy, idle)
                self.assertEqual(decision.action, "quiet")

    def test_express_requires_a_json_boolean_and_valid_face(self):
        for invalid in ("true", 1, None, [], {}):
            payload = json.loads(_visual_json())
            payload["express"] = invalid
            with self.subTest(invalid=invalid):
                self.assertFalse(parse_context(json.dumps(payload)).express)
        valid = parse_context(_visual_json(emotion="surprised"))
        self.assertTrue(valid.express)
        self.assertEqual(valid.emotion, "surprised")


class ContextExpressionFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="aura-context-face-")
        self.root = Path(self.directory.name)
        self.vault = MemoryCredentialBackend()
        self.settings = isolated_settings(self.root / "preferences.ini", self.vault)
        self.camera_observer = SyntheticCameraObserver()
        self.memory_patch = patch("desktop_pet.application_controller.MemoryService",
                                  return_value=_DisabledMemory())
        self.memory_patch.start()
        # Inject through the application boundary, preserving real camera
        # controller signal wiring without constructing a hardware observer.
        with patch("desktop_pet.application_controller.CameraController",
                   side_effect=lambda *args, **kwargs: CameraController(
                       *args, observer=self.camera_observer, **kwargs)):
            self.session = ApplicationController(PetConfig(), settings=self.settings)
        self.character = self.session.pet_controller
        self.pet = self.session.pet
        self.character.asset_player.directory = self.root / "empty-animations"
        self.scene = SyntheticScene()
        self.clock = AdjustableClock()
        self.session.perception.observer = self.scene.observer()
        for controller in (self.session.perception, self.session.camera):
            controller._clock = self.clock
        self.session.show()
        self.pet.refresh_timer.stop()
        self.app.processEvents()
        spin_until(lambda: not self.session.chat.history_loading)

    def tearDown(self):
        self.session.shutdown()
        spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
        for window in (self.pet, self.session.debug_panel, self.session.chat_window):
            if window is not None:
                window.deleteLater()
        self.session.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.memory_patch.stop()
        self.directory.cleanup()

    def configure_api(self, server):
        self.settings.save_api_settings(ApiSettings(server.base_url, "test-model", 5))
        self.settings.save_api_key(LOCAL_TEST_KEY)

    def configure_visual(self, source, server):
        self.configure_api(server)
        if source == "camera":
            controller = self.session.camera
            self.assertTrue(controller.update_settings(CameraSettings(enabled=True, allow_remote=True)))
            self.camera_observer.produce()
        else:
            controller = self.session.perception
            self.assertTrue(controller.update_settings(PerceptionSettings(enabled=True, allow_remote=True)))
        controller._timer.stop()
        return controller

    def assert_face(self, code):
        expected = self.character.expressions.resolve(code).pixmap
        self.assertIsNotNone(expected, f"The actual {code} face asset is unavailable")
        self.assertEqual(self.character.expression, code)
        self.assertEqual(self.pet._source.toImage(), expected.toImage(),
                         f"The displayed sprite does not match the {code} face")

    def test_seven_api_faces_reach_the_actual_sprite_without_demo_speech_or_hops(self):
        with MockApiServer() as server:
            self.configure_api(server)
            for index, code in enumerate(EXPRESSIONS):
                with self.subTest(emotion=code):
                    text = f"Synthetic response {index}: {code}"
                    server.enqueue(json.dumps({"reply": text, "emotion": code}))
                    self.assertTrue(self.session.chat.send(f"Synthetic question {index}"))
                    spin_until(lambda: not self.session.chat.busy)
                    self.assert_face(code)
                    self.assertEqual(self.pet.bubble._text, text)
                    self.assertEqual(self.pet.animation.state, AnimationState.IDLE)
            self.assertEqual(len(server.requests), 7)
            self.assertEqual(len(self.session.chat.messages), 14)

    def test_toggle_roundtrips_and_off_preserves_manual_face_during_chat_and_motion(self):
        self.assertTrue(self.character.auto_expression_enabled)
        changed = QSignalSpy(self.character.auto_expression_changed)
        status = QSignalSpy(self.character.auto_expression_status_changed)
        self.assertTrue(self.character.set_auto_expression_enabled(False))
        restored = isolated_settings(self.root / "preferences.ini", self.vault)
        self.assertFalse(restored.load_auto_expression())
        self.character.set_expression("angry", feedback=False)
        for code in EXPRESSIONS:
            self.assertFalse(self.character.apply_context_expression(code, source="camera"))
            self.character.play_motion(code)
        with MockApiServer() as server:
            self.configure_api(server)
            server.enqueue(json.dumps({"reply": "The reply still appears.", "emotion": "happy"}))
            self.assertTrue(self.session.chat.send("Keep the manually selected face."))
            self.assert_face("angry")  # Pending thinking must also respect OFF.
            spin_until(lambda: not self.session.chat.busy)
        self.assert_face("angry")
        self.assertEqual(self.pet.bubble._text, "The reply still appears.")
        self.assertTrue(self.character.set_auto_expression_enabled(True))
        self.assertTrue(restored.load_auto_expression())
        self.assertEqual(changed.count(), 2)
        self.assertGreaterEqual(status.count(), 2)
        self.assertFalse(self.character.set_auto_expression_enabled("false"))

    def test_duplicate_chat_signal_and_reply_do_not_reinstall_face_or_restart_motion(self):
        changed = QSignalSpy(self.character.expression_changed)
        with patch.object(self.pet, "set_character_source", wraps=self.pet.set_character_source) as render:
            self.session.chat.emotion_requested.emit("happy")
            self.session.chat.reply_received.emit("A real response, not a demo phrase.", "happy", None)
            self.assertTrue(self.character.apply_context_expression("happy", source="camera"))
        self.assertEqual(render.call_count, 1)
        self.assertEqual(changed.count(), 1)
        self.assert_face("happy")
        self.assertEqual(self.pet.bubble._text, "A real response, not a demo phrase.")
        self.character.play_motion("thinking")
        self.pet.animation.advance(.12)
        previous_pose = self.pet.animation.pose
        with patch.object(self.pet.animation, "play_surprised", wraps=self.pet.animation.play_surprised) as hop:
            self.character.play_motion("thinking")
        hop.assert_not_called()
        self.assertEqual(self.pet.animation.pose, previous_pose)

    def test_drag_defers_latest_face_until_return_and_retains_facial_only_intent(self):
        self.character.set_expression("angry", feedback=False)
        frozen = self.pet._source.toImage()
        self.pet.animation.start_dragging()
        self.character.apply_context_expression("happy", source="screen")
        self.character.apply_context_expression("surprised", source="camera")
        self.assertEqual(self.pet._source.toImage(), frozen)
        self.assertEqual(self.pet.animation.state, AnimationState.DRAGGING)
        self.pet.animation.finish_dragging()
        self.pet.frame_tick.emit(.01)
        self.assertEqual(self.pet._source.toImage(), frozen)
        self.pet.animation.advance(2)
        self.pet.frame_tick.emit(2)
        self.assert_face("surprised")
        self.assertEqual(self.pet.animation.state, AnimationState.IDLE)

    def test_turning_off_discards_a_context_face_queued_during_drag(self):
        self.character.set_expression("sleepy", feedback=False)
        self.pet.animation.start_dragging()
        self.character.apply_context_expression("happy", source="chat")
        self.assertTrue(self.character.set_auto_expression_enabled(False))
        self.pet.animation.finish_dragging()
        self.pet.animation.advance(2)
        self.pet.frame_tick.emit(2)
        self.assert_face("sleepy")
        self.assertEqual(self.pet.animation.state, AnimationState.IDLE)

    def test_reply_uses_camera_and_screen_source_labels(self):
        for source, label, code in (("camera", "摄像头", "sleepy"), ("screen", "屏幕", "thinking")):
            with self.subTest(source=source):
                link = {"source_kind": source,
                        "conversation_id": self.session.chat.current_conversation_id,
                        "message_id": f"synthetic-{source}-message"}
                self.character.respond("A linked visual response", code, conversation_link=link)
                self.assert_face(code)
                self.assertIn(label, self.character.auto_expression_status)
                self.assertEqual(self.pet.bubble._conversation_link["source_kind"], source)

    def expression_only_case(self, source, code):
        with MockApiServer() as server:
            controller = self.configure_visual(source, server)
            quota = controller.agent.quota_snapshot(self.clock(), controller.settings)
            conversation_id = self.session.chat.current_conversation_id
            before = self.session.chat.store.load_messages(conversation_id)
            self.pet.bubble.dismiss()
            server.enqueue(_visual_json(emotion=code, key=f"{source}-expression"))
            self.assertTrue(controller.poll())
            spin_until(lambda: not controller.busy)
            self.assert_face(code)
            self.assertEqual(controller.history[-1]["动作"], "expression")
            self.assertIn("context_expression", controller.status)
            self.assertFalse(self.pet.bubble.isVisible())
            self.assertEqual(self.pet.animation.state, AnimationState.IDLE)
            self.assertEqual(controller.agent.quota_snapshot(self.clock(), controller.settings), quota)
            self.assertEqual(self.session.chat.messages, [])
            self.assertEqual(self.session.chat.store.load_messages(conversation_id), before)
            self.assertEqual(self.session.chat.store.recent_screen_events(conversation_id), [])
            self.assertEqual(len(server.requests), 1)

    def test_screen_expression_only_changes_face_without_speech_history_or_quota(self):
        self.expression_only_case("screen", "happy")

    def test_camera_expression_only_changes_face_without_speech_history_or_quota(self):
        self.expression_only_case("camera", "surprised")

    def test_late_screen_and_camera_analysis_cannot_override_a_newer_chat_face(self):
        for source in ("screen", "camera"):
            with self.subTest(source=source), MockApiServer() as server:
                controller = self.configure_visual(source, server)
                self.clock.advance(61)
                server.enqueue(_visual_json(emotion="angry", key=f"{source}-late"), delay=.65)
                self.assertTrue(controller.poll())
                spin_until(lambda: len(server.requests) == 1)
                server.enqueue(json.dumps({"reply": f"Current {source} chat response", "emotion": "happy"}))
                self.assertTrue(self.session.chat.send(f"Prioritize this {source} conversation."))
                spin_until(lambda: not self.session.chat.busy)
                self.assertTrue(controller.busy, "The visual fixture must actually finish after chat")
                self.assert_face("happy")
                spin_until(lambda: not controller.busy)
                self.assert_face("happy")
                self.assertEqual(controller.history[-1]["原因"], "user_busy")
                self.assertEqual(self.pet.bubble._text, f"Current {source} chat response")
                self.assertEqual(self.session.chat.store.recent_screen_events(
                    self.session.chat.current_conversation_id), [])

    def test_switching_off_during_remote_analysis_keeps_the_manual_face(self):
        with MockApiServer() as server:
            controller = self.configure_visual("camera", server)
            server.enqueue(_visual_json(emotion="happy"), delay=.2)
            self.assertTrue(controller.poll())
            spin_until(lambda: len(server.requests) == 1)
            self.character.set_auto_expression_enabled(False)
            self.character.set_expression("angry", feedback=False)
            spin_until(lambda: not controller.busy)
            self.assert_face("angry")
            self.assertEqual(controller.history[-1]["原因"], "auto_expression_disabled")
            self.assertFalse(self.pet.bubble.isVisible())
            self.assertEqual(self.session.chat.messages, [])


if __name__ == "__main__":
    unittest.main()
