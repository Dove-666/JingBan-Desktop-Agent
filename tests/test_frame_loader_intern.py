"""Lossless per-load sharing preserves independent frame wrappers and errors."""
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage, QImageReader
from PySide6.QtWidgets import QApplication
from shiboken6 import VoidPtr

from desktop_pet import frame_animation as animation
from tests import test_frame_animation as fixture


class FrameLoaderInternTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)
    setUp = fixture.FrameAnimationTests.setUp
    tearDown = fixture.FrameAnimationTests.tearDown
    make_clip = fixture.FrameAnimationTests.make_clip
    write_manifest = fixture.FrameAnimationTests.write_manifest

    def paths(self):
        return self.clip_dir / "think_scratch_head/frames/0001.png", self.clip_dir / "think_scratch_head/frames/0002.png"

    def repeated(self):
        first, second = self.paths()
        shutil.copyfile(first, second)
        return first, second

    def load(self):
        return animation.load_clip(self.clip_dir, "think_scratch_head")

    def test_identical_payload_uses_shared_buffer_with_independent_wrappers(self):
        self.repeated()
        clip = self.load()
        self.assertEqual(len(clip.frames), 2)
        self.assertIsNot(clip.frames[0], clip.frames[1])
        self.assertEqual(clip.frames[0].cacheKey(), clip.frames[1].cacheKey())
        self.assertEqual(int(VoidPtr(clip.frames[0].constBits())), int(VoidPtr(clip.frames[1].constBits())))
        self.assertEqual(clip.manifest.fps, 12)
        self.assertEqual(clip.manifest.duration, 2 / 12)

    def test_shared_frame_mutation_detaches_and_preserves_other_slot(self):
        self.repeated()
        clip = self.load()
        previous = clip.frames[1].pixelColor(250, 250)
        clip.frames[0].setPixelColor(250, 250, QColor("#a83269"))
        clip.frames[0].setText("owned-note", "slot-zero")
        self.assertEqual(clip.frames[1].pixelColor(250, 250), previous)
        self.assertEqual(clip.frames[1].text("owned-note"), "")
        self.assertNotEqual(clip.frames[0].cacheKey(), clip.frames[1].cacheKey())

    def test_different_payload_metadata_and_pixels_stay_independent(self):
        first, second = self.repeated()
        image = QImage(str(second))
        image.setText("owned-note", "different metadata")
        self.assertTrue(image.save(str(second)))
        self.assertNotEqual(first.read_bytes(), second.read_bytes())
        clip = self.load()
        self.assertEqual(clip.frames[0].pixelColor(250, 250), clip.frames[1].pixelColor(250, 250))
        self.assertEqual(clip.frames[0].text("owned-note"), "")
        self.assertEqual(clip.frames[1].text("owned-note"), "different metadata")
        self.assertNotEqual(clip.frames[0].cacheKey(), clip.frames[1].cacheKey())

    def test_distinct_frames_and_new_loads_never_share_cached_bank(self):
        first = self.load()
        second = self.load()
        self.assertNotEqual(first.frames[0], first.frames[1])
        self.assertNotEqual(first.frames[0].cacheKey(), first.frames[1].cacheKey())
        self.assertNotEqual(first.frames[0].cacheKey(), second.frames[0].cacheKey())

    def test_filename_aware_qt_properties_match_original_reader_for_mixed_names(self):
        first, second = self.repeated()
        special = [first.parent / "frame@2x.png", first.parent / "frame@3x.png",
                   first.parent / "frame@2x.extra.png", first.parent / "parent@3x/frame.png"]
        for path in special:
            path.parent.mkdir(exist_ok=True)
            shutil.copyfile(first, path)
        paths = [first, *special, second]
        self.data.update(frame_count=len(paths), frames=[path.relative_to(first.parent.parent).as_posix() for path in paths])
        self.write_manifest(self.data)
        expected = [QImageReader(str(path), b"png").read().convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
                    for path in paths]
        clip = self.load()
        for index, (actual, original) in enumerate(zip(clip.frames, expected)):
            with self.subTest(index=index, name=paths[index].name):
                self.assertEqual(actual, original)
                self.assertEqual(actual.devicePixelRatio(), original.devicePixelRatio())
                self.assertEqual(actual.colorSpace(), original.colorSpace())
                self.assertEqual(actual.dotsPerMeterX(), original.dotsPerMeterX())
                self.assertEqual(actual.dotsPerMeterY(), original.dotsPerMeterY())
                self.assertEqual(actual.offset(), original.offset())
                self.assertEqual(actual.textKeys(), original.textKeys())
        self.assertEqual(clip.frames[0].cacheKey(), clip.frames[-1].cacheKey())
        self.assertNotEqual(clip.frames[0].cacheKey(), clip.frames[1].cacheKey())

    def test_each_regular_file_is_read_once_and_each_payload_decoded_once(self):
        self.repeated()
        opens, reads = [], []
        original_open = Path.open
        class Reader:
            def __init__(self, *args):
                self.reader = QImageReader(*args)
            def size(self):
                return self.reader.size()
            def read(self):
                reads.append(True)
                return self.reader.read()
        def opened(path, *args, **kwargs):
            if path.suffix == ".png":
                opens.append(path)
            return original_open(path, *args, **kwargs)
        with patch.object(Path, "open", opened), patch.object(animation, "QImageReader", Reader):
            clip = self.load()
        self.assertEqual(len(opens), 2)
        self.assertEqual(len(reads), 1)
        self.assertEqual(len(clip.frames), 2)

    def test_dimension_and_corrupt_payload_errors_are_preserved(self):
        first, _ = self.paths()
        for kind in ("small", "corrupt"):
            with self.subTest(kind=kind):
                if kind == "small":
                    image = QImage(16, 16, QImage.Format.Format_ARGB32)
                    image.fill(Qt.GlobalColor.transparent)
                    self.assertTrue(image.save(str(first)))
                else:
                    first.write_bytes(b"owned corrupt png")
                with self.assertRaisesRegex(animation.FrameAssetError, "必须统一为 1024"):
                    self.load()

    def test_alpha_corners_and_empty_frame_validation_are_preserved(self):
        first, second = self.paths()
        for kind in ("rgb", "corner", "empty"):
            with self.subTest(kind=kind):
                image = QImage(1024, 1024, QImage.Format.Format_RGB32 if kind == "rgb" else QImage.Format.Format_ARGB32)
                image.fill(Qt.GlobalColor.transparent)
                if kind == "corner":
                    image.setPixelColor(0, 0, QColor("red"))
                self.assertTrue(image.save(str(first)))
                shutil.copyfile(first, second)
                expected = {"rgb": "缺少透明通道", "corner": "四角必须透明", "empty": "完全透明"}[kind]
                with self.assertRaisesRegex(animation.FrameAssetError, expected):
                    self.load()

    def test_large_encoded_png_remains_supported_without_unbounded_intern_key(self):
        first, second = self.paths()
        raw = first.read_bytes()
        first.write_bytes(raw + b"x" * (4 * 1024 * 1024 + 64))
        shutil.copyfile(first, second)
        clip = self.load()
        self.assertEqual(clip.frames[0], clip.frames[1])
        self.assertNotEqual(clip.frames[0].cacheKey(), clip.frames[1].cacheKey())

    def test_compressed_key_budget_stays_bounded(self):
        first, _ = self.paths()
        png = first.read_bytes()
        paths = []
        for index, marker in enumerate((b"a", b"b", b"c", b"a", b"c")):
            path = first.parent / f"budget-{index}.png"
            path.write_bytes(png + marker * (1536 * 1024))
            paths.append(path)
        self.data.update(frame_count=len(paths), frames=["frames/" + path.name for path in paths])
        self.write_manifest(self.data)
        observed = []
        def profile(frame, event, value):
            if frame.f_code.co_filename == animation.__file__ and frame.f_code.co_name == "load_clip" and event == "return":
                retained = frame.f_locals.get("cached_payload_bytes")
                if retained is not None:
                    observed.append(retained)
        previous = sys.getprofile()
        try:
            sys.setprofile(profile)
            clip = self.load()
        finally:
            sys.setprofile(previous)
        self.assertEqual(len(observed), 1)
        self.assertLessEqual(observed[0], 4 * 1024 * 1024)
        self.assertGreater(observed[0], 3 * 1024 * 1024)
        self.assertEqual(len(clip.frames), 5)
        self.assertEqual(clip.frames[0].cacheKey(), clip.frames[3].cacheKey())
        self.assertNotEqual(clip.frames[2].cacheKey(), clip.frames[4].cacheKey())
        self.assertTrue(all(frame == clip.frames[0] for frame in clip.frames))

    def test_concurrent_growth_uses_bounded_read_then_original_reader(self):
        first, _ = self.paths()
        first.write_bytes(first.read_bytes() + b"x" * (4 * 1024 * 1024 + 64))
        original_stat, original_open = Path.stat, Path.open
        read_sizes = []
        def small_stat(path, *args, **kwargs):
            value = original_stat(path, *args, **kwargs)
            if path != first:
                return value
            fields = {name: getattr(value, name) for name in dir(value) if name.startswith("st_")}
            return SimpleNamespace(**dict(fields, st_size=1))
        class BoundedStream:
            def __init__(self, stream):
                self.stream = stream
            def __enter__(self):
                self.stream.__enter__()
                return self
            def __exit__(self, *args):
                return self.stream.__exit__(*args)
            def read(self, size):
                read_sizes.append(size)
                return self.stream.read(size)
        def opened(path, *args, **kwargs):
            stream = original_open(path, *args, **kwargs)
            return BoundedStream(stream) if path == first else stream
        with patch.object(Path, "stat", small_stat), patch.object(Path, "open", opened):
            clip = self.load()
        self.assertEqual(read_sizes, [4 * 1024 * 1024 + 1])
        self.assertEqual(len(clip.frames), 2)

    def test_manifest_path_validation_still_precedes_pixel_loading(self):
        self.data["frames"][1] = "../outside.png"
        self.write_manifest(self.data)
        with patch.object(animation, "QImageReader") as reader:
            with self.assertRaises(animation.FrameAssetError):
                self.load()
        reader.assert_not_called()


if __name__ == "__main__":
    unittest.main()
