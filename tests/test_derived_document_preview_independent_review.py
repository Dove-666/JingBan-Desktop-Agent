"""Independent codec boundary review; generated pixels, no models or rendering."""
import base64
import hashlib
import io
import json
import random
import struct
import threading
import unittest
from unittest.mock import patch
import warnings
import zlib

from PIL import Image, PngImagePlugin
from agent_backend.nanobot import derived_document_preview as subject


def png(image, **kwargs):
    stream = io.BytesIO(); image.save(stream, format="PNG", **kwargs); return stream.getvalue()


def changed_animation_count(raw, count):
    changed = bytearray(raw)
    offset = 8
    while offset < len(changed):
        size = struct.unpack_from(">I", changed, offset)[0]
        if changed[offset+4:offset+8] == b"acTL":
            struct.pack_into(">I", changed, offset+8, count)
            struct.pack_into(">I", changed, offset+8+size,
                zlib.crc32(changed[offset+4:offset+8+size]) & 0xffffffff)
            return bytes(changed)
        offset += 12 + size
    raise AssertionError("Owned APNG fixture lacks animation control")


class DerivedPreviewIndependentReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with Image.frombytes("RGB", (1000, 700), random.Random(13751).randbytes(1000*700*3)) as image:
            cls.large_png = png(image)

    def test_animation_cannot_be_hidden_by_malformed_declared_frame_count(self):
        with Image.new("RGB", (16, 12), "red") as first, Image.new("RGB", (16, 12), "blue") as second:
            animated = png(first, save_all=True, append_images=[second], duration=100, loop=0)
        self.assertEqual(2, animated.count(b"fcTL"))
        # Both streams retain the two actual frame controls and valid chunk
        # CRCs. Pillow's is_animated alone sees 0/1 as a static-image fallback.
        for count in (0, 1):
            with self.subTest(declared_frames=count), warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                with self.assertRaisesRegex(subject.DerivedPreviewError, "DOCUMENT_DERIVED_PREVIEW_INVALID"):
                    subject.derive_document_preview(changed_animation_count(animated, count))

    def test_chunk_payload_containing_animation_word_is_still_a_valid_static_png(self):
        info = PngImagePlugin.PngInfo()
        info.add_text("Description", "Literal acTL fcTL fdAT are ordinary text, not PNG chunk types.")
        with Image.new("RGB", (12, 10), "green") as image: raw = png(image, pnginfo=info)
        result = subject.derive_document_preview(raw)
        self.assertEqual(raw, result.image_bytes)
        self.assertFalse(result.derived)

    def test_large_png_metadata_orientation_and_profiles_are_not_copied_to_jpeg_or_public_metadata(self):
        marker = "SYNTHETIC_PRIVATE_PNG_METADATA_13751"
        info = PngImagePlugin.PngInfo(); info.add_text("Description", marker)
        exif = Image.Exif(); exif[274] = 6
        with Image.open(io.BytesIO(self.large_png)) as image:
            raw = png(image, pnginfo=info, exif=exif, icc_profile=marker.encode())
        original_sha = hashlib.sha256(raw).hexdigest()
        result = subject.derive_document_preview(raw)
        self.assertTrue(result.derived)
        self.assertEqual(original_sha, hashlib.sha256(raw).hexdigest())
        with Image.open(io.BytesIO(result.image_bytes)) as decoded:
            self.assertEqual("JPEG", decoded.format)
            self.assertFalse(decoded.getexif())
            self.assertNotIn("icc_profile", decoded.info)
            self.assertEqual((1000, 700), (result.source_width, result.source_height))
            self.assertEqual((result.derived_width, result.derived_height), decoded.size)
            self.assertEqual(decoded.width * 700, decoded.height * 1000, "No silent EXIF rotation or crop")
        self.assertNotIn(marker.encode(), result.image_bytes)
        self.assertNotIn(marker, json.dumps(result.metadata()))

    def test_palette_alpha_is_matted_on_white_and_sent_metadata_matches_decoded_wire(self):
        generator = random.Random(13752)
        indices = bytearray(generator.randbytes(1100*800))
        for row in range(70): indices[row*1100:row*1100+70] = b"\x00"*70
        with Image.frombytes("P", (1100, 800), bytes(indices)) as image:
            image.putpalette([value for index in range(256) for value in (index, (index*3)%256, (index*7)%256)])
            raw = png(image, transparency=b"\x00" + b"\xff"*255)
        self.assertGreater(len(raw), 750000)
        result = subject.derive_document_preview(raw)
        self.assertTrue(result.derived)
        prefix, encoded = result.data_url.split(",", 1)
        sent = base64.b64decode(encoded, validate=True)
        self.assertEqual("data:image/jpeg;base64", prefix)
        self.assertLessEqual(len(result.data_url), 1_000_000)
        self.assertEqual(hashlib.sha256(sent).hexdigest(), result.metadata()["sent_image_sha256"])
        self.assertEqual(hashlib.sha256(raw).hexdigest(), result.metadata()["original_png_sha256"])
        with Image.open(io.BytesIO(sent)) as decoded:
            self.assertEqual((result.derived_width, result.derived_height), decoded.size)
            self.assertTrue(all(channel >= 245 for channel in decoded.getpixel((20,20))))
        # A caller's metadata dictionary is not a mutable handle to host state.
        untrusted = result.metadata(); untrusted["sent_image_sha256"] = "forged"
        self.assertEqual(hashlib.sha256(sent).hexdigest(), result.metadata()["sent_image_sha256"])

    def test_stop_after_actual_pixel_decode_emits_no_derived_bytes_or_encoder_attempt(self):
        stopped = threading.Event()
        decode = PngImagePlugin.PngImageFile.load
        def stop_after_decode(image, *args, **kwargs):
            value = decode(image, *args, **kwargs)
            stopped.set()
            return value
        with patch.object(PngImagePlugin.PngImageFile, "load", stop_after_decode), \
             patch.object(subject._BoundedOutput, "write", side_effect=AssertionError("No cancelled encoding")):
            with self.assertRaisesRegex(subject.DerivedPreviewError, "DOCUMENT_DERIVED_PREVIEW_CANCELLED"):
                subject.derive_document_preview(self.large_png, cancel=stopped)


if __name__ == "__main__": unittest.main()
