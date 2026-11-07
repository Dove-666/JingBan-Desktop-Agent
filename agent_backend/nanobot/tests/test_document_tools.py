"""Actual in-memory Office/PDF roundtrips, with no provider or desktop use."""
from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from artifact_verifier import ArtifactVerifier
from document_tools import DocumentError, DocumentTools
from safe_files import WorkspaceFiles
from task_harness import TaskHarness

HAS_WRITERS = all(importlib.util.find_spec(name) for name in ("docx", "openpyxl", "pptx"))
HAS_PDF = importlib.util.find_spec("pypdf") is not None


class _DocumentFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="aura-documents-")
        self.base = Path(self.temporary.name)
        self.root = self.base / "workspace"; self.root.mkdir()
        self.external = self.base / "selected"; self.external.mkdir()
        self.files = WorkspaceFiles(self.root)
        self.documents = DocumentTools(self.root, files=self.files)

    def tearDown(self):
        self.temporary.cleanup()


class DocumentReaderTests(_DocumentFixture):
    def test_docx_reader_rejects_entities_and_external_path_escape(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("word/document.xml", '<!DOCTYPE x [<!ENTITY x "injected">]><x>&x;</x>')
        self.files.write_bytes("bad.docx", stream.getvalue())
        with self.assertRaisesRegex(DocumentError, "UNSAFE_DOCUMENT_XML"):
            self.documents.read_document("bad.docx")
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("../outside.txt", "not extracted")
        self.files.write_bytes("escape.docx", stream.getvalue())
        with self.assertRaisesRegex(DocumentError, "UNSAFE_DOCUMENT_PACKAGE"):
            self.documents.read_document("escape.docx")
        self.assertFalse((self.base / "outside.txt").exists())

    def test_artifact_identity_is_actual_file_hash_and_does_not_grant_more_access(self):
        self.files.write("result.txt", "actual result")
        artifact = json.loads(self.documents.describe_artifact("result.txt"))
        self.assertEqual(artifact["bytes"], len(b"actual result"))
        self.assertEqual(len(artifact["sha256"]), 64)
        with self.assertRaisesRegex(ValueError, "OUTSIDE_WORKSPACE"):
            self.documents.describe_artifact(str(self.external / "missing.txt"))

    @unittest.skipUnless(HAS_PDF, "PDF dependency not installed in this interpreter")
    def test_pdf_pages_are_real_text_extraction_and_explicitly_paginated(self):
        from pypdf import PdfWriter
        from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
        writer = PdfWriter()
        for index in range(6):
            page = writer.add_blank_page(width=612, height=792)
            font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                                     NameObject("/Subtype"): NameObject("/Type1"),
                                     NameObject("/BaseFont"): NameObject("/Helvetica")})
            page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"):
                DictionaryObject({NameObject("/F1"): font})})
            content = DecodedStreamObject()
            content.set_data(f"BT /F1 12 Tf 72 720 Td (Actual page {index + 1}) Tj ET".encode())
            page[NameObject("/Contents")] = content
        stream = io.BytesIO(); writer.write(stream)
        self.files.write_bytes("source.pdf", stream.getvalue())
        result = json.loads(self.documents.read_document("source.pdf", pages="2-3"))
        self.assertEqual(result["total_pages"], 6)
        self.assertEqual(result["selected_pages"], [2, 3])
        self.assertIn("Actual page 2", result["text"])
        self.assertNotIn("Actual page 1", result["text"])
        self.assertFalse(result["image_only_pages_need_vision"])

    @unittest.skipUnless(HAS_PDF, "PDF dependency not installed in this interpreter")
    def test_encrypted_pdf_requires_explicit_password_handling(self):
        from pypdf import PdfWriter
        writer = PdfWriter(); writer.add_blank_page(width=20, height=20); writer.encrypt("synthetic-test")
        stream = io.BytesIO(); writer.write(stream)
        self.files.write_bytes("private.pdf", stream.getvalue())
        with self.assertRaisesRegex(DocumentError, "PDF_PASSWORD_REQUIRED"):
            self.documents.read_document("private.pdf")


@unittest.skipUnless(HAS_WRITERS, "Use the installed nanobot interpreter for document writer tests")
class DocumentWriterTests(_DocumentFixture):
    def test_docx_long_content_paginates_and_pptx_section_selects_exact_slide(self):
        self.documents.create_document("long.docx", "docx", {"title": "Long",
            "sections": [{"paragraphs": ["鲸鱼🙂" * 5000, "SECOND_HALF" * 1500]}]})
        chunks, offset = [], 0
        first = json.loads(self.documents.read_document("long.docx", limit=1000))
        while offset is not None:
            page = json.loads(self.documents.read_document("long.docx", offset=offset,
                limit=1000, expected_hash=first["sha256"]))
            chunks.append(page["text"]); offset = page["next_offset"]
        self.assertEqual(len("".join(chunks)), first["total_characters"])
        self.assertIn("SECOND_HALF", "".join(chunks))
        self.documents.create_document("slides.pptx", "pptx", {"title": "Title slide",
            "sections": [{"heading": "Second slide", "paragraphs": ["SECOND_ONLY"]}]})
        second = json.loads(self.documents.read_document("slides.pptx", section="2"))
        self.assertIn("SECOND_ONLY", second["text"])
        self.assertNotIn("Title slide", second["text"])
        self.assertEqual(second["sections"], ["1", "2"])

    def test_docx_and_pptx_are_written_then_reopened_with_actual_unicode_contents(self):
        spec = {"title": "鲸鱼娘测试🙂", "sections": [
            {"heading": "实际内容", "paragraphs": ["第一段中文", "Second paragraph"]},
            {"heading": "表格", "table": {"headers": ["项目", "金额"], "rows": [["资料", "10.10"], ["设备", "20.20"]]}}]}
        for kind in ("docx", "pptx"):
            with self.subTest(kind=kind):
                result = json.loads(self.documents.create_document("result." + kind, kind, spec))
                read = json.loads(self.documents.read_document("result." + kind))
                for value in ("鲸鱼娘测试🙂", "第一段中文", "Second paragraph", "设备", "20.20"):
                    self.assertIn(value, read["text"])
                self.assertTrue(result["verification"]["passed"])
                self.assertFalse(result["verification"]["visual_layout_verified"])
                self.assertEqual(result["sha256"], read["sha256"])
                self.assertTrue(ArtifactVerifier(self.root).verify("result." + kind)["passed"])

    def test_xlsx_sections_and_literal_formula_like_text_roundtrip(self):
        formula_text = '=HYPERLINK("https://example.invalid", "literal")'
        spec = {"sheets": [
            {"name": "数据", "columns": ["名称", "数值"], "rows": [["a", 10], [formula_text, "20.20"]]},
            {"name": "Other", "rows": [["SECOND_SHEET"]]}]}
        self.documents.create_document("sheet.xlsx", "xlsx", spec)
        first = json.loads(self.documents.read_document("sheet.xlsx"))
        second = json.loads(self.documents.read_document("sheet.xlsx", section="Other"))
        self.assertEqual(first["sections"], ["数据", "Other"])
        self.assertIn(formula_text, first["text"])
        self.assertNotIn("FORMULA:", first["text"])
        self.assertIn("SECOND_SHEET", second["text"])
        from openpyxl import load_workbook
        workbook = load_workbook(self.root / "sheet.xlsx", data_only=False)
        self.assertEqual(workbook["数据"]["A3"].data_type, "s")
        workbook.close()

    def test_granted_external_document_input_and_output_share_harness_capabilities(self):
        source_spec = {"title": "External selected document", "sections": [{"paragraphs": ["REAL_SOURCE"]}]}
        self.documents.create_document("source.docx", "docx", source_spec)
        source = self.external / "selected.docx"
        source.write_bytes((self.root / "source.docx").read_bytes())
        self.files.add_grant({"path": str(source), "access": "read", "directory": False})
        self.assertIn("REAL_SOURCE", json.loads(self.documents.read_document(str(source)))["text"])
        with self.assertRaisesRegex(ValueError, "OUTSIDE_WORKSPACE"):
            self.documents.create_document(str(source), "docx", source_spec, overwrite=True)
        target = self.external / "output.docx"
        self.files.add_grant({"path": str(target), "access": "write", "directory": False})
        args = {"path": str(target), "kind": "docx", "spec": {"title": "Actual exported output"}}
        result = self.documents.create_document(**args)
        harness = TaskHarness(self.root, files=self.files)
        harness.record_success("create_document", args, result)
        self.assertTrue(harness.assess()["passed"])
        self.assertTrue(target.is_file())

    def test_document_overwrite_cannot_discard_an_external_edit(self):
        self.documents.create_document("source.docx", "docx", {"title": "original"})
        self.documents.read_document("source.docx")
        (self.root / "source.docx").write_bytes(b"external edit")
        with self.assertRaisesRegex(ValueError, "FILE_CHANGED"):
            self.documents.create_document("source.docx", "docx", {"title": "stale"}, overwrite=True)
        self.assertEqual((self.root / "source.docx").read_bytes(), b"external edit")

    def test_xlsx_rejects_silent_precision_loss_and_duplicate_casefolded_names(self):
        for sheets in ([{"name": "A", "rows": [[12345678901234567890]]}],
                       [{"name": "Data"}, {"name": "data"}]):
            with self.subTest(sheets=sheets), self.assertRaises(DocumentError):
                self.documents.create_document("bad.xlsx", "xlsx", {"sheets": sheets})
        self.assertFalse((self.root / "bad.xlsx").exists())


if __name__ == "__main__":
    unittest.main()
