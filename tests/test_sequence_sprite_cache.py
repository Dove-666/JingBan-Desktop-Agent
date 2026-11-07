"""Hidden real pet: bounded display surfaces preserve every source/time slot."""
import gc
import hashlib
from pathlib import Path
import tempfile
import unittest
import weakref
from unittest.mock import patch

from PySide6.QtCore import QEvent, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication

from desktop_pet.config import PetConfig
from desktop_pet import pet_window


def frame(left=8, top=6, width=35, height=51):
    image = QImage(64, 64, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.fillRect(left, top, width, height, QColor(60, 150, 210, 220))
    painter.end()
    return image


class SequenceSpriteCacheTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)
        cls.owned = tempfile.TemporaryDirectory(prefix="aura-sequence-cache-")
        path = Path(cls.owned.name) / "owned-avatar.png"
        image = frame()
        if not image.save(str(path), "PNG"): raise AssertionError("OWNED_AVATAR_SAVE_FAILED")
        cls.pet = pet_window.PetWindow(PetConfig(image_path=path, character_size=180))
        cls.pet.refresh_timer.stop()

    @classmethod
    def tearDownClass(cls):
        cls.pet.close(); cls.pet.deleteLater()
        cls.app.sendPostedEvents(None, QEvent.Type.DeferredDelete); cls.app.processEvents()
        target = Path(cls.owned.name).resolve()
        assert target.parent == Path(tempfile.gettempdir()).resolve() and target.name.startswith("aura-sequence-cache-")
        cls.owned.cleanup()

    def setUp(self):
        self.pet.clear_frame_sequence()
        self.pet.set_scale_percent(100)
        self.pet.animation.return_to_idle()
        self.assertFalse(self.pet.isVisible())

    def tearDown(self):
        self.pet.clear_frame_sequence()
        self.assertFalse(self.pet.isVisible())

    def test_shared_source_slots_use_one_surface_and_one_alpha_measurement(self):
        image = frame()
        frames = (image,) * 48
        alpha = pet_window._alpha_bounds
        with patch.object(pet_window, "_alpha_bounds", wraps=alpha) as measure:
            self.pet.set_frame_sequence(frames)
            self.assertEqual(measure.call_count, 1)
        self.assertIs(self.pet._sequence_frames, frames)
        self.assertEqual(len(self.pet._sequence_sprites), 48)
        self.assertEqual(len({hit.cacheKey() for _pixmap, hit in self.pet._sequence_sprites}), 1)
        self.assertEqual(len({pixmap.cacheKey() for pixmap, _hit in self.pet._sequence_sprites}), 1)
        self.assertEqual(len(self.pet._sequence_alpha_rects), 48)

    def test_implicit_qimage_copy_uses_the_same_surface(self):
        image = frame(); implicit = QImage(image)
        self.assertEqual(image.cacheKey(), implicit.cacheKey())
        self.pet.set_frame_sequence((image, implicit, image, implicit))
        sprites = self.pet._sequence_sprites
        self.assertEqual(len(sprites), 4)
        self.assertEqual(len({hit.cacheKey() for _pixmap, hit in sprites}), 1)

    def test_detached_source_uses_different_surface_and_alpha_without_mutating_original(self):
        image = frame(); digest = hashlib.sha256(image.constBits()).hexdigest()
        detached = QImage(image); detached.fill(Qt.GlobalColor.transparent)
        painter = QPainter(detached); painter.fillRect(25, 20, 14, 16, QColor("#df88a0")); painter.end()
        self.assertNotEqual(image.cacheKey(), detached.cacheKey())
        self.pet.set_frame_sequence((image, detached, image, detached))
        self.assertEqual(len({hit.cacheKey() for _pixmap, hit in self.pet._sequence_sprites}), 2)
        self.assertNotEqual(self.pet._sequence_alpha_rects[0], self.pet._sequence_alpha_rects[1])
        self.assertEqual(hashlib.sha256(image.constBits()).hexdigest(), digest)
        for index, source in enumerate(self.pet._sequence_frames):
            expected = source.scaled(self.pet._sprite.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                     Qt.TransformationMode.SmoothTransformation)
            self.assertEqual(self.pet._sequence_sprites[index][1], expected)
            self.assertEqual(self.pet._sequence_alpha_rects[index], pet_window._alpha_bounds(expected))

    def test_equal_pixels_with_different_source_keys_are_not_content_deduplicated(self):
        image = frame(); independent = image.copy()
        self.assertEqual(image, independent)
        self.assertNotEqual(image.cacheKey(), independent.cacheKey())
        self.pet.set_frame_sequence((image, independent, image, independent))
        self.assertEqual(len({hit.cacheKey() for _pixmap, hit in self.pet._sequence_sprites}), 2)

    def test_slot_pixels_alpha_timing_and_selected_indices_match_unoptimized_scaling(self):
        first, second = frame(), frame(20, 12, 25, 33)
        frames = (first, second, QImage(first), second, first)
        times = (0.0, .03, .11, .17, .29)
        original_hashes = [hashlib.sha256(image.constBits()).hexdigest() for image in frames]
        self.pet.set_frame_sequence(frames, (.48, .92), frame_times=times)
        self.assertIs(self.pet._sequence_frames, frames)
        self.assertEqual(self.pet._sequence_times, times)
        self.assertEqual(self.pet._sequence_anchor, (.48, .92))
        for index in (4, 1, 2, 0, 3):
            source = frames[index]
            expected = source.scaled(self.pet._sprite.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                     Qt.TransformationMode.SmoothTransformation)
            self.assertEqual(self.pet._sequence_sprites[index][1], expected)
            self.assertEqual(self.pet._sequence_sprites[index][0].toImage(), expected)
            self.assertEqual(self.pet._sequence_alpha_rects[index], pet_window._alpha_bounds(expected))
            self.pet.set_frame_index(index)
            self.assertEqual(self.pet._sequence_index, index)
            self.assertEqual(self.pet._hit_image, expected)
            self.assertEqual(self.pet._visible_alpha_rect, self.pet._sequence_alpha_rects[index])
        self.assertEqual([hashlib.sha256(image.constBits()).hexdigest() for image in frames], original_hashes)

    def test_scale_rebuild_releases_previous_surfaces_and_preserves_source_slots(self):
        image = frame(); frames = (image,) * 12
        self.pet.set_frame_sequence(frames)
        old_size = self.pet._sprite.size()
        previous = [weakref.ref(item) for pair in self.pet._sequence_sprites for item in pair]
        self.pet.set_scale_percent(150); gc.collect()
        self.assertNotEqual(self.pet._sprite.size(), old_size)
        self.assertIs(self.pet._sequence_frames, frames)
        self.assertTrue(all(ref() is None for ref in previous))
        self.assertEqual(len({hit.cacheKey() for _pixmap, hit in self.pet._sequence_sprites}), 1)
        self.assertEqual(len(self.pet._sequence_sprites), 12)

    def test_clear_and_new_clip_do_not_retain_a_global_source_or_scale_cache(self):
        image = frame(); self.pet.set_frame_sequence((image,) * 6)
        keys = {hit.cacheKey() for _pixmap, hit in self.pet._sequence_sprites}
        previous = [weakref.ref(item) for pair in self.pet._sequence_sprites for item in pair]
        self.pet.clear_frame_sequence(); gc.collect()
        self.assertTrue(all(ref() is None for ref in previous))
        self.assertEqual(self.pet._sequence_frames, ())
        self.assertEqual(self.pet._sequence_sprites, ())
        self.assertEqual(self.pet._sequence_alpha_rects, ())
        self.pet.set_frame_sequence((image,) * 4)
        self.assertFalse(keys & {hit.cacheKey() for _pixmap, hit in self.pet._sequence_sprites})
        self.assertEqual(len(self.pet._sequence_sprites), 4)


if __name__ == "__main__": unittest.main()
