"""One owned DSH task through the existing durable QA quota, never a new quota.

Credentials and model text are not written to reports. Run explicitly only.
"""
from dataclasses import replace
import argparse
import base64
import hashlib
import json
from pathlib import Path
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-directory", type=Path,
                        default=ROOT / "artifacts/release-validation" / ("dsh-live-" + uuid4().hex))
    args = parser.parse_args()
    output = args.report_directory.resolve()
    output.mkdir(parents=True, exist_ok=False)
    from PySide6.QtCore import QCoreApplication, QSettings, QStandardPaths
    from desktop_pet.settings_manager import SettingsManager
    from desktop_pet.agents import AgentService, AgentRequest, AgentSettings
    from desktop_pet.reasoning import resolve_reasoning, reasoning_body
    from tools.qa_api_budget import BudgetLedger, BudgetProxy

    app = QCoreApplication.instance() or QCoreApplication([])
    app.setApplicationName("AURA Desktop Pet")
    path = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppConfigLocation)) / "settings.ini"
    if not path.is_file():
        raise RuntimeError("CONFIGURATION_MISSING")
    original_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    manager = SettingsManager(QSettings(str(path), QSettings.Format.IniFormat))
    api, key = manager.load_api_settings(), manager.get_api_key()
    api.validate()
    if not key:
        raise RuntimeError("CREDENTIAL_MISSING")
    ledger = BudgetLedger(ROOT / "artifacts/release-validation/api-budget.sqlite3",
                          session_id="reliability-20261108", limit=100, create=False)
    before = ledger.snapshot()
    workspace = ROOT / "agent_workspace" / ("dsh-qa-" + uuid4().hex)
    workspace.mkdir(parents=True)
    (workspace / "inventory.csv").write_text("name,quantity,price\npen,4,3\nbook,2,17\n", encoding="utf-8")
    (workspace / "notes.txt").write_text("This is a synthetic QA fixture. Do not change this file.\n", encoding="utf-8")
    preserved = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in workspace.iterdir()}
    service = AgentService(checkpoint_path=output / (uuid4().hex + ".sqlite3"))
    settings = AgentSettings(workspace=str(workspace), computer_enabled=False, timeout_seconds=240)
    cap = resolve_reasoning(api.base_url, api.model, api.reasoning_profile, api.reasoning_metadata)
    tools = []
    provider_errors = []
    cid = str(uuid4())
    started = time.monotonic()
    report = {"scope": "One synthetic file task via real configured provider and official DSH SDK", "passed": False}
    def event(frame):
        if frame.get("kind") == "tool" and frame.get("status") == "completed":
            tools.append(frame.get("label", "tool"))
    try:
        with BudgetProxy(ledger, api.base_url, key, timeout_seconds=90,
                         error_observer=provider_errors.append) as proxy:
            metadata = api.reasoning_metadata
            if isinstance(metadata, dict):
                metadata = {**metadata, "base_url": proxy.base_url}
            elif api.reasoning_profile == "auto" and cap.id not in ("auto", "custom", "legacy"):
                metadata = {"version": 1, "base_url": proxy.base_url, "model": api.model,
                            "profile": cap.id, "values": list(cap.values)}
            relayed = replace(api, base_url=proxy.base_url, reasoning_metadata=metadata)
            def body(value):
                return reasoning_body(value.base_url, value.model, value.reasoning_effort,
                                      value.reasoning_profile, value.reasoning_budget_tokens, value.reasoning_metadata)
            if body(api) != body(relayed):
                raise RuntimeError("QA_REASONING_CHANGED")
            task = ("请读取当前工作目录的 inventory.csv 和 notes.txt，核算quantity乘price的合计，"
                    "创建 result.json，内容为对象：total为合计整数、items为商品种数整数。"
                    "不要改动输入文件，不访问工作目录以外的内容，不使用网络或电脑操作。"
                    "写好后读回核对，并交付 result.json。")
            result = service.execute(settings, proxy.token, AgentRequest(task, "agent"),
                {"api": relayed, "messages": [], "conversation_id": cid,
                 "conversation_generation": str(uuid4())}, event_callback=event)
            target = workspace / "result.json"
            actual = json.loads(target.read_text(encoding="utf-8-sig")) if target.is_file() else None
            inputs_ok = all(hashlib.sha256((workspace / name).read_bytes()).hexdigest() == digest
                            for name, digest in preserved.items())
            # Independently verify the immutable download, rather than trusting a model's reply.
            download_ok = False
            for item in result.artifacts:
                if item.get("name") != "result.json":
                    continue
                page = service.artifact_page(cid, item["reference"])
                raw = base64.b64decode(page["data_base64"], validate=True)
                download_ok = (page["next_offset"] is None and len(raw) == page["total_bytes"]
                               and hashlib.sha256(raw).hexdigest() == page["sha256"]
                               and json.loads(raw.decode("utf-8-sig")) == {"total": 46, "items": 2})
            report.update(status=result.status, backend=result.backend, output=actual,
                          inputs_preserved=inputs_ok, artifacts=len(result.artifacts), download_verified=download_ok,
                          passed=result.status == "completed" and actual == {"total": 46, "items": 2}
                                 and inputs_ok and download_ok)
    except Exception as error:
        report.update(error_type=type(error).__name__)
        if getattr(error, "code", None) and isinstance(error.code, str):
            report["error_code"] = error.code
    finally:
        service.shutdown()
        service.reconcile_worker_closures()
        key = ""
        current_api = SettingsManager(QSettings(str(path), QSettings.Format.IniFormat)).load_api_settings()
        report.update(seconds=round(time.monotonic() - started, 2), tools=tools,
                      provider_errors=provider_errors,
                      settings_file_unchanged=hashlib.sha256(path.read_bytes()).hexdigest() == original_hash,
                      api_configuration_unchanged=current_api == api,
                      budget_before=before, budget_after=ledger.snapshot(),
                      resources_closed=not service.execution_resources_pending())
        report["passed"] = report["passed"] and report["api_configuration_unchanged"] and report["resources_closed"]
        (output / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
