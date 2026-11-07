"""Independent byte-fidelity fixtures; no API, user files or execution."""
import codecs
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from agent_backend.nanobot.safe_files import MAX_FILE_BYTES, WorkspaceFiles


class SafeFilesTextFidelityReviewTests(unittest.TestCase):
    def setUp(self):
        self.owner = tempfile.TemporaryDirectory(prefix="aura-edit-fidelity-")
        self.addCleanup(self.owner.cleanup)
        self.root = Path(self.owner.name)
        self.files = WorkspaceFiles(self.root)

    def edit(self, name, original, old_text, new_text):
        path = self.root/name
        path.write_bytes(original)
        observed = json.loads(self.files.read_page(name))
        result = json.loads(self.files.edit(name, old_text, new_text, expected_hash=observed["sha256"]))
        actual = path.read_bytes()
        self.assertEqual(hashlib.sha256(actual).hexdigest(), result["sha256"])
        self.assertEqual(len(actual), result["bytes"])
        return actual

    def test_lf_crlf_and_mixed_endings_stay_unchanged_outside_local_replacement(self):
        for endings in (("\n", "\n"), ("\r\n", "\r\n"), ("\r\n", "\n"), ("\n", "\r\n")):
            with self.subTest(endings=endings):
                original = ("安排"+endings[0]+"地点：东门咖啡馆"+endings[1]+"备注：🐳请保留").encode("utf-8")
                actual = self.edit("schedule.txt", original, "东门咖啡馆", "南门图书馆")
                self.assertEqual(original.replace("东门咖啡馆".encode(), "南门图书馆".encode()), actual)

    def test_no_final_newline_and_json_indentation_are_preserved(self):
        for original in (b'{\n    "enabled": true,\n    "nested": { "keep": 1 }\n}',
                         b'{\r\n\t"enabled": true,\r\n\t"nested": { "keep": 1 }\r\n}'):
            with self.subTest(original=original):
                actual = self.edit("settings.json", original, '"enabled": true', '"enabled": false')
                self.assertEqual(original.replace(b'"enabled": true', b'"enabled": false'), actual)
                self.assertFalse(actual.endswith(b"\n"))

    def test_utf8_bom_is_preserved_for_an_actual_local_edit(self):
        original = codecs.BOM_UTF8+'地点：东门咖啡馆\r\n其他文字不变🐳'.encode("utf-8")
        actual = self.edit("bom.txt", original, "东门咖啡馆", "南门图书馆")
        self.assertEqual(original.replace("东门咖啡馆".encode(), "南门图书馆".encode()), actual)

    def test_noop_local_edit_never_changes_a_utf8_bom_or_newline_bytes(self):
        original = codecs.BOM_UTF8+b'preserve\r\nALL_BYTES'
        self.assertEqual(original, self.edit("noop.txt", original, "preserve", "preserve"))

    def test_read_page_keeps_crlf_and_marks_the_hash_of_actual_bom_bytes(self):
        original = codecs.BOM_UTF8+'第一行\r\n第二行'.encode("utf-8")
        (self.root/"read.txt").write_bytes(original)
        page = json.loads(self.files.read_page("read.txt"))
        self.assertEqual("第一行\r\n第二行", page["text"])
        self.assertEqual(len(original), page["bytes"])
        self.assertEqual(hashlib.sha256(original).hexdigest(), page["sha256"])

    def test_bom_counts_toward_the_existing_text_limit_and_rejected_growth_changes_nothing(self):
        original = codecs.BOM_UTF8+b'EDIT'+b'x'*(MAX_FILE_BYTES-len(codecs.BOM_UTF8)-4)
        self.assertEqual(MAX_FILE_BYTES, len(original))
        self.assertEqual(original, self.edit("limit.txt", original, "EDIT", "EDIT"))
        observed = json.loads(self.files.read_page("limit.txt"))
        names = set(self.root.iterdir())
        with self.assertRaisesRegex(ValueError, "INVALID_WRITE"):
            self.files.edit("limit.txt", "EDIT", "EDITS", expected_hash=observed["sha256"])
        self.assertEqual(original, (self.root/"limit.txt").read_bytes())
        self.assertEqual(names, set(self.root.iterdir()))

    def test_stale_hash_cannot_edit_or_remove_a_bom(self):
        original = codecs.BOM_UTF8+b'EDIT\r\nKEEP'
        target = self.root/"stale.txt"; target.write_bytes(original)
        observed = json.loads(self.files.read_page("stale.txt"))
        changed = codecs.BOM_UTF8+b'EDIT\r\nNEW_EXTERNAL_VERSION'
        target.write_bytes(changed)
        with self.assertRaisesRegex(ValueError, "FILE_CHANGED"):
            self.files.edit("stale.txt", "EDIT", "UPDATED", expected_hash=observed["sha256"])
        self.assertEqual(changed, target.read_bytes())


if __name__ == "__main__": unittest.main()
