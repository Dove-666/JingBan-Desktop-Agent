"""Canvas processing preserves the source and never draws replacement art."""

import hashlib
from pathlib import Path
import tempfile
import unittest

from PIL import Image, ImageDraw

from tools.asset_pipeline.static_assets import normalize_static


class StaticAssetLayoutTests(unittest.TestCase):
    def test_aspect_ratio_transparency_and_bottom_anchor_preserve_source(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "source.png", Path(directory) / "output.png"
            image = Image.new("RGBA", (640, 480), (0, 0, 0, 0))
            ImageDraw.Draw(image).rectangle((100, 50, 499, 249), fill=(40, 90, 220, 255))
            image.save(source)
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            report = normalize_static(source, output)
            with Image.open(output) as result:
                self.assertEqual(result.size, (1024, 1024))
                self.assertEqual(result.mode, "RGBA")
                self.assertEqual(result.getpixel((0, 0))[3], 0)
                bounds = report["visible_bounds"]
                self.assertAlmostEqual((bounds[2] - bounds[0]) / (bounds[3] - bounds[1]), 2, delta=.03)
                self.assertAlmostEqual(bounds[3], 960, delta=2)
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), digest)

    def test_rgb_or_empty_input_is_rejected_without_fake_alpha(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "source.png", Path(directory) / "output.png"
            for image in (Image.new("RGB", (100, 100)), Image.new("RGBA", (100, 100))):
                image.save(source)
                with self.assertRaises(ValueError):
                    normalize_static(source, output)
                self.assertFalse(output.exists())

    def test_cannot_overwrite_source(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.png"
            Image.new("RGBA", (20, 20), (0, 10, 20, 255)).save(source)
            before = source.read_bytes()
            with self.assertRaises(ValueError):
                normalize_static(source, source)
            self.assertEqual(source.read_bytes(), before)
