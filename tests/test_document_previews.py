"""Preview capability/version bounds, not a visual-quality judgment."""
import hashlib
from pathlib import Path
import struct
import tempfile
import unittest
import zlib

from agent_backend.nanobot.document_previews import PagePreviewCache, PreviewError
from agent_backend.nanobot.safe_files import WorkspaceFiles


def png(width=1, height=1):
    def chunk(kind, data):
        return struct.pack(">I", len(data))+kind+data+struct.pack(">I", zlib.crc32(kind+data) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n"+chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        +chunk(b"IDAT", zlib.compress(b"\0\xff\xff\xff"))+chunk(b"IEND", b""))


class DocumentPreviewTests(unittest.TestCase):
    def setUp(self):
        owned = tempfile.TemporaryDirectory(prefix="aura-page-reference-"); self.addCleanup(owned.cleanup)
        self.root = Path(owned.name); self.files = WorkspaceFiles(self.root)
        self.pdf = b"%PDF-1.7 owned synthetic version A"
        (self.root/"owned.pdf").write_bytes(self.pdf)
        self.digest = hashlib.sha256(self.pdf).hexdigest()
        self.cache = PagePreviewCache(self.files)

    def publish(self): return self.cache.publish("owned.pdf", self.digest, (png(),), pages=1)[0]["reference"]

    def test_page_metadata_contains_no_image_data_and_foreign_cache_cannot_read_reference(self):
        reference = self.publish(); image = self.cache.image(reference)
        self.assertEqual((1, 1), (image.width, image.height))
        self.assertTrue(image.data_url.startswith("data:image/png;base64,"))
        with self.assertRaises(PreviewError): PagePreviewCache(self.files).image(reference)
        with self.assertRaises(PreviewError): self.cache.image({"reference": reference})

    def test_changed_or_removed_pdf_prevents_access_to_the_old_image(self):
        reference = self.publish()
        (self.root/"owned.pdf").write_bytes(b"%PDF-1.7 DIFFERENT")
        with self.assertRaisesRegex(PreviewError, "DOCUMENT_PREVIEW_STALE"): self.cache.image(reference)
        (self.root/"owned.pdf").unlink()
        with self.assertRaisesRegex(PreviewError, "DOCUMENT_PREVIEW_UNAVAILABLE"): self.cache.image(reference)

    def test_revoked_external_read_grant_does_not_leave_a_preview_capability(self):
        with tempfile.TemporaryDirectory(prefix="aura-external-preview-") as other:
            path = Path(other)/"external.pdf"; path.write_bytes(self.pdf)
            self.files.add_grant({"path": str(path), "access": "read", "directory": False})
            reference = self.cache.publish(str(path), self.digest, [png()], pages=1)[0]["reference"]
            self.files._read_roots.clear()
            with self.assertRaises(PreviewError): self.cache.image(reference)

    def test_bounded_eviction_removes_old_references_without_writing_any_extra_files(self):
        cache = PagePreviewCache(self.files, max_bytes=len(png()), max_pages=1)
        old = cache.publish("owned.pdf", self.digest, [png()], pages=1)[0]["reference"]
        new = cache.publish("owned.pdf", self.digest, [png()], pages=1)[0]["reference"]
        self.assertLessEqual(cache.retained_bytes, len(png()))
        with self.assertRaises(PreviewError): cache.image(old)
        self.assertEqual(new, cache.image(new).reference)
        self.assertEqual({"owned.pdf"}, {p.name for p in self.root.iterdir()})

    def test_bad_crc_truncation_huge_dimensions_and_wrong_page_count_never_publish(self):
        for pages, raw in ((1, png()[:-1]), (1, png()[:-5]+b"WRONG"), (1, png(width=1801)), (2, png())):
            with self.subTest(pages=pages, size=len(raw)), self.assertRaises(PreviewError):
                self.cache.publish("owned.pdf", self.digest, [raw], pages=pages)
        self.assertEqual(0, self.cache.retained_bytes)


if __name__ == "__main__": unittest.main()
