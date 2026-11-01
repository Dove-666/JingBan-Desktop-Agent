"""Explicit, opt-in real-provider acceptance using only a new QA workspace.

Run with the desktop application's Python and --run-real-provider. Reads its
configured credentials in memory, sends only synthetic QA tasks, and never
captures pixels, opens applications, or modifies production settings.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
import time
from uuid import uuid4

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-real-provider", action="store_true", required=True)
    parser.parse_args()
    from PySide6.QtCore import QCoreApplication
    from desktop_pet.agents import AgentError, AgentRequest, AgentService
    from desktop_pet.settings_manager import SettingsManager
    app = QCoreApplication.instance() or QCoreApplication([])
    app.setApplicationName("AURA Desktop Pet")
    manager = SettingsManager()
    settings_path = Path(manager.config_path)
    settings_before = hashlib.sha256(settings_path.read_bytes()).hexdigest()
    settings = manager.load_agent_settings()
    key, api = manager.get_api_key(), manager.load_api_settings()
    workspace = REPO / "agent_workspace" / ("qa-direct-agent-" + uuid4().hex)
    workspace.mkdir(parents=True)
    settings = replace(settings, workspace=str(workspace), computer_enabled=False)
    identifier = str(uuid4())
    context = {"api": api, "messages": [], "conversation_id": identifier}
    service = AgentService()
    report = {"workspace": str(workspace), "gui_enabled": False, "turns": []}
    tasks = [
        "请使用 write_file 在授权工作目录创建 qa-note.txt，文件内容必须恰好为 QA_AGENT_ALPHA（UTF-8，无换行），再调用 read_file 核对。只操作此文件并报告核对结果。",
        "把刚才文件追加 QA_AGENT_BETA，然后读取确认。追加内容前后都不添加任何空格或换行；请先读取确定前值，再用 write_file 覆盖保存原内容与追加内容的连接，并用 read_file 核对。",
    ]
    expected = [b"QA_AGENT_ALPHA", b"QA_AGENT_ALPHAQA_AGENT_BETA"]
    try:
        for index, task in enumerate(tasks):
            events = []
            before = sum(item["role"] == "assistant" for item in service._sessions.get(identifier, []))
            started = time.monotonic()
            result = service.execute(settings, key, AgentRequest(task, "agent"), context, event_callback=events.append)
            actual = (workspace / "qa-note.txt").read_bytes() if (workspace / "qa-note.txt").is_file() else b""
            after = sum(item["role"] == "assistant" for item in service._sessions.get(identifier, []))
            turn = {"turn": index + 1, "status": result.status,
                    "duration_seconds": round(time.monotonic() - started, 3),
                    "native_model_turns": after - before,
                    "completed_tool_calls": sum(event["kind"] == "tool" and event["status"] == "completed" for event in events),
                    "file_bytes_match": actual == expected[index]}
            report["turns"].append(turn)
            if actual != expected[index] or result.status != "completed":
                report["passed"] = False
                break
        else:
            report["passed"] = True
    except AgentError as exc:
        report.update(passed=False, error_code=exc.code)
    except Exception as exc:
        report.update(passed=False, exception_type=type(exc).__name__)
    finally:
        service.shutdown()
        report["production_settings_unchanged"] = settings_before == hashlib.sha256(settings_path.read_bytes()).hexdigest()
    (workspace / "verification-real-agent.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report.get("passed") and report["production_settings_unchanged"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
