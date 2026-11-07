"""Bounded pure-host derivation. Synthetic pixels only, no models or file IO."""
from dataclasses import FrozenInstanceError
import hashlib
import io
from pathlib import Path
import random
import struct
import threading
import unittest
from unittest.mock import patch
import zlib

from PIL import Image
from agent_backend.nanobot import derived_document_preview as derived


def png_image(image):
    output = io.BytesIO(); image.save(output, format="PNG"); return output.getvalue()


def noise(width=1200, height=800):
    generator = random.Random(75319)
    return Image.frombytes("RGB", (width, height), generator.randbytes(width * height * 3))


class DerivedDocumentPreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with noise() as image:
            # Large corner markers make full-canvas preservation observable.
            for rectangle, color in (((0, 0, 80, 80), "red"), ((1120, 0, 1200, 80), "green"),
                                      ((0, 720, 80, 800), "blue"), ((1120, 720, 1200, 800), "yellow")):
                image.paste(color, rectangle)
            cls.large_png = png_image(image)
        with Image.new("RGBA", (24, 16), (255, 0, 0, 128)) as image: cls.small_png = png_image(image)

    def test_small_png_is_exact_and_metadata_never_contains_data_url_or_bytes(self):
        result = derived.derive_document_preview(self.small_png)
        self.assertFalse(result.derived)
        self.assertEqual("image/png", result.content_type)
        self.assertEqual(self.small_png, result.image_bytes)
        self.assertEqual(result.original_png_sha256, result.sent_image_sha256)
        self.assertEqual((24, 16), (result.derived_width, result.derived_height))
        self.assertEqual(0, result.attempts)
        self.assertEqual("original_png_exact_v1", result.transform)
        self.assertNotIn("data_url", result.metadata())
        self.assertNotIn("_image_bytes", result.metadata())
        self.assertNotIn("\\x89PNG", repr(result))
        with self.assertRaises(FrozenInstanceError): result.derived = True

    def test_high_entropy_legal_png_is_explicit_jpeg_under_sdk_budget(self):
        before = hashlib.sha256(self.large_png).hexdigest()
        self.assertGreater(22 + 4 * ((len(self.large_png) + 2) // 3), 1_000_000)
        result = derived.derive_document_preview(self.large_png)
        self.assertTrue(result.derived)
        self.assertEqual("image/jpeg", result.content_type)
        self.assertTrue(result.image_bytes.startswith(b"\xff\xd8"))
        self.assertLessEqual(len(result.data_url), 1_000_000)
        self.assertEqual(before, result.original_png_sha256)
        self.assertEqual(before, hashlib.sha256(self.large_png).hexdigest())
        self.assertNotEqual(result.original_png_sha256, result.sent_image_sha256)
        self.assertEqual(hashlib.sha256(result.image_bytes).hexdigest(), result.sent_image_sha256)
        self.assertTrue(1 <= result.attempts <= len(derived.JPEG_TIERS))
        self.assertIn("white_matte_full_canvas", result.transform)
        self.assertFalse(result.metadata()["visual_layout_verified"])
        self.assertTrue(result.metadata()["reference_only"])

    def test_full_canvas_preserves_aspect_ratio_and_all_four_corners_without_crop(self):
        result = derived.derive_document_preview(self.large_png)
        self.assertLessEqual(result.derived_width, result.source_width)
        self.assertLessEqual(result.derived_height, result.source_height)
        self.assertLessEqual(abs(result.derived_width * result.source_height - result.derived_height * result.source_width),
                             max(result.source_width, result.source_height))
        with Image.open(io.BytesIO(result.image_bytes)) as output:
            self.assertEqual((result.derived_width, result.derived_height), output.size)
            dx, dy = max(2, round(output.width * .025)), max(2, round(output.height * .025))
            corners = [output.getpixel(point) for point in ((dx, dy), (output.width-dx-1, dy),
                (dx, output.height-dy-1), (output.width-dx-1, output.height-dy-1))]
            self.assertGreater(corners[0][0], 180); self.assertLess(corners[0][1], 60)
            self.assertGreater(corners[1][1], 70); self.assertLess(corners[1][0], 60)
            self.assertGreater(corners[2][2], 180); self.assertLess(corners[2][0], 60)
            self.assertGreater(min(corners[3][:2]), 180); self.assertLess(corners[3][2], 60)

    def test_transparency_is_composited_onto_white_without_changing_original_png(self):
        with noise() as source, source.convert("RGBA") as rgba, Image.new("L", source.size, 0) as alpha:
            alpha.paste(255, (400, 260, 800, 540)); rgba.putalpha(alpha)
            original = png_image(rgba)
        self.assertGreater(len(original), 750000)
        result = derived.derive_document_preview(original)
        self.assertTrue(result.derived)
        with Image.open(io.BytesIO(result.image_bytes)) as output:
            self.assertEqual("RGB", output.mode)
            self.assertTrue(all(channel >= 245 for channel in output.getpixel((5, 5))))
            self.assertNotIn("transparency", output.info)
        with Image.open(io.BytesIO(original)) as untouched: self.assertEqual(0, untouched.getpixel((0, 0))[3])

    def test_corrupt_pixels_crc_and_non_png_are_fixed_rejections(self):
        def chunk(kind, payload):
            return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xffffffff)
        damaged = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", b"NOT_VALID_ZLIB") + chunk(b"IEND", b""))
        for raw in (self.small_png[:-1], self.small_png[:-5] + b"WRONG", b"NOT_PNG", damaged):
            with self.subTest(size=len(raw)), self.assertRaisesRegex(derived.DerivedPreviewError, "DOCUMENT_DERIVED_PREVIEW_INVALID"):
                derived.derive_document_preview(raw)

    def test_input_byte_and_dimension_limits_are_checked_before_decoder(self):
        with Image.new("RGB", (1801, 1), "white") as image: too_wide = png_image(image)
        with patch.object(derived, "_load_pillow", side_effect=AssertionError("Must fail before decoder")):
            for raw in (b"X" * (derived.MAX_INPUT_BYTES + 1), too_wide):
                with self.assertRaisesRegex(derived.DerivedPreviewError, "DOCUMENT_DERIVED_PREVIEW_INPUT_LIMIT"):
                    derived.derive_document_preview(raw)

    def test_animated_png_is_not_silently_reduced_to_one_frame(self):
        with Image.new("RGB", (20, 20), "red") as one, Image.new("RGB", (20, 20), "blue") as two:
            stream = io.BytesIO(); one.save(stream, format="PNG", save_all=True, append_images=[two], duration=100, loop=0)
        with self.assertRaisesRegex(derived.DerivedPreviewError, "DOCUMENT_DERIVED_PREVIEW_INVALID"):
            derived.derive_document_preview(stream.getvalue())

    def test_each_animation_control_chunk_is_rejected_before_decoder(self):
        for kind in (b"acTL", b"fcTL", b"fdAT"):
            # Even an isolated malformed animation control is not a static PNG.
            chunk = struct.pack(">I", 0) + kind + struct.pack(">I", zlib.crc32(kind) & 0xffffffff)
            raw = self.small_png[:33] + chunk + self.small_png[33:]
            with self.subTest(kind=kind), \
                 patch.object(derived, "_load_pillow", side_effect=AssertionError("Must reject before Pillow")), \
                 self.assertRaisesRegex(derived.DerivedPreviewError, "DOCUMENT_DERIVED_PREVIEW_INVALID"):
                derived.derive_document_preview(raw)

    def test_missing_pillow_is_fixed_unavailable_without_install_or_fallback(self):
        with patch.object(derived, "_load_pillow", side_effect=derived.DerivedPreviewError("DOCUMENT_DERIVED_PREVIEW_UNAVAILABLE")):
            with self.assertRaisesRegex(derived.DerivedPreviewError, "DOCUMENT_DERIVED_PREVIEW_UNAVAILABLE"):
                derived.derive_document_preview(self.small_png)

    def test_output_limit_exhaustion_is_fixed_and_attempts_are_bounded(self):
        writes = []
        def too_large(*args): writes.append(1); raise derived._CandidateTooLarge()
        with patch.object(derived._BoundedOutput, "write", side_effect=too_large):
            with self.assertRaisesRegex(derived.DerivedPreviewError, "DOCUMENT_DERIVED_PREVIEW_OUTPUT_LIMIT"):
                derived.derive_document_preview(self.large_png)
        self.assertGreater(len(writes), 0)
        self.assertLessEqual(len(writes), len(derived.JPEG_TIERS))

    def test_cancel_before_or_during_encode_returns_no_image_and_never_starts_a_request(self):
        stopped = threading.Event(); stopped.set()
        with patch.object(derived, "_load_pillow", side_effect=AssertionError("No cancelled decode")):
            with self.assertRaisesRegex(derived.DerivedPreviewError, "DOCUMENT_DERIVED_PREVIEW_CANCELLED"):
                derived.derive_document_preview(self.large_png, cancel=stopped)
        stopped.clear(); write = derived._BoundedOutput.write
        def stop_after_first_chunk(target, value): stopped.set(); return write(target, value)
        with patch.object(derived._BoundedOutput, "write", stop_after_first_chunk):
            with self.assertRaisesRegex(derived.DerivedPreviewError, "DOCUMENT_DERIVED_PREVIEW_CANCELLED"):
                derived.derive_document_preview(self.large_png, cancel=stopped)

    def test_conversion_never_opens_files_network_or_child_processes(self):
        with patch("builtins.open", side_effect=AssertionError("No file IO")), \
             patch("pathlib.Path.write_bytes", side_effect=AssertionError("No files")), \
             patch("socket.socket", side_effect=AssertionError("No network")), \
             patch("subprocess.Popen", side_effect=AssertionError("No process")):
            result = derived.derive_document_preview(self.large_png)
        self.assertTrue(result.derived)


if __name__ == "__main__": unittest.main()
