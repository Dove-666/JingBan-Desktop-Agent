"""Read-only ZIP facts over owned bytes; members are never extracted/executed."""
from __future__ import annotations

import hashlib
import io
import itertools
import json
from pathlib import Path
import stat
import struct
import sys
import tempfile
import tracemalloc
import unittest
from unittest.mock import patch
import warnings
import zipfile

BACKEND = Path(__file__).resolve().parents[1] / "agent_backend" / "nanobot"
sys.path.insert(0, str(BACKEND))
import artifact_verifier as module
from artifact_verifier import ArtifactVerifier
from safe_files import MAX_DOCUMENT_BYTES, MAX_FILE_BYTES
from task_harness import TaskHarness
from tool_catalog import CATALOG


def package(entries=(), *, compression=zipfile.ZIP_STORED):
    stream = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(stream, "w", compression=compression) as archive:
            for name, value in entries:
                archive.writestr(name, value)
    return stream.getvalue()


class ZipArtifactVerifierTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="aura-zip-verifier-")
        self.root = Path(self.temporary.name)
        self.verifier = ArtifactVerifier(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def verify(self, raw, *, name="artifact.zip", kind="auto"):
        path = self.root / name
        path.write_bytes(raw)
        return self.verifier.verify(name, kind=kind)

    def assert_rejected(self, raw, *, issue=None):
        result = self.verify(raw)
        self.assertFalse(result["passed"], result)
        self.assertFalse(result["task_fully_verified"])
        if issue is not None:
            self.assertIn(issue, result["issues"], result)
        return result

    def test_actual_zip_auto_readback_has_current_hash_without_extraction_or_goal_claim(self):
        raw = package([("代码/solution.py", b"raise AssertionError('MUST NEVER EXECUTE')"),
                       ("results.json", '{"鲸鱼":"0.30"}')], compression=zipfile.ZIP_DEFLATED)
        before = set(self.root.iterdir())
        with patch.object(zipfile.ZipFile, "extract", side_effect=AssertionError("No extraction")), \
             patch.object(zipfile.ZipFile, "extractall", side_effect=AssertionError("No extraction")):
            result = self.verify(raw)
        self.assertTrue(result["passed"], result)
        self.assertEqual("zip", result["kind"])
        self.assertEqual("zip_structure_crc_only", result["scope"])
        self.assertFalse(result["task_fully_verified"])
        self.assertEqual(hashlib.sha256(raw).hexdigest(), result["sha256"])
        self.assertEqual(len(raw), result["bytes"])
        self.assertEqual(before | {self.root / "artifact.zip"}, set(self.root.iterdir()))
        self.assertEqual(raw, (self.root / "artifact.zip").read_bytes())

    def test_plain_text_masquerading_as_zip_cannot_bypass_harness_minimum_with_explicit_text(self):
        (self.root / "fake.zip").write_text("This is ordinary text, not an archive.", encoding="utf-8")
        harness = TaskHarness(self.root)
        result = json.loads(harness.verify_artifact("fake.zip", kind="text"))
        self.assertFalse(result["passed"], result)
        self.assertFalse(result["task_fully_verified"])

    def test_explicit_zip_kind_and_empty_container_are_scoped_valid_formats(self):
        for name, raw in (("empty.zip", package()), ("opaque.bin", package([("a.txt", "hello")]))):
            with self.subTest(name=name):
                result = self.verify(raw, name=name, kind="zip")
                self.assertTrue(result["passed"], result)
                self.assertEqual("zip_structure_crc_only", result["scope"])

    def test_bounded_single_disk_zip64_and_forced_local_zip64_are_supported(self):
        raw = package([("owned.txt", "ZIP64 owned fixture")])
        at = raw.rfind(b"PK\x05\x06")
        _, _, on_disk, count, size, offset, _ = struct.unpack_from("<HHHHIIH", raw, at + 4)
        record = struct.pack("<4sQHHIIQQQQ", b"PK\x06\x06", 44, 45, 45, 0, 0,
                             on_disk, count, size, offset)
        locator = struct.pack("<4sIQI", b"PK\x06\x07", 0, at, 1)
        end = bytearray(raw[at:])
        struct.pack_into("<HHII", end, 8, 0xffff, 0xffff, 0xffffffff, 0xffffffff)
        self.assertTrue(self.verify(raw[:at] + record + locator + end)["passed"])
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            with archive.open("owned.txt", "w", force_zip64=True) as member:
                member.write(b"explicit local ZIP64 header")
        self.assertTrue(self.verify(stream.getvalue())["passed"])
        class Unseekable(io.BytesIO):
            def seekable(self): return False
            def seek(self, *args): raise OSError("Owned streaming writer cannot seek")
        stream = Unseekable()
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            with archive.open("owned.txt", "w", force_zip64=True) as member:
                member.write(b"streamed ZIP64 data descriptor" * 4000)
        result = self.verify(stream.getvalue())
        self.assertTrue(result["passed"], result)
        self.assertEqual(hashlib.sha256(b"streamed ZIP64 data descriptor" * 4000).hexdigest(), result["zip_members"][0]["sha256"])

    def test_large_stored_zip_uses_document_byte_bound(self):
        raw = package([("data.bin", b"x" * (MAX_FILE_BYTES + 1))])
        self.assertGreater(len(raw), MAX_FILE_BYTES)
        self.assertTrue(self.verify(raw)["passed"])
        self.assertTrue(self.verify(raw, name="opaque.bin", kind="zip")["passed"])
        self.assertIn("file_unavailable", self.verify(b"x" * (MAX_DOCUMENT_BYTES + 1))["issues"])

    def test_member_names_reject_traversal_aliases_control_and_windows_conflicts(self):
        invalid = ["../escape", "/absolute", "C:/drive", "a\\b", "a/../b", "a/./b", "a//b",
                   "a:stream", "a\0hidden", "bad\x01name", "trailing.", "space ", "a/CON.txt",
                   "nul", "con .txt", "LPT1.txt", "COM¹.log", "a?b", "a*b", "a|b", "a<b", "a>b", 'a"b']
        for name in invalid:
            with self.subTest(name=name):
                # ZipInfo itself truncates a NUL-containing filename; inject
                # the NUL into both actual local/central bytes instead.
                if "\0" in name:
                    raw = package([("aXhidden", "payload")]).replace(b"aXhidden", b"a\0hidden")
                elif "\\" in name:
                    # Windows' ZipInfo writer normalizes backslashes. Make
                    # the actual wire name unsafe rather than testing a safe
                    # archive that the fixture silently rewrote to a/b.
                    raw = package([("aXb", "payload")]).replace(b"aXb", b"a\\b")
                else:
                    raw = package([(name, "payload")])
                result = self.assert_rejected(raw)
                self.assertNotIn(name, json.dumps(result, ensure_ascii=False))

    def test_duplicate_case_alias_and_file_directory_collisions_are_rejected(self):
        entries = [[("a", "one"), ("a", "two")], [("Readme", "one"), ("README", "two")],
                   [("a", "one"), ("a/b", "two")], [("a/b", "one"), ("A", "two")],
                   [("a/", ""), ("a", "one")]]
        for values in entries:
            with self.subTest(values=values):
                self.assert_rejected(package(values))
        self.assertTrue(self.verify(package([("a/b", "one"), ("a/", "")]))["passed"])

    def test_component_prefix_conflicts_are_independent_of_order_and_sibling_names(self):
        for entries in itertools.permutations([("a", "file"), ("A/b/c", "child"), ("a-", "sibling")]):
            with self.subTest(entries=entries):
                self.assert_rejected(package(entries), issue="conflicting_zip_members")
        allowed = [("a/", ""), ("A/b/c", "child"), ("a-", "sibling"), ("ab", "file"), ("ab-child", "file")]
        for entries in (allowed, list(reversed(allowed))):
            self.assertTrue(self.verify(package(entries))["passed"])

    def test_deep_distinct_names_have_bounded_verifier_memory(self):
        # This owned fixture uses only 128 empty members. Keeping all implicit
        # parent tuples used over 30 MiB here despite a ZIP below 140 KiB.
        entries = [("/".join([f"n{index:03d}"] + ["a"] * 238 + ["f"]), "")
                   for index in range(128)]
        raw = package(entries)
        result = {"checks": []}
        self.assertFalse(tracemalloc.is_tracing())
        tracemalloc.start()
        try:
            ArtifactVerifier._zip(raw, result)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertTrue(all(check["passed"] for check in result["checks"]))
        self.assertEqual(128, result["zip_members_total"])
        self.assertLess(peak, 8 * 1024 * 1024)

    def test_symlink_special_file_and_directory_payload_are_rejected(self):
        for mode in (stat.S_IFLNK, stat.S_IFIFO, stat.S_IFSOCK, stat.S_IFBLK, stat.S_IFCHR):
            member = zipfile.ZipInfo("owned-entry")
            member.create_system = 3
            member.external_attr = (mode | 0o600) << 16
            with self.subTest(mode=mode):
                self.assert_rejected(package([(member, "target")]))
        member = zipfile.ZipInfo("directory/")
        self.assert_rejected(package([(member, "unexpected directory payload")]))

    def test_encrypted_and_unsupported_compression_are_rejected_before_reading_members(self):
        raw = bytearray(package([("owned.txt", "fixed")]))
        central = raw.index(b"PK\x01\x02")
        for description, offset1, offset2, value in (("encryption", 6, central + 8, 1),
                                                    ("compression", 8, central + 10, 99)):
            variant = bytearray(raw)
            struct.pack_into("<H", variant, offset1, value)
            struct.pack_into("<H", variant, offset2, value)
            with self.subTest(description=description), \
                 patch.object(module.zlib, "decompressobj", side_effect=AssertionError("Unsafe metadata reached decompressor")):
                self.assert_rejected(bytes(variant))

    def test_crc_corruption_truncation_and_non_zip_bytes_are_rejected(self):
        raw = package([("owned.txt", b"UNIQUE_PAYLOAD")])
        damaged = raw.replace(b"UNIQUE_PAYLOAD", b"UNIQUE_PAYLOAE")
        for content in (damaged, raw[:-1], b"PK\x03\x04invalid", b"not a zip", b"MZ" + raw):
            with self.subTest(length=len(content)):
                self.assert_rejected(content)

    def test_zero_byte_member_crc_and_local_header_policy_are_not_trusted(self):
        raw = bytearray(package([("empty.txt", b"")]))
        central = raw.index(b"PK\x01\x02")
        struct.pack_into("<I", raw, 14, 0x12345678)
        struct.pack_into("<I", raw, central + 16, 0x12345678)
        self.assert_rejected(bytes(raw))
        raw = package([("owned.txt", b"fixed")])
        for offset, value in ((6, 1), (8, 99)):
            variant = bytearray(raw)
            struct.pack_into("<H", variant, offset, value)
            with self.subTest(local_field=offset):
                self.assert_rejected(bytes(variant))

    def test_member_count_guard_runs_before_zipfile_allocates_filelist(self):
        raw = package([(f"item-{index}", "") for index in range(2001)])
        # A forged small EOCD count must not bypass the pre-construction
        # central-directory walk (ZipFile ignores the count when loading).
        forged = bytearray(raw)
        eocd = forged.rfind(b"PK\x05\x06")
        struct.pack_into("<HH", forged, eocd + 8, 1, 1)
        for value in (raw, bytes(forged)):
            with self.subTest(forged=value is not raw), \
                 patch.object(module.zipfile, "ZipFile", side_effect=AssertionError("Oversized filelist was parsed")):
                self.assert_rejected(value)

    def test_expansion_guard_runs_before_decompression(self):
        raw = package([("expansion.bin", b"x" * (MAX_DOCUMENT_BYTES + 1))], compression=zipfile.ZIP_DEFLATED)
        self.assertLess(len(raw), MAX_FILE_BYTES)
        with patch.object(module.zlib, "decompressobj", side_effect=AssertionError("Oversized member reached decompressor")):
            self.assert_rejected(raw)

    def test_current_bytes_are_rechecked_after_crc_and_metadata_is_bounded(self):
        raw = package([(f"item-{index}.txt", str(index)) for index in range(70)])
        result = self.verify(raw)
        self.assertTrue(result["passed"], result)
        self.assertLessEqual(len(result["zip_members"]), 64)
        self.assertEqual(70, result["zip_members_total"])
        self.assertEqual(70 - len(result["zip_members"]), result["zip_members_omitted"])
        for value in result["zip_members"]:
            self.assertEqual(hashlib.sha256(str(int(value["name"][5:-4])).encode()).hexdigest(), value["sha256"])
        original = self.verifier._zip
        def changed(data, output):
            original(data, output)
            (self.root / "artifact.zip").write_bytes(package([("changed.txt", "different")]))
        with patch.object(self.verifier, "_zip", side_effect=changed):
            result = self.verify(raw)
        self.assertFalse(result["passed"])
        self.assertIn("artifact_unchanged_during_check", result["issues"])

    def test_catalog_exposes_zip_without_a_new_tool_or_goal_success_flag(self):
        spec = CATALOG["verify_artifact"]
        self.assertIn("zip", spec.properties["kind"]["enum"])
        self.assertTrue(spec.read_only)
        self.assertEqual("none", spec.effect)


if __name__ == "__main__":
    unittest.main()
