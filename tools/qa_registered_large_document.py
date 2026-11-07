"""Opt-in real-model rendered-image task over an owned synthetic DOCX.

The visual answer exists only in generated pixels, not in the task or document
text. Default invocation does not read credentials, create files or call a model.
Reports retain fixed checks and hashes, not image data or provider responses.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))


def sha(raw): return hashlib.sha256(raw).hexdigest()


def unique_members(pairs):
    value = {}
    for key, item in pairs:
        if key in value: raise ValueError("DUPLICATE_JSON_MEMBER")
        value[key] = item
    return value


def valid_digest(value):
    return type(value) is str and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def valid_edge(value): return type(value) is int and 1 <= value <= 1800


def prepare(workspace, python):
    label = "AURA_" + secrets.token_hex(5).upper()
    # Fixed trusted fixture code, never a model-provided Python string. The
    # large noisy border exercises derived transport; the central fact is clear.
    source = """
import io,random,sys
from pathlib import Path
from PIL import Image,ImageDraw,ImageFont
from docx import Document
from docx.shared import Inches
image=Image.frombytes('RGB',(1300,900),random.Random(75319).randbytes(1300*900*3))
draw=ImageDraw.Draw(image)
draw.ellipse((235,35,1065,865),fill=(255,151,40),outline=(30,45,70),width=12)
font=ImageFont.truetype('C:/Windows/Fonts/arialbd.ttf',70)
draw.text((650,450),sys.argv[2],fill=(10,25,45),font=font,anchor='mm')
buffer=io.BytesIO();image.save(buffer,format='PNG');image.close()
document=Document();section=document.sections[0]
section.top_margin=section.bottom_margin=Inches(.6)
document.add_heading('AURA Visual Reference',0)
document.add_paragraph('Owned synthetic fixture; read the central picture.')
document.add_picture(io.BytesIO(buffer.getvalue()),width=Inches(6.5))
document.save(sys.argv[1])
"""
    subprocess.run([str(python), "-I", "-c", source, str(workspace/"visual.docx"), label],
        capture_output=True, timeout=20, check=True, creationflags=0x08000000)
    return {"shape": "circle", "color": "orange", "label": label}


def oracle(workspace, expected, source_hash, history):
    from tools.qa_registered_agent_tasks import tool_results
    try: answer = json.loads((workspace/"answer.json").read_text(encoding="utf-8"), object_pairs_hook=unique_members)
    except (OSError, ValueError): answer = None
    pdf = workspace/"visual.pdf"
    pdf_hash = sha(pdf.read_bytes()) if pdf.is_file() else None
    reviews = tool_results(history, "inspect_document_page")
    renderings = tool_results(history, "render_document")
    rendered_pages = [page for row in renderings for page in row.get("preview_pages", []) if isinstance(page, dict)]
    actual = [row for row in reviews if row.get("scope") == "model_visual_observation"
        and row.get("pdf_sha256") == pdf_hash and type(row.get("page")) is int and row["page"] == 1
        and row.get("reference_only") is True and row.get("visual_layout_verified") is False
        and row.get("whole_document_verified") is False and row.get("derived") is True
        and row.get("sent_content_type") == "image/jpeg"
        and isinstance(row.get("observation"), str) and bool(row["observation"].strip())
        and valid_digest(row.get("png_sha256")) and valid_digest(row.get("sent_image_sha256"))
        and valid_edge(row.get("sent_width")) and valid_edge(row.get("sent_height"))
        and any(page.get("png_sha256") == row.get("png_sha256") and page.get("reference") == row.get("reference")
            and page.get("pdf_sha256") == pdf_hash and type(page.get("page")) is int and page["page"] == 1
            and valid_edge(page.get("width")) and valid_edge(page.get("height")) for page in rendered_pages)
        and row.get("png_sha256") != row.get("sent_image_sha256")]
    # The marker is only in pixels. A text extractor must not reveal the answer
    # through the fixture's heading, task, or document XML text.
    text_reads = tool_results(history, "read_document")
    checks = {
        "EXACT_VISUAL_FACTS": answer == expected,
        "CURRENT_PDF_DERIVED_PAGE_OBSERVATION": bool(actual),
        "SOURCE_DOCX_UNCHANGED": (workspace/"visual.docx").is_file() and sha((workspace/"visual.docx").read_bytes()) == source_hash,
        "ONLY_DECLARED_FILES": {p.name for p in workspace.iterdir()} == {"visual.docx", "visual.pdf", "answer.json"},
        "NO_IMAGE_DATA_IN_PARENT_HISTORY": "data:image" not in json.dumps(history, ensure_ascii=False).casefold(),
        "NO_TEXT_READ_LEAKS_VISUAL_MARKER": all(expected["label"] not in row.get("text", "") for row in text_reads if row.get("path") in {"visual.docx", "visual.pdf"}),
    }
    metadata = [{key: row.get(key) for key in ("pdf_sha256", "png_sha256", "sent_image_sha256", "sent_content_type", "derived", "sent_width", "sent_height")} for row in actual]
    return {"passed": all(checks.values()), "checks": checks}, metadata


def run(configured, timeout):
    from desktop_pet.agents import AgentService, AgentRequest
    from desktop_pet.agents.contracts import AgentError
    from desktop_pet.agents.settings import DEFAULT_PYTHON
    from desktop_pet.agents.service import _ERROR_MESSAGES
    from tools.run_agent_benchmarks import runtime_fingerprint, project_execution_diagnostics, project_finish_diagnostics
    from agent_backend.nanobot.acceptance_report import checked_assessment
    parent = ROOT/"agent_workspace"; parent.mkdir(exist_ok=True)
    workspace = Path(tempfile.mkdtemp(prefix="registered-visual-large-", dir=parent))
    python = configured.settings.python_executable or DEFAULT_PYTHON
    expected = prepare(workspace, python)
    source_hash = sha((workspace/"visual.docx").read_bytes())
    task = ("请把已有 visual.docx 用 render_document 导出 visual.pdf，并使用第一页引用调用 inspect_document_page，"
        "识别图片中央图形的形状、填充颜色和图形内部完整标签。答案只存在于图片像素中。"
        "将结果写入 answer.json，严格只含 shape、color、label 三个字符串字段；shape 和 color 使用小写英文名称，label 完整照录。"
        "只能新增 visual.pdf 和 answer.json。不要声称单页观察验证了整份文档质量。")
    initial, runner_before, start = runtime_fingerprint(), sha(Path(__file__).read_bytes()), time.monotonic()
    service, handles, diagnostics = AgentService(), [], []
    result, code, cid = None, None, str(uuid4())
    def diagnostic(frame):
        diagnostics.extend(project_execution_diagnostics([frame])["events"]); del diagnostics[:-64]
    try:
        settings = replace(configured.settings, enabled=True, tools_enabled=True, computer_enabled=False,
            workspace=str(workspace), max_steps=0, timeout_seconds=timeout)
        try:
            result = service.execute(settings, configured.key, AgentRequest(task, "agent"),
                {"api": configured.api, "messages": [], "conversation_id": cid},
                worker_lifecycle_callback=handles.append, diagnostic_callback=diagnostic)
        except AgentError as error: code = error.code if error.code in _ERROR_MESSAGES else "QA_AGENT_ERROR"
        history = service._sessions.get(cid, [])
        checked, transport = oracle(workspace, expected, source_hash, history)
        closed = bool(handles and service.validate_worker_closure(handles[0], service.worker_closure(handles[0])))
        final, runner_after = runtime_fingerprint(), sha(Path(__file__).read_bytes())
        return {"schema": "aura.registered_large_visual_task.v1", "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "passed" if result and result.status == "completed" and checked["passed"] and closed
                and initial == final and runner_before == runner_after and configured.unchanged() else "failed",
            "model": configured.model, "agent_status": result.status if result else None, "error_code": code,
            "duration_seconds": round(time.monotonic()-start, 3), "independent_oracle": checked,
            "worker_closed": closed, "host_transport_metadata": transport,
            "tool_names": sorted({row.get("name", "") for row in history if row.get("role") == "tool"}),
            "final_acceptance": checked_assessment(result.acceptance) if result else None,
            "finish_diagnostics": project_finish_diagnostics(history), "execution_diagnostics": project_execution_diagnostics(diagnostics),
            "source_snapshot": {"before": initial, "after": final, "unchanged": initial == final},
            "runner_source_snapshot": {"before": runner_before, "after": runner_after, "unchanged": runner_before == runner_after},
            "settings_unchanged": configured.unchanged(), "owned_workspace": str(workspace),
            "artifacts": [{"name": p.name, "bytes": p.stat().st_size, "sha256": sha(p.read_bytes())} for p in workspace.iterdir() if p.is_file()],
            "scope": "one owned synthetic visual-answer task; explicit tool use; not screenshot/desktop or product parity",
            "raw_model_data_saved": False}
    finally: service.shutdown()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("offline", "configured"), default="offline")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--output", type=Path, default=ROOT/"artifacts/registered-large-visual-task.json")
    args = parser.parse_args()
    if not 5 <= args.timeout <= 900: parser.error("Timeout must be 5..900 seconds")
    if args.provider == "offline":
        print(json.dumps({"status": "not_run", "model_called": False})); return 0
    from tools.run_agent_benchmarks import ConfiguredExecutor
    try: report = run(ConfiguredExecutor(args.timeout), args.timeout)
    except Exception:
        report = {"status": "failed", "error_code": "QA_PREPARATION_OR_BOUNDARY_FAILED", "raw_model_data_saved": False}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2)+"\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "oracle_passed": report.get("independent_oracle", {}).get("passed")}))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__": raise SystemExit(main())
