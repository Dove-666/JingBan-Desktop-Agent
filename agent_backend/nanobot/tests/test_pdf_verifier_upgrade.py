"""Real pypdf files and harness contracts, no Office/model/desktop required."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from artifact_verifier import ArtifactVerifier
from document_tools import DocumentError, DocumentTools
from safe_files import WorkspaceFiles
from task_harness import TaskHarness


def pdf_bytes(*, pages=3, encrypted=False, active=None, image_only=False):
    from pypdf import PdfWriter
    from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, FloatObject, NameObject, TextStringObject
    writer = PdfWriter()
    for number in range(1, pages + 1):
        page = writer.add_blank_page(width=612, height=792)
        if not image_only:
            font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
            page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
            stream = DecodedStreamObject()
            stream.set_data(f"BT /F1 12 Tf 72 720 Td (Owned synthetic page {number}: amount 42) Tj ET".encode("ascii"))
            page[NameObject("/Contents")] = writer._add_object(stream)
    js = DictionaryObject({NameObject("/S"): NameObject("/JavaScript"), NameObject("/JS"): TextStringObject("void(0)")})
    if active == "names_js":
        writer._root_object[NameObject("/Names")] = DictionaryObject({NameObject("/JavaScript"): DictionaryObject({NameObject("/Names"): ArrayObject([TextStringObject("Owned synthetic action"), writer._add_object(js)])})})
    elif active == "open_js":
        writer._root_object[NameObject("/OpenAction")] = writer._add_object(js)
    elif active == "catalog_aa_js":
        writer._root_object[NameObject("/AA")] = DictionaryObject({NameObject("/WC"): writer._add_object(js)})
    elif active == "page_aa_js":
        writer.pages[0][NameObject("/AA")] = DictionaryObject({NameObject("/O"): writer._add_object(js)})
    elif active in {"annotation_launch", "passive_uri"}:
        action = (DictionaryObject({NameObject("/S"): NameObject("/Launch"), NameObject("/F"): TextStringObject("synthetic-never-executed.exe")})
                  if active == "annotation_launch" else DictionaryObject({NameObject("/S"): NameObject("/URI"), NameObject("/URI"): TextStringObject("https://learn.microsoft.com/")}))
        annotation = DictionaryObject({NameObject("/Type"): NameObject("/Annot"), NameObject("/Subtype"): NameObject("/Link"),
            NameObject("/Rect"): ArrayObject([FloatObject(0), FloatObject(0), FloatObject(100), FloatObject(50)]), NameObject("/A"): writer._add_object(action)})
        writer.pages[0][NameObject("/Annots")] = ArrayObject([writer._add_object(annotation)])
    if encrypted:
        writer.encrypt("owned-fixture-password")
    output = io.BytesIO(); writer.write(output); return output.getvalue()


class PdfVerifierUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aura-pdf-upgrade-")
        self.root = Path(self.temp.name)
        self.files = WorkspaceFiles(self.root)
        self.documents = DocumentTools(self.root, files=self.files)
        self.verifier = ArtifactVerifier(self.root, files=self.files)

    def tearDown(self):
        self.temp.cleanup()

    def write_pdf(self, raw, name="owned.pdf"):
        self.files.write_bytes(name, raw)
        return name

    def test_actual_text_pdf_verifies_in_auto_and_pdf_modes_with_same_identity(self):
        raw = pdf_bytes()
        path = self.write_pdf(raw)
        for kind in ("auto", "pdf"):
            with self.subTest(kind=kind):
                result = self.verifier.verify(path, kind=kind)
                self.assertTrue(result["passed"], result)
                self.assertEqual(result["kind"], "pdf")
                self.assertEqual(result["sha256"], hashlib.sha256(raw).hexdigest())
                self.assertEqual(result["scope"], "pdf_structure_and_page_readback")
                self.assertFalse(result["task_fully_verified"])
                self.assertEqual(next(item["pages"] for item in result["checks"] if item["code"] == "pdf_pages_read"), 3)

    def test_verified_pdf_read_document_pages_and_byte_hash_interoperate(self):
        path = self.write_pdf(pdf_bytes(pages=4))
        verified = self.verifier.verify(path)
        read = json.loads(self.documents.read_document(path, pages="2-3", expected_hash=verified["sha256"]))
        self.assertEqual(read["total_pages"], 4)
        self.assertEqual(read["selected_pages"], [2, 3])
        self.assertEqual(read["next_page"], 4)
        self.assertIn("Owned synthetic page 2: amount 42", read["text"])
        self.assertIn("Owned synthetic page 3: amount 42", read["text"])
        self.assertNotIn("Owned synthetic page 1", read["text"])
        self.assertNotIn("Owned synthetic page 4", read["text"])
        self.assertEqual(read["sha256"], verified["sha256"])
        self.assertFalse(read["image_only_pages_need_vision"])

    def test_pdf_text_offset_pagination_does_not_repeat_or_omit_content(self):
        path = self.write_pdf(pdf_bytes())
        whole = json.loads(self.documents.read_document(path, pages="1-3"))
        slices = []; offset = 0
        while True:
            page = json.loads(self.documents.read_document(path, pages="1-3", offset=offset, limit=23, expected_hash=whole["sha256"]))
            slices.append(page["text"])
            if page["next_offset"] is None: break
            offset = page["next_offset"]
        self.assertEqual("".join(slices), whole["text"])

    def test_image_only_pdf_is_structure_pass_without_claiming_text_or_layout(self):
        path = self.write_pdf(pdf_bytes(image_only=True))
        self.assertTrue(self.verifier.verify(path)["passed"])
        read = json.loads(self.documents.read_document(path))
        self.assertTrue(read["image_only_pages_need_vision"])
        self.assertEqual(read["scope"], "extracted_content_not_visual_layout")

    def test_encrypted_document_rejected_without_password_attempt(self):
        path = self.write_pdf(pdf_bytes(encrypted=True))
        result = self.verifier.verify(path)
        self.assertFalse(result["passed"])
        self.assertIn("unsupported_pdf_document", result["issues"])
        with self.assertRaisesRegex(DocumentError, "PDF_PASSWORD_REQUIRED"):
            self.documents.read_document(path)

    def test_malformed_pdf_does_not_become_verified_or_readable(self):
        path = self.write_pdf(b"%PDF-1.7\nnot a PDF object tree\n%%EOF\n")
        result = self.verifier.verify(path)
        self.assertFalse(result["passed"])
        self.assertIn("invalid_pdf_structure", result["issues"])
        with self.assertRaisesRegex(DocumentError, "PDF_READ_FAILED"):
            self.documents.read_document(path)

    def test_names_javascript_and_open_javascript_rejected(self):
        for active in ("names_js", "open_js"):
            with self.subTest(active=active):
                path = self.write_pdf(pdf_bytes(active=active), active + ".pdf")
                result = self.verifier.verify(path)
                self.assertFalse(result["passed"], result)
                self.assertIn("active_pdf_content", result["issues"])

    def test_nested_catalog_page_and_annotation_actions_rejected(self):
        for active in ("catalog_aa_js", "page_aa_js", "annotation_launch"):
            with self.subTest(active=active):
                path = self.write_pdf(pdf_bytes(active=active), active + ".pdf")
                result = self.verifier.verify(path)
                self.assertFalse(result["passed"], result)
                self.assertIn("active_pdf_content", result["issues"])

    def test_inert_webpage_annotation_is_allowed_without_fetching(self):
        path = self.write_pdf(pdf_bytes(active="passive_uri"))
        self.assertTrue(self.verifier.verify(path)["passed"])

    def test_render_result_tracks_written_pdf_instead_of_source_document(self):
        self.documents.create_document("source.docx", "docx", {"title": "Owned source", "sections": []})
        harness = TaskHarness(self.root, files=self.files)
        # This is a typed renderer observation over actual WorkspaceFiles
        # publication, not a claimed successful Microsoft Office rendering.
        written = json.loads(self.files.write_bytes("rendered.pdf", pdf_bytes()))
        observation = json.dumps({"status": "rendered", "source": "source.docx", "pages": 3, **written})
        harness.record_success("render_document", {"path": "source.docx", "output_path": "rendered.pdf"}, observation)
        self.assertEqual(harness.writes, {"rendered.pdf"})
        self.assertEqual(harness.written_hashes["rendered.pdf"], written["sha256"])
        self.assertEqual(harness.written_sizes["rendered.pdf"], written["bytes"])
        verified = json.loads(harness.verify_artifact("rendered.pdf", kind="pdf"))
        self.assertTrue(verified["passed"], verified)
        assessment = harness.assess()
        self.assertTrue(assessment["passed"], assessment)
        self.assertEqual([artifact["path"] for artifact in assessment["artifacts"]], ["rendered.pdf"])

    def test_changed_rendered_pdf_cannot_use_previous_renderer_hash(self):
        self.files.write_bytes("source.docx", b"only a synthetic source identity")
        harness = TaskHarness(self.root, files=self.files)
        written = json.loads(self.files.write_bytes("rendered.pdf", pdf_bytes()))
        harness.record_success("render_document", {"path": "source.docx"}, json.dumps(written))
        (self.root / "rendered.pdf").write_bytes(pdf_bytes(pages=2))
        verified = json.loads(harness.verify_artifact("rendered.pdf", kind="pdf"))
        self.assertFalse(verified["passed"])
        self.assertIn("ARTIFACT_CHANGED_AFTER_WRITE_OR_VERIFICATION", verified["issues"])


if __name__ == "__main__":
    unittest.main()
