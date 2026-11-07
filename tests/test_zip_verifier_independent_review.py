"""Independent raw ZIP counterexamples; no extraction or member execution."""
import hashlib
import io
from pathlib import Path
import struct
import sys
import tempfile
import unittest
import zipfile
import zlib

BACKEND = Path(__file__).resolve().parents[1] / "agent_backend/nanobot"
sys.path.insert(0, str(BACKEND))
from artifact_verifier import ArtifactVerifier
from safe_files import MAX_DOCUMENT_BYTES


def package(payload, compression=zipfile.ZIP_DEFLATED):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=compression) as archive:
        archive.writestr("owned.bin", payload)
    return output.getvalue()


def claimed_length(raw, payload, length):
    wire = bytearray(raw)
    central = wire.index(b"PK\x01\x02")
    crc = zlib.crc32(payload[:length]) & 0xffffffff
    for at in (14, central + 16):
        struct.pack_into("<I", wire, at, crc)
    for at in (22, central + 24):
        struct.pack_into("<I", wire, at, length)
    return bytes(wire)


def compressed_bytes(raw, *, trailing=b"", remove=0):
    wire = bytearray(raw)
    old_central = wire.index(b"PK\x01\x02")
    size = struct.unpack_from("<I", wire, 18)[0]
    name_length, extra_length = struct.unpack_from("<HH", wire, 26)
    end = 30 + name_length + extra_length + size
    assert end == old_central and 0 <= remove < size
    wire[end - remove:end] = trailing
    difference = len(trailing) - remove
    central = old_central + difference
    for at in (18, central + 20):
        struct.pack_into("<I", wire, at, size + difference)
    eocd = wire.rfind(b"PK\x05\x06")
    struct.pack_into("<I", wire, eocd + 16, central)
    return bytes(wire)


class ZipVerifierIndependentReviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="zip-raw-review-")
        self.root = Path(self.temporary.name)
        self.verifier = ArtifactVerifier(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def verify(self, raw):
        (self.root / "owned.zip").write_bytes(raw)
        result = self.verifier.verify("owned.zip")
        self.assertFalse(result["task_fully_verified"])
        return result

    def test_nonempty_stream_cannot_claim_zero_bytes_and_empty_crc(self):
        payload = b"OWNED_NONEMPTY_PAYLOAD" * 30
        for method in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            with self.subTest(method=method):
                self.assertFalse(self.verify(claimed_length(package(payload, method), payload, 0))["passed"])

    def test_only_prefix_crc_cannot_verify_an_unread_suffix(self):
        payload = b"OWNED_PREFIX" + b"UNVERIFIED_SUFFIX" * 30
        for method in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            with self.subTest(method=method):
                self.assertFalse(self.verify(claimed_length(package(payload, method), payload, len(b"OWNED_PREFIX")))["passed"])

    def test_actual_expansion_limit_cannot_be_hidden_by_zero_metadata(self):
        payload = b"Q" * (MAX_DOCUMENT_BYTES + 1)
        self.assertFalse(self.verify(claimed_length(package(payload), payload, 0))["passed"])

    def test_deflate_requires_real_end_of_stream(self):
        raw = package(b"a" * 10000)
        self.assertFalse(self.verify(compressed_bytes(raw, remove=1))["passed"])

    def test_compressed_region_cannot_hide_trailing_bytes_after_deflate_eof(self):
        raw = package(b"OWNED_CRC_DATA")
        self.assertFalse(self.verify(compressed_bytes(raw, trailing=b"OWNED_HIDDEN_TRAILER"))["passed"])

    def test_valid_streamed_data_descriptor_keeps_exact_member_hashes(self):
        class Unseekable(io.BytesIO):
            def seek(self, *_):
                raise io.UnsupportedOperation("Owned streaming writer")
        output = Unseekable()
        payload = b"OWNED_STREAMED_DATA" * 100
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("owned.bin", payload)
        raw = output.getvalue()
        self.assertTrue(struct.unpack_from("<H", raw, 6)[0] & 8)
        result = self.verify(raw)
        self.assertTrue(result["passed"], result)
        self.assertEqual("zip_structure_crc_only", result["scope"])
        self.assertEqual([{"name": "owned.bin", "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}], result["zip_members"])
        self.assertEqual(raw, (self.root / "owned.zip").read_bytes())
        self.assertEqual({"owned.zip"}, {p.name for p in self.root.iterdir()})


if __name__ == "__main__":
    unittest.main()
