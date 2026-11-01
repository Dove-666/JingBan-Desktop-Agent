"""Seven cached part rigs, verified with independent temporary characters."""

import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtCore import QEvent, QPointF, QRect, QSize, Qt
from PySide6.QtGui import QColor, QContextMenuEvent, QImage, QPainter, QPixmap
from PySide6.QtWidgets import QApplication

from desktop_pet.animation_config import AnimationConfig
from desktop_pet.config import PetConfig
from desktop_pet.eye_tracking import EyeTracker
from desktop_pet.part_interaction import PartInteraction, load_interaction_rig
from desktop_pet.pet_window import PetWindow
from tests.test_part_interaction import capture, global_for_source, make_part_fixture, paint_parts, send_mouse


STATES = ("neutral", "happy", "surprised", "sleepy", "angry", "thinking", "talk_open")
POINTS = {"left_ear": QPointF(260, 370), "right_ear": QPointF(760, 370), "tail": QPointF(770, 740)}


def make_expression_bank(root):
    expressions, interactions, eyes = root / "expressions", root / "interactions", root / "eyes"
    expressions.mkdir(parents=True)
    for index, name in enumerate(STATES):
        folder = interactions / name
        rig, source, data = make_part_fixture(folder)
        # Different real source pixels distinguish all states and expose a
        # reused body's cache. Face/part geometry is shared deliberately.
        for path in (source, folder / "base.png", folder / "eye_base.png"):
            image = QImage(str(path))
            painter = QPainter(image)
            painter.fillRect(QRect(430, 630, 160, 200), QColor(50 + index * 25, 90 + index * 12, 190 - index * 15))
            painter.end()
            image.save(str(path))
        shutil.copyfile(source, expressions / f"{name}.png")
        digest = hashlib.sha256((expressions / f"{name}.png").read_bytes()).hexdigest()
        data["source_sha256"] = digest
        rig.write_text(json.dumps(data), encoding="utf-8")
        if name != "happy":
            eye_folder = eyes / name
            eye_folder.mkdir(parents=True)
            eye_data = json.loads((folder / "eyes.json").read_text(encoding="utf-8"))
            eye_data["source_sha256"] = digest
            for filename in ("eye_base.png", "left_iris.png", "right_iris.png", "left_aperture.png", "right_aperture.png"):
                shutil.copyfile(folder / filename, eye_folder / filename)
            (eye_folder / "rig.json").write_text(json.dumps(eye_data), encoding="utf-8")
    # Re-encode the neutral image with different PNG metadata, so the alias
    # proves pixel matching rather than file-byte identity or filename lookup.
    alias = QImage(str(expressions / "neutral.png"))
    alias.setText("qa_encoding", "same pixels, independent PNG metadata")
    alias.save(str(expressions / "talk_closed.png"))
    return expressions, interactions, eyes


class AllExpressionPartTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="aura-all-parts-")
        self.root = Path(self.directory.name)
        self.expressions, self.interactions, self.eyes = make_expression_bank(self.root)
        self.images = {name: QPixmap(str(self.expressions / f"{name}.png")) for name in (*STATES, "talk_closed")}
        self.windows = []
        self.area = QRect(-1600, -300, 4096, 3000)

    def tearDown(self):
        for window in self.windows:
            window.close()
            window.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.directory.cleanup()

    def bank(self):
        with patch("desktop_pet.part_interaction.DEFAULT_RIG_PATH", self.interactions / "neutral/rig.json"), \
                patch("desktop_pet.part_interaction.DEFAULT_SOURCE_PATH", self.expressions / "neutral.png"):
            return PartInteraction()

    def eye_bank(self):
        with patch("desktop_pet.eye_tracking.DEFAULT_RIG_PATH", self.eyes / "neutral/rig.json"), \
                patch("desktop_pet.eye_tracking.DEFAULT_NEUTRAL_PATH", self.expressions / "neutral.png"):
            return EyeTracker()

    def window(self):
        with patch("desktop_pet.pet_window.PartInteraction", return_value=self.bank()), \
                patch("desktop_pet.pet_window.EyeTracker", return_value=self.eye_bank()), \
                patch("desktop_pet.pet_window.work_area", return_value=self.area):
            window = PetWindow(PetConfig(image_path=self.expressions / "neutral.png", animation=AnimationConfig(
                idle_amplitude=0, breathing_amplitude=0, happy_amplitude=0, surprised_amplitude=0)))
        window.refresh_timer.stop()
        window.move(-800, 80)
        self.windows.append(window)
        return window

    def test_registry_preloads_seven_trusted_sources_and_alias_uses_canonical_pixels(self):
        with patch("desktop_pet.part_interaction.load_interaction_rig", wraps=load_interaction_rig) as loader:
            bank = self.bank()
        self.assertEqual(loader.call_count, 7)
        self.assertEqual({Path(call.args[1]).stem for call in loader.call_args_list}, set(STATES))
        self.assertEqual(set(bank.rigs), set(STATES))
        self.assertTrue(bank.has_rigs)
        for name in STATES:
            bank.bind_source(self.images[name].toImage())
            self.assertTrue(bank.active)
            self.assertEqual(bank.rig_name, name)
            self.assertIs(bank.rig, bank.rigs[name])
        self.assertNotEqual((self.expressions / "neutral.png").read_bytes(), (self.expressions / "talk_closed.png").read_bytes())
        bank.bind_source(self.images["talk_closed"].toImage())
        self.assertEqual(bank.rig_name, "neutral")
        self.assertIs(bank.rig, bank.rigs["neutral"])

    def test_one_missing_and_one_corrupt_rig_leave_other_five_usable(self):
        (self.interactions / "surprised/rig.json").unlink()
        (self.interactions / "sleepy/rig.json").write_text("corrupt JSON", encoding="utf-8")
        bank = self.bank()
        healthy = set(STATES) - {"surprised", "sleepy"}
        self.assertEqual(set(bank.rigs), healthy)
        for name in STATES:
            bank.bind_source(self.images[name].toImage())
            self.assertEqual(bank.active, name in healthy, name)
            if name in healthy:
                self.assertTrue(bank.trigger("tail"))
                bank.advance(.08)
                self.assertNotEqual(bank.angle("tail"), 0)
            else:
                self.assertIsNone(bank.rig)
                self.assertFalse(bank.trigger("tail"))
        self.assertTrue(bank.has_rigs)

    def test_json_source_metadata_cannot_redirect_trusted_expression_provenance(self):
        path = self.interactions / "thinking/rig.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["source_file"] = str(self.expressions / "neutral.png")
        data["source_sha256"] = hashlib.sha256((self.expressions / "neutral.png").read_bytes()).hexdigest()
        path.write_text(json.dumps(data), encoding="utf-8")
        bank = self.bank()
        self.assertEqual(set(bank.rigs), set(STATES) - {"thinking"})
        bank.bind_source(self.images["thinking"].toImage())
        self.assertFalse(bank.active)
        bank.bind_source(self.images["happy"].toImage())
        self.assertTrue(bank.active)

    def test_switches_are_cached_reset_pending_motion_and_preserve_each_states_body(self):
        bank = self.bank()
        objects = {name: id(rig) for name, rig in bank.rigs.items()}
        with patch("desktop_pet.part_interaction.load_interaction_rig", side_effect=AssertionError("Switch rig read")), \
                patch("desktop_pet.part_interaction._read_image", side_effect=AssertionError("Switch image decode")), \
                patch.object(Path, "read_bytes", side_effect=AssertionError("Switch disk read")), \
                patch.object(Path, "read_text", side_effect=AssertionError("Switch JSON read")):
            for name in (*STATES, *reversed(STATES)):
                bank.bind_source(self.images[name].toImage())
                self.assertEqual(bank.rig_name, name)
                self.assertEqual(id(bank.rig), objects[name])
                self.assertFalse(bank.animating, "A source switch resumed another state's pending action")
                self.assertTrue(all(bank.angle(part) == 0 for part in POINTS))
                self.assertEqual(paint_parts(bank), self.images[name].toImage().convertToFormat(QImage.Format.Format_ARGB32_Premultiplied))
                bank.trigger("left_ear")
                bank.advance(.08)
                image = paint_parts(bank)
                self.assertEqual(image.pixelColor(500, 700), self.images[name].toImage().pixelColor(500, 700))
        unknown = self.images["neutral"].toImage().copy()
        unknown.setPixelColor(500, 700, QColor("#010101"))
        bank.bind_source(unknown)
        self.assertFalse(bank.active)
        self.assertIsNone(bank.rig)
        self.assertFalse(bank.animating)
        self.assertTrue(bank.has_rigs)

    def test_all_states_and_six_scales_hit_only_selected_part_on_negative_monitor(self):
        window = self.window()
        handled = []
        window.click_handler = handled.append
        with patch("desktop_pet.pet_window.work_area", return_value=self.area), \
                patch("desktop_pet.pet_window.screen_for_rect", return_value=None), \
                patch("desktop_pet.pet_window.screen_for_point", return_value=None):
            for name in STATES:
                window.set_character_source(self.images[name])
                for percent in (50, 75, 100, 125, 150, 200):
                    window.set_scale_percent(percent)
                    for part, source in POINTS.items():
                        with self.subTest(state=name, percent=percent, part=part):
                            window._part_interaction.reset()
                            next_body_click = window._next_click_happy
                            point = global_for_source(window, source)
                            send_mouse(window, QEvent.Type.MouseButtonPress, point, pressed=True)
                            send_mouse(window, QEvent.Type.MouseButtonRelease, point)
                            window._part_interaction.advance(.08)
                            self.assertNotEqual(window._part_interaction.angle(part), 0)
                            self.assertTrue(all(window._part_interaction.angle(other) == 0 for other in POINTS if other != part))
                            self.assertEqual(window._next_click_happy, next_body_click)
                            self.assertEqual(handled, [])
                            self.assertFalse(window.bubble.isVisible())

    def test_global_toggle_and_gaze_survive_every_state_including_closed_happy(self):
        window = self.window()
        target = window._eye_tracking.rig.face_center + QPointF(2200, -150)
        window._advance_eye_tracking(.2, global_for_source(window, target))
        gaze = window._eye_tracking.gaze
        window.set_part_interaction_enabled(False)
        with patch("desktop_pet.pet_window.work_area", return_value=self.area), \
                patch("desktop_pet.pet_window.screen_for_rect", return_value=None):
            for name in STATES:
                window.set_character_source(self.images[name])
                self.assertFalse(window.part_interaction_active)
                self.assertEqual(window._eye_tracking.gaze, gaze)
                window.set_part_interaction_enabled(True)
                self.assertTrue(window.part_interaction_active)
                self.assertEqual(window.eye_tracking_active, name != "happy")
                window.set_part_interaction_enabled(False)
            window.set_character_source(self.images["happy"])
        point = global_for_source(window, QPointF(500, 700)).toPoint()
        event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, window.mapFromGlobal(point), point)
        window.contextMenuEvent(event)
        action = next(action for action in window._menu.actions() if action.text() == "耳尾点击互动")
        self.assertFalse(action.isChecked())
        action.trigger()
        self.assertTrue(window.part_interaction_active)
        self.assertFalse(window.eye_tracking_active)
        window._menu.close()

    def test_drag_freezes_then_resumes_and_sequence_resets_each_state_without_leaks(self):
        window = self.window()
        handled = []
        window.click_handler = handled.append
        with patch("desktop_pet.pet_window.work_area", return_value=self.area), \
                patch("desktop_pet.pet_window.screen_for_rect", return_value=None), \
                patch("desktop_pet.pet_window.screen_for_point", return_value=None):
            for name in STATES:
                window.set_character_source(self.images[name])
                parts = window._part_interaction
                parts.trigger("tail")
                parts.advance(.08)
                frozen = parts.angle("tail")
                start = global_for_source(window, POINTS["left_ear"])
                send_mouse(window, QEvent.Type.MouseButtonPress, start, pressed=True)
                window._advance_frame(.1)
                self.assertEqual(parts.angle("tail"), frozen)
                end = start + QPointF(45, 30)
                send_mouse(window, QEvent.Type.MouseMove, end, pressed=True)
                send_mouse(window, QEvent.Type.MouseButtonRelease, end)
                window._advance_frame(.1)
                self.assertNotEqual(parts.angle("tail"), frozen)
                self.assertEqual(parts.angle("left_ear"), 0)
                window.set_frame_sequence((self.images[name].toImage(),))
                self.assertFalse(window.part_interaction_active)
                self.assertFalse(parts.animating)
                window._advance_frame(.1)
                self.assertEqual(parts.angle("tail"), 0)
                window.clear_frame_sequence()
                self.assertTrue(window.part_interaction_active)
                self.assertFalse(parts.animating)
        self.assertEqual(handled, [])

    def test_explicit_single_rig_interface_remains_isolated(self):
        bank = PartInteraction(self.interactions / "happy/rig.json", self.expressions / "happy.png")
        bank.bind_source(self.images["happy"].toImage())
        self.assertTrue(bank.active)
        bank.bind_source(self.images["neutral"].toImage())
        self.assertFalse(bank.active)
        bank.bind_source(self.images["happy"].toImage())
        self.assertTrue(bank.active)

    def test_full_local_and_empty_hit_masks_crop_without_changing_hit_coordinates(self):
        folder = self.interactions / "neutral"
        data = json.loads((folder / "rig.json").read_text(encoding="utf-8"))
        for part in data["parts"]:
            full = part["name"] != "right_ear"
            mask = QImage(QSize(1024, 1024) if full else QSize(160, 100),
                          QImage.Format.Format_ARGB32_Premultiplied)
            mask.fill(Qt.GlobalColor.transparent)
            painter = QPainter(mask)
            if part["name"] == "left_ear":
                painter.fillRect(QRect(245, 355, 30, 20), Qt.GlobalColor.white)
            elif part["name"] == "right_ear":
                painter.fillRect(QRect(50, 25, 40, 35), Qt.GlobalColor.white)
            painter.end()
            if part["name"] == "left_ear":
                mask.setPixelColor(260, 370, QColor(255, 255, 255, 1))
            elif part["name"] == "right_ear":
                mask.setPixelColor(75, 40, QColor(255, 255, 255, 1))
            filename = part["name"] + "_hit.png"
            self.assertTrue(mask.save(str(folder / filename)))
            part["hit_mask"] = filename
        (folder / "rig.json").write_text(json.dumps(data), encoding="utf-8")
        bank = self.bank()
        bank.bind_source(self.images["neutral"].toImage())
        parts = {part.name: part for part in bank.rig.parts}
        self.assertEqual(parts["left_ear"].hit_mask.size(), QSize(30, 20))
        self.assertEqual(parts["left_ear"].hit_origin, QPointF(245, 355))
        self.assertEqual(parts["right_ear"].hit_mask.size(), QSize(40, 35))
        self.assertEqual(parts["right_ear"].hit_origin, QPointF(715, 345))
        self.assertEqual(parts["tail"].hit_mask.size(), QSize(1, 1))
        for name, accepted, padding in (
                ("left_ear", QPointF(260.5, 370.5), QPointF(300.5, 370.5)),
                ("right_ear", QPointF(740.5, 360.5), QPointF(780.5, 370.5))):
            bank.reset()
            self.assertEqual(bank.hit_test(accepted), name)
            self.assertFalse(bank.hit_test(padding))
            bank.trigger(name)
            bank.advance(.08)
            transform = parts[name].transform(bank.angle(name))
            self.assertEqual(bank.hit_test(transform.map(accepted)), name)
            self.assertFalse(bank.hit_test(transform.map(padding)))
        bank.reset()
        self.assertFalse(bank.hit_test(POINTS["tail"]))
        self.assertTrue(bank.contains(POINTS["tail"]), "An empty click mask must not erase visible artwork")


if __name__ == "__main__":
    unittest.main()
