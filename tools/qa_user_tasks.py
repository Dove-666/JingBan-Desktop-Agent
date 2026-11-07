"""Natural user-task fixtures and independent, bounded result checks.

Default CLI is offline. This module never loads credentials or app settings.
Real calls require a caller-supplied loopback budget proxy and explicit opt-in.
The fixtures contain synthetic data only; no candidate code is imported/run.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal, DecimalException
import hashlib
import io
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit
from uuid import uuid4
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
CASES = ("edit_two_files", "long_notes", "editable_budget", "activity_notice", "web_defaults")
ESTIMATED_CALLS = {"edit_two_files": 6, "long_notes": 5, "editable_budget": 7, "activity_notice": 8, "web_defaults": 5}
_RETAINED = []
_MAX_BYTES = 4 * 1024 * 1024
_S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def sha(raw): return hashlib.sha256(raw).hexdigest()


def build_case(case_id, workspace):
    """Seed a fresh owned directory; prompts do not name internal tools."""
    if case_id not in CASES: raise ValueError("UNKNOWN_USER_TASK")
    workspace = Path(workspace)
    if any(workspace.iterdir()): raise ValueError("USER_TASK_WORKSPACE_NOT_EMPTY")
    expected = {"case_id": case_id, "expected_calls": ESTIMATED_CALLS[case_id]}
    if case_id == "edit_two_files":
        note = "周末安排\r\n时间：周六 14:30\r\n地点：东门咖啡馆\r\n备注：带上笔记本和蓝色雨伞。\r\n"
        reminders = '{\n    "enabled": true,\n    "hour": 13,\n    "label": "鲸鱼的周末提醒",\n    "tags": ["周末", "重要"]\n}\n'
        inputs = {"安排.txt": note.encode("utf-8"), "提醒.json": reminders.encode("utf-8")}
        task = "把安排.txt里的地点从东门咖啡馆改为南门图书馆，再把提醒.json里的提醒关掉。其他文字、字段、缩进和换行都照旧保留，先别新增文件。"
        expected["final_bytes"] = {"安排.txt": note.replace("东门咖啡馆", "南门图书馆").encode("utf-8"),
            "提醒.json": reminders.replace('"enabled": true', '"enabled": false').encode("utf-8")}
        expected["mutable"] = set(inputs)
        expected["outputs"] = set()
    elif case_id == "long_notes":
        tag = uuid4().hex[:10]
        filler = "".join(f"第 {index:04d} 次进度：已核对旧方案，编号 {tag}-{index:04d}，继续保留记录。\n" for index in range(2200))
        final = "最终确认：周日 16:20，在南门图书馆服务台取件。\n"
        text = "草稿：周六 14:30，东门咖啡馆。此条已作废。\n" + filler + final + "上一条就是最终安排，此后没有修改。\n"
        inputs = {"收尾记录.txt": text.encode("utf-8")}
        task = "看看收尾记录.txt，最后确认的取件时间和地点是什么？一句话告诉我，别改文件。"
        expected.update(answer_tokens=("周日", "16:20", "南门图书馆", "服务台"), outputs=set(), mutable=set())
    elif case_id == "editable_budget":
        inputs = {"费用.csv": "项目,金额\n车费,17.30\n饮料,2.45\n材料,10.00\n".encode("utf-8")}
        task = "把费用.csv整理成费用汇总.xlsx：每项费用列清楚，最后放合计。金额要能继续修改，改一项时合计也要跟着变。原始CSV别动，表里不要多出无关项目。"
        expected.update(amounts={"车费": "17.30", "饮料": "2.45", "材料": "10.00"},
            total="29.75", outputs={"费用汇总.xlsx"}, mutable=set())
    elif case_id == "web_defaults":
        inputs = {}
        task = ("请打开Python官方教程 https://docs.python.org/3/tutorial/controlflow.html#default-argument-values ，"
            "找到默认参数只计算一次的说明。用中文简短解释为什么不建议把空列表当默认参数，给一个最短的例子，并附官方出处。不要保存文件或运行代码。")
        expected.update(outputs=set(), mutable=set())
    else:
        inputs = {"讨论记录.txt": "活动：周末城市观察\n时间：周日 14:30\n地点：南门图书馆一楼\n预算：每人 35 元\n请参加的同学带纸笔，提前十分钟集合。\n".encode("utf-8")}
        task = "帮我把讨论记录.txt整理成一页能直接发给同学的活动通知，标题用“周末活动安排”，时间、地点和每人预算别漏，版面清爽一点。保存活动安排.docx和活动安排.pdf，原记录别改。"
        expected.update(tokens=("周末活动安排", "周日", "14:30", "南门图书馆一楼", "35", "纸笔"),
            outputs={"活动安排.docx", "活动安排.pdf"}, mutable=set(), visual_review_required=True)
    for name, raw in inputs.items(): (workspace / name).write_bytes(raw)
    expected["input_hashes"] = {name: sha(raw) for name, raw in inputs.items()}
    expected["names"] = set(inputs) | expected["outputs"]
    return task, expected


def _read(root, relative):
    target = root / relative
    if not target.is_file() or target.is_symlink() or not target.resolve().is_relative_to(root.resolve()):
        raise ValueError("OWNED_FILE_UNAVAILABLE")
    with target.open("rb") as stream: raw = stream.read(_MAX_BYTES + 1)
    if len(raw) > _MAX_BYTES: raise ValueError("OWNED_FILE_TOO_LARGE")
    return raw


def _parts(raw, required):
    if len(raw) > _MAX_BYTES: raise ValueError("PACKAGE_TOO_LARGE")
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entries = archive.infolist()
        if len(entries) > 128 or len({e.filename for e in entries}) != len(entries): raise ValueError("PACKAGE_MEMBERS_INVALID")
        if any(e.flag_bits & 1 or e.file_size > 2 * 1024 * 1024 for e in entries): raise ValueError("PACKAGE_LIMIT")
        if required not in archive.namelist(): raise ValueError("PACKAGE_PART_MISSING")
        parts = {}
        for entry in entries:
            if entry.is_dir(): continue
            with archive.open(entry) as stream: data = stream.read(2 * 1024 * 1024 + 1)
            if len(data) > 2 * 1024 * 1024: raise ValueError("PACKAGE_LIMIT")
            parts[entry.filename] = data
    return parts


def _xml(raw):
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper(): raise ValueError("XML_DECLARATION_REJECTED")
    return ET.fromstring(raw)


def _formula_cells(expression):
    """Independent linear SUM/addition subset; never evals executable text."""
    expression = expression.strip().lstrip("=").replace("$", "").upper()
    cells = []
    for term in expression.split("+"):
        if re.fullmatch(r"[A-Z]{1,3}[1-9][0-9]{0,4}", term): cells.append(term); continue
        match = re.fullmatch(r"SUM\(([A-Z]{1,3})([1-9][0-9]{0,4}):\1([1-9][0-9]{0,4})\)", term)
        if not match: raise ValueError("FORMULA_NOT_VERIFIED")
        start, end = int(match[2]), int(match[3])
        if not start <= end <= start + 100: raise ValueError("FORMULA_RANGE_TOO_LARGE")
        cells.extend(f"{match[1]}{row}" for row in range(start, end + 1))
    return cells


def workbook_facts(raw, expected):
    parts = _parts(raw, "xl/workbook.xml")
    workbook = _xml(parts["xl/workbook.xml"])
    sheets = workbook.findall(f".//{_S}sheet")
    if len(sheets) != 1: return {"exact_expense_rows": False, "editable_total_formula": False}
    identifier = sheets[0].get(f"{_R}id")
    relations = _xml(parts["xl/_rels/workbook.xml.rels"])
    targets = [n.get("Target", "") for n in relations if n.get("Id") == identifier and n.get("TargetMode") != "External"]
    if len(targets) != 1: raise ValueError("WORKSHEET_RELATIONSHIP_INVALID")
    target = targets[0].lstrip("/")
    if not target.startswith("xl/"): target = "xl/" + target
    if ".." in Path(target).parts: raise ValueError("WORKSHEET_PATH_INVALID")
    worksheet = _xml(parts[target])
    strings = []
    if "xl/sharedStrings.xml" in parts:
        strings = ["".join(node.itertext()) for node in _xml(parts["xl/sharedStrings.xml"]).findall(f"{_S}si")]
    rows, numbers, formulas = [], {}, {}
    for row in worksheet.findall(f".//{_S}row"):
        actual = []
        for cell in row.findall(f"{_S}c"):
            address = cell.get("r", ""); value = cell.findtext(f"{_S}v", "")
            cell_type = cell.get("t", "n")
            if cell_type == "inlineStr": text = "".join(cell.find(f"{_S}is").itertext())
            elif cell_type == "s": text = strings[int(value)]
            elif cell_type == "str": text = value
            else:
                text = None
                if value: numbers[address] = Decimal(value)
            actual.append((address, text))
            formula = cell.findtext(f"{_S}f")
            if formula is not None: formulas[address] = formula
        rows.append(actual)
    amount_cells = []
    expenses_exact = True
    for label, amount in expected["amounts"].items():
        matched = [row for row in rows if any(text == label for _, text in row)]
        if len(matched) != 1: expenses_exact = False; continue
        candidates = [address for address, _ in matched[0] if address in numbers and address not in formulas]
        if len(candidates) != 1 or numbers[candidates[0]] != Decimal(amount): expenses_exact = False; continue
        amount_cells.append(candidates[0])
    formula_ok = False
    for address, formula in formulas.items():
        try: dependencies = _formula_cells(formula)
        except ValueError: continue
        if (len(dependencies) == len(set(dependencies)) == len(expected["amounts"])
                and set(dependencies) == set(amount_cells) and numbers.get(address) == Decimal(expected["total"])):
            formula_ok = True
    expenses_exact = expenses_exact and set(amount_cells) == {address for address in numbers if address not in formulas}
    return {"exact_expense_rows": expenses_exact, "editable_total_formula": formula_ok}


def _pdf_facts(path, python):
    # Fixed trusted reader only. The PDF path is data; generated Python is never run.
    code = ("import json,sys;from pypdf import PdfReader;"
        "r=PdfReader(sys.argv[1],strict=True);n=len(r.pages);"
        "print(json.dumps({'pages':n,'text':r.pages[0].extract_text()[:16000] if n==1 else ''},ensure_ascii=True))")
    result = subprocess.run([str(python), "-I", "-c", code, str(path)], capture_output=True, timeout=15,
        creationflags=0x08000000 if sys.platform == "win32" else 0)
    if result.returncode or len(result.stdout) > 256 * 1024: raise ValueError("PDF_READER_FAILED")
    value = json.loads(result.stdout)
    if type(value) is not dict or type(value.get("pages")) is not int or type(value.get("text")) is not str:
        raise ValueError("PDF_READER_INVALID")
    return value


def _official_web_evidence(checkpoint, conversation_id):
    """Read only this owned QA namespace, including host-spilled tool results.

    The original checkpoint/report is never changed. A recalled assistant
    claim is not source evidence; only an actual web_fetch tool result and its
    matching immutable host archive entry can provide a source fact.
    """
    checkpoint = Path(checkpoint).resolve()
    owned = (ROOT / "agent_workspace").resolve()
    observation_db = (checkpoint.parent / "agent_context/observations.sqlite3").resolve()
    if not checkpoint.is_relative_to(owned) or not observation_db.is_relative_to(owned):
        return []
    connections = []
    try:
        cp = sqlite3.connect(checkpoint.as_uri() + "?mode=ro", uri=True); connections.append(cp)
        row = cp.execute("SELECT scope_hash,history_json FROM agent_checkpoints WHERE conversation_id=?", (conversation_id,)).fetchone()
        if row is None or not re.fullmatch(r"[a-f0-9]{64}", row[0]): return []
        history = json.loads(row[1]); facts = []; archive = None
        for message in history:
            if message.get("role") != "tool" or message.get("name") != "web_fetch": continue
            value = json.loads(message.get("content", ""))
            if isinstance(value, dict) and "context_observation" in value:
                reference = value["context_observation"].get("reference", "")
                if not re.fullmatch(r"obs_[a-f0-9]{64}", reference): continue
                if archive is None:
                    archive = sqlite3.connect(observation_db.as_uri() + "?mode=ro", uri=True); connections.append(archive)
                saved = archive.execute("SELECT text,metadata FROM observations WHERE namespace=? AND reference=?",
                    (conversation_id + ":" + row[0], reference)).fetchone()
                if saved is None or len(saved[0].encode("utf-8")) > _MAX_BYTES: continue
                if sha(saved[0].encode("utf-8")) != reference[4:]: continue
                meta = json.loads(saved[1]); origin = meta.get("source", {})
                if (meta.get("kind") != "tool_result" or origin.get("tool") != "web_fetch"
                        or origin.get("call_id") != message.get("tool_call_id")): continue
                value = json.loads(saved[0])
            if not isinstance(value, dict): continue
            url = urlsplit(value.get("url", "")); digest = value.get("source_sha256", "")
            if (url.scheme != "https" or url.hostname != "docs.python.org"
                    or not url.path.endswith("/tutorial/controlflow.html")
                    or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)): continue
            text = re.sub(r"\s+", " ", str(value.get("text", ""))).casefold()
            facts.append({"source_url": value["url"], "source_sha256": digest, "offset": value.get("offset"),
                "has_once_warning": "evaluated only once" in text,
                "has_definition_scope": "point of function definition" in text,
                "untrusted_external_data": value.get("untrusted_external_data") is True})
        return facts
    except (OSError, sqlite3.Error, ValueError, TypeError, AttributeError):
        return []
    finally:
        for connection in reversed(connections): connection.close()


def _current_arrangement_answer(answer, tokens):
    """Conservative fixture check, allowing explicitly discarded old facts.

    Only a whole parenthesis containing obsolete facts, an explicit discard
    marker, and no current facts is ignored. Contradictory text elsewhere
    still fails. This is not a general natural-language truth evaluator.
    """
    def obsolete(match):
        text = match.group(0)
        # Recognize only a complete, simple discard statement. A quoted
        # marker inside a denial or a second proposed arrangement must remain
        # visible to the ambiguity check, not disappear with its parenthesis.
        statement = (r'(?:文件开头的)?["“]?周六(?:\s*14:30)?[，,\s]*东门咖啡馆["”]?'
            r'(?:的安排)?已(?:注明|明确|经)?作废(?:[；;，,]文件未做任何修改)?[。.]?')
        return "" if re.fullmatch(statement, text[1:-1].strip()) else text
    current = re.sub(r"（[^（）]*）|\([^()]*\)", obsolete, answer)
    facts = all(token in current for token in tokens)
    unambiguous = not any(token in current for token in
        ("周六", "东门咖啡馆", "不是", "并非", "不在南门", "取消", "作废", "不正确"))
    unambiguous = unambiguous and not re.search(r"周[一二三四五六天]", current)
    return facts, unambiguous


def evaluate(case_id, workspace, expected, answer="", *, pdf_reader=None):
    root = Path(workspace).resolve(); checks = {}
    try:
        checks["ONLY_REQUESTED_FILES"] = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()} == expected["names"]
        checks["UNCHANGED_INPUTS"] = all(sha(_read(root, name)) == digest for name, digest in expected["input_hashes"].items() if name not in expected["mutable"])
        if case_id == "edit_two_files":
            checks["ONLY_REQUESTED_LOCAL_EDITS"] = all(_read(root, name) == raw for name, raw in expected["final_bytes"].items())
        elif case_id == "long_notes":
            facts, unambiguous = _current_arrangement_answer(answer, expected["answer_tokens"])
            checks["FINAL_ARRANGEMENT_ANSWER"] = facts
            checks["FINAL_ARRANGEMENT_UNAMBIGUOUS"] = unambiguous
            checks["SHORT_USER_FACING_ANSWER"] = len(answer.strip()) <= 160
        elif case_id == "editable_budget": checks.update(workbook_facts(_read(root, "费用汇总.xlsx"), expected))
        elif case_id == "web_defaults":
            checks["DEFAULT_EVALUATED_AT_DEFINITION_ONCE"] = "定义" in answer and "一次" in answer
            checks["MUTABLE_DEFAULT_SHARED"] = any(word in answer for word in ("共享", "复用", "同一个", "共用"))
            checks["SMALL_ILLUSTRATIVE_EXAMPLE"] = "def " in answer and "[]" in answer and "append" in answer
            checks["OFFICIAL_SOURCE_LINK"] = bool(re.search(r"https://docs\.python\.org/3(?:\.\d+)?/tutorial/controlflow\.html", answer))
            checks["BOUNDED_USER_FACING_ANSWER"] = 20 <= len(answer.strip()) <= 2200
        elif case_id == "activity_notice":
            parts = _parts(_read(root, "活动安排.docx"), "word/document.xml")
            text = "".join(node.text or "" for node in _xml(parts["word/document.xml"]).iter(f"{_W}t"))
            checks["WORD_HAS_REQUESTED_FACTS"] = all("".join(token.split()) in "".join(text.split()) for token in expected["tokens"])
            _read(root, "活动安排.pdf")
            if pdf_reader is None: raise ValueError("INDEPENDENT_PDF_READER_REQUIRED")
            pdf = pdf_reader(root / "活动安排.pdf")
            checks["ONE_PAGE_PDF"] = pdf["pages"] == 1
            checks["PDF_HAS_REQUESTED_FACTS"] = all("".join(token.split()) in "".join(pdf["text"].split()) for token in expected["tokens"])
        else: raise ValueError("UNKNOWN_USER_TASK")
    except (ValueError, KeyError, TypeError, IndexError, AttributeError, DecimalException, OSError, subprocess.TimeoutExpired, ET.ParseError, zipfile.BadZipFile):
        checks["ACTUAL_OUTPUT_READBACK"] = False
    passed = bool(checks) and all(checks.values())
    return {"passed": passed, "checks": checks,
        "scope": "owned source bytes and requested output facts; no general visual-quality claim",
        "workbook_formula_subset": "SUM_same_column_ranges_or_cell_addition" if case_id == "editable_budget" else None,
        "manual_visual_review_required": case_id == "activity_notice"}


def run_case(case_id, configured, *, report_path=None, timeout=180, budgeted=False):
    """Root-operated only: configured.api/key must belong to the quota proxy."""
    if not budgeted: raise ValueError("EXPLICIT_BUDGET_PROXY_REQUIRED")
    base = urlsplit(configured.api.base_url)
    if base.scheme != "http" or base.hostname != "127.0.0.1" or not base.port or base.username or base.password:
        raise ValueError("LOOPBACK_BUDGET_PROXY_REQUIRED")
    if _RETAINED: raise RuntimeError("USER_TASK_CLEANUP_UNCONFIRMED")
    target = Path(report_path).resolve() if report_path is not None else None
    if target is not None and (not target.is_relative_to((ROOT / "artifacts").resolve()) or target.exists()):
        raise ValueError("NEW_ARTIFACT_REPORT_REQUIRED")
    from desktop_pet.agents import AgentService, AgentRequest
    from desktop_pet.agents.settings import DEFAULT_PYTHON
    from tools.run_agent_benchmarks import runtime_fingerprint
    from agent_backend.nanobot.acceptance_report import checked_assessment
    parent = ROOT / "agent_workspace"; parent.mkdir(exist_ok=True)
    owner = tempfile.TemporaryDirectory(prefix="user-task-", dir=parent); workspace = Path(owner.name).resolve()
    task, expected = build_case(case_id, workspace)
    service = AgentService(checkpoint_path=workspace.parent / (workspace.name + ".checkpoints.sqlite3"))
    handles, tools = [], []
    cid = str(uuid4()); started = time.monotonic(); before = runtime_fingerprint(); closed = False
    report = None; result = None; error_code = None; web_evidence = []
    qa_before = sha(Path(__file__).read_bytes())
    try:
        settings = replace(configured.settings, workspace=str(workspace), enabled=True, tools_enabled=True,
            computer_enabled=False, max_steps=0, timeout_seconds=timeout)
        def activity(frame):
            if frame.get("kind") == "tool" and frame.get("status") == "running":
                tools.append({"id":frame.get("id"), "label":frame.get("label")})
        result = service.execute(settings, configured.key, AgentRequest(task, "agent"),
            {"api":configured.api, "messages":[], "conversation_id":cid},
            event_callback=activity, worker_lifecycle_callback=handles.append)
    except Exception as error:
        candidate = getattr(error, "code", None)
        error_code = candidate if type(candidate) is str and re.fullmatch(r"[A-Za-z0-9_]{1,80}", candidate) else "USER_TASK_EXECUTION_FAILED"
    finally:
        try:
            service.shutdown()
        finally:
            closed = bool(handles and service.validate_worker_closure(handles[0], service.worker_closure(handles[0])))
            owner._finalizer.detach()
            if not closed: _RETAINED.append((owner, service, handles))
    try:
        if case_id == "web_defaults":
            web_evidence = _official_web_evidence(workspace.parent / (workspace.name + ".checkpoints.sqlite3"), cid)
        checked = evaluate(case_id, workspace, expected, result.reply,
            pdf_reader=lambda path: _pdf_facts(path, configured.settings.python_executable or DEFAULT_PYTHON)) if result else {"passed":False,"checks":{}}
        if case_id == "web_defaults":
            checked["checks"]["REAL_OFFICIAL_WEB_OBSERVATION"] = any(row["has_once_warning"] and row["has_definition_scope"]
                and row["untrusted_external_data"] for row in web_evidence)
            checked["passed"] = all(checked["checks"].values())
        after = runtime_fingerprint()
        functional = bool(result and result.status == "completed" and checked["passed"] and closed)
        stable = before == after and qa_before == sha(Path(__file__).read_bytes())
        needs_visual = case_id == "activity_notice"
        # These tasks always seed fresh owned synthetic inputs. Retain only
        # the final public answer, never provider bodies or hidden reasoning.
        visible_answer = result.reply if result else ""
        report = {"schema":"aura.natural_user_task.v1", "case_id":case_id, "created_at":datetime.now(timezone.utc).isoformat(),
            "status":"needs_visual_review" if functional and stable and needs_visual else "passed" if functional and stable else "failed",
            "functional_pass":functional, "error_code":error_code,
            "agent_status":result.status if result else None, "independent_oracle":checked, "final_acceptance":checked_assessment(result.acceptance) if result else None,
            "worker_exact_closed":closed, "duration_seconds":round(time.monotonic()-started,3),
            "source_before":before, "source_after":after, "source_unchanged":stable,
            "qa_source_sha256":qa_before,
            "tool_count":len({row["id"] for row in tools}), "raw_model_data_saved":False,
            "public_tool_labels":[row["label"] for row in tools[:100]
                if type(row.get("label")) is str and len(row["label"]) <= 80],
            "public_web_evidence":web_evidence,
            "visible_answer":visible_answer[:4096], "visible_answer_sha256":sha(visible_answer.encode("utf-8")),
            "visible_answer_truncated":len(visible_answer)>4096,
            "visible_answer_scope":"final public answer to this owned synthetic fixture only; no hidden reasoning or provider body",
            "expected_api_calls":ESTIMATED_CALLS[case_id], "actual_api_calls":"see root persistent proxy ledger",
            "owned_workspace":str(workspace), "model_generated_python_enabled":False,
            "manual_visual_review_required":case_id == "activity_notice"}
        if report_path is not None:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("x", encoding="utf-8") as stream:
                stream.write(json.dumps(report, ensure_ascii=False, indent=2))
        # Preserve actual task deliverables for separate visual/byte review.
        return report
    except Exception:
        # The already-closed workspace stays available for root inspection.
        # Failure while inspecting/writing a report must not replay the task.
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    print(json.dumps({"status":"not_run", "real_api_calls":0,
        "cases":[{"id":name, "estimated_calls":ESTIMATED_CALLS[name]} for name in CASES],
        "scope":"offline natural-task catalog; root budget proxy required for execution"}))
    return 0


if __name__ == "__main__": raise SystemExit(main())
