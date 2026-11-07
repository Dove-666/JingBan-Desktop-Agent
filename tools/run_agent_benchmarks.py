"""Isolated, independently checked Agent tasks; offline validation by default.

Use --provider configured to explicitly opt into the existing configured model
through SettingsManager and AgentService. No desktop input is enabled. The
runner never saves replies, transcripts, tool arguments, credentials or model
reasoning. Offline oracle fixtures and negative guards are reported separately
from real-model outcomes. Unimplemented tasks cannot pass.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import csv
import hashlib
import io
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import posixpath
import re
import sys
import tempfile
import time
from uuid import uuid4
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape
import zipfile
from functools import lru_cache

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG = ROOT / "benchmarks" / "agent_tasks" / "catalog.json"
MAX_BYTES = 16 * 1024 * 1024
_ID = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_KINDS = {"answer_json", "answer_text", "file_text", "file_json", "decimal_csv",
          "docx_text", "xlsx_cells", "pptx_slides", "goal_contract_guard",
          "js_hidden_cases", "js_algorithm_artifact", "xlsx_editable_formulas"}
_GUARDS = {"missing_output", "wrong_content", "stale_judge", "missing_judge"}
_FINISH_CODES = frozenset({
    "EXECUTION_EVIDENCE_MISSING", "PLAN_UNFINISHED", "UNRESOLVED_TOOL_FAILURE",
    "ARTIFACT_VERIFICATION_FAILED", "GUI_GOAL_UNVERIFIED", "GOAL_SNAPSHOT_INVALID",
    "GOAL_REQUIREMENT_FAILED", "GOAL_EFFECT_UNRESOLVED", "GOAL_ARTIFACT_READBACK_FAILED",
    "GOAL_ARTIFACT_CHANGED", "GOAL_JUDGE_MISSING", "GOAL_JUDGE_INVALID", "GOAL_JUDGE_STALE",
    "GOAL_ANSWER_ONLY_INVALID", "GOAL_JUDGE_EVIDENCE_MISSING", "GOAL_NOT_SATISFIED", "GOAL_UNVERIFIED",
    "GOAL_FILE_UNAVAILABLE", "GOAL_FILE_FORMAT_MISMATCH", "GOAL_FILE_CHANGED",
    "GOAL_FILE_FORMAT_UNVERIFIED", "GOAL_FILE_TEXT_MISMATCH", "GOAL_FILE_CONTENT_MISSING",
    "GOAL_FILE_HASH_MISMATCH", "GOAL_FILE_NUMBER_MISMATCH", "GOAL_WRITE_NOT_OBSERVED",
    "GOAL_GUI_UNVERIFIED", "GOAL_REVISION_UNRESOLVED", "GOAL_REVISION_INVALID", "GOAL_REVISION_STALE",
    "GOAL_REVISION_VERIFIER_UNAVAILABLE", "GOAL_REVISION_UNCERTAIN",
    "GOAL_REVISION_GUI_REVIEW_REQUIRED",
})
_JUDGE_STATUSES = frozenset({"satisfied", "answer_only", "not_satisfied", "unknown", "missing", "invalid", "stale", "not_required"})
_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_P = "http://schemas.openxmlformats.org/presentationml/2006/main"
_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG = "http://schemas.openxmlformats.org/package/2006/relationships"


class BenchmarkError(ValueError):
    """Only static non-secret codes may be written to a report."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@lru_cache(maxsize=1)
def _js_oracle():
    """Load the fixed trusted host verifier, never Python from a task workspace."""
    try:
        path = ROOT / "benchmarks" / "agent_tasks" / "js_oracle.py"
        spec = importlib.util.spec_from_file_location("aura_benchmark_js_oracle", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except (OSError, ImportError, AttributeError, TypeError):
        raise BenchmarkError("JS_ORACLE_MODULE_UNAVAILABLE") from None


@lru_cache(maxsize=1)
def _xlsx_oracle():
    try:
        path = ROOT / "benchmarks/agent_tasks/xlsx_formula_oracle.py"
        spec = importlib.util.spec_from_file_location("aura_benchmark_xlsx_oracle", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except (OSError, ImportError, AttributeError, TypeError):
        raise BenchmarkError("XLSX_ORACLE_MODULE_UNAVAILABLE") from None


def _utc():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def runtime_fingerprint(root=ROOT):
    """Hash the declared host/worker source boundary, never user configuration.

    These are on-disk snapshots, not a claim to attest every loaded library or
    binary. Capture both sides of each case so editing during a run cannot
    silently produce evidence attributed to a single stable source revision.
    """
    root = Path(root).resolve()
    paths = set()
    for relative, pattern in (("agent_backend/nanobot", "*.py"),
                              ("agent_backend/nanobot", "*.js"),
                              ("agent_backend/ui_tars", "*.mjs"),
                              ("desktop_pet/agents", "*.py"),
                              ("benchmarks/agent_tasks", "*.py")):
        paths.update((root / relative).glob(pattern))
    paths.update(root / relative for relative in (
        "agent_backend/nanobot/requirements.lock", "agent_backend/nanobot/requirements.txt",
        "agent_backend/ui_tars/package-lock.json", "tools/run_agent_benchmarks.py"))
    files = {}
    try:
        for path in sorted(paths):
            # A redirected source path cannot make this evidence collector read
            # an unrelated file into its scope, even as an opaque hash.
            if not path.resolve().is_relative_to(root):
                raise BenchmarkError("BENCHMARK_SOURCE_SCOPE_INVALID")
            files[path.relative_to(root).as_posix()] = _hash(path.read_bytes())
    except OSError:
        raise BenchmarkError("BENCHMARK_SOURCE_UNAVAILABLE") from None
    digest = _hash(json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    return {"scope": "declared_host_worker_sources_and_dependency_pins_on_disk",
            "loaded_binary_attestation": False, "sha256": digest, "files": files}


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BenchmarkError("DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def _json(raw):
    return json.loads(raw, object_pairs_hook=_unique, parse_float=Decimal,
                      parse_constant=lambda _: (_ for _ in ()).throw(BenchmarkError("INVALID_JSON_NUMBER")))


def _relative(value):
    if (not isinstance(value, str) or not value or len(value) > 300 or "\0" in value
            or PureWindowsPath(value).drive or value.startswith(("/", "\\"))):
        raise BenchmarkError("INVALID_FIXTURE_PATH")
    path = PurePosixPath(value.replace("\\", "/"))
    if ".." in path.parts or ":" in value or str(path) == ".":
        raise BenchmarkError("INVALID_FIXTURE_PATH")
    return path.as_posix()


def _owned(workspace, value):
    target = (workspace / _relative(value)).resolve()
    if not target.is_relative_to(workspace.resolve()):
        raise BenchmarkError("WORKSPACE_ESCAPE")
    return target


def _read(workspace, value):
    target = _owned(workspace, value)
    if not target.is_file() or target.stat().st_size > MAX_BYTES:
        raise BenchmarkError("OUTPUT_UNAVAILABLE")
    raw = target.read_bytes()
    if len(raw) > MAX_BYTES or _owned(workspace, value) != target:
        raise BenchmarkError("OUTPUT_UNAVAILABLE")
    return raw


def _same(actual, expected):
    """JSON semantics with booleans distinct from numeric 0/1."""
    if isinstance(actual, bool) or isinstance(expected, bool):
        return type(actual) is bool and type(expected) is bool and actual == expected
    if isinstance(actual, (int, Decimal)) and isinstance(expected, (int, Decimal)):
        return Decimal(actual) == Decimal(expected)
    if isinstance(actual, dict) and isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(_same(actual[key], value) for key, value in expected.items())
    if isinstance(actual, list) and isinstance(expected, list):
        return len(actual) == len(expected) and all(_same(a, b) for a, b in zip(actual, expected))
    return type(actual) is type(expected) and actual == expected


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise BenchmarkError("NUMBER_MISMATCH")
    try:
        result = Decimal(value)
        if not result.is_finite():
            raise InvalidOperation
        return result
    except (InvalidOperation, ValueError):
        raise BenchmarkError("NUMBER_MISMATCH") from None


def validate_catalog(value):
    if (not isinstance(value, dict) or value.get("schema") != "aura.agent_task_catalog.v1"
            or type(value.get("version")) is not int or value["version"] < 1
            or not isinstance(value.get("tasks"), list) or not 20 <= len(value["tasks"]) <= 200):
        raise BenchmarkError("INVALID_CATALOG")
    seen = set()
    for task in value["tasks"]:
        if (not isinstance(task, dict) or not isinstance(task.get("id"), str) or not _ID.fullmatch(task["id"])
                or task["id"] in seen or task.get("suite") not in {"real_model", "guard"}
                or task.get("implementation") not in {"implemented", "not_implemented"}
                or not isinstance(task.get("category"), str) or not task["category"]
                or not isinstance(task.get("objective"), str) or not task["objective"]):
            raise BenchmarkError("INVALID_TASK")
        seen.add(task["id"])
        if task["implementation"] == "not_implemented":
            if not isinstance(task.get("blocked_reason"), str) or not task["blocked_reason"]:
                raise BenchmarkError("UNIMPLEMENTED_REASON_MISSING")
            continue
        if (not isinstance(task.get("fixtures"), list) or not isinstance(task.get("oracles"), list)
                or not 1 <= len(task["oracles"]) <= 16
                or task["suite"] == "real_model" and (not isinstance(task.get("prompt"), str)
                    or not 1 <= len(task["prompt"]) <= 12000)):
            raise BenchmarkError("INVALID_IMPLEMENTED_TASK")
        paths = set()
        for fixture in task["fixtures"]:
            if not isinstance(fixture, dict) or set(fixture) not in ({"path", "text"}, {"path", "generator"}):
                raise BenchmarkError("INVALID_FIXTURE")
            path = _relative(fixture["path"])
            if path.casefold() in paths:
                raise BenchmarkError("DUPLICATE_FIXTURE")
            paths.add(path.casefold())
            if "text" in fixture and (not isinstance(fixture["text"], str) or len(fixture["text"]) > MAX_BYTES):
                raise BenchmarkError("INVALID_FIXTURE")
            if "generator" in fixture and fixture["generator"] != "long_notes_v1":
                raise BenchmarkError("UNKNOWN_FIXTURE_GENERATOR")
        for oracle in task["oracles"]:
            if (not isinstance(oracle, dict) or not isinstance(oracle.get("kind"), str)
                    or oracle["kind"] not in _KINDS):
                raise BenchmarkError("UNKNOWN_ORACLE")
            kind = oracle["kind"]
            if kind == "xlsx_editable_formulas":
                try:
                    _xlsx_oracle().validate(oracle)
                except _xlsx_oracle().XlsxOracleError as error:
                    raise BenchmarkError(error.code) from None
            if kind in {"js_hidden_cases", "js_algorithm_artifact"}:
                verifier = _js_oracle()
                fields = ({"kind", "path", "case"} if kind == "js_hidden_cases"
                          else {"kind", "path", "case", "source_path"})
                if (set(oracle) != fields or not isinstance(oracle.get("case"), str) or oracle["case"] not in verifier.CASES
                        or oracle.get("path") != ("solution.js" if kind == "js_hidden_cases" else "result.json")
                        or kind == "js_algorithm_artifact" and (oracle.get("case") != verifier.INTERVAL_CASE
                            or oracle.get("source_path") != "intervals.json")):
                    raise BenchmarkError("INVALID_JS_ORACLE")
            if (task["suite"] == "guard") != (kind == "goal_contract_guard"):
                raise BenchmarkError("GUARD_SUITE_MISMATCH")
            if kind == "goal_contract_guard":
                if oracle.get("case") not in _GUARDS:
                    raise BenchmarkError("UNKNOWN_GUARD")
                continue
            if not kind.startswith("answer_"):
                _relative(oracle.get("path"))
            if kind in {"answer_json", "answer_text", "file_json", "file_text"} and "equals" not in oracle:
                raise BenchmarkError("ORACLE_EXPECTATION_MISSING")
            if kind in {"answer_text", "file_text"} and not isinstance(oracle["equals"], str):
                raise BenchmarkError("INVALID_ORACLE_EXPECTATION")
            if kind == "decimal_csv":
                _relative(oracle.get("source_path"))
                if not all(isinstance(oracle.get(key), str) and oracle[key] for key in ("group_column", "value_column")):
                    raise BenchmarkError("INVALID_CSV_ORACLE")
                if not isinstance(oracle.get("reference"), dict):
                    raise BenchmarkError("ORACLE_REFERENCE_MISSING")
            if kind == "docx_text" and (not isinstance(oracle.get("contains"), list) or not oracle["contains"]
                    or any(not isinstance(text, str) or not text for text in oracle["contains"])):
                raise BenchmarkError("INVALID_DOCX_ORACLE")
            if kind == "xlsx_cells":
                if not isinstance(oracle.get("sheet"), str) or not isinstance(oracle.get("cells"), dict) or not oracle["cells"]:
                    raise BenchmarkError("INVALID_XLSX_ORACLE")
                for address, expected in oracle["cells"].items():
                    if (not re.fullmatch(r"[A-Z]{1,3}[1-9][0-9]{0,6}", address)
                            or not isinstance(expected, dict) or set(expected) not in ({"text"}, {"number"})):
                        raise BenchmarkError("INVALID_XLSX_ORACLE")
                    if "number" in expected:
                        _number(expected["number"])
                    elif not isinstance(expected["text"], str):
                        raise BenchmarkError("INVALID_XLSX_ORACLE")
            if kind == "pptx_slides" and (not isinstance(oracle.get("slides"), list) or not oracle["slides"]
                    or any(not isinstance(slide, dict) or not isinstance(slide.get("contains"), list)
                           or not slide["contains"] or any(not isinstance(text, str) or not text for text in slide["contains"])
                           for slide in oracle["slides"])):
                raise BenchmarkError("INVALID_PPTX_ORACLE")
        if any(check["kind"] == "xlsx_editable_formulas" for check in task["oracles"]):
            if task != _xlsx_oracle().catalog_entry():
                raise BenchmarkError("INVALID_XLSX_FORMULA_TASK")
        js_checks = [check for check in task["oracles"] if check["kind"].startswith("js_")]
        if js_checks:
            canonical = next((entry for entry in _js_oracle().catalog_entries() if entry["id"] == task["id"]), None)
            if (canonical is None or task["suite"] != "real_model" or task["category"] != "development_js_subset"
                    or task["oracles"] != canonical["oracles"]):
                raise BenchmarkError("INVALID_JS_SUBSET_TASK")
            actual_fixtures = {item["path"]: item.get("text") for item in task["fixtures"]}
            expected_fixtures = {item["path"]: item["text"] for item in canonical["fixtures"]}
            if (actual_fixtures.keys() != expected_fixtures.keys()
                    or any(actual_fixtures[name] != value for name, value in expected_fixtures.items() if name != "solution.js")
                    or not isinstance(actual_fixtures["solution.js"], str)
                    or not 1 <= len(actual_fixtures["solution.js"].encode("utf-8")) <= _js_oracle().MAX_SOURCE_BYTES):
                raise BenchmarkError("INVALID_JS_SUBSET_FIXTURES")
    return value


def load_catalog(path=DEFAULT_CATALOG):
    try:
        raw = Path(path).read_bytes()
        if len(raw) > 2 * MAX_BYTES:
            raise BenchmarkError("CATALOG_TOO_LARGE")
        return validate_catalog(_json(raw)), _hash(raw)
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, RecursionError):
        raise BenchmarkError("CATALOG_INVALID_OR_UNAVAILABLE") from None


def prepare_fixtures(task, workspace):
    workspace.mkdir(parents=True, exist_ok=False)
    before = {}
    for fixture in task.get("fixtures", []):
        if "text" in fixture:
            text = fixture["text"]
        else:
            lines = [f"record_{index:04d}: synthetic ocean survey note, no recovery credential.\n" for index in range(1600)]
            lines[1207] = "FINAL_RECOVERY_CODE=WHALE-73F8A2\n"
            text = "".join(lines)
        path = _owned(workspace, fixture["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = text.encode("utf-8")
        path.write_bytes(raw)
        before[fixture["path"]] = _hash(raw)
    return before


def _package(raw, main):
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entries = archive.infolist()
        names = [item.filename for item in entries]
        if (len(entries) > 4096 or len(set(names)) != len(names)
                or sum(item.file_size for item in entries) > 64 * 1024 * 1024
                or any(name.startswith("/") or ".." in PurePosixPath(name).parts or "\\" in name
                       or name.casefold().endswith("vbaproject.bin") for name in names)
                or not {"[Content_Types].xml", "_rels/.rels", main} <= set(names)):
            raise BenchmarkError("INVALID_OFFICE_PACKAGE")
        parts = {item.filename: archive.read(item) for item in entries if not item.is_dir()}
    ET.fromstring(parts["[Content_Types].xml"])
    root_rels = ET.fromstring(parts["_rels/.rels"])
    if not any(item.get("Type", "").endswith("/officeDocument")
               and item.get("Target", "").lstrip("/") == main and item.get("TargetMode") != "External"
               for item in root_rels):
        raise BenchmarkError("INVALID_OFFICE_RELATIONSHIP")
    return parts


def _related(parts, base, rels_path, identifier):
    rels = ET.fromstring(parts[rels_path])
    selected = [node for node in rels if node.get("Id") == identifier and node.get("TargetMode") != "External"]
    if len(selected) != 1:
        raise BenchmarkError("OFFICE_PART_MISSING")
    target = selected[0].get("Target", "")
    path = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join(base, target))
    if path.startswith("../") or path not in parts:
        raise BenchmarkError("OFFICE_PART_MISSING")
    return ET.fromstring(parts[path])


def _decimal_csv(workspace, oracle, actual):
    text = _read(workspace, oracle["source_path"]).decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or not {oracle["group_column"], oracle["value_column"]} <= set(reader.fieldnames):
        raise BenchmarkError("SOURCE_COLUMNS_MISSING")
    groups, total, count = {}, Decimal(0), 0
    for row in reader:
        if None in row or any(value is None for value in row.values()):
            raise BenchmarkError("SOURCE_ROW_INVALID")
        value = _number(row[oracle["value_column"]])
        group = row[oracle["group_column"]]
        groups[group] = groups.get(group, Decimal(0)) + value
        total += value
        count += 1
    if (not isinstance(actual, dict) or set(actual) != {"category_totals", "total", "record_count"}
            or not isinstance(actual["category_totals"], dict) or set(actual["category_totals"]) != set(groups)
            or not isinstance(actual["total"], str)
            or any(not isinstance(value, str) for value in actual["category_totals"].values())
            or type(actual["record_count"]) is not int or actual["record_count"] != count
            or _number(actual["total"]) != total
            or any(_number(actual["category_totals"][key]) != value for key, value in groups.items())):
        raise BenchmarkError("CSV_TOTAL_MISMATCH")


def _check(oracle, workspace, answer):
    kind = oracle["kind"]
    if kind == "xlsx_editable_formulas":
        result = _xlsx_oracle().evaluate(workspace, oracle["case"])
        if not result["passed"]:
            raise BenchmarkError(result["issues"][0])
        return result["scope"]
    if kind in {"js_hidden_cases", "js_algorithm_artifact"}:
        result = _js_oracle().evaluate_js(workspace, oracle["case"], artifact=kind == "js_algorithm_artifact")
        if not result["passed"]:
            raise BenchmarkError(result["issues"][0])
        return result["scope"]
    if kind == "answer_text":
        if answer != oracle["equals"]:
            raise BenchmarkError("ANSWER_TEXT_MISMATCH")
        return "exact_reply_text"
    if kind == "answer_json":
        if not _same(_json(answer), oracle["equals"]):
            raise BenchmarkError("ANSWER_JSON_MISMATCH")
        return "typed_reply_json"
    raw = _read(workspace, oracle["path"])
    if kind == "file_text":
        if raw != oracle["equals"].encode("utf-8"):
            raise BenchmarkError("FILE_BYTES_MISMATCH")
        return "exact_file_bytes"
    if kind == "file_json":
        if not _same(_json(raw), oracle["equals"]):
            raise BenchmarkError("FILE_JSON_MISMATCH")
        return "complete_typed_json_object"
    if kind == "decimal_csv":
        _decimal_csv(workspace, oracle, _json(raw))
        return "source_derived_exact_decimal_totals"
    if kind == "docx_text":
        parts = _package(raw, "word/document.xml")
        document = ET.fromstring(parts["word/document.xml"])
        paragraphs = ["".join(text.text or "" for text in node.findall(f".//{{{_W}}}t"))
                      for node in document.findall(f".//{{{_W}}}p")]
        if any(required not in paragraphs for required in oracle["contains"]):
            raise BenchmarkError("DOCX_REQUIRED_BLOCK_MISSING")
        return "office_package_and_required_paragraphs_no_visual_quality_claim"
    if kind == "xlsx_cells":
        parts = _package(raw, "xl/workbook.xml")
        workbook = ET.fromstring(parts["xl/workbook.xml"])
        sheets = [node for node in workbook.findall(f".//{{{_S}}}sheet") if node.get("name") == oracle["sheet"]]
        if len(sheets) != 1:
            raise BenchmarkError("XLSX_SHEET_MISSING")
        sheet = _related(parts, "xl", "xl/_rels/workbook.xml.rels", sheets[0].get(f"{{{_R}}}id"))
        strings = []
        if "xl/sharedStrings.xml" in parts:
            strings = ["".join(node.itertext()) for node in ET.fromstring(parts["xl/sharedStrings.xml"]).findall(f"{{{_S}}}si")]
        cells = {node.get("r"): node for node in sheet.findall(f".//{{{_S}}}c")}
        for address, expected in oracle["cells"].items():
            node = cells.get(address)
            if node is None:
                raise BenchmarkError("XLSX_CELL_MISSING")
            value = node.findtext(f"{{{_S}}}v", "")
            cell_type = node.get("t", "n")
            if "number" in expected:
                if cell_type != "n" or node.find(f"{{{_S}}}f") is not None or _number(value) != _number(expected["number"]):
                    raise BenchmarkError("XLSX_CELL_MISMATCH")
            else:
                if cell_type == "s":
                    value = strings[int(value)]
                elif cell_type == "inlineStr":
                    inline = node.find(f"{{{_S}}}is")
                    value = "".join(inline.itertext()) if inline is not None else ""
                elif cell_type != "str":
                    raise BenchmarkError("XLSX_CELL_MISMATCH")
                if value != expected["text"]:
                    raise BenchmarkError("XLSX_CELL_MISMATCH")
        return "office_package_named_sheet_and_typed_cells_no_rendering_claim"
    if kind == "pptx_slides":
        parts = _package(raw, "ppt/presentation.xml")
        presentation = ET.fromstring(parts["ppt/presentation.xml"])
        slides = presentation.findall(f".//{{{_P}}}sldId")
        if len(slides) != len(oracle["slides"]):
            raise BenchmarkError("PPTX_SLIDE_COUNT_MISMATCH")
        for slide_id, expected in zip(slides, oracle["slides"]):
            slide = _related(parts, "ppt", "ppt/_rels/presentation.xml.rels", slide_id.get(f"{{{_R}}}id"))
            text = "\n".join(node.text or "" for node in slide.findall(f".//{{{_A}}}t"))
            if any(required not in text for required in expected["contains"]):
                raise BenchmarkError("PPTX_SLIDE_TEXT_MISMATCH")
        return "office_package_and_ordered_slide_text_no_visual_quality_claim"
    raise BenchmarkError("UNKNOWN_ORACLE")


def evaluate_oracles(task, workspace, answer, source_hashes):
    checks = []
    for index, oracle in enumerate(task["oracles"]):
        try:
            scope = _check(oracle, workspace, answer)
            row = {"id": f"oracle-{index + 1}", "kind": oracle["kind"], "passed": True, "scope": scope, "issues": []}
        except Exception as exc:
            code = exc.code if isinstance(exc, BenchmarkError) else "ORACLE_INPUT_INVALID"
            row = {"id": f"oracle-{index + 1}", "kind": oracle["kind"], "passed": False, "issues": [code]}
        checks.append(row)
    mutable = {oracle.get("path") for oracle in task["oracles"]}
    unchanged = True
    for path, digest in source_hashes.items():
        if path not in mutable:
            try:
                unchanged &= _hash(_read(workspace, path)) == digest
            except (ValueError, OSError):
                unchanged = False
    checks.append({"id": "source-preservation", "kind": "source_hashes", "passed": bool(unchanged),
                   "scope": "all_unmodified_input_bytes", "issues": [] if unchanged else ["SOURCE_CHANGED"]})
    return {"passed": all(row["passed"] for row in checks), "checks": checks}


def _save_package(path, parts, main, content_type):
    parts = dict(parts)
    parts["[Content_Types].xml"] = f'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/{main}" ContentType="{content_type}"/></Types>'
    parts["_rels/.rels"] = f'<Relationships xmlns="{_PKG}"><Relationship Id="rId1" Type="{_R}/officeDocument" Target="{main}"/></Relationships>'
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, body in parts.items():
            archive.writestr(name, body)


def _reference_outputs(task, workspace):
    """Oracle test inputs only. These never increment a model success count."""
    answer = "Reference fixture; not a model answer."
    js_references = set()
    for oracle in task["oracles"]:
        kind = oracle["kind"]
        if kind == "xlsx_editable_formulas":
            _xlsx_oracle().write_reference_outputs(workspace, oracle["case"])
            continue
        if kind in {"js_hidden_cases", "js_algorithm_artifact"}:
            if oracle["case"] not in js_references:
                _js_oracle().write_reference_outputs(workspace, oracle["case"])
                js_references.add(oracle["case"])
            continue
        if kind.startswith("answer_"):
            answer = json.dumps(oracle["equals"]) if kind == "answer_json" else oracle["equals"]
            continue
        path = _owned(workspace, oracle["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        if kind == "file_text":
            path.write_bytes(oracle["equals"].encode("utf-8"))
        elif kind in {"file_json", "decimal_csv"}:
            path.write_text(json.dumps(oracle.get("equals", oracle.get("reference"))), encoding="utf-8")
        elif kind == "docx_text":
            body = "".join(f"<w:p><w:r><w:t>{escape(text)}</w:t></w:r></w:p>" for text in oracle["contains"])
            _save_package(path, {"word/document.xml": f'<w:document xmlns:w="{_W}"><w:body>{body}</w:body></w:document>'},
                          "word/document.xml", "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml")
        elif kind == "xlsx_cells":
            cells = []
            for address, expected in oracle["cells"].items():
                cell = (f'<c r="{address}"><v>{escape(str(expected["number"]))}</v></c>' if "number" in expected
                        else f'<c r="{address}" t="inlineStr"><is><t>{escape(expected["text"])}</t></is></c>')
                cells.append(cell)
            parts = {"xl/workbook.xml": f'<workbook xmlns="{_S}" xmlns:r="{_R}"><sheets><sheet name="{escape(oracle["sheet"])}" sheetId="1" r:id="rId1"/></sheets></workbook>',
                     "xl/_rels/workbook.xml.rels": f'<Relationships xmlns="{_PKG}"><Relationship Id="rId1" Type="{_R}/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
                     "xl/worksheets/sheet1.xml": f'<worksheet xmlns="{_S}"><sheetData><row>{"".join(cells)}</row></sheetData></worksheet>'}
            _save_package(path, parts, "xl/workbook.xml", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml")
        elif kind == "pptx_slides":
            identifiers, relationships, parts = [], [], {}
            for index, slide in enumerate(oracle["slides"], 1):
                identifiers.append(f'<p:sldId id="{255 + index}" r:id="rId{index}"/>')
                relationships.append(f'<Relationship Id="rId{index}" Type="{_R}/slide" Target="slides/slide{index}.xml"/>')
                text = "".join(f"<a:p><a:r><a:t>{escape(item)}</a:t></a:r></a:p>" for item in slide["contains"])
                parts[f"ppt/slides/slide{index}.xml"] = f'<p:sld xmlns:p="{_P}" xmlns:a="{_A}"><p:cSld><p:spTree><p:sp><p:txBody>{text}</p:txBody></p:sp></p:spTree></p:cSld></p:sld>'
            parts["ppt/presentation.xml"] = f'<p:presentation xmlns:p="{_P}" xmlns:r="{_R}"><p:sldIdLst>{"".join(identifiers)}</p:sldIdLst></p:presentation>'
            parts["ppt/_rels/presentation.xml.rels"] = f'<Relationships xmlns="{_PKG}">{"".join(relationships)}</Relationships>'
            _save_package(path, parts, "ppt/presentation.xml", "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml")
    return answer


def oracle_self_test(task, parent):
    workspace = parent / (task["id"] + "-" + uuid4().hex)
    before = prepare_fixtures(task, workspace)
    answer = _reference_outputs(task, workspace)
    positive = evaluate_oracles(task, workspace, answer, before)
    first = task["oracles"][0]
    if first["kind"].startswith("answer_"):
        negative = evaluate_oracles(task, workspace, "wrong reference answer", before)
    else:
        _owned(workspace, first["path"]).write_bytes(b"invalid reference output")
        negative = evaluate_oracles(task, workspace, answer, before)
    hidden_mutant = None
    xlsx_guards = None
    if first["kind"] == "xlsx_editable_formulas":
        xlsx_guards = _xlsx_oracle().oracle_selfchecks()
    if first["kind"] == "js_hidden_cases":
        _js_oracle().write_public_only_mutant(workspace, first["case"])
        mutant = evaluate_oracles(task, workspace, answer, before)
        hidden_mutant = (not mutant["passed"] and any(check["kind"] == "js_hidden_cases"
            and check["issues"] == ["JS_ORACLE_HIDDEN_CASE_MISMATCH"] for check in mutant["checks"]))
    return {"status": "passed" if positive["passed"] and not negative["passed"] and hidden_mutant is not False
            and (xlsx_guards is None or xlsx_guards["passed"] is True) else "failed",
            "positive_accepted": positive["passed"], "negative_rejected": not negative["passed"],
            "checks": positive["checks"], "model_called": False,
            **({"hidden_mutant_rejected": hidden_mutant} if hidden_mutant is not None else {}),
            **({"xlsx_reference_and_mutants": xlsx_guards} if xlsx_guards is not None else {})}


def run_guard(task, parent):
    backend = str(ROOT / "agent_backend" / "nanobot")
    if backend not in sys.path:
        sys.path.insert(0, backend)
    from goal_contract import GoalContract
    from goal_evidence import GoalEvidence
    from safe_files import WorkspaceFiles
    workspace = parent / (task["id"] + "-" + uuid4().hex)
    workspace.mkdir()
    files, evidence = WorkspaceFiles(workspace), GoalEvidence()
    case = task["oracles"][0]["case"]
    contract = GoalContract.from_task("Create report.txt containing exactly HELLO.")
    evidence.observe("system_info", {}, '{"fixture":true}')
    if case in {"wrong_content", "stale_judge"}:
        args = {"path": "report.txt", "content": "WRONG" if case == "wrong_content" else "HELLO"}
        evidence.observe("write_file", args, files.write(**args))
    if case == "missing_judge":
        contract = GoalContract.from_task("Complete the unspecified synthetic objective")
    snapshot = contract.snapshot(files, evidence, answer="Claimed done")
    receipt = {"schema": "aura.goal_verdict.v1", "goal_id": snapshot["goal_id"], "evidence_id": snapshot["evidence_id"],
               "answer_sha256": snapshot["answer_sha256"], "status": "satisfied", "evidence_ids": snapshot["evidence_ids"]}
    if case == "stale_judge":
        snapshot = contract.snapshot(files, evidence, answer="A changed answer")
    report = contract.assess(snapshot, None if case == "missing_judge" else receipt)
    return {"passed": not report["passed"], "issues": report["issues"],
            "scope": "negative_host_guard_fixture_not_model_capability", "model_called": False}


def _model_identifier(value, secrets):
    if not isinstance(value, str) or any(secret and secret in value for secret in secrets):
        return "configured-model-redacted"
    if not re.fullmatch(r"[A-Za-z0-9_.:/+ -]{1,160}", value):
        return "configured-model-" + _hash(value.encode("utf-8"))[:16]
    return value


def project_finish_diagnostics(records):
    """Only fixed codes/counts from actual finish tool observations may persist.

    Never copy a summary, path, requirement ID, answer, raw receipt, arbitrary
    provider status or message. These observations may predate the final goal
    check; their limited scope is explicit instead of inventing a failure cause.
    """
    observations = [record for record in records if isinstance(record, dict)
                    and record.get("role") == "tool" and record.get("name") == "finish_task"]
    rows = []
    for record in observations[-32:]:
        row = {"accepted": None, "passed": None, "status": "unavailable", "codes": [],
               "judge_status": "unavailable", "requirement_total": 0, "requirement_passed": 0,
               "requirement_failed": 0, "requirement_codes": []}
        try:
            content = record.get("content")
            if not isinstance(content, str) or len(content) > 262144:
                raise BenchmarkError("FINISH_DIAGNOSTIC_UNAVAILABLE")
            value = _json(content)
            if not isinstance(value, dict):
                raise BenchmarkError("FINISH_DIAGNOSTIC_UNAVAILABLE")
            row["accepted"] = value.get("accepted") if type(value.get("accepted")) is bool else None
            row["passed"] = value.get("passed") if type(value.get("passed")) is bool else None
            status = value.get("status")
            if isinstance(status, str) and status in {"completed", "needs_user"}:
                row["status"] = status
            goal = value.get("goal") if isinstance(value.get("goal"), dict) else {}
            issues = value.get("issues", [])
            goal_issues = goal.get("issues", [])
            row["codes"] = sorted({item for items in (issues, goal_issues) if isinstance(items, list)
                                   for item in items if isinstance(item, str) and item in _FINISH_CODES})
            judge_status = goal.get("judge_status")
            if isinstance(judge_status, str) and judge_status in _JUDGE_STATUSES:
                row["judge_status"] = judge_status
            requirements = goal.get("requirements", [])
            if isinstance(requirements, list) and len(requirements) <= 256:
                actual = [item for item in requirements if isinstance(item, dict) and type(item.get("passed")) is bool]
                row["requirement_total"] = len(actual)
                row["requirement_passed"] = sum(item["passed"] for item in actual)
                row["requirement_failed"] = len(actual) - row["requirement_passed"]
                row["requirement_codes"] = sorted({code for item in actual if isinstance(item.get("issues"), list)
                    for code in item["issues"] if isinstance(code, str) and code in _FINISH_CODES})
        except Exception:
            row["codes"] = ["FINISH_DIAGNOSTIC_UNAVAILABLE"]
        rows.append(row)
    return {"scope": "finish_tool_observations_only_final_check_may_differ",
            "observed_count": len(observations), "retained_count": len(rows),
            "truncated": len(observations) > 32, "attempts": rows}


def service_finish_diagnostics(service, conversation_id):
    # AgentService's session lives only in memory for this isolated benchmark.
    # Project before shutdown clears it; never serialize the session itself.
    with service._lock:
        records = list(service._sessions.get(conversation_id, []))
    return project_finish_diagnostics(records)


def project_execution_diagnostics(records):
    """Fixed observation boundaries, never raw SDK/provider error payloads."""
    from agent_backend.nanobot.goal_judge import JUDGE_DIAGNOSTIC_CODES
    from agent_backend.nanobot.partial_delivery import FAILURE_DIAGNOSTIC_CODES
    allowed_codes = FAILURE_DIAGNOSTIC_CODES | JUDGE_DIAGNOSTIC_CODES | {"WORKER_CLEANUP_UNCONFIRMED"}
    rows = []
    for item in records:
        if (isinstance(item, dict) and set(item) == {"phase", "code"}
                and isinstance(item["phase"], str) and item["phase"] in {"model", "runtime"}
                and isinstance(item["code"], str) and item["code"] in allowed_codes):
            rows.append({"phase": item["phase"], "code": item["code"]})
    return {"scope": "observed_failure_boundaries_not_inferred_root_cause", "count": len(rows),
            "truncated": len(rows) > 64, "events": rows[-64:]}


class ConfiguredExecutor:
    """The only real-model runner. Loads existing configuration without saving."""
    def __init__(self, timeout):
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from PySide6.QtCore import QCoreApplication, QSettings, QStandardPaths
        from desktop_pet.settings_manager import SettingsManager
        self.app = QCoreApplication.instance() or QCoreApplication([])
        self.app.setApplicationName("AURA Desktop Pet")
        path = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppConfigLocation)) / "settings.ini"
        if not path.is_file():
            raise BenchmarkError("CONFIGURED_SETTINGS_MISSING")
        self.settings_path = path
        self.before_settings = _hash(path.read_bytes())
        manager = SettingsManager(QSettings(str(path), QSettings.Format.IniFormat))
        self.api, self.key = manager.load_api_settings(), manager.get_api_key()
        self.api.validate()
        if not self.key:
            raise BenchmarkError("CONFIGURED_CREDENTIAL_MISSING")
        self.settings = manager.load_agent_settings()
        self.timeout = timeout
        self.model = _model_identifier(self.api.model, (self.key,))

    def unchanged(self):
        try:
            return _hash(self.settings_path.read_bytes()) == self.before_settings
        except OSError:
            return False

    def run(self, task, workspace, before):
        initial = runtime_fingerprint()
        result = self._run(task, workspace, before)
        final = runtime_fingerprint()
        stable = initial == final
        result["source_snapshot"] = {"before": initial, "after": final, "unchanged": stable}
        if not stable:
            result["execution_status_before_source_check"] = result["status"]
            result.update(status="error", error_code="BENCHMARK_SOURCE_CHANGED")
        return result

    def _run(self, task, workspace, before):
        from desktop_pet.agents import AgentService, AgentRequest, AgentError
        from agent_backend.nanobot.acceptance_report import checked_assessment
        settings = replace(self.settings, workspace=str(workspace), enabled=True,
                           tools_enabled=True, computer_enabled=False, timeout_seconds=self.timeout, max_steps=0)
        service = AgentService()  # No saved transcripts/checkpoints or user history.
        seen, count, diagnostics = set(), 0, []
        diagnostic_count = 0
        def diagnostic(frame):
            nonlocal diagnostic_count
            # AgentService already projects this fixed schema. Bound retained
            # memory again; this callback never sees tool args or model text.
            safe = project_execution_diagnostics([frame])["events"]
            if safe:
                diagnostic_count += 1
                diagnostics.extend(safe)
                del diagnostics[:-64]
        def diagnostic_report():
            value = project_execution_diagnostics(diagnostics)
            value.update(count=diagnostic_count, truncated=diagnostic_count > 64)
            return value
        def activity(frame):
            nonlocal count
            if frame.get("kind") == "tool" and frame.get("status") == "running":
                identity = frame.get("id")
                if identity not in seen:
                    seen.add(identity)
                    count += 1
        started = time.monotonic()
        conversation_id = str(uuid4())
        try:
            context = {"api": self.api, "messages": [], "conversation_id": conversation_id}
            result = service.execute(settings, self.key, AgentRequest(task["prompt"], "agent"), context,
                                     event_callback=activity, diagnostic_callback=diagnostic)
            oracle = evaluate_oracles(task, workspace, result.reply, before)
            status = "passed" if result.status == "completed" and oracle["passed"] else "failed"
            return {"status": status, "agent_status": result.status, "independent_oracle": oracle,
                    "final_acceptance": checked_assessment(result.acceptance),
                    "finish_diagnostics": service_finish_diagnostics(service, conversation_id),
                    "execution_diagnostics": diagnostic_report(),
                    "tool_count": count, "duration_seconds": round(time.monotonic() - started, 3),
                    "reply_saved": False, "model": self.model, "real_model_attempted": True}
        except AgentError as exc:
            code = exc.code if re.fullmatch(r"[A-Za-z0-9_]{1,80}", str(exc.code)) else "AGENT_ERROR"
            return {"status": "error", "error_code": code, "tool_count": count,
                    "final_acceptance": None,
                    "finish_diagnostics": service_finish_diagnostics(service, conversation_id),
                    "execution_diagnostics": diagnostic_report(),
                    "duration_seconds": round(time.monotonic() - started, 3), "model": self.model,
                    "independent_oracle": evaluate_oracles(task, workspace, "", before),
                    "real_model_attempted": True, "reply_saved": False}
        except Exception:
            return {"status": "error", "error_code": "BENCHMARK_EXECUTION_FAILED", "tool_count": count,
                    "final_acceptance": None,
                    "finish_diagnostics": service_finish_diagnostics(service, conversation_id),
                    "execution_diagnostics": diagnostic_report(),
                    "duration_seconds": round(time.monotonic() - started, 3), "model": self.model,
                    "real_model_attempted": True, "reply_saved": False,
                    "independent_oracle": {"passed": False, "checks": []}}
        finally:
            service.shutdown()


def summarize(cases, planned=None, *, full_catalog_real_tasks=None):
    real = [row for row in cases if row["suite"] == "real_model"]
    attempted = [row for row in real if row.get("real_model_attempted")]
    passed = sum(row["status"] == "passed" for row in attempted)
    guards = [row for row in cases if row["suite"] == "guard"]
    if planned is None:
        planned = {identifier: sum(row["task_id"] == identifier for row in real)
                   for identifier in {row["task_id"] for row in real}}
    identifiers = set(planned)
    complete = sum(len([row for row in real if row["task_id"] == identifier]) == planned[identifier]
                   and all(row["status"] == "passed" and row.get("real_model_attempted")
                           for row in real if row["task_id"] == identifier) for identifier in identifiers)
    result = {"catalog_real_tasks": len(identifiers), "real_model_cases": len(real),
            "planned_real_cases": sum(planned.values()), "cases_not_finished": sum(planned.values()) - len(real),
            "real_model_attempted": len(attempted), "real_model_passed": passed,
            "real_model_success_rate": passed / len(attempted) if attempted else None,
            "catalog_coverage_pass_rate": complete / len(identifiers) if attempted and identifiers else None,
            "catalog_coverage_scope": "selected_task_definitions_only",
            "not_implemented": len({row["task_id"] for row in real if row["status"] == "not_implemented"}),
            "oracle_self_tests_passed": sum(row.get("oracle_self_test", {}).get("status") == "passed" for row in real),
            "guard_suite_passed": sum(row["status"] == "passed" for row in guards), "guard_suite_cases": len(guards),
            "guard_results_in_model_rate": False, "offline_results_in_model_rate": False}
    if type(full_catalog_real_tasks) is int and full_catalog_real_tasks >= len(identifiers):
        result.update(full_catalog_real_tasks=full_catalog_real_tasks,
                      full_catalog_unselected_tasks=full_catalog_real_tasks - len(identifiers),
                      fully_verified_task_types=complete,
                      full_catalog_verified_task_coverage=(complete / full_catalog_real_tasks
                          if attempted and full_catalog_real_tasks else None))
    return result


def save_report(report, output):
    """Commit one complete JSON version; interrupted writes retain the old one."""
    report["summary"] = summarize(report["cases"], report.get("planned_real_cases"),
        full_catalog_real_tasks=report.get("catalog_scope", {}).get("real_task_types"))
    report["last_updated_utc"] = _utc()
    raw = (json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".benchmark-report-", suffix=".tmp", dir=output.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("offline", "configured"), default="offline")
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--task", action="append", default=[], help="Select exact task IDs; repeat for multiple tasks")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=180, help="Per-task in-memory deadline; production settings are unchanged")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args(argv)
    if not 1 <= args.repeat <= 10 or not 5 <= args.timeout <= 900:
        parser.error("repeat must be 1..10 and timeout 5..900 seconds")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:12]
    report = {"schema": "aura.agent_benchmark_report.v1", "run_id": run_id, "started_utc": _utc(),
              "runner_sha256": _hash(Path(__file__).read_bytes()), "runtime_python": sys.version.split()[0],
              "provider": args.provider, "mode": "real_model" if args.provider == "configured" else "catalog_oracle_validation",
              "cases": [], "logs_include_replies": False, "logs_include_reasoning": False,
              "run_status": "running", "validation_passed": None,
              "logs_include_credentials": False, "uses_real_user_data": False,
              "baselines": {name: {"status": "not_run", "substitute_used": False} for name in ("gpt_agent", "claude_code", "workbuddy")}}
    output = args.output or ROOT / "artifacts" / "agent-benchmarks" / run_id / "report.json"
    executor = None
    try:
        catalog, digest = load_catalog(args.catalog)
        report["catalog_sha256"] = digest
        report["catalog_version"] = catalog["version"]
        selected = [task for task in catalog["tasks"] if not args.task or task["id"] in args.task]
        real_tasks = [task for task in catalog["tasks"] if task["suite"] == "real_model"]
        selected_real = [task for task in selected if task["suite"] == "real_model"]
        report["catalog_scope"] = {
            "real_task_types": len(real_tasks), "selected_real_task_types": len(selected_real),
            "implemented_task_types": sum(task["implementation"] == "implemented" for task in real_tasks),
            "not_implemented_task_types": sum(task["implementation"] == "not_implemented" for task in real_tasks),
            "is_subset": len(selected_real) != len(real_tasks),
            "unselected_tasks_are_unverified": True,
        }
        if any(check["kind"].startswith("js_") for task in selected for check in task.get("oracles", [])):
            module = _js_oracle()
            report["javascript_subset_verifier"] = {
                "implementation_sha256": _hash(Path(module.__file__).read_bytes()),
                "runtime_worker_sha256": _hash(module.CODE_WORKER.read_bytes()),
                "runtime_pin_sha256": _hash((ROOT / "agent_backend/nanobot/requirements.txt").read_bytes()),
                "scope": "javascript_function_and_json_artifact_subset", "host_io_bindings": False,
                "executes_candidate_python_or_shell": False}
        if any(check["kind"] == "xlsx_editable_formulas" for task in selected for check in task.get("oracles", [])):
            module = _xlsx_oracle()
            report["xlsx_formula_subset_verifier"] = {
                "implementation_sha256": _hash(Path(module.__file__).read_bytes()),
                "scope": module.SCOPE, "starts_office": False,
                "executes_candidate_code": False, "excel_recalculation_verified": False}
        if set(args.task) - {task["id"] for task in selected}:
            raise BenchmarkError("UNKNOWN_TASK_ID")
        if args.list:
            for task in selected:
                print(f'{task["id"]}\t{task["suite"]}\t{task["implementation"]}')
            return 0
        report["planned_real_cases"] = {task["id"]: (args.repeat if task["implementation"] == "implemented" else 1)
                                        for task in selected if task["suite"] == "real_model"}
        save_report(report, output)
        if args.provider == "configured" and any(task["suite"] == "real_model" and task["implementation"] == "implemented" for task in selected):
            try:
                executor = ConfiguredExecutor(args.timeout)
            except Exception:
                raise BenchmarkError("CONFIGURED_PROVIDER_UNAVAILABLE") from None
            report["model"] = executor.model
        workspace_parent = ROOT / "agent_workspace" / "benchmarks" / run_id
        workspace_parent.mkdir(parents=True, exist_ok=False)
        with tempfile.TemporaryDirectory(prefix="oracle-self-test-") as temporary:
            for task in selected:
                repetitions = args.repeat if task["suite"] == "real_model" and task["implementation"] == "implemented" else 1
                for repetition in range(1, repetitions + 1):
                    case_started = time.monotonic()
                    row = {"task_id": task["id"], "suite": task["suite"], "category": task["category"],
                           "repetition": repetition, "started_utc": _utc(), "real_model_attempted": False,
                           "model": executor.model if executor else None, "tool_count": 0}
                    if task["implementation"] == "not_implemented":
                        row.update(status="not_implemented", reason=task["blocked_reason"])
                    elif task["suite"] == "guard":
                        guard = run_guard(task, Path(temporary))
                        row.update(status="passed" if guard["passed"] else "failed", guard_result=guard)
                    else:
                        row["oracle_self_test"] = oracle_self_test(task, Path(temporary))
                        if row["oracle_self_test"]["status"] != "passed":
                            row.update(status="oracle_invalid")
                        elif executor is None:
                            row.update(status="not_run", reason="Offline validates the catalog and oracle only; no model was called.")
                        else:
                            workspace = workspace_parent / f'{task["id"]}-{repetition}'
                            before = prepare_fixtures(task, workspace)
                            row.update(executor.run(task, workspace, before))
                            row["workspace"] = workspace.relative_to(ROOT).as_posix()
                    row.setdefault("duration_seconds", round(time.monotonic() - case_started, 3))
                    row["finished_utc"] = _utc()
                    report["cases"].append(row)
                    save_report(report, output)
                    print(json.dumps({"type": "case_result", "task_id": row["task_id"], "suite": row["suite"],
                        "status": row["status"], "duration_seconds": row["duration_seconds"],
                        "oracle_pass": row.get("independent_oracle", {}).get("passed")}, ensure_ascii=True), flush=True)
        report["production_settings_unchanged"] = executor.unchanged() if executor else True
        report["validation_passed"] = (report["production_settings_unchanged"] and all(
            row["status"] in {"passed", "not_run", "not_implemented"} for row in report["cases"]))
        report["run_status"] = "completed"
    except KeyboardInterrupt:
        report["validation_passed"] = False
        report["run_status"] = "interrupted"
        report["error_code"] = "BENCHMARK_INTERRUPTED"
    except Exception as exc:
        report["validation_passed"] = False
        report["run_status"] = "failed"
        report["error_code"] = exc.code if isinstance(exc, BenchmarkError) else "BENCHMARK_VALIDATION_FAILED"
    report["finished_utc"] = _utc()
    try:
        save_report(report, output)
    except (OSError, ValueError):
        print('{"error_code":"BENCHMARK_REPORT_UNAVAILABLE"}')
        return 1
    print(json.dumps({"report": str(output.resolve()), "validation_passed": report["validation_passed"],
                      "summary": report["summary"]}, ensure_ascii=True))
    return 0 if report["validation_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
