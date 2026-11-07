"""Owned fixtures for bounded, versioned multi-line file searches; no API."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_backend.nanobot import safe_files
from agent_backend.nanobot.safe_files import WorkspaceFiles
from agent_backend.nanobot.tool_catalog import CATALOG


class SafeFilesSearchReviewTests(unittest.TestCase):
    def setUp(self):
        owner = tempfile.TemporaryDirectory(prefix="aura-search-review-")
        self.addCleanup(owner.cleanup)
        self.root = Path(owner.name)
        self.files = WorkspaceFiles(self.root)

    def fixture(self, name="notes.txt", text="安排：旧地点，已作废\n其他内容\n安排：最终地点\n"):
        path = self.root / name
        path.write_bytes(text.encode("utf-8"))
        return path

    def test_default_search_exposes_later_matching_lines_in_the_same_file(self):
        self.fixture()
        result = json.loads(self.files.search("安排"))
        self.assertEqual(1, len(result["matches"]))
        item = result["matches"][0]
        self.assertEqual(1, item["line"])
        self.assertIn("旧地点", item["text"])
        lines = item.get("line_matches", [])
        self.assertEqual([1, 3], [hit["line"] for hit in lines],
                         "The old first hit must not silently hide a later correction.")
        self.assertIn("最终地点", lines[-1]["text"])

    def test_concrete_authorized_file_path_can_be_searched(self):
        path = self.fixture()
        try:
            result = json.loads(self.files.search("安排", path="notes.txt"))
        except ValueError as exc:
            self.fail(f"An authorized concrete file must be searchable: {exc}")
        item = result["matches"][0]
        self.assertEqual("notes.txt", item["path"])
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), item["sha256"])
        self.assertEqual([1, 3], [hit["line"] for hit in item["line_matches"]])

    def test_file_hit_offset_is_compatible_and_final_page_has_no_fake_continuation(self):
        names = {f"source-{i}.txt" for i in range(7)}
        for name in names:
            self.fixture(name, "needle\nneedle again\n")
        found, offset = [], 0
        while offset is not None:
            page = json.loads(self.files.search("needle", offset=offset, limit=2))
            self.assertEqual(offset, page["offset"])
            found.extend(item["path"] for item in page["matches"])
            if page["next_offset"] is not None:
                self.assertEqual(offset + len(page["matches"]), page["next_offset"])
            offset = page["next_offset"]
        self.assertEqual(names, set(found))
        self.assertEqual(len(names), len(found))
        self.assertEqual(1, len(page["matches"]))
        self.assertIsNone(page["next_offset"])

    def test_more_than_twenty_same_file_lines_continue_with_a_version_bound_range(self):
        text = "".join(f"needle {i}\n" for i in range(47))
        path = self.fixture(text=text)
        raw = path.read_bytes()
        offset, lines, digest = 0, [], hashlib.sha256(raw).hexdigest()
        for _ in range(3):
            page = json.loads(self.files.search("needle", "notes.txt", line_offset=offset,
                                               expected_hash=digest))
            item = page["matches"][0]
            self.assertEqual(digest, item["sha256"])
            self.assertEqual(len(raw), item["bytes"])
            self.assertLessEqual(len(item["line_matches"]), 20)
            self.assertEqual(47-offset, item["line_matches_total"])
            lines.extend(hit["line"] for hit in item["line_matches"])
            offset = item["next_line_offset"]
            if offset is None:
                break
            self.assertTrue(item["line_matches_truncated"])
            self.assertTrue(page["truncated"])
        self.assertEqual(list(range(1, 48)), lines)
        self.assertIsNone(offset)
        self.assertFalse(item["line_matches_truncated"])
        self.assertEqual(raw, path.read_bytes())

    def test_continuation_rejects_external_edits_instead_of_mixing_versions(self):
        path = self.fixture(text="needle\n" * 25)
        first = json.loads(self.files.search("needle", "notes.txt"))["matches"][0]
        path.write_bytes(b"needle EXTERNALLY_CHANGED\n" * 25)
        with self.assertRaisesRegex(ValueError, "^FILE_CHANGED$"):
            self.files.search("needle", "notes.txt", line_offset=first["next_line_offset"],
                              expected_hash=first["sha256"])
        self.assertIn(b"EXTERNALLY_CHANGED", path.read_bytes())

    def test_change_during_snapshot_is_rejected_even_without_an_expected_hash(self):
        path = self.fixture(text="needle original\n")
        original_open = Path.open
        original_mtime = path.stat().st_mtime_ns

        class ChangingRead:
            def __init__(self, stream): self.stream = stream
            def __enter__(self): self.stream.__enter__(); return self
            def __exit__(self, *args): return self.stream.__exit__(*args)
            def read(self, size):
                data = self.stream.read(size)
                with original_open(path, "wb") as output:
                    output.write(b"needle changed\n")
                os.utime(path, ns=(original_mtime+1_000_000_000, original_mtime+1_000_000_000))
                return data

        def changing_open(target, mode="r", *args, **kwargs):
            stream = original_open(target, mode, *args, **kwargs)
            return ChangingRead(stream) if target == path and mode == "rb" else stream

        with patch.object(Path, "open", changing_open):
            with self.assertRaisesRegex(ValueError, "^FILE_CHANGED$"):
                self.files.search("needle")

    def test_search_does_not_grant_a_write_or_implicitly_authorize_an_overwrite(self):
        self.fixture(text="needle original\n")
        self.files.search("needle", "notes.txt")
        with self.assertRaisesRegex(ValueError, "READ_BEFORE_OVERWRITE"):
            self.files.write("notes.txt", "changed", overwrite=True)

    def test_exact_file_read_grant_does_not_authorize_its_parent_or_sibling(self):
        with tempfile.TemporaryDirectory(prefix="aura-search-external-") as td:
            external = Path(td)
            selected, sibling = external/"selected.txt", external/"sibling.txt"
            selected.write_bytes(b"needle selected\n")
            sibling.write_bytes(b"needle ungranted\n")
            self.files.add_grant({"path": str(selected), "access": "read", "directory": False})
            item = json.loads(self.files.search("needle", str(selected)))["matches"][0]
            self.assertEqual(str(selected.resolve()), item["path"])
            self.assertEqual(hashlib.sha256(selected.read_bytes()).hexdigest(), item["sha256"])
            for denied in (external, sibling):
                with self.subTest(path=denied):
                    with self.assertRaisesRegex(ValueError, "OUTSIDE_WORKSPACE"):
                        self.files.search("needle", str(denied))
            with self.assertRaisesRegex(ValueError, "OUTSIDE_WORKSPACE"):
                self.files.write(str(selected), "changed", overwrite=True, expected_hash=item["sha256"])

    def test_default_io_budget_really_bounds_content_reads_and_reports_incomplete_search(self):
        self.assertEqual(32*1024*1024, safe_files.MAX_SEARCH_READ_BYTES)
        for i in range(4):
            self.fixture(f"file-{i}.txt", "needle"+"x"*494)
        opened, original_open = [], Path.open

        class CountRead:
            def __init__(self, stream): self.stream = stream
            def __enter__(self): self.stream.__enter__(); return self
            def __exit__(self, *args): return self.stream.__exit__(*args)
            def read(self, size):
                body = self.stream.read(size)
                opened.append(len(body))
                return body

        def counting_open(target, mode="r", *args, **kwargs):
            stream = original_open(target, mode, *args, **kwargs)
            return CountRead(stream) if mode == "rb" else stream

        with patch.object(safe_files, "MAX_SEARCH_READ_BYTES", 1024), patch.object(Path, "open", counting_open):
            page = json.loads(self.files.search("needle"))
        self.assertEqual([500, 500], opened)
        self.assertEqual(1000, page["content_bytes_read"])
        self.assertLessEqual(sum(opened), 1024)
        self.assertTrue(page["read_limited"])
        self.assertTrue(page["truncated"])

    def test_real_default_thirty_two_mib_cap_stops_before_another_full_file_read(self):
        body = b"x"*(safe_files.MAX_FILE_BYTES-6)+b"needle"
        for i in range(33):
            (self.root/f"source-{i}.txt").write_bytes(body)
        actual_reads, snapshot = [], self.files._snapshot
        def observed_snapshot(*args, **kwargs):
            result = snapshot(*args, **kwargs)
            actual_reads.append(len(result[1]))
            return result
        with patch.object(self.files, "_snapshot", observed_snapshot):
            page = json.loads(self.files.search("needle"))
        self.assertEqual(31, len(actual_reads))
        self.assertEqual(31*1024*1024, sum(actual_reads))
        self.assertEqual(sum(actual_reads), page["content_bytes_read"])
        self.assertLessEqual(sum(actual_reads), 32*1024*1024)
        self.assertTrue(page["read_limited"])
        self.assertTrue(page["truncated"])

    def test_filename_match_does_not_suppress_later_content_matches(self):
        self.fixture("needle-notes.txt", "needle obsolete\nnot related\nneedle final\n")
        item = json.loads(self.files.search("needle"))["matches"][0]
        self.assertEqual("needle-notes.txt", item["path"])
        self.assertEqual([1, 3], [hit["line"] for hit in item["line_matches"]])

    def test_empty_query_keeps_the_existing_filename_only_listing_without_content_io(self):
        self.fixture("listed.txt", "do not read")
        with patch.object(self.files, "_snapshot", side_effect=AssertionError("listing must not read")):
            page = json.loads(self.files.search())
        self.assertEqual([{"path": "listed.txt"}], page["matches"])
        self.assertEqual(0, page["content_bytes_read"])
        self.assertFalse(page["truncated"])

    def test_entry_cap_does_not_even_consume_an_extra_directory_candidate(self):
        self.assertEqual(2000, safe_files.MAX_SEARCH_ENTRIES)
        paths = [self.fixture(f"item-{i}.txt", "unchanged") for i in range(7)]
        yielded = []
        def candidates():
            for path in paths:
                yielded.append(path)
                yield path
        with patch.object(safe_files, "MAX_SEARCH_ENTRIES", 3), patch.object(Path, "rglob", return_value=candidates()):
            page = json.loads(self.files.search())
        self.assertEqual(paths[:3], yielded)
        self.assertEqual(3, page["entries_scanned"])
        self.assertTrue(page["scan_limited"])
        self.assertTrue(page["truncated"])
        self.assertEqual(0, page["content_bytes_read"])

    def test_encoded_output_budget_includes_unicode_escapes_and_preserves_file_pagination(self):
        query = "🌀"*100
        body = (("\x01"*160)+query+" end\n")*20
        names = {f"bounded-{i}.txt" for i in range(9)}
        for name in names:
            self.fixture(name, body)
        found, offset, limited_pages = [], 0, 0
        while offset is not None:
            encoded = self.files.search(query, offset=offset)
            self.assertLessEqual(len(encoded.encode("utf-8")), 65_536)
            page = json.loads(encoded)
            self.assertTrue(page["matches"])
            limited_pages += bool(page["response_limited"])
            for item in page["matches"]:
                self.assertEqual(20, len(item["line_matches"]))
                self.assertEqual(1, item["line"])
                self.assertIn(query, item["text"])
                found.append(item["path"])
            offset = page["next_offset"]
        self.assertGreater(limited_pages, 0)
        self.assertEqual(names, set(found))
        self.assertEqual(len(names), len(found))

    def test_single_large_result_trims_lines_with_an_exact_continuation(self):
        query = "\x01"*500
        self.fixture(text=(query+" trailing\n")*41)
        found, offset, digest = [], 0, None
        while offset is not None:
            encoded = self.files.search(query, "notes.txt", line_offset=offset, expected_hash=digest)
            self.assertLessEqual(len(encoded.encode("utf-8")), 65_536)
            item = json.loads(encoded)["matches"][0]
            digest = item["sha256"]
            found.extend(hit["line"] for hit in item["line_matches"])
            offset = item["next_line_offset"]
            if offset is not None:
                self.assertTrue(item["line_matches_truncated"])
        self.assertEqual(list(range(1, 42)), found)

    def test_bom_crlf_bytes_and_unicode_offsets_match_the_current_source(self):
        original = b"\xef\xbb\xbf"+("ß"*2000+"NEEDLE\r\n第二行 needle\r\n").encode("utf-8")
        path = self.root/"unicode.txt"; path.write_bytes(original)
        item = json.loads(self.files.search("needle", "unicode.txt"))["matches"][0]
        self.assertEqual(hashlib.sha256(original).hexdigest(), item["sha256"])
        self.assertEqual(len(original), item["bytes"])
        self.assertEqual([1, 2], [hit["line"] for hit in item["line_matches"]])
        self.assertEqual(1840, item["character_offset"])
        page = json.loads(self.files.read_page("unicode.txt", unit="lines", offset=1, limit=1,
                                               expected_hash=item["sha256"]))
        self.assertEqual("第二行 needle\r\n", page["text"])
        self.assertEqual(original, path.read_bytes())

    def test_oversized_or_non_utf8_contents_are_explicitly_not_a_complete_negative_search(self):
        self.fixture("large.dat", "x"*(safe_files.MAX_FILE_BYTES+1))
        (self.root/"binary.dat").write_bytes(b"\xff\xfeNOT_UTF8")
        with patch.object(self.files, "_snapshot", wraps=self.files._snapshot) as snapshot:
            page = json.loads(self.files.search("not-a-filename"))
        self.assertEqual([], page["matches"])
        self.assertEqual(1, page["skipped_contents"]["FILE_TOO_LARGE"])
        self.assertEqual(1, page["skipped_contents"]["UNSUPPORTED_TEXT_ENCODING"])
        self.assertTrue(page["truncated"])
        self.assertEqual(1, snapshot.call_count)

    def test_validation_disallows_directory_line_continuations_and_single_file_hashes(self):
        self.fixture()
        for kwargs, code in (({"line_offset": 1}, "INVALID_SEARCH"),
                             ({"expected_hash": "0"*64}, "EXPECTED_HASH_REQUIRES_FILE"),
                             ({"path": "notes.txt", "line_offset": True}, "INVALID_SEARCH"),
                             ({"path": "notes.txt", "expected_hash": "bad"}, "INVALID_EXPECTED_HASH"),
                             ({"query": "x"*501}, "INVALID_SEARCH")):
            with self.subTest(kwargs=kwargs):
                with self.assertRaisesRegex(ValueError, code):
                    self.files.search(**kwargs)

    def test_tool_schemas_describe_file_line_and_character_units(self):
        search = CATALOG["search_files"].parameters
        self.assertIn("line_offset", search["properties"])
        self.assertIn("expected_hash", search["properties"])
        self.assertFalse(search["additionalProperties"])
        self.assertIn("FILE", search["properties"]["offset"]["description"])
        read = CATALOG["read_file"]
        self.assertIn("offset=N-1", read.description)
        self.assertIn("0-based", read.properties["offset"]["description"])
        self.assertIn("Default characters", read.properties["unit"]["description"])


if __name__ == "__main__":
    unittest.main()
