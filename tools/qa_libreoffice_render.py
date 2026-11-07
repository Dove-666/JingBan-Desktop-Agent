"""Fixed synthetic DOCX/XLSX/PPTX -> real isolated LibreOffice -> PDF/PNG QA."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))


def fixtures(root):
    from docx import Document
    from docx.shared import Inches, Pt
    document = Document()
    section = document.sections[0]
    section.top_margin = section.bottom_margin = Inches(.8)
    document.add_heading("AURA Writer Render QA", 0)
    document.add_paragraph("隔离排版验证 · Synthetic fixtures only")
    document.add_paragraph("This file was created only to check real pagination, text and page previews.")
    table = document.add_table(rows=1, cols=2); table.style = "Light Shading Accent 1"
    table.rows[0].cells[0].text, table.rows[0].cells[1].text = "Check", "Expected"
    for name, expected in (("Source", "Unchanged"), ("Profile", "Private per task"), ("Output", "Real PDF pages")):
        cells = table.add_row().cells; cells[0].text, cells[1].text = name, expected
    document.add_page_break(); document.add_heading("Writer Page Two", 1)
    document.add_paragraph("AURA_WRITER_SECOND_PAGE_75319")
    document.save(root / "synthetic.docx")

    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    workbook = Workbook(); sheet = workbook.active; sheet.title = "Budget"
    for row in [("Item", "Quantity", "Unit Price", "Amount"), ("Notebook", 3, 2.5, "=ROUND(B2*C2,2)"),
                ("Pens", 2, 1.25, "=ROUND(B3*C3,2)"), ("Total", None, None, "=SUM(D2:D3)")]: sheet.append(row)
    for cell in sheet[1]: cell.font = Font(color="FFFFFF", bold=True); cell.fill = PatternFill("solid", fgColor="244B73")
    for row in sheet.iter_rows(min_row=2, max_row=4, min_col=3, max_col=4):
        for cell in row: cell.number_format = "0.00"
    for column, width in (("A", 24), ("B", 14), ("C", 18), ("D", 18)): sheet.column_dimensions[column].width = width
    for row in sheet:
        for cell in row: cell.alignment = Alignment(vertical="center")
    sheet.print_area = "A1:D4"; sheet.sheet_properties.pageSetUpPr.fitToPage = True
    sheet.page_setup.fitToWidth = 1; sheet.page_setup.fitToHeight = 1
    workbook.save(root / "synthetic.xlsx")

    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.util import Inches as PptInches, Pt as PptPt
    deck = Presentation(); deck.slide_width = PptInches(13.333); deck.slide_height = PptInches(7.5)
    for title, text in (("AURA Impress Render QA", "Actual slide rendering / Private profile / No production data"),
                        ("Slide Two", "AURA_IMPRESS_SECOND_SLIDE_75319")):
        slide = deck.slides.add_slide(deck.slide_layouts[6])
        slide.background.fill.solid(); slide.background.fill.fore_color.rgb = RGBColor(239, 246, 255)
        box = slide.shapes.add_textbox(PptInches(.85), PptInches(1.2), PptInches(11.6), PptInches(1.3))
        paragraph = box.text_frame.paragraphs[0]; paragraph.text = title; paragraph.font.size = PptPt(36)
        paragraph.font.bold = True; paragraph.font.color.rgb = RGBColor(26, 57, 91)
        box = slide.shapes.add_textbox(PptInches(.9), PptInches(3), PptInches(11.4), PptInches(1.2))
        paragraph = box.text_frame.paragraphs[0]; paragraph.text = text; paragraph.font.size = PptPt(24)
    deck.save(root / "synthetic.pptx")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--generate", type=Path)
    parser.add_argument("--runtime", type=Path, default=Path("E:/Tools/AURA/LibreOffice-26.8.2-extracted/runtime"))
    parser.add_argument("--output", type=Path, default=PROJECT / "artifacts/qa-libreoffice-render")
    parser.add_argument("--failures-only", action="store_true", help="Actual owned Job timeout/cancel checks, synthetic input only")
    args = parser.parse_args()
    if args.generate:
        args.generate.mkdir(parents=True, exist_ok=True); fixtures(args.generate); return 0
    from desktop_pet.agents.libreoffice_renderer import LibreOfficeRenderer, LibreOfficeError
    output = args.output.absolute(); output.mkdir(parents=True, exist_ok=True)
    inputs = output / "inputs"
    python = PROJECT / "agent_backend/nanobot/.venv/Scripts/python.exe"
    generated = subprocess.run([str(python), "-I", str(Path(__file__).resolve()), "--generate", str(inputs)],
        capture_output=True, timeout=20, creationflags=0x08000000)
    if generated.returncode: raise ValueError("SYNTHETIC_FIXTURE_GENERATION_FAILED")
    pdftoppm = shutil.which("pdftoppm.exe")
    if not pdftoppm: raise ValueError("BUNDLED_POPPLER_UNAVAILABLE")
    renderer = LibreOfficeRenderer(runtime=args.runtime, python=python, pdftoppm=pdftoppm,
        scratch_parent=Path("E:/Tools/AURA/render-scratch"), enabled=True)
    if args.failures_only:
        from desktop_pet.agents import libreoffice_renderer as prototype
        from desktop_pet.agents.guardian_native import OwnedProcess, has_pending_cleanup
        from unittest.mock import patch
        import threading
        raw = (inputs / "synthetic.docx").read_bytes(); before = hashlib.sha256(raw).hexdigest()
        records = []
        for action in ("timeout", "cancel"):
            baseline = set(renderer.scratch_parent.glob("libreoffice-owned-*"))
            event = threading.Event(); timers = []
            original = OwnedProcess.create
            def observed(*args, **kwargs):
                result = original(*args, **kwargs)
                if action == "cancel":
                    timer = threading.Timer(.15, event.set); timers.append(timer); timer.start()
                return result
            expected = "LIBREOFFICE_TIMEOUT" if action == "timeout" else "LIBREOFFICE_CANCELLED"
            code = "UNEXPECTED_SUCCESS"; started = time.monotonic()
            try:
                with patch.object(OwnedProcess, "create", side_effect=observed) as actual_process:
                    try: renderer.render(raw, ".docx", expected_source_sha256=before,
                        cancel=event, timeout_seconds=.02 if action == "timeout" else 30)
                    except LibreOfficeError as error: code = error.code
            finally:
                for timer in timers: timer.cancel(); timer.join(1)
            clean = not prototype._pending and not prototype._unbound_handles and not prototype._unbound_directories and not has_pending_cleanup()
            same_directories = set(renderer.scratch_parent.glob("libreoffice-owned-*")) == baseline
            records.append({"scenario": action, "passed": code == expected and clean and same_directories and actual_process.call_count == 1,
                "error": code, "actual_owned_processes": actual_process.call_count, "no_unconfirmed_cleanup": clean,
                "pending_cleanup": [{"stage": run.cleanup_stage, "effects_closed": run.effects_closed} for run in prototype._pending],
                "owned_scratch_restored": same_directories, "source_unchanged": hashlib.sha256((inputs / "synthetic.docx").read_bytes()).hexdigest() == before,
                "elapsed_ms": round((time.monotonic() - started) * 1000)})
        report = {"schema": "aura.libreoffice.failure_qa.v1", "prototype_only": True,
            "production_integration": False, "passed": all(row["passed"] for row in records), "records": records}
        (output / "failure-verification.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report)); return 0 if report["passed"] else 1
    requirements = {"docx": (2, ["AURA Writer Render QA", "AURA_WRITER_SECOND_PAGE_75319"]),
        "xlsx": (1, ["Notebook", "Pens", "Total", "10.00"]),
        "pptx": (2, ["AURA Impress Render QA", "AURA_IMPRESS_SECOND_SLIDE_75319"])}
    records = []
    for kind, (pages, text_items) in requirements.items():
        source = inputs / ("synthetic." + kind); raw = source.read_bytes()
        before = hashlib.sha256(raw).hexdigest(); started = time.monotonic()
        try:
            result = renderer.render(raw, "." + kind, expected_source_sha256=before)
            artifact = output / kind; artifact.mkdir(exist_ok=True)
            (artifact / "rendered.pdf").write_bytes(result.pdf)
            (artifact / "text.txt").write_text(result.text, encoding="utf-8")
            for index, image in enumerate(result.previews, 1): (artifact / ("page-%02d.png" % index)).write_bytes(image)
            checks = [item in result.text for item in text_items]
            records.append({"format": kind, "passed": result.pages == pages and all(checks),
                "pages": result.pages, "expected_pages": pages, "text_checks": checks,
                "previews": len(result.previews), "pdf_sha256": result.pdf_sha256,
                "source_unchanged": hashlib.sha256(source.read_bytes()).hexdigest() == before,
                "cleanup_confirmed": result.cleanup_confirmed, "human_visual_review": False,
                "elapsed_ms": round((time.monotonic() - started) * 1000)})
        except LibreOfficeError as error:
            from desktop_pet.agents import libreoffice_renderer as prototype
            from desktop_pet.agents.guardian_native import Accounting
            import ctypes as C
            pending = []
            for run in prototype._pending:
                active = Accounting()
                queried = run.native.k.QueryInformationJobObject(run.job.handle, 1, C.byref(active), C.sizeof(active), None) if run.job.handle else False
                pending.append({"stage": run.cleanup_stage, "effects_closed": run.effects_closed,
                    "active_processes": active.active if queried else None,
                    "root_handle_present": bool(run.process.handle),
                    "root_signaled": run.native.signaled(run.process.handle) if run.process.handle else None})
            recovered = prototype.reconcile_cleanup()
            records.append({"format": kind, "passed": False, "error": error.code,
                "pending_cleanup": pending, "explicit_cleanup_reconciled": recovered,
                "source_unchanged": hashlib.sha256(source.read_bytes()).hexdigest() == before,
                "elapsed_ms": round((time.monotonic() - started) * 1000)})
    report = {"schema": "aura.libreoffice.qa.v1", "prototype_only": True,
        "production_integration": False, "real_libreoffice_rendering": all(row["passed"] for row in records),
        "microsoft_office_rendering": False, "production_documents_used": False, "records": records}
    (output / "verification.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))
    return 0 if report["real_libreoffice_rendering"] else 1


if __name__ == "__main__": raise SystemExit(main())
