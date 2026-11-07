"""Real Qt arbitration with temporary faces and two-frame synthetic clips.

The window's existing timer is stopped. Each test advances that same frame
path explicitly; no camera, network request, production settings, or actual
idle video is opened. Only clip decoding is replaced by small in-memory art.
"""

from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtCore import QEvent, QSettings, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication

from desktop_pet.animation_controller import AnimationState
from desktop_pet.config import PetConfig
from desktop_pet.expression_manager import EXPRESSIONS, ExpressionManager
from desktop_pet.frame_animation import ClipManifest, LoadedClip, static_canvas
from desktop_pet.idle_behavior import DEFAULT_IDLE_ACTIONS, IDLE_DURATION_SECONDS, IdleBehaviorPolicy
from desktop_pet.pet_controller import PetController
from desktop_pet.pet_window import PetWindow
from desktop_pet.settings_manager import CredentialStore, SettingsManager


class _MemoryVault:
    def get_password(self, _service, _account):
        return None

    def set_password(self, _service, _account, _password):
        pass

    def delete_password(self, _service, _account):
        pass


class IdleBehaviorIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="aura-idle-behavior-")
        self.root = Path(self.directory.name)
        self.faces = self.root / "expressions"
        self.faces.mkdir()
        colors = ("#3c7eb0", "#edb637", "#d979be", "#7584c5", "#c64759", "#598d72", "#a867c1")
        for (code, (_label, filenames)), color in zip(EXPRESSIONS.items(), colors):
            image = self._rectangle(128, color)
            self.assertTrue(image.save(str(self.faces / filenames[0]), "PNG"))
        self.original = self.faces / "neutral.png"
        self.settings = SettingsManager(
            QSettings(str(self.root / "preferences.ini"), QSettings.Format.IniFormat),
            CredentialStore(backend=_MemoryVault()),
        )
        self.pet = PetWindow(PetConfig(
            image_path=self.original, window_width=240, window_height=300, character_size=220,
        ))
        self.pet.refresh_timer.stop()
        manager = ExpressionManager(self.faces, character_directory=self.root / "no-static-art")
        self.controller = PetController(
            self.pet, self.settings, manager, animation_directory=self.root / "no-videos",
        )
        # The synthetic character opts into the production-character behavior.
        # Its explicitly injected expression pack still supplies every image.
        self.controller._uses_project_character = True
        self.controller.behavior = IdleBehaviorPolicy(random.Random(37))
        self.player = self.controller.asset_player
        self.clip_frames = (
            self._rectangle(1024, "#167acb"), self._rectangle(1024, "#52b9dc"),
        )
        self._patches = (
            patch("desktop_pet.frame_animation.read_manifest", side_effect=self._manifest),
            patch("desktop_pet.frame_animation.load_clip", side_effect=self._loaded_clip),
            patch.object(self.player.runner, "submit", side_effect=self._submit_inline),
        )
        for mock_patch in self._patches:
            mock_patch.start()
        self.pet.show()
        self.app.processEvents()

    def tearDown(self):
        self.pet.close()
        self.controller.deleteLater()
        self.pet.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        for mock_patch in reversed(self._patches):
            mock_patch.stop()
        self.directory.cleanup()

    @staticmethod
    def _rectangle(size, color):
        image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.fillRect(size // 4, size // 5, size // 2, size * 3 // 5, QColor(color))
        painter.end()
        return image

    def _manifest(self, _directory, name):
        return ClipManifest(
            name=name, paths=(self.root / "frame-1.png", self.root / "frame-2.png"),
            fps=12, loop=name not in {"think_scratch_head", "snarl_cute"},
            completion="hold_last" if name == "snarl_cute" else "idle",
            anchor=(.5, .9375), transition_ms=250,
        )

    def _loaded_clip(self, directory, name):
        return LoadedClip(self._manifest(directory, name), self.clip_frames)

    @staticmethod
    def _submit_inline(operation, finished):
        # Keep loading, installation, bridges, and the player clock real while
        # avoiding worker scheduling and large asset reads in arbitration tests.
        finished(operation(), "")

    def tick(self, seconds):
        self.pet._advance_frame(seconds)

    def assert_face(self, code):
        self.assertEqual(self.controller.expression, code)
        self.assertEqual(self.pet._source.toImage(), self.controller.expressions.resolve(code).pixmap.toImage())

    def irritate(self):
        for part in ("left_ear", "tail", "right_ear"):
            self.pet.part_clicked.emit(part)
        self.assertTrue(self.controller.behavior.is_angry)
        self.assert_face("angry")

    def start_random_clip(self):
        self.tick(self.controller.behavior.next_idle_in + .001)
        self.assertTrue(self.controller._idle_clip_owned)
        self.assertTrue(self.player.playing)
        self.assertTrue(self.pet._sequence_frames)

    def test_random_actions_play_once_and_do_not_repeat_at_shuffle_boundaries(self):
        with patch.object(self.player, "play", return_value=True) as play:
            for _ in range(len(DEFAULT_IDLE_ACTIONS) * 3):
                self.tick(self.controller.behavior.next_idle_in + .001)
        choices = [call.args[0] for call in play.call_args_list]
        self.assertEqual(len(choices), len(DEFAULT_IDLE_ACTIONS) * 3)
        self.assertTrue(all(call.kwargs == {"once": True, "duration_seconds": IDLE_DURATION_SECONDS}
                            for call in play.call_args_list))
        self.assertTrue(all(first != second for first, second in zip(choices, choices[1:])))
        for offset in range(0, len(choices), len(DEFAULT_IDLE_ACTIONS)):
            self.assertEqual(set(choices[offset:offset + len(DEFAULT_IDLE_ACTIONS)]), set(DEFAULT_IDLE_ACTIONS))

    def test_automatic_clip_really_finishes_and_restores_static_pixels(self):
        self.start_random_clip()
        self.assertFalse(self.player._clip.manifest.loop)
        self.tick(IDLE_DURATION_SECONDS - .01)
        self.assertTrue(self.player.playing)
        self.tick(.02)
        self.assertFalse(self.player.playing)
        self.assertFalse(self.pet._sequence_frames)
        self.assertFalse(self.controller._idle_clip_owned)
        self.assert_face("neutral")

    def test_new_video_idles_play_once_and_restore_neutral(self):
        for name in ("idle_hands_on_hips", "idle_heart_cheeks", "idle_typing_work", "idle_eating_rice"):
            with self.subTest(name=name):
                self.assertIn(name, DEFAULT_IDLE_ACTIONS)
                self.controller.behavior = IdleBehaviorPolicy(
                    random.Random(37), idle_actions=(name,),
                )
                self.start_random_clip()
                self.assertEqual(self.player.name, name)
                self.assertFalse(self.player._clip.manifest.static_pose_idle)
                self.assertFalse(self.player._clip.manifest.loop)
                self.tick(IDLE_DURATION_SECONDS - .01)
                self.assertTrue(self.player.playing)
                self.tick(.02)
                self.assertFalse(self.player.playing)
                self.assertFalse(self.pet._sequence_frames)
                self.assertFalse(self.controller._idle_clip_owned)
                self.assert_face("neutral")

    def test_reused_actions_finish_once_with_a_visible_return_bridge(self):
        for name in ("think_scratch_head", "snarl_cute"):
            with self.subTest(name=name):
                self.assertIn(name, DEFAULT_IDLE_ACTIONS)
                self.controller.behavior = IdleBehaviorPolicy(random.Random(37), idle_actions=(name,))
                self.start_random_clip()
                self.assertEqual(self.player.name, name)
                self.assertFalse(self.player._clip.manifest.loop)
                self.assertEqual(self.player._clip.manifest.completion, "idle")
                self.tick(IDLE_DURATION_SECONDS - .25)
                # The old snarl source ends crouching and manually holds that
                # pose. Its automatic route must render a return bridge, not
                # jump directly from the last pose to static neutral pixels.
                self.assertTrue(self.player.playing)
                self.assertEqual(self.player._phase, "exit")
                last_pose = self.pet._hit_image.copy()
                self.tick(.12)
                self.assertEqual(self.player._phase, "exit")
                self.assertNotEqual(self.pet._hit_image, last_pose)
                self.tick(.14)
                self.assertFalse(self.player.busy)
                self.assertFalse(self.pet._sequence_frames)
                self.assertFalse(self.controller._idle_clip_owned)
                self.assert_face("neutral")

    def test_both_static_poses_are_registered_in_the_random_idle_pool(self):
        self.assertIn("idle_sleepy", DEFAULT_IDLE_ACTIONS)
        self.assertIn("idle_tsundere", DEFAULT_IDLE_ACTIONS)

    def test_active_idle_duration_freezes_while_dragging_and_returning(self):
        self.start_random_clip()
        self.tick(1)
        before = self.player._clock.elapsed
        frozen = self.pet._hit_image.copy()
        self.pet.animation.start_dragging()
        self.tick(20)
        self.assertEqual(self.player._clock.elapsed, before)
        self.assertEqual(self.pet._hit_image, frozen)
        self.pet.animation.finish_dragging()
        self.tick(.01)
        self.assertEqual(self.pet.animation.state, AnimationState.RETURNING)
        self.assertEqual(self.player._clock.elapsed, before)
        self.tick(self.pet.config.animation.return_duration + .01)
        self.assertLess(self.player._clock.elapsed - before, .5)
        self.tick(IDLE_DURATION_SECONDS - 2)
        self.assertTrue(self.player.playing)
        self.tick(2)
        self.assertFalse(self.player.playing)
        self.assert_face("neutral")

    def test_duration_finishes_at_six_seconds_for_fine_and_delayed_ticks(self):
        neutral = static_canvas(self.pet._source.toImage())
        variants = ((self.clip_frames[0], self.clip_frames[-1]),
                    (self.clip_frames[0], neutral), (neutral, neutral))
        for frames in variants:
            self.clip_frames = frames
            for step in (1 / 60, .37, IDLE_DURATION_SECONDS):
                with self.subTest(neutral_end=frames[-1] == neutral, step=step):
                    self.controller.behavior = IdleBehaviorPolicy(random.Random(37),
                                                                  idle_actions=("idle_puff_cheeks",))
                    self.start_random_clip()
                    elapsed = 0.0
                    while elapsed < IDLE_DURATION_SECONDS - .001:
                        delta = min(step, IDLE_DURATION_SECONDS - .001 - elapsed)
                        self.tick(delta)
                        elapsed += delta
                        self.assertTrue(self.player.playing,
                                        f"Idle ended before its six-second deadline at {elapsed}")
                    self.tick(.001)
                    self.assertFalse(self.player.playing,
                                     "A completed clip must not defer leftover exit time to the next tick")
                    self.assertFalse(self.controller._idle_clip_owned)
                    self.assert_face("neutral")
            with self.subTest(neutral_end=frames[-1] == neutral, tick="single-six-second-tick"):
                self.controller.behavior = IdleBehaviorPolicy(random.Random(37),
                                                              idle_actions=("idle_puff_cheeks",))
                self.start_random_clip()
                self.tick(IDLE_DURATION_SECONDS)
                self.assertFalse(self.player.playing)
                self.assertFalse(self.controller._idle_clip_owned)
                self.assert_face("neutral")

    def test_invalid_duration_arguments_preserve_the_active_clip_and_pixels(self):
        self.start_random_clip()
        self.tick(1)
        active = self.player._clip
        frame = self.pet._hit_image.copy()
        for options in ({"once": False, "duration_seconds": IDLE_DURATION_SECONDS},
                        *({"once": True, "duration_seconds": invalid}
                          for invalid in (True, 0, -1, .25, .5, float("nan"), float("inf"), "6"))):
            with self.subTest(options=options):
                self.assertFalse(self.player.play("snarl_cute", **options))
                self.assertIs(self.player._clip, active)
                self.assertEqual(self.pet._hit_image, frame)
                self.assertFalse(self.player.loading)

    def test_conversation_busy_blocks_idle_and_false_waits_for_fresh_quiet(self):
        with patch.object(self.player, "play", return_value=True) as play:
            self.controller.set_conversation_busy(True)
            self.tick(100)
            play.assert_not_called()
            self.controller.set_conversation_busy(False)
            play.assert_not_called()
            self.tick(.001)
            play.assert_not_called()
            self.tick(self.controller.behavior.next_idle_in + .001)
            play.assert_called_once()

    def test_visible_reply_bubble_blocks_idle_until_reading_finishes(self):
        self.controller.respond("A synthetic reply that is still being read.", "happy")
        self.assertTrue(self.pet.bubble.isVisible())
        with patch.object(self.player, "play", return_value=True) as play:
            self.tick(100)
            play.assert_not_called()
            self.assert_face("happy")
            self.pet.bubble.dismiss()
            self.tick(self.controller.behavior.next_idle_in + .001)
            play.assert_called_once()

    def test_drag_and_return_pause_the_random_scheduler(self):
        with patch.object(self.player, "play", return_value=True) as play:
            self.pet.animation.start_dragging()
            self.tick(100)
            self.assertEqual(self.pet.animation.state, AnimationState.DRAGGING)
            play.assert_not_called()
            self.pet.animation.finish_dragging()
            self.tick(.01)
            self.assertEqual(self.pet.animation.state, AnimationState.RETURNING)
            play.assert_not_called()
            self.tick(self.pet.config.animation.return_duration + .01)
            self.tick(self.controller.behavior.next_idle_in + .001)
            play.assert_called_once()

    def test_hidden_pet_does_not_load_random_clips(self):
        self.pet.hide()
        with patch.object(self.player, "play", return_value=True) as play:
            self.tick(100)
            play.assert_not_called()
            self.pet.show()
            self.tick(self.controller.behavior.next_idle_in + .001)
            play.assert_called_once()

    def test_custom_picture_and_nonoriginal_appearance_are_not_replaced(self):
        with patch.object(self.player, "play", return_value=True) as play:
            self.controller._uses_project_character = False
            self.tick(100)
            play.assert_not_called()
            self.controller._uses_project_character = True
            self.controller.appearance = "tsundere"
            self.tick(100)
            play.assert_not_called()

    def test_three_ear_tail_clicks_show_anger_then_return_to_random_idle(self):
        self.irritate()
        with patch.object(self.player, "play", return_value=True) as play:
            self.tick(7.9)
            self.assert_face("angry")
            play.assert_not_called()
            self.tick(.11)
            self.assertFalse(self.controller.behavior.is_angry)
            self.assert_face("neutral")
            play.assert_not_called()
            self.tick(self.controller.behavior.next_idle_in + .001)
            play.assert_called_once()

    def test_api_does_not_override_anger_and_latest_reply_returns_while_visible(self):
        self.irritate()
        self.controller.apply_context_expression("sleepy", source="screen")
        self.controller.respond("The latest actual answer stays visible.", "happy")
        self.assert_face("angry")
        self.assertTrue(self.pet.bubble.isVisible())
        self.tick(8.01)
        self.assert_face("happy")
        with patch.object(self.player, "play", return_value=True) as play:
            self.tick(100)
            play.assert_not_called()
            self.assert_face("happy")

    def test_stale_context_is_not_replayed_after_anger_when_no_reply_is_visible(self):
        self.irritate()
        self.controller.apply_context_expression("surprised", source="camera")
        self.tick(8.01)
        self.assert_face("neutral")
        self.assertIsNone(self.controller._queued_context)

    def test_latest_waiting_chat_expression_returns_after_anger(self):
        self.irritate()
        self.controller.set_conversation_busy(True)
        self.controller.apply_context_expression("sleepy", source="screen")
        self.controller.apply_context_expression("thinking", source="chat")
        self.tick(8.01)
        self.assert_face("thinking")
        with patch.object(self.player, "play", return_value=True) as play:
            self.tick(100)
            play.assert_not_called()

    def test_anger_expiry_during_drag_keeps_frozen_pixels_then_renders_neutral(self):
        self.irritate()
        self.pet.animation.start_dragging()
        frozen = self.pet._source.toImage().copy()
        self.tick(8.01)
        self.assertFalse(self.controller.behavior.is_angry)
        self.assertEqual(self.controller.expression, "neutral")
        self.assertEqual(self.pet._source.toImage(), frozen)
        self.assertEqual(self.pet.animation.state, AnimationState.DRAGGING)
        self.pet.animation.finish_dragging()
        self.tick(.01)
        self.assertEqual(self.pet._source.toImage(), frozen)
        self.tick(self.pet.config.animation.return_duration + .01)
        self.assert_face("neutral")

    def test_disabling_auto_expression_discards_context_queued_during_anger(self):
        self.irritate()
        self.controller.respond("This answer remains readable without its queued face.", "happy")
        self.assertTrue(self.controller.set_auto_expression_enabled(False))
        self.tick(8.01)
        self.assert_face("neutral")
        self.assertIsNone(self.controller._queued_context)
        self.assertTrue(self.controller.set_auto_expression_enabled(True))
        self.tick(.01)
        self.assert_face("neutral")

    def test_auto_expression_off_preserves_a_manually_chosen_face(self):
        self.controller.set_auto_expression_enabled(False)
        self.controller.set_expression("happy", feedback=False, animate=False)
        with patch.object(self.player, "play", return_value=True) as play:
            self.tick(100)
            self.assertFalse(self.controller.apply_context_expression("sleepy", source="chat"))
            self.tick(100)
            play.assert_not_called()
            self.assert_face("happy")

    def test_manual_preview_remains_looping_and_is_not_replaced_by_idle_scheduler(self):
        with patch.object(self.player, "play", wraps=self.player.play) as play:
            self.assertTrue(self.controller.play_asset_animation("idle_puff_cheeks"))
            self.assertTrue(self.player._clip.manifest.loop)
            self.assertFalse(self.controller._idle_clip_owned)
            self.tick(100)
            self.assertTrue(self.player.playing)
            self.assertEqual(self.player.name, "idle_puff_cheeks")
            self.assertFalse(self.controller._idle_clip_owned)
            play.assert_called_once_with("idle_puff_cheeks")

    def test_api_face_waits_for_idle_exit_bridge_and_duplicate_does_not_reinstall(self):
        self.start_random_clip()
        self.tick(.12)
        with patch.object(self.pet, "set_character_source", wraps=self.pet.set_character_source) as render:
            self.assertTrue(self.controller.apply_context_expression("happy", source="chat"))
            self.assertEqual(self.player._phase, "exit")
            self.assertEqual(self.controller.expression, "happy")
            self.assertEqual(self.pet._source.toImage(), self.controller.expressions.resolve("neutral").pixmap.toImage())
            render.assert_not_called()
            self.tick(.12)
            render.assert_not_called()
            self.tick(.14)
            self.assert_face("happy")
            self.assertFalse(self.pet._sequence_frames)
            self.assertIsNone(self.controller._pending_expression)
            self.controller.respond("A duplicated delivery uses the same face.", "happy")
            self.assertEqual(render.call_count, 1)

    def test_same_api_face_can_return_after_random_idle_restores_neutral(self):
        self.controller.apply_context_expression("happy", source="chat")
        self.assert_face("happy")
        self.start_random_clip()
        self.assertEqual(self.controller.expression, "neutral")
        self.controller.apply_context_expression("happy", source="chat")
        self.tick(.26)
        self.assert_face("happy")
        self.assertFalse(self.pet._sequence_frames)

    def test_chat_expression_during_drag_preserves_frozen_clip_until_recovery(self):
        self.start_random_clip()
        self.tick(.12)
        frozen = self.pet._hit_image.copy()
        self.pet.animation.start_dragging()
        self.controller.apply_context_expression("surprised", source="chat")
        self.tick(1)
        self.assertEqual(self.pet._hit_image, frozen)
        self.assertTrue(self.pet._sequence_frames)
        self.assertEqual(self.pet.animation.state, AnimationState.DRAGGING)
        self.pet.animation.finish_dragging()
        self.tick(.01)
        self.assertEqual(self.pet._hit_image, frozen)
        self.tick(self.pet.config.animation.return_duration + .01)
        self.tick(.26)
        self.tick(.26)
        self.assert_face("surprised")
        self.assertFalse(self.pet._sequence_frames)


if __name__ == "__main__":
    unittest.main()
