"""Actual host-injected tool→LibreOffice→WorkspaceFiles PDF fixture; no cloud model."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))


async def exercise(output):
    from agent_backend.nanobot.libreoffice_render import LibreOfficeRenderAdapter, render_document
    from agent_backend.nanobot.safe_files import WorkspaceFiles
    from desktop_pet.agents import libreoffice_renderer as native
    from desktop_pet.agents.guardian_native import OwnedProcess, has_pending_cleanup
    python = PROJECT / "agent_backend/nanobot/.venv/Scripts/python.exe"
    inputs = output / "inputs"
    generated = subprocess.run([str(python), "-I", str(PROJECT / "tools/qa_libreoffice_render.py"),
        "--generate", str(inputs)], capture_output=True, timeout=30, creationflags=0x08000000)
    if generated.returncode: raise RuntimeError("SYNTHETIC_FIXTURE_GENERATION_FAILED")
    poppler = shutil.which("pdftoppm.exe")
    if not poppler: raise RuntimeError("BUNDLED_POPPLER_UNAVAILABLE")
    engine = native.LibreOfficeRenderer(runtime="E:/Tools/AURA/LibreOffice-26.8.2-extracted/runtime",
        python=python, pdftoppm=poppler, scratch_parent="E:/Tools/AURA/render-scratch", enabled=True)
    bound = LibreOfficeRenderAdapter(engine, cleanup_reconciler=engine.reconcile_cleanup_evidence)
    files = WorkspaceFiles(output)
    sources = {kind: hashlib.sha256((inputs / ("synthetic." + kind)).read_bytes()).hexdigest() for kind in ("docx", "xlsx", "pptx")}
    records = []
    for kind in sources:
        started = time.monotonic()
        result = json.loads(await render_document(files, "inputs/synthetic." + kind,
            output_path=kind + ".pdf", renderer=bound))
        records.append({"format": kind, "pages": result["pages"], "artifact": result["artifact"],
            "source_unchanged": sources[kind] == hashlib.sha256((inputs / ("synthetic." + kind)).read_bytes()).hexdigest(),
            "cleanup_confirmed": result["cleanup_confirmed"], "engine": result["engine"],
            "visual_layout_verified": result["visual_layout_verified"],
            "microsoft_office_rendered": result["microsoft_office_rendered"],
            "elapsed_ms": round((time.monotonic() - started) * 1000)})

    # Wait until this exact native process was created, then Stop twice. A
    # subsequent real conversion must work without host reconcile/replay.
    entered = threading.Event(); original = OwnedProcess.create; creations = []
    def observe(*args, **kwargs):
        process = original(*args, **kwargs)
        creations.append(1); entered.set(); return process
    with patch.object(OwnedProcess, "create", side_effect=observe):
        task = asyncio.create_task(render_document(files, "inputs/synthetic.docx",
            output_path="cancelled-must-not-exist.pdf", renderer=bound))
        for _ in range(1000):
            if entered.is_set(): break
            if task.done(): break
            await asyncio.sleep(.005)
        if not entered.is_set(): raise RuntimeError("OWNED_RENDERER_NOT_STARTED")
        task.cancel(); await asyncio.sleep(.01); task.cancel()
        stopped = False
        try: await task
        except asyncio.CancelledError: stopped = True
    clean = not bound.host_status()["cleanup_unknown"] and not bound.host_status()["busy"]
    resumed = json.loads(await render_document(files, "inputs/synthetic.docx",
        output_path="after-stop.pdf", renderer=bound))
    cancel = {"passed": stopped and clean and len(creations) == 1
        and not (output / "cancelled-must-not-exist.pdf").exists() and resumed["cleanup_confirmed"] is True,
        "actual_owned_processes": len(creations), "cancelled": stopped,
        "no_cancelled_publication": not (output / "cancelled-must-not-exist.pdf").exists(),
        "next_real_render_completed_without_reconcile": resumed["cleanup_confirmed"] is True}

    # The independent SDK reader uses only the fixed synthetic output paths.
    reader = r'''
import json,sys
from pathlib import Path
from pypdf import PdfReader
root=Path(sys.argv[1]); required={'docx':(2,['AURA Writer Render QA','AURA_WRITER_SECOND_PAGE_75319']),'xlsx':(1,['Notebook','Pens','10.00']),'pptx':(2,['AURA Impress Render QA','AURA_IMPRESS_SECOND_SLIDE_75319'])}
rows=[]
for kind,(pages,items) in required.items():
    pdf=PdfReader(root/(kind+'.pdf'),strict=True); text='\n'.join(page.extract_text() or '' for page in pdf.pages)
    rows.append({'format':kind,'pages':len(pdf.pages),'passed':len(pdf.pages)==pages and all(item in text for item in items)})
print(json.dumps(rows))
'''
    checked = subprocess.run([str(python), "-I", "-c", reader, str(output)], capture_output=True,
        timeout=30, creationflags=0x08000000)
    if checked.returncode: raise RuntimeError("INDEPENDENT_PDF_READBACK_FAILED")
    readback = json.loads(checked.stdout)
    only_pdf = sorted(path.name for path in output.iterdir() if path.is_file()) == ["after-stop.pdf", "docx.pdf", "pptx.pdf", "xlsx.pdf"]
    pending = bool(native._pending or native._unbound_handles or native._unbound_directories or has_pending_cleanup())
    return {"schema": "aura.libreoffice.adapter.qa.v1", "production_registered": False,
        "actual_adapter_api_call": True, "worker_tool_registry_used": False,
        "cloud_model_used": False, "production_documents_used": False,
        "records": records, "independent_pdf_readback": readback, "repeated_cancel": cancel,
        "only_declared_pdf_outputs": only_pdf, "pending_cleanup": pending,
        "passed": all(row["passed"] for row in readback) and all(row["source_unchanged"] for row in records)
            and cancel["passed"] and only_pdf and not pending}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=PROJECT / "artifacts/qa-libreoffice-adapter")
    args = parser.parse_args()
    output = args.output.absolute()
    # Fresh fixture directory avoids accidental replacement of existing files.
    output.mkdir(parents=True, exist_ok=False)
    try: report = asyncio.run(exercise(output))
    except BaseException:
        report = {"schema": "aura.libreoffice.adapter.qa.v1", "passed": False,
            "error": "CONTROLLED_ADAPTER_FIXTURE_FAILED", "production_registered": False}
    (output / "verification.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))
    return 0 if report["passed"] else 1


if __name__ == "__main__": raise SystemExit(main())
