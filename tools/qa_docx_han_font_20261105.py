"""0-model-API DOCX generation -> production host PDF export -> independent text/PNG evidence."""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
from pathlib import Path
import sys
import zipfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "agent_backend/nanobot"))
from document_tools import DocumentTools
from agent_backend.nanobot.host_office_runtime import HostOfficeRuntime
from agent_backend.nanobot.safe_files import WorkspaceFiles

TITLE = "周末活动安排"
CHINESE = {"title": TITLE, "sections": [
    {"heading": "活动主题：周末城市观察", "paragraphs": ["以下是本次活动的具体安排，请同学们留意时间、地点与费用。"],
     "table": {"headers": ["项目", "具体安排"], "rows": [["时间", "周日 14:30"], ["地点", "南门图书馆一楼"], ["每人预算", "35 元"]]}},
    {"paragraphs": ["温馨提示：请参加的同学带纸笔，提前十分钟集合。"]},
]}
ENGLISH = {"title": "Weekend activity notice", "sections": [
    {"heading": "Schedule", "paragraphs": ["Bring paper and arrive ten minutes early."],
     "table": {"headers": ["Item", "Details"], "rows": [["Time", "Sunday 14:30"], ["Budget", "35"]]}},
]}
NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


async def exercise(output, expect_fix):
    from pypdf import PdfReader
    config = json.loads((ROOT / "agent_backend/nanobot/runtime/office_renderer.json").read_text(encoding="utf-8"))
    source = ROOT / "agent_backend/nanobot/document_tools.py"
    before = sha(source)
    rows = []
    for name, spec in (("chinese", CHINESE), ("latin", ENGLISH)):
        folder = output / name
        folder.mkdir()
        files = WorkspaceFiles(folder)
        documents = DocumentTools(folder, files=files)
        created = json.loads(documents.create_document("notice.docx", "docx", spec))
        readback = json.loads(documents.read_document("notice.docx"))
        source_hash = sha(folder / "notice.docx")
        with zipfile.ZipFile(folder / "notice.docx") as archive:
            xml = ET.fromstring(archive.read("word/document.xml"))
            run_fonts = [{"text": "".join(run.itertext()), "fonts": fonts.attrib if fonts is not None else {}}
                for run in xml.findall(".//w:r", NS) for fonts in [run.find("w:rPr/w:rFonts", NS)]]
        runtime = HostOfficeRuntime(config, files)
        receipt = json.loads(await runtime.render_document("notice.docx", "notice.pdf"))
        (folder / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
        reader = PdfReader(folder / "notice.pdf")
        pdf_text = "\n".join(page.extract_text() or "" for page in reader.pages)
        # Export exact production preview bytes, rather than a second differently configured rendering.
        for metadata in receipt.get("preview_pages", []):
            preview = runtime.preview(metadata["reference"])
            png = base64.b64decode(preview.data_url.split(",", 1)[1], validate=True)
            assert hashlib.sha256(png).hexdigest() == metadata["png_sha256"]
            (folder / f"page-{metadata['page']}.png").write_bytes(png)
        row = {"variant": name, "readback_text": readback["text"], "pdf_text": pdf_text,
            "title_correct": spec["title"] in pdf_text, "pages": len(reader.pages), "runs": run_fonts,
            "source_unchanged_by_export": source_hash == sha(folder / "notice.docx"),
            "created_sha256": created["sha256"], "docx_sha256": source_hash, "pdf_sha256": sha(folder / "notice.pdf"),
            "preview_status": receipt.get("preview_status"), "engine": receipt.get("engine"),
            "layout_manual_review_pending": True}
        rows.append(row)
    report = {"schema": "aura.docx_han_font.qa.v1", "real_model_requests": 0,
        "source_before": before, "source_after": sha(source), "sources_stable": before == sha(source),
        "expected_fix": expect_fix, "rows": rows,
        "passed": before == sha(source) and all(row["source_unchanged_by_export"] and row["pages"] == 1
            and row["preview_status"] == "available" for row in rows)
            and rows[1]["title_correct"] and (rows[0]["title_correct"] == expect_fix),
        "scope": "new owned synthetic DOCX only; actual installed HostOfficeRuntime; Windows Chinese default font; no cross-platform CJK guarantee"}
    (output / "verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "sources_stable": report["sources_stable"],
        "real_model_requests": 0, "rows": [{key: row[key] for key in ("variant", "title_correct", "pages", "engine")} for row in rows]}, ensure_ascii=False))
    assert report["passed"], "See retained verification.json and PNGs"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--expect-fix", action="store_true")
    args = parser.parse_args()
    output = args.output.resolve()
    assert output.is_relative_to((ROOT / "artifacts/user-testing-20261105").resolve())
    output.mkdir(parents=True, exist_ok=False)
    asyncio.run(exercise(output, args.expect_fix))
