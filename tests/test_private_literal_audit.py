"""Synthetic-only checks: never open real settings or credential stores."""
import io
import json
from pathlib import Path
import tempfile
from types import ModuleType
import unittest
from unittest.mock import Mock, patch

from tools.qa_release.verify_private_literals import Literals, audit, code_values


class PrivateLiteralAuditTests(unittest.TestCase):
    def test_encodings_and_chunk_boundaries(self):
        profile = r"X:\SyntheticUser\名字"
        key = "fake-audit-key"
        literals = Literals([profile], [key])
        cases = [profile, profile.replace("\\", "/"), json.dumps(profile)[1:-1], key]
        for text in cases:
            for encoding in ("utf-8", "utf-16-le", "utf-16-be"):
                with self.subTest(encoding=encoding, kind="synthetic"):
                    raw = b"prefix" + text.encode(encoding) + b"suffix"
                    counts, size = literals.scan(io.BytesIO(raw), chunk_bytes=7)
                    self.assertGreater(sum(counts.values()), 0)
                    self.assertEqual(size, len(raw))

    def test_no_false_match_and_report_labels_redacted(self):
        literals = Literals([r"X:\SyntheticUser"], ["fake-audit-key"])
        counts, _ = literals.scan(io.BytesIO(b"unrelated bytes"), chunk_bytes=3)
        self.assertEqual(counts, {})
        self.assertEqual(literals.safe_label("cache/fake-audit-key.txt"), "cache/[redacted].txt")

    def test_nested_compiled_constants_are_included(self):
        compiled = compile("values=('fake-audit-key',('another',))", "synthetic.py", "exec")
        self.assertIn("fake-audit-key", list(code_values(compiled)))

    def installed_fixture(self, directory):
        payload = Path(directory)
        members = ["鲸伴.exe", "鲸伴网页桥.exe"]
        for name in members:
            (payload / name).write_bytes(b"synthetic executable")
        rows = [{"path": name, "bytes": (payload / name).stat().st_size, "sha256": "0" * 64}
                for name in members]
        (payload / "PACKAGE-MANIFEST.json").write_text(json.dumps({"files": rows}), encoding="utf-8")
        return payload

    def test_installer_extras_are_reported_scanned_and_not_opened_as_pyz(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = self.installed_fixture(directory)
            for name in ("unins000.exe", "unins000.dat", "unins000.msg"):
                (payload / name).write_bytes(b"fake-audit-key")
            reader = Mock()
            reader.return_value.toc = {}
            readers = ModuleType("PyInstaller.archive.readers")
            readers.CArchiveReader = reader
            with patch.dict("sys.modules", {"PyInstaller": ModuleType("PyInstaller"),
                                             "PyInstaller.archive": ModuleType("PyInstaller.archive"),
                                             "PyInstaller.archive.readers": readers}):
                report = audit(payload, Literals(keys=["fake-audit-key"]), {"unreadable_stores": 0})
            self.assertFalse(report["passed"])
            self.assertEqual(report["installer_extra_files"], ["unins000.dat", "unins000.exe", "unins000.msg"])
            self.assertEqual({item["path"] for item in report["findings"]}, set(report["installer_extra_files"]))
            self.assertEqual({Path(call.args[0]).name for call in reader.call_args_list}, {"鲸伴.exe", "鲸伴网页桥.exe"})
            self.assertEqual(report["files_scanned"], 6)

    def test_other_unregistered_files_remain_rejected(self):
        for name in ("unins001.exe", "extra.dat", "nested/unins000.exe"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                payload = self.installed_fixture(directory)
                extra = payload / name
                extra.parent.mkdir(parents=True, exist_ok=True)
                extra.write_bytes(b"unregistered")
                with self.assertRaisesRegex(ValueError, "CANDIDATE_INVENTORY_MISMATCH"):
                    audit(payload, Literals(), {"unreadable_stores": 0})

    def test_allowed_extra_does_not_excuse_missing_manifest_member(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = self.installed_fixture(directory)
            (payload / "鲸伴网页桥.exe").unlink()
            (payload / "unins000.exe").write_bytes(b"uninstaller")
            with self.assertRaisesRegex(ValueError, "CANDIDATE_INVENTORY_MISMATCH"):
                audit(payload, Literals(), {"unreadable_stores": 0})


if __name__ == "__main__":
    unittest.main()
