"""Real static-pose idle asset with isolated Qt state and deterministic timing.

These tests do not generate art, use a camera/API, or read live preferences.
Every clip is decoded once per suite; playback/bridges/arbitration stay real.
"""
from pathlib import Path
import hashlib
import json
import random
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QSettings, Qt
from PySide6.QtGui import QImage, QMouseEvent
from PySide6.QtWidgets import QApplication

from desktop_pet.animation_controller import AnimationState
from desktop_pet.config import PetConfig, PROJECT_ROOT
from desktop_pet.frame_animation import load_clip
from desktop_pet.idle_behavior import DEFAULT_IDLE_ACTIONS, IDLE_DURATION_SECONDS, IdleBehaviorPolicy
from desktop_pet.pet_controller import PetController
from desktop_pet.pet_window import PetWindow
from desktop_pet.placement import clamp_visible_position
from desktop_pet.settings_manager import CredentialStore, SettingsManager


class _MemoryVault:
    def get_password(self, _service, _account):
        return None

    def set_password(self, _service, _account, _password):
        pass

    def delete_password(self, _service, _account):
        pass


class TsundereIdleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)
        cls.animations = PROJECT_ROOT / "assets/character/animations"
        cls.asset_manifest = json.loads((cls.animations / "idle_tsundere/animation.json").read_text(encoding="utf-8"))
        cls.clip = load_clip(cls.animations, "idle_tsundere")
        cls.pose_path = PROJECT_ROOT / "assets/character/static/tsundere.png"
        cls.neutral_path = PROJECT_ROOT / "assets/expressions/neutral.png"
        cls.pose = QImage(str(cls.pose_path)).convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
        cls.neutral = QImage(str(cls.neutral_path)).convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aura-tsundere-idle-")
        self.area = QRect(0, 0, 1920, 1040)
        self.work_area = patch("desktop_pet.pet_window.work_area", return_value=self.area)
        self.work_area.start()
        self.settings = SettingsManager(
            QSettings(str(Path(self.temp.name) / "preferences.ini"), QSettings.Format.IniFormat),
            CredentialStore(backend=_MemoryVault()),
        )
        self.pet = PetWindow(PetConfig())
        self.pet.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        self.pet.refresh_timer.stop()
        self.controller = PetController(self.pet, self.settings)
        self.controller.behavior = IdleBehaviorPolicy(random.Random(17), idle_actions=("idle_tsundere",))
        self.player = self.controller.asset_player
        self.decoder = patch("desktop_pet.frame_animation.load_clip", return_value=self.clip)
        self.submit = patch.object(self.player.runner, "submit", side_effect=lambda work, done: done(work(), ""))
        self.decoder.start()
        self.submit.start()
        self.pet.show()
        self.app.processEvents()

    def tearDown(self):
        self.pet.close()
        self.controller.deleteLater()
        self.pet.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.submit.stop()
        self.decoder.stop()
        self.work_area.stop()
        self.temp.cleanup()

    def tick(self, seconds):
        self.pet._advance_frame(seconds)

    def start_random_pose(self):
        self.tick(self.controller.behavior.next_idle_in + .001)
        self.assertTrue(self.controller._idle_clip_owned)
        self.assertTrue(self.player.playing)
        self.assertEqual(self.player.name, "idle_tsundere")

    def finish(self):
        for _ in range(200):
            self.tick(.05)
            if not self.player.busy and self.pet._edge_recovery is None:
                return
        self.fail("Tsundere pose did not finish or settle in the bounded test clock")

    def assert_face(self, code):
        self.assertEqual(self.controller.expression, code)
        image = self.controller.expressions.resolve(code).pixmap.toImage()
        self.assertEqual(self.pet._source.toImage(), image)

    def test_registered_real_pose_has_exact_hold_and_neutral_endpoints(self):
        self.assertIn("idle_tsundere", DEFAULT_IDLE_ACTIONS)
        self.assertEqual(len(self.clip.frames), 30)
        self.assertEqual(self.clip.manifest.fps, 12)
        self.assertEqual(self.clip.manifest.anchor, (.5, .9375))
        self.assertEqual(self.clip.frames[0], self.neutral)
        self.assertEqual(self.clip.frames[-1], self.neutral)
        self.assertTrue(all(frame == self.pose for frame in self.clip.frames[3:27]))
        self.assertNotEqual(self.pose, self.neutral)
        origin = self.asset_manifest["origin"]
        self.assertEqual(origin["source_pose_sha256"], hashlib.sha256(self.pose_path.read_bytes()).hexdigest())
        self.assertEqual(origin["source_neutral_sha256"], hashlib.sha256(self.neutral_path.read_bytes()).hexdigest())
        self.assertEqual(origin.get("video_generation_attempts", 0), 0)

    def test_random_dispatch_plays_once_without_selecting_a_permanent_appearance(self):
        with patch.object(self.player, "play", wraps=self.player.play) as play:
            self.start_random_pose()
        play.assert_called_once_with("idle_tsundere", once=True, duration_seconds=IDLE_DURATION_SECONDS)
        self.assertFalse(self.player._clip.manifest.loop)
        self.assertEqual(self.controller.appearance, "original")
        self.assert_face("neutral")
        self.finish()
        self.assertFalse(self.controller._idle_clip_owned)
        self.assertEqual(self.controller.appearance, "original")
        self.assert_face("neutral")

    def test_player_extends_the_static_hold_to_the_common_idle_duration(self):
        self.start_random_pose()
        runtime = self.player._clip
        self.assertTrue(runtime.manifest.static_pose_idle)
        self.assertEqual((len(runtime.frames), runtime.manifest.fps), (144, 24))
        self.assertEqual(runtime.manifest.duration, IDLE_DURATION_SECONDS)
        self.assertEqual(sum(frame == self.pose for frame in runtime.frames) / runtime.manifest.fps, 5.5)
        self.assertEqual(runtime.frames[0], self.neutral)
        self.assertEqual(runtime.frames[-1], self.neutral)
        self.assertEqual((len(self.clip.frames), self.clip.manifest.fps), (30, 12))
        self.finish()
        self.assert_face("neutral")

    def test_manual_static_idle_preview_keeps_its_source_duration(self):
        self.assertTrue(self.controller.play_asset_animation("idle_tsundere"))
        self.assertTrue(self.player._clip.manifest.loop)
        self.assertEqual(self.player._clip.manifest.duration, self.clip.manifest.duration)
        self.assertEqual((len(self.player._clip.frames), self.player._clip.manifest.fps), (60, 24))

    def test_chat_interrupts_the_pose_and_restores_the_api_face(self):
        self.start_random_pose()
        self.tick(.6)
        self.controller.set_conversation_busy(True)
        self.controller.apply_context_expression("happy", source="chat")
        self.finish()
        self.assert_face("happy")
        self.assertEqual(self.controller.appearance, "original")
        self.tick(100)
        self.assertFalse(self.player.playing)
        self.assert_face("happy")

    def test_temporary_anger_wins_and_idle_can_resume_after_it_expires(self):
        self.start_random_pose()
        self.tick(.6)
        for part in ("left_ear", "tail", "right_ear"):
            self.pet.part_clicked.emit(part)
        self.assertTrue(self.controller.behavior.is_angry)
        self.tick(.3)
        self.assert_face("angry")
        self.controller.apply_context_expression("surprised", source="chat")
        self.assert_face("angry")
        self.tick(7.8)
        self.assertFalse(self.controller.behavior.is_angry)
        self.assert_face("neutral")
        self.start_random_pose()
        self.assertEqual(self.controller.appearance, "original")

    def test_drag_freezes_the_pose_and_keeps_the_new_drop_position(self):
        self.pet.move(self.area.center() - self.pet.rect().center())
        self.start_random_pose()
        self.tick(.6)
        local = self.pet.character_transform.map(QPointF(
            self.pet.sprite_rect.x() + self.pet._hit_image.width() * .5,
            self.pet.sprite_rect.y() + self.pet._hit_image.height() * .7,
        )).toPoint()
        self.assertTrue(self.pet.character_contains(local))
        start = self.pet.mapToGlobal(local)
        end = start + QPoint(-95, 65)

        def send(kind, position, held):
            button = Qt.MouseButton.NoButton if kind == QEvent.Type.MouseMove else Qt.MouseButton.LeftButton
            event = QMouseEvent(kind, QPointF(self.pet.mapFromGlobal(position)), QPointF(position), button,
                                Qt.MouseButton.LeftButton if held else Qt.MouseButton.NoButton,
                                Qt.KeyboardModifier.NoModifier)
            with patch("desktop_pet.pet_window.screen_for_point", return_value=None):
                QApplication.sendEvent(self.pet, event)

        send(QEvent.Type.MouseButtonPress, start, True)
        send(QEvent.Type.MouseMove, end, True)
        frame = self.pet._sequence_index
        pose = self.pet.animation.pose
        self.tick(.9)
        self.assertEqual(self.pet.animation.state, AnimationState.DRAGGING)
        self.assertEqual(self.pet._sequence_index, frame)
        self.assertEqual(self.pet.animation.pose, pose)
        send(QEvent.Type.MouseButtonRelease, end, False)
        self.tick(.1)
        self.assertEqual(self.pet.animation.state, AnimationState.RETURNING)
        self.assertEqual(self.pet._sequence_index, frame)
        self.tick(.5)
        dropped = QPoint(self.pet.pos())
        self.finish()
        self.assertEqual(self.pet.pos(), dropped)
        self.assert_face("neutral")

    def test_near_edge_playback_returns_to_original_position_at_two_sizes(self):
        for scale in (100, 200):
            with self.subTest(scale=scale):
                self.pet.set_scale_percent(scale)
                self.pet.move(clamp_visible_position(QPoint(10000, -10000), self.pet.character_placement_bounds, self.area))
                home = QPoint(self.pet.pos())
                self.start_random_pose()
                self.finish()
                self.assertEqual(self.pet.pos(), home)
                self.assert_face("neutral")

    def test_manual_static_selection_still_stays_selected(self):
        self.assertTrue(self.controller.set_static_character("tsundere"))
        self.tick(100)
        self.assertEqual(self.controller.appearance, "tsundere")
        self.assertEqual(self.pet._source.toImage().convertToFormat(QImage.Format.Format_ARGB32_Premultiplied), self.pose)
        self.assertFalse(self.player.playing)


if __name__ == "__main__":
    unittest.main()
