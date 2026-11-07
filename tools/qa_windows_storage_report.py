"""Explicit real-model, read-only C-drive report through production Agent tools.

No deletion/cleanup is requested. The only intended write is an owned report.
Default offline mode does not read credentials, scan storage, or launch a worker.
Fixed metadata checks are independent of the final model completion statement.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))

TASK = r"""只调查 C 盘存储空间，先不要清理，不删除文件，不启动程序，也不申请新的权限。
使用电脑的只读存储检查工具观察实际容量和已知可检查位置。扫描可能不完整，不能把未扫描位置当作零，也不要把测量值当成肯定可释放空间。
把实际观察结果保存到当前工作区 storage-report.json。JSON 包含 drive、total_bytes、used_bytes、free_bytes、scan_complete、guaranteed_reclaimable_bytes（应为 null）、cleanup_performed（应为 false），以及 categories 数组。
每个 category 只包含 category、measured_bytes、scan_status、recommendation，保持工具实际返回的分类和数值。scan_complete 只有完整扫描时才为 true。
写好后重新读取报告，核对实际结果，再简短告诉我发现了什么；这次只调查，不执行任何清理。"""


def sha(raw): return hashlib.sha256(raw).hexdigest()


def check_report(raw, observed, before, after):
    """Check independently captured tool output and current Win32 disk facts."""
    checks = {}
    try:
        result = json.loads(raw)
        disk = observed["disk"]
        checks["EXACT_FIELDS"] = set(result) == {
            "drive", "total_bytes", "used_bytes", "free_bytes", "scan_complete",
            "guaranteed_reclaimable_bytes", "cleanup_performed", "categories"}
        checks["OBSERVED_DISK_VALUES"] = all(type(result.get(name)) is int and result[name] == disk[name]
            for name in ("total_bytes", "used_bytes", "free_bytes")) and result.get("drive") == disk["drive"]
        checks["INDEPENDENT_CAPACITY"] = result.get("total_bytes") == before.total == after.total
        # Free space can change while the live computer is in use. This bounded
        # interval is a disk-observation check, not exact temporal attestation.
        checks["INDEPENDENT_FREE_SPACE"] = min(before.free, after.free) - 64*1024**2 <= result.get("free_bytes", -1) <= max(before.free, after.free) + 64*1024**2
        checks["HONEST_SCAN_STATUS"] = type(result.get("scan_complete")) is bool and result["scan_complete"] == (observed["status"] == "complete")
        checks["NO_GUARANTEED_RECLAIM"] = result.get("guaranteed_reclaimable_bytes", False) is None
        checks["NO_CLEANUP_CLAIM"] = result.get("cleanup_performed") is False and observed.get("cleanup_performed") is False
        fields = ("category", "measured_bytes", "scan_status", "recommendation")
        expected = [{name: item.get(name) for name in fields} for item in observed["categories"]]
        checks["ACTUAL_CATEGORY_MEASUREMENTS"] = result.get("categories") == expected
        checks["READ_ONLY_METADATA_BOUNDARY"] = observed.get("read_only") is True and observed.get("deleted_files") == 0 and observed.get("file_contents_read") is False
    except (ValueError, TypeError, KeyError): checks["STRUCTURE_VALID"] = False
    return {"passed": bool(checks) and all(checks.values()), "checks": checks}


def execute(timeout):
    from desktop_pet.agents import AgentService, AgentRequest, AgentError
    from tools.run_agent_benchmarks import ConfiguredExecutor, runtime_fingerprint
    config = ConfiguredExecutor(timeout)
    source_before = runtime_fingerprint()
    before = shutil.disk_usage("C:/")
    root = ROOT / "agent_workspace"; root.mkdir(exist_ok=True)
    workspace = Path(tempfile.mkdtemp(prefix="storage-report-qa-", dir=root))
    service, handles = AgentService(), []
    cid, started = str(uuid4()), time.monotonic()
    result, code = None, None
    try:
        settings = replace(config.settings, enabled=True, tools_enabled=True, computer_enabled=False,
            workspace=str(workspace), timeout_seconds=timeout, max_steps=0)
        try:
            result = service.execute(settings, config.key, AgentRequest(TASK, "agent"),
                {"api": config.api, "messages": [], "conversation_id": cid},
                worker_lifecycle_callback=handles.append)
        except AgentError as exc:
            from desktop_pet.agents.service import _ERROR_MESSAGES
            code = exc.code if exc.code in _ERROR_MESSAGES else "QA_AGENT_ERROR"
        history = service._sessions.get(cid, [])
        observations = []
        for item in history:
            if item.get("role") == "tool" and item.get("name") == "inspect_disk_usage":
                try: value = json.loads(item["content"])
                except (ValueError, TypeError): continue
                if isinstance(value, dict) and isinstance(value.get("disk"), dict) and value["disk"].get("drive", "").casefold() == "c:\\": observations.append(value)
        after = shutil.disk_usage("C:/")
        report_file = workspace / "storage-report.json"
        raw = report_file.read_bytes() if report_file.is_file() and report_file.stat().st_size <= 262144 else b""
        oracle = check_report(raw, observations[-1], before, after) if observations else {"passed": False, "checks": {"STORAGE_OBSERVATION_REQUIRED": False}}
        oracle["checks"]["ONLY_DECLARED_WORKSPACE_FILE"] = set(workspace.iterdir()) == {report_file}
        allowed = {"inspect_system", "inspect_disk_usage", "inspect_directory", "write_file", "read_file",
                   "calculate", "finish_task", "update_plan", "reflect", "describe_artifact", "verify_artifact",
                   "discover_tools", "system_info", "search_files"}
        tool_names = sorted({item.get("name", "") for item in history if item.get("role") == "tool"})
        oracle["checks"]["ONLY_READ_ONLY_OR_REPORT_TOOLS"] = all(name in allowed for name in tool_names)
        oracle["passed"] = all(oracle["checks"].values())
        closure = bool(handles and service.validate_worker_closure(handles[0], service.worker_closure(handles[0])))
        # Retain the useful report and its owned workspace. Unknown closure does
        # not trigger deletion or another model/task attempt.
        source_after = runtime_fingerprint()
        return {"schema": "aura.windows_storage_real_task.v1", "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "passed" if result and result.status == "completed" and oracle["passed"] and closure and source_before == source_after and config.unchanged() else "failed",
            "model": config.model, "agent_status": result.status if result else None, "error_code": code,
            "duration_seconds": round(time.monotonic()-started, 3), "independent_oracle": oracle,
            "worker_closed": closure, "storage_observation_count": len(observations),
            "tool_names": tool_names,
            "report_sha256": sha(raw) if raw else None, "owned_report_path": str(report_file),
            "source_snapshot": {"before": source_before, "after": source_after, "unchanged": source_before == source_after},
            "settings_unchanged": config.unchanged(), "raw_model_data_saved": False,
            "scope": "actual current-computer read-only metadata and owned report, not cleanup or full-drive completeness"}
    finally: service.shutdown()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("offline", "configured"), default="offline")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/windows-storage-real-task.json")
    args = parser.parse_args()
    if not 5 <= args.timeout <= 900: parser.error("Timeout must be 5..900 seconds")
    value = execute(args.timeout) if args.provider == "configured" else {"schema": "aura.windows_storage_real_task.v1", "status": "not_run", "real_model_attempted": False}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(value, indent=2)+"\n", encoding="utf-8")
    print(json.dumps({"status": value["status"], "oracle_passed": value.get("independent_oracle", {}).get("passed")}))
    return 0 if value["status"] in {"passed", "not_run"} else 1


if __name__ == "__main__": raise SystemExit(main())
