"""Typed document import/export without shell, macros, or external references.

Readers use bounded local snapshots. Office rendering is deliberately not claimed:
structure/content readback is narrower than visual layout or full-goal verification.
"""
from __future__ import annotations

import io
import json
from pathlib import Path, PurePosixPath
import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile

from safe_files import MAX_DOCUMENT_BYTES, MAX_PAGE_CHARACTERS, WorkspaceFiles
if __package__:
    from .spreadsheet_formulas import SpreadsheetError, prepare_workbook, cache_and_verify_xlsx
else:
    from spreadsheet_formulas import SpreadsheetError, prepare_workbook, cache_and_verify_xlsx

_MAX_EXPANDED = 64 * 1024 * 1024
_MAX_MEMBERS = 3000
_MAX_XML = 16 * 1024 * 1024
_MAX_TEXT = 4 * 1024 * 1024
_HAN_TEXT = re.compile("[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0002fa1f\U00030000-\U000323af]")
_NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rels": "http://schemas.openxmlformats.org/package/2006/relationships",
}


class DocumentError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _set_docx_han_run_font(run):
    """Windows Chinese default for newly generated Han text; retain Latin fonts and style inheritance."""
    if not _HAN_TEXT.search(run.text):
        return
    from docx.oxml.ns import qn
    # Font.name only sets ascii/hAnsi. A direct eastAsia font prevents a theme
    # fallback from splitting common simplified Chinese glyphs during PDF export.
    run._r.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), "Microsoft YaHei")


class DocumentTools:
    def __init__(self, workspace: Path, *, files=None):
        self.files = files if files is not None else WorkspaceFiles(workspace)

    @staticmethod
    def _archive(raw):
        try:
            archive = zipfile.ZipFile(io.BytesIO(raw))
            entries = archive.infolist()
            names = [item.filename for item in entries]
            if (len(entries) > _MAX_MEMBERS or len(names) != len(set(names))
                    or sum(item.file_size for item in entries) > _MAX_EXPANDED
                    or any(item.flag_bits & 1 for item in entries)
                    or any(name.startswith(("/", "\\")) or "\\" in name
                           or ".." in PurePosixPath(name).parts for name in names)):
                archive.close()
                raise DocumentError("UNSAFE_DOCUMENT_PACKAGE")
            return archive
        except (zipfile.BadZipFile, zipfile.LargeZipFile):
            raise DocumentError("INVALID_DOCUMENT_PACKAGE") from None

    @staticmethod
    def _xml(archive, name):
        try:
            info = archive.getinfo(name)
            if info.file_size > _MAX_XML:
                raise DocumentError("DOCUMENT_XML_TOO_LARGE")
            data = archive.read(name)
            if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
                raise DocumentError("UNSAFE_DOCUMENT_XML")
            return ET.fromstring(data)
        except (KeyError, ET.ParseError, OSError, zipfile.BadZipFile):
            raise DocumentError("INVALID_DOCUMENT_XML") from None

    @staticmethod
    def _relationship(base, target):
        if not isinstance(target, str) or "\\" in target:
            raise DocumentError("INVALID_DOCUMENT_RELATIONSHIP")
        name = posixpath.normpath(target.lstrip("/") if target.startswith("/") else posixpath.join(base, target))
        if name.startswith("../") or name == "..":
            raise DocumentError("INVALID_DOCUMENT_RELATIONSHIP")
        return name

    def _office_text(self, raw, suffix, section=""):
        with self._archive(raw) as archive:
            metadata = {"sections": []}
            if suffix == ".docx":
                root = self._xml(archive, "word/document.xml")
                if root.tag != "{%s}document" % _NS["w"]:
                    raise DocumentError("INVALID_DOCUMENT_XML")
                paragraphs = []
                for paragraph in root.findall(".//w:p", _NS):
                    parts = []
                    for item in paragraph.iter():
                        if item.tag == "{%s}t" % _NS["w"] and item.text:
                            parts.append(item.text)
                        elif item.tag == "{%s}tab" % _NS["w"]:
                            parts.append("\t")
                        elif item.tag == "{%s}br" % _NS["w"]:
                            parts.append("\n")
                    paragraphs.append("".join(parts))
                text = "\n".join(paragraphs)
            elif suffix == ".xlsx":
                root = self._xml(archive, "xl/workbook.xml")
                if root.tag != "{%s}workbook" % _NS["s"]:
                    raise DocumentError("INVALID_DOCUMENT_XML")
                relationships = self._xml(archive, "xl/_rels/workbook.xml.rels")
                rels = {item.get("Id"): item.get("Target") for item in relationships
                        if item.get("TargetMode") != "External"}
                sheets = [(item.get("name", ""), item.get("{%s}id" % _NS["r"]))
                          for item in root.findall("s:sheets/s:sheet", _NS)]
                metadata["sections"] = [name for name, _ in sheets]
                selected = [item for item in sheets if item[0] == section] if section else sheets[:1]
                if not selected:
                    raise DocumentError("DOCUMENT_SECTION_NOT_FOUND")
                shared = []
                if "xl/sharedStrings.xml" in archive.namelist():
                    for item in self._xml(archive, "xl/sharedStrings.xml").findall("s:si", _NS):
                        shared.append("".join(node.text or "" for node in item.findall(".//s:t", _NS)))
                pieces = []
                cells = 0
                for name, identifier in selected:
                    if identifier not in rels:
                        raise DocumentError("INVALID_DOCUMENT_RELATIONSHIP")
                    sheet = self._xml(archive, self._relationship("xl", rels[identifier]))
                    pieces.append("Sheet: " + name)
                    for row in sheet.findall("s:sheetData/s:row", _NS):
                        values = []
                        for cell in row.findall("s:c", _NS):
                            cells += 1
                            if cells > 200_000:
                                raise DocumentError("DOCUMENT_CELL_LIMIT")
                            kind, value = cell.get("t"), cell.findtext("s:v", default="", namespaces=_NS)
                            if kind == "s":
                                try:
                                    index = int(value)
                                    if not 0 <= index < len(shared):
                                        raise ValueError()
                                    value = shared[index]
                                except (ValueError, IndexError):
                                    raise DocumentError("INVALID_DOCUMENT_CELL") from None
                            elif kind == "inlineStr":
                                value = "".join(t.text or "" for t in cell.findall(".//s:t", _NS))
                            formula = cell.findtext("s:f", default=None, namespaces=_NS)
                            if formula is not None:
                                value = f"FORMULA: ={formula}; cached_value={value or '[not calculated]'}"
                            values.append(f"{cell.get('r', '?')}={value}")
                        pieces.append("\t".join(values))
                text = "\n".join(pieces)
                metadata["selected_section"] = selected[0][0]
                metadata["formula_values_are_cached"] = True
            elif suffix == ".pptx":
                presentation = self._xml(archive, "ppt/presentation.xml")
                if presentation.tag != "{%s}presentation" % _NS["p"]:
                    raise DocumentError("INVALID_DOCUMENT_XML")
                relationships = self._xml(archive, "ppt/_rels/presentation.xml.rels")
                rels = {item.get("Id"): item.get("Target") for item in relationships
                        if item.get("TargetMode") != "External"}
                slides = [item.get("{%s}id" % _NS["r"])
                          for item in presentation.findall("p:sldIdLst/p:sldId", _NS)]
                metadata["sections"] = [str(i + 1) for i in range(len(slides))]
                if section:
                    if not section.isdigit() or not 1 <= int(section) <= len(slides):
                        raise DocumentError("DOCUMENT_SECTION_NOT_FOUND")
                    selected = [(int(section), slides[int(section) - 1])]
                else:
                    selected = list(enumerate(slides, 1))
                pieces = []
                for index, identifier in selected:
                    if identifier not in rels:
                        raise DocumentError("INVALID_DOCUMENT_RELATIONSHIP")
                    slide = self._xml(archive, self._relationship("ppt", rels[identifier]))
                    pieces.append(f"Slide {index}\n" + "\n".join(
                        "".join(item.text or "" for item in paragraph.findall(".//a:t", _NS))
                        for paragraph in slide.findall(".//a:p", _NS)))
                text = "\n\n".join(pieces)
            else:
                raise DocumentError("UNSUPPORTED_DOCUMENT_FORMAT")
            if len(text) > _MAX_TEXT:
                raise DocumentError("DOCUMENT_TEXT_LIMIT_SELECT_SECTION")
            return text, metadata

    @staticmethod
    def _page_numbers(value, total):
        if not isinstance(value, str):
            raise DocumentError("INVALID_PDF_PAGES")
        if not value:
            return list(range(min(total, 5)))
        if not isinstance(value, str) or len(value) > 200:
            raise DocumentError("INVALID_PDF_PAGES")
        result = []
        for item in value.split(","):
            item = item.strip()
            if not re.fullmatch(r"\d+(?:-\d+)?", item):
                raise DocumentError("INVALID_PDF_PAGES")
            bounds = [int(n) for n in item.split("-")]
            start, end = bounds[0], bounds[-1]
            if not 1 <= start <= end <= total or end - start > 19:
                raise DocumentError("INVALID_PDF_PAGES")
            result += [index - 1 for index in range(start, end + 1) if index - 1 not in result]
            if len(result) > 20:
                raise DocumentError("PDF_PAGE_LIMIT")
        return result

    def read_document(self, path: str, offset: int = 0, limit: int = 12_000,
                      section: str = "", pages: str = "", expected_hash=None) -> str:
        if (type(offset) is not int or offset < 0 or type(limit) is not int
                or not 1 <= limit <= MAX_PAGE_CHARACTERS or not isinstance(section, str)):
            raise DocumentError("INVALID_DOCUMENT_RANGE")
        target, raw, digest = self.files.read_bytes(path)
        expected = self.files._expected(expected_hash)
        if expected is not None and expected != digest:
            raise DocumentError("FILE_CHANGED")
        suffix, metadata = target.suffix.casefold(), {}
        if pages and suffix != ".pdf":
            raise DocumentError("PAGES_ONLY_SUPPORTED_FOR_PDF")
        if section and suffix not in {".xlsx", ".pptx"}:
            raise DocumentError("SECTION_ONLY_SUPPORTED_FOR_SHEETS_OR_SLIDES")
        if suffix in {".docx", ".xlsx", ".pptx"}:
            text, metadata = self._office_text(raw, suffix, section)
        elif suffix == ".pdf":
            try:
                from pypdf import PdfReader
            except ImportError:
                raise DocumentError("PDF_READER_UNAVAILABLE") from None
            try:
                reader = PdfReader(io.BytesIO(raw))
                if reader.is_encrypted:
                    raise DocumentError("PDF_PASSWORD_REQUIRED")
                selected = self._page_numbers(pages, len(reader.pages))
                extracted = [(index, reader.pages[index].extract_text() or "") for index in selected]
                text = "\n\n".join(f"Page {index + 1}\n{body}" for index, body in extracted)
                metadata = {"total_pages": len(reader.pages), "selected_pages": [p + 1 for p in selected],
                    "next_page": (max(selected) + 2 if selected and max(selected) + 1 < len(reader.pages) else None),
                    "image_only_pages_need_vision": any(not body for _, body in extracted)}
            except DocumentError:
                raise
            except Exception:
                raise DocumentError("PDF_READ_FAILED") from None
        else:
            return self.files.read_page(path, offset, limit, expected_hash=expected_hash)
        if len(text) > _MAX_TEXT:
            raise DocumentError("DOCUMENT_TEXT_LIMIT_SELECT_SECTION")
        body = text[offset:offset + limit]
        end = min(len(text), offset + len(body))
        return json.dumps({"path": self.files.display_path(target), "format": suffix.lstrip("."),
            "sha256": digest, "bytes": len(raw), "offset": offset, "returned": len(body),
            "total_characters": len(text), "truncated": end < len(text),
            "next_offset": end if end < len(text) else None, "text": body,
            "scope": "extracted_content_not_visual_layout", **metadata}, ensure_ascii=False)

    @staticmethod
    def _spec(spec):
        if not isinstance(spec, dict):
            raise DocumentError("INVALID_DOCUMENT_SPEC")
        try:
            encoded = json.dumps(spec, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError, RecursionError):
            raise DocumentError("INVALID_DOCUMENT_SPEC") from None
        if len(encoded) > 300_000:
            raise DocumentError("INVALID_DOCUMENT_SPEC")
        return spec

    @staticmethod
    def _text(value, *, limit=20_000):
        if not isinstance(value, str) or len(value) > limit or "\0" in value:
            raise DocumentError("INVALID_DOCUMENT_TEXT")
        return value

    def create_document(self, path: str, kind: str, spec: dict, overwrite=False, expected_hash=None) -> str:
        if (not isinstance(kind, str) or kind not in {"docx", "xlsx", "pptx"}
                or not isinstance(path, str) or Path(path).suffix.casefold() != "." + kind):
            raise DocumentError("INVALID_DOCUMENT_FORMAT")
        spreadsheet_plan = None
        if kind == "xlsx":
            if not isinstance(spec, dict): raise DocumentError("INVALID_DOCUMENT_SPEC")
            try: spreadsheet_plan = prepare_workbook(spec.get("sheets"))
            except SpreadsheetError as failure: raise DocumentError(failure.code) from None
        spec = self._spec(spec)
        self.files.resolve(path, write=True)
        expected_text = []
        stream = io.BytesIO()
        try:
            if kind == "xlsx":
                from openpyxl import Workbook
                from openpyxl.styles import Font, PatternFill
                workbook = Workbook()
                workbook.remove(workbook.active)
                for prepared in spreadsheet_plan.sheets:
                    sheet = workbook.create_sheet(prepared.name)
                    for values in prepared.rows:
                        sheet.append([cell.value for cell in values])
                        for cell, prepared_cell in zip(sheet[sheet.max_row], values):
                            cell.number_format = prepared_cell.number_format
                            # A formula-like string is still a literal. Only
                            # an explicit closed typed object may emit <f>.
                            if prepared_cell.kind == "string":
                                cell.data_type = "s"
                                if cell.value: expected_text.append(cell.value)
                    if prepared.has_columns:
                        for cell in sheet[1]:
                            cell.font = Font(bold=True, color="FFFFFF")
                            cell.fill = PatternFill("solid", fgColor="286B99")
                        sheet.freeze_panes = "A2"
                        sheet.auto_filter.ref = sheet.dimensions
                    for column in sheet.columns:
                        width = min(50, max(12, max((len(str(cell.value or "")) for cell in column), default=0) + 2))
                        sheet.column_dimensions[column[0].column_letter].width = width
                workbook.save(stream)
            else:
                title = self._text(spec.get("title", ""), limit=400)
                sections = spec.get("sections", [])
                if not isinstance(sections, list) or len(sections) > 100:
                    raise DocumentError("INVALID_DOCUMENT_SECTIONS")
                if kind == "docx":
                    from docx import Document
                    document = Document()
                    if title:
                        document.add_heading(title, 0)
                        expected_text.append(title)
                    for section in sections:
                        heading, paragraphs, table = self._section(section)
                        if heading:
                            document.add_heading(heading, 1); expected_text.append(heading)
                        for paragraph in paragraphs:
                            document.add_paragraph(paragraph); expected_text.append(paragraph)
                        if table:
                            headers, rows = table
                            widget = document.add_table(rows=1, cols=len(headers)); widget.style = "Light Shading Accent 1"
                            for cell, value in zip(widget.rows[0].cells, headers): cell.text = str(value)
                            for row in rows:
                                for cell, value in zip(widget.add_row().cells, row): cell.text = str(value)
                            expected_text.extend(str(v) for row in [headers, *rows] for v in row)
                    for paragraph in document.paragraphs:
                        for run in paragraph.runs:
                            _set_docx_han_run_font(run)
                    for widget in document.tables:
                        for row in widget.rows:
                            for cell in row.cells:
                                for paragraph in cell.paragraphs:
                                    for run in paragraph.runs:
                                        _set_docx_han_run_font(run)
                    document.save(stream)
                else:
                    from pptx import Presentation
                    from pptx.util import Inches, Pt
                    presentation = Presentation()
                    presentation.slide_width, presentation.slide_height = Inches(13.333), Inches(7.5)
                    if title:
                        slide = presentation.slides.add_slide(presentation.slide_layouts[0])
                        slide.shapes.title.text = title; expected_text.append(title)
                        slide.placeholders[1].text = self._text(spec.get("subtitle", ""), limit=1000)
                    for section in sections:
                        heading, paragraphs, table = self._section(section)
                        slide = presentation.slides.add_slide(presentation.slide_layouts[1 if not table else 5])
                        slide.shapes.title.text = heading; expected_text.append(heading)
                        if table:
                            headers, rows = table
                            shape = slide.shapes.add_table(len(rows) + 1, len(headers), Inches(.7), Inches(1.6), Inches(12), Inches(4.8))
                            for i, row in enumerate([headers, *rows]):
                                for j, value in enumerate(row):
                                    shape.table.cell(i, j).text = str(value)
                            expected_text.extend(str(v) for row in [headers, *rows] for v in row)
                        else:
                            frame = slide.placeholders[1].text_frame
                            frame.clear()
                            for index, paragraph in enumerate(paragraphs):
                                p = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
                                p.text = paragraph; p.font.size = Pt(24); expected_text.append(paragraph)
                    presentation.save(stream)
        except ImportError:
            raise DocumentError("DOCUMENT_WRITER_UNAVAILABLE") from None
        except SpreadsheetError as failure:
            raise DocumentError(failure.code) from None
        raw = stream.getvalue()
        if spreadsheet_plan is not None:
            try: raw = cache_and_verify_xlsx(raw, spreadsheet_plan)
            except SpreadsheetError as failure: raise DocumentError(failure.code) from None
        if len(raw) > MAX_DOCUMENT_BYTES:
            raise DocumentError("DOCUMENT_SIZE_LIMIT")
        # Readback is done before publishing; malformed or incomplete packages
        # never replace a pre-existing user file.
        extracted, metadata = self._office_text(raw, "." + kind)
        if kind == "xlsx":
            parts = [self._office_text(raw, ".xlsx", name)[0] for name in metadata["sections"]]
            extracted = "\n".join(parts)
        if any(value and value not in extracted for value in expected_text):
            raise DocumentError("DOCUMENT_CONTENT_READBACK_FAILED")
        result = json.loads(self.files.write_bytes(path, raw, overwrite, expected_hash))
        result["verification"] = {"passed": True, "scope": "generated_package_and_content_readback",
                                  "visual_layout_verified": False, "expected_text_items": len(expected_text)}
        if spreadsheet_plan is not None:
            result["verification"]["spreadsheet_calculation"] = spreadsheet_plan.verification()
        return json.dumps(result, ensure_ascii=False)

    def _section(self, item):
        if not isinstance(item, dict):
            raise DocumentError("INVALID_DOCUMENT_SECTION")
        heading = self._text(item.get("heading", ""), limit=400)
        paragraphs = item.get("paragraphs", [])
        if not isinstance(paragraphs, list) or len(paragraphs) > 1000:
            raise DocumentError("INVALID_DOCUMENT_PARAGRAPHS")
        paragraphs = [self._text(p) for p in paragraphs]
        table = item.get("table")
        if table is not None:
            if not isinstance(table, dict):
                raise DocumentError("INVALID_DOCUMENT_TABLE")
            headers, rows = table.get("headers"), table.get("rows", [])
            if (not isinstance(headers, list) or not 1 <= len(headers) <= 30
                    or not isinstance(rows, list) or len(rows) > 1000
                    or any(not isinstance(row, list) or len(row) != len(headers) for row in rows)):
                raise DocumentError("INVALID_DOCUMENT_TABLE")
            for row in [headers, *rows]:
                for value in row:
                    if not isinstance(value, (str, int, float, bool)) or len(str(value)) > 5000:
                        raise DocumentError("INVALID_DOCUMENT_TABLE")
            table = headers, rows
        return heading, paragraphs, table

    def describe_artifact(self, path: str) -> str:
        target, raw, digest = self.files.read_bytes(path)
        import mimetypes
        return json.dumps({"path": self.files.display_path(target), "name": target.name,
            "sha256": digest, "bytes": len(raw),
            "mime": mimetypes.guess_type(target.name)[0] or "application/octet-stream",
            "scope": "existing_file_identity_not_full_goal_verification"}, ensure_ascii=False)

