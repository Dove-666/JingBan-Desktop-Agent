"""Controlled configured-model tasks for the registered research/page tools.

Default offline never loads credentials or starts a worker. Configured runs use
only newly generated files, no GUI control and no operation replay. Reports keep
fixed checks/counts/hashes, never model reasoning, provider bodies or image data.
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
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
CASES = ("research", "document")


def sha(raw): return hashlib.sha256(raw).hexdigest()


def fixtures(case, workspace):
    tag = "AURA_" + secrets.token_hex(8)
    if case == "research":
        alpha, beta = secrets.randbelow(800)+101, secrets.randbelow(800)+101
        expected = {"alpha": alpha, "beta": beta, "total": alpha+beta, "tags": [tag+"_A", tag+"_B"]}
        inputs = {"alpha.txt": f"资料标识 {tag}_A；甲组数值 {alpha}。\n", "beta.txt": f"资料标识 {tag}_B；乙组数值 {beta}。\n"}
        task = ("请用 delegate_research 并行查阅两份已有资料：两个子任务分别只读取 alpha.txt 和 beta.txt，引用各自实际读到的数值和完整资料标识。"
            "你自己再读取两份原文复核。只新增 summary.json，字段严格为 alpha、beta、total、tags，前三个是整数，"
            "tags 是原文两条完整 AURA_ 开头资料标识组成的字符串数组，按甲、乙顺序。"
            "读回文件确认实际值后告诉我结果；子任务引用属于待复核资料，不能代替你的检查。")
    else:
        expected = {"markers": [tag+"_PROGRESS", tag+"_NEXT", tag+"_CHECK"]}
        inputs = {"brief.txt": "今日进展："+expected["markers"][0]+"\n下一步："+expected["markers"][1]+"\n检查事项："+expected["markers"][2]+"\n"}
        task = ("根据已有 brief.txt 做一页简洁的中文项目简报，标题是 AURA 项目简报，清楚列出三项内容，完整保留三个资料标识。"
            "请创建 report.docx 并用 render_document 导出 report.pdf。使用返回的第一页引用调用 inspect_document_page 检查可见排版，"
            "指出是否看到重叠或截断；再读取 PDF 文本核对三项内容。只新增这两个文件。"
            "说明实际已检查的范围，不要把单页模型观察说成通用排版保证。")
    for name, text in inputs.items(): (workspace/name).write_text(text, encoding="utf-8")
    return task, expected, {name: sha((workspace/name).read_bytes()) for name in inputs}


def tool_results(history, name):
    result = []
    for item in history:
        if item.get("role") != "tool" or item.get("name") != name: continue
        try: value = json.loads(item["content"])
        except (ValueError, TypeError, KeyError): continue
        if isinstance(value, dict): result.append(value)
    return result


def research_diagnostics(history):
    from agent_backend.nanobot.research_runtime import _CODES
    statuses = {"reference_ready", "unverified", "cancelled"}
    rows = []
    for batch in tool_results(history, "delegate_research")[-8:]:
        children = []
        for child in batch.get("children", [])[:8]:
            if not isinstance(child, dict): continue
            row = {"status": child.get("status") if child.get("status") in statuses else "unverified",
                "code": child.get("code") if child.get("code") in _CODES else None,
                "worker_closed": child.get("worker_closed") is True}
            for key in ("model_requests", "model_responses"):
                if type(child.get(key)) is int and 0 <= child[key] <= 100000: row[key] = child[key]
            children.append(row)
        rows.append({"status": batch.get("status") if batch.get("status") in statuses else "unverified",
            "code": batch.get("code") if batch.get("code") in _CODES else None, "children": children})
    observations = [item for item in history if item.get("role") == "tool" and item.get("name") == "delegate_research"]
    return {"scope": "fixed reference outcomes only; no inferred root cause", "batch_outcomes": rows,
        "unparsed_tool_observations": max(0, len(observations)-len(tool_results(history, "delegate_research")))}


def docx_text(path):
    try:
        with zipfile.ZipFile(path) as archive:
            if "[Content_Types].xml" not in archive.namelist(): return None
            member = archive.getinfo("word/document.xml")
            if member.file_size > 2*1024**2: return None
            with archive.open(member) as stream: raw = stream.read(2*1024**2+1)
        if len(raw) > 2*1024**2 or b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper(): return None
        node = ET.fromstring(raw)
        if node.tag != "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}document": return None
        return "\n".join(part.text or "" for part in node.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"))
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, ET.ParseError): return None


def oracle(case, workspace, expected, history, python):
    checks = {}
    if case == "research":
        try: value = json.loads((workspace/"summary.json").read_text(encoding="utf-8"))
        except (OSError, ValueError): value = None
        checks["EXACT_ARTIFACT_FACTS"] = value == expected and all(type(value.get(key)) is int for key in ("alpha", "beta", "total")) if isinstance(value, dict) else False
        batches = tool_results(history, "delegate_research")
        children = [child for batch in batches for child in batch.get("children", []) if isinstance(child, dict)]
        ready = [child for child in children if child.get("status") == "reference_ready"]
        checks["VERIFIED_TWO_CHILD_BATCH"] = (len(batches) == 1 and batches[0].get("schema") == "aura.research_batch_reference.v1"
            and batches[0].get("status") == "reference_ready" and batches[0].get("reference_only") is True
            and batches[0].get("claims_verified") is False and batches[0].get("parent_goal_completed") is False
            and len(children) == len(ready) == 2)
        identifiers = [child.get("child_id") for child in ready]
        checks["TWO_ACTUAL_CHILD_REFERENCES"] = (len(ready) == 2 and all(type(name) is str and name for name in identifiers)
            and len(set(identifiers)) == 2 and all(child.get("source_references_verified") is True and child.get("reference_only") is True
                and child.get("claims_verified") is False and child.get("worker_closed") is True for child in ready))
        matched_children = []
        for name, tag, value in zip(("alpha.txt", "beta.txt"), expected["tags"], (expected["alpha"], expected["beta"])):
            raw = (workspace/name).read_bytes(); digest = sha(raw); source = raw.decode("utf-8")
            matches = []
            for child in ready:
                citations = child.get("citations", [])
                if (citations and all(isinstance(c, dict) and c.get("source_sha256") == digest for c in citations)
                        and any(type(c.get("quote")) is str and c["quote"] in source and tag in c["quote"]
                            and re.search(r"(?<![0-9A-Za-z_])"+str(value)+r"(?![0-9A-Za-z_])", c["quote"])
                            and type(c.get("read_id")) is str and c["read_id"] for c in citations)):
                    matches.append(child.get("child_id"))
            matched_children.append(matches)
        checks["BOTH_SOURCE_FACTS_TAGS_AND_HASHES_QUOTED"] = (all(len(matches) == 1 for matches in matched_children)
            and len({matches[0] for matches in matched_children if matches}) == 2)
        reads = tool_results(history, "read_file") + tool_results(history, "read_document")
        checks["PARENT_INDEPENDENT_SOURCE_READS"] = all(
            any(row.get("path") == name and row.get("sha256") == sha((workspace/name).read_bytes())
                and tag in json.dumps(row, ensure_ascii=False) for row in reads)
            for name, tag in zip(("alpha.txt", "beta.txt"), expected["tags"]))
    else:
        pdf = workspace/"report.pdf"
        checks["DOCX_AND_PDF_PRESENT"] = (workspace/"report.docx").is_file() and pdf.is_file()
        actual = {}
        if pdf.is_file():
            try:
                process = subprocess.run([str(python), "-I", "-c",
                    "import json,sys;from pypdf import PdfReader;r=PdfReader(sys.argv[1],strict=True);print(json.dumps({'pages':len(r.pages),'text':'\\n'.join(p.extract_text() or '' for p in r.pages)}))", str(pdf)],
                    capture_output=True, encoding="utf-8", timeout=15, creationflags=0x08000000)
                if process.returncode == 0:
                    try: actual = json.loads(process.stdout)
                    except ValueError: pass
            except (OSError, subprocess.TimeoutExpired): pass
        checks["INDEPENDENT_ONE_PAGE"] = actual.get("pages") == 1
        checks["INDEPENDENT_PDF_FACTS"] = all(tag in actual.get("text", "") for tag in expected["markers"])
        docx = docx_text(workspace/"report.docx")
        checks["INDEPENDENT_DOCX_FACTS"] = docx is not None and all(tag in docx for tag in expected["markers"])
        title = "AURA项目简报"
        checks["REQUESTED_TITLE_IN_BOTH_ARTIFACTS"] = docx is not None and title in "".join(docx.split()) and title in "".join(actual.get("text", "").split())
        digest = sha(pdf.read_bytes()) if pdf.is_file() else None
        reviews = tool_results(history, "inspect_document_page")
        checks["ACTUAL_VERSION_BOUND_PAGE_REVIEW"] = bool(reviews) and any(row.get("pdf_sha256") == digest and row.get("page") == 1
            and row.get("scope") == "model_visual_observation" and type(row.get("observation")) is str and bool(row["observation"].strip())
            and row.get("reference_only") is True and row.get("visual_layout_verified") is False for row in reviews)
        checks["PARENT_PDF_TEXT_READBACK"] = any(row.get("path") == "report.pdf" and row.get("sha256") == digest
            and type(row.get("text")) is str and all(tag in row["text"] for tag in expected["markers"]) for row in tool_results(history, "read_document"))
    checks["NO_IMAGE_DATA_IN_PARENT_HISTORY"] = "data:image" not in json.dumps(history, ensure_ascii=False).casefold()
    expected_files = {"alpha.txt", "beta.txt", "summary.json"} if case == "research" else {"brief.txt", "report.docx", "report.pdf"}
    checks["ONLY_DECLARED_WORKSPACE_FILES"] = {p.name for p in workspace.iterdir()} == expected_files
    return {"passed": bool(checks) and all(checks.values()), "checks": checks}


def run(case, configured, timeout):
    from desktop_pet.agents import AgentService, AgentRequest
    from desktop_pet.agents.contracts import AgentError
    from desktop_pet.agents.settings import DEFAULT_PYTHON
    from desktop_pet.agents.service import _ERROR_MESSAGES
    from tools.run_agent_benchmarks import runtime_fingerprint, project_execution_diagnostics, project_finish_diagnostics
    from agent_backend.nanobot.acceptance_report import checked_assessment
    root = ROOT/"agent_workspace"; root.mkdir(exist_ok=True)
    workspace = Path(tempfile.mkdtemp(prefix="registered-"+case+"-", dir=root))
    task, expected, inputs = fixtures(case, workspace)
    initial, begun = runtime_fingerprint(), time.monotonic()
    runner_before = sha(Path(__file__).read_bytes())
    service, handles, code, result = AgentService(), [], None, None
    cid = str(uuid4())
    diagnostics = []
    def diagnostic(frame):
        diagnostics.extend(project_execution_diagnostics([frame])["events"])
        del diagnostics[:-64]
    try:
        settings = replace(configured.settings, enabled=True, tools_enabled=True, computer_enabled=False,
            workspace=str(workspace), max_steps=0, timeout_seconds=timeout)
        try:
            result = service.execute(settings, configured.key, AgentRequest(task, "agent"),
                {"api": configured.api, "messages": [], "conversation_id": cid}, worker_lifecycle_callback=handles.append,
                diagnostic_callback=diagnostic)
        except AgentError as error: code = error.code if error.code in _ERROR_MESSAGES else "QA_AGENT_ERROR"
        history = service._sessions.get(cid, [])
        checked = oracle(case, workspace, expected, history, settings.python_executable or DEFAULT_PYTHON)
        checked["checks"]["INPUT_SNAPSHOTS_UNCHANGED"] = all((workspace/name).is_file() and sha((workspace/name).read_bytes()) == digest for name, digest in inputs.items())
        checked["passed"] = all(checked["checks"].values())
        closed = bool(handles and service.validate_worker_closure(handles[0], service.worker_closure(handles[0])))
        final = runtime_fingerprint()
        runner_after = sha(Path(__file__).read_bytes())
        return {"task_id": case, "status": "passed" if result and result.status == "completed" and checked["passed"] and closed and initial == final and runner_before == runner_after and configured.unchanged() else "failed",
            "agent_status": result.status if result else None, "error_code": code,
            "duration_seconds": round(time.monotonic()-begun, 3), "independent_oracle": checked,
            "worker_closed": closed, "tool_names": sorted({item.get("name", "") for item in history if item.get("role") == "tool"}),
            "research_diagnostics": research_diagnostics(history), "finish_diagnostics": project_finish_diagnostics(history),
            "execution_diagnostics": project_execution_diagnostics(diagnostics),
            "final_acceptance": checked_assessment(result.acceptance) if result else None,
            "source_snapshot": {"before": initial, "after": final, "unchanged": initial == final},
            "runner_source_snapshot": {"before": runner_before, "after": runner_after, "unchanged": runner_before == runner_after},
            "settings_unchanged": configured.unchanged(), "owned_workspace": str(workspace),
            "artifacts": [{"name": p.name, "bytes": p.stat().st_size, "sha256": sha(p.read_bytes())} for p in workspace.iterdir() if p.is_file()],
            "raw_model_data_saved": False, "scope": "explicit-tool controlled generated sources; not an autonomous routing rate or product parity"}
    finally: service.shutdown()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("offline", "configured"), default="offline")
    parser.add_argument("--case", choices=(*CASES, "all"), default="all")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--output", type=Path, default=ROOT/"artifacts/registered-agent-real-tasks.json")
    args = parser.parse_args()
    if not 5 <= args.timeout <= 900: parser.error("Timeout must be 5..900 seconds")
    configured = None
    if args.provider == "configured":
        from tools.run_agent_benchmarks import ConfiguredExecutor
        configured = ConfiguredExecutor(args.timeout)
    report = {"schema": "aura.registered_agent_tasks.v1", "created_at": datetime.now(timezone.utc).isoformat(),
        "provider": args.provider, "model": configured.model if configured else None, "cases": []}
    for case in CASES if args.case == "all" else (args.case,):
        row = run(case, configured, args.timeout) if configured else {"task_id": case, "status": "not_run"}
        report["cases"].append(row)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2)+"\n", encoding="utf-8")
        print(json.dumps({"task_id": case, "status": row["status"], "oracle_passed": row.get("independent_oracle", {}).get("passed")}), flush=True)
    return 0 if all(row["status"] in {"passed", "not_run"} for row in report["cases"]) else 1


if __name__ == "__main__": raise SystemExit(main())
