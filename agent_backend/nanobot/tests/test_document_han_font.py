"""New DOCX Han runs get a Windows Chinese font without changing Latin/style data."""
from __future__ import annotations

import io
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import document_tools

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": W}


@unittest.skipUnless(importlib.util.find_spec("docx"), "Use the installed nanobot interpreter for DOCX writer tests")
class DocumentHanFontTests(unittest.TestCase):
    def setUp(self):
        owned = tempfile.TemporaryDirectory(prefix="aura-owned-docx-han-")
        self.addCleanup(owned.cleanup)
        self.root = Path(owned.name)
        self.documents = document_tools.DocumentTools(self.root)

    def create(self, spec):
        receipt = json.loads(self.documents.create_document("generated.docx", "docx", spec))
        with zipfile.ZipFile(self.root / "generated.docx") as archive:
            xml = ET.fromstring(archive.read("word/document.xml"))
            styles = archive.read("word/styles.xml")
        runs = {"".join(run.itertext()): run for run in xml.findall(".//w:r", NS)}
        return receipt, xml, styles, runs

    def assertFont(self, run, expected):
        fonts = run.find("w:rPr/w:rFonts", NS)
        self.assertIsNotNone(fonts)
        self.assertEqual({f"{{{W}}}eastAsia": expected}, fonts.attrib)

    def test_generated_title_heading_paragraph_and_table_han_runs_have_direct_eastasia_font(self):
        fields = ["周末活动安排", "活动主题", "提前十分钟集合。", "项目", "具体安排", "地点", "南门图书馆一楼"]
        spec = {"title": fields[0], "sections": [{"heading": fields[1], "paragraphs": [fields[2]],
            "table": {"headers": fields[3:5], "rows": [[fields[5], fields[6]]]}}]}
        receipt, _, _, runs = self.create(spec)
        read = json.loads(self.documents.read_document("generated.docx"))
        for text in fields:
            with self.subTest(text=text):
                self.assertIn(text, read["text"])
                self.assertFont(runs[text], "Microsoft YaHei")
        self.assertTrue(receipt["verification"]["passed"])
        self.assertFalse(receipt["verification"]["visual_layout_verified"])

    def test_mixed_language_applies_only_to_han_runs_and_preserves_default_styles(self):
        from docx import Document
        spec = {"title": "AURA 周末活动安排 2026", "sections": [{"heading": "English heading",
            "paragraphs": ["Budget 35 元", "English paragraph", "カタカナ", "한글"],
            "table": {"headers": ["Item", "预算"], "rows": [["Paper", 35]]}}]}
        _, root, styles, runs = self.create(spec)
        for text in ("AURA 周末活动安排 2026", "Budget 35 元", "预算"):
            self.assertFont(runs[text], "Microsoft YaHei")
        for text in ("English heading", "English paragraph", "カタカナ", "한글", "Item", "Paper", "35"):
            self.assertIsNone(runs[text].find("w:rPr/w:rFonts", NS), text)
        baseline = io.BytesIO(); Document().save(baseline)
        with zipfile.ZipFile(baseline) as archive:
            self.assertEqual(archive.read("word/styles.xml"), styles)
        paragraph_styles = [node.get(f"{{{W}}}val") for node in root.findall(".//w:pPr/w:pStyle", NS)]
        self.assertIn("Title", paragraph_styles)
        self.assertIn("Heading1", paragraph_styles)

    def test_pure_latin_document_does_not_gain_direct_run_formatting(self):
        _, root, _, runs = self.create({"title": "Activity notice", "sections": [{"heading": "Details",
            "paragraphs": ["Bring paper and arrive ten minutes early."],
            "table": {"headers": ["Item", "Value"], "rows": [["Budget", "35"]]}}]})
        self.assertGreater(len(runs), 0)
        self.assertEqual([], root.findall(".//w:rPr", NS))

    def test_han_run_font_preserves_existing_latin_font_size_and_emphasis(self):
        from docx import Document
        from docx.shared import Pt
        run = Document().add_paragraph().add_run("AURA 活动 2026")
        run.font.name = "Arial"; run.font.size = Pt(17)
        run.bold = True; run.italic = True; run.style = "Strong"
        before = ET.fromstring(run._r.xml)
        document_tools._set_docx_han_run_font(run)
        after = ET.fromstring(run._r.xml)
        fonts = after.find("w:rPr/w:rFonts", NS)
        self.assertEqual("Microsoft YaHei", fonts.attrib.pop(f"{{{W}}}eastAsia"))
        self.assertEqual(ET.tostring(before), ET.tostring(after))

    def test_non_han_run_is_not_mutated_even_if_it_already_has_latin_formatting(self):
        from docx import Document
        run = Document().add_paragraph().add_run("AURA English 2026 カタカナ 한글 🙂")
        run.font.name = "Arial"; run.bold = True
        before = run._r.xml
        document_tools._set_docx_han_run_font(run)
        self.assertEqual(before, run._r.xml)


if __name__ == "__main__": unittest.main()
