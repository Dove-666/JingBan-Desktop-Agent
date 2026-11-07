"""Opt-in real-model acceptance of read-only computer tools; no desktop input.

Uses existing credentials in memory, an isolated Agent conversation and owned
fixture files. Advertises the configured GUI tool but blocks all activation,
GUI and write callbacks so an unexpected model choice fails the acceptance.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
import time
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-real-provider", action="store_true", required=True)
    parser.parse_args()
    from PySide6.QtCore import QCoreApplication
    from desktop_pet.agents import AgentError, AgentRequest, AgentService
    from desktop_pet.agents import service as host
    from desktop_pet.settings_manager import SettingsManager

    app = QCoreApplication.instance() or QCoreApplication([])
    app.setApplicationName("AURA Desktop Pet")
    manager = SettingsManager()
    settings_path = Path(manager.config_path)
    before_settings = hashlib.sha256(settings_path.read_bytes()).hexdigest()
    workspace = ROOT / "agent_workspace" / ("qa-readonly-agent-" + uuid4().hex)
    workspace.mkdir(parents=True)
    fixture = workspace / "metadata-fixture"
    fixture.mkdir()
    (fixture / "alpha.txt").write_bytes(b"AURA_READONLY_ALPHA")
    (fixture / "beta.txt").write_bytes(b"AURA_READONLY_BETA")
    before_files = {p.name: p.read_bytes() for p in fixture.iterdir()}
    guard = workspace / "blocked-actions.json"
    harness = workspace / "readonly-worker.py"
    harness.write_text(
        "import asyncio,importlib.util,json\nfrom pathlib import Path\n"
        f"spec=importlib.util.spec_from_file_location('qa_readonly_worker',{str(ROOT / 'agent_backend/nanobot/worker.py')!r})\n"
        "worker=importlib.util.module_from_spec(spec)\nspec.loader.exec_module(worker)\n"
        "import windows_apps\n"
        f"guard=Path({str(guard)!r})\n"
        "def blocked(*args,**kwargs):\n"
        "    guard.write_text(json.dumps({'unexpected_action_requested':True}))\n"
        "    raise RuntimeError('READONLY_ACCEPTANCE_ACTION_BLOCKED')\n"
        "async def blocked_gui(*args,**kwargs):\n"
        "    blocked()\n"
        "worker.GuiBridge._run_once=blocked_gui\n"
        "worker.WorkspaceFiles.write=blocked\n"
        "windows_apps.WindowsApplications.begin_launch=blocked\n"
        "asyncio.run(worker.main())\n", encoding="utf-8")
    settings = replace(manager.load_agent_settings(), workspace=str(workspace),
                       enabled=True, tools_enabled=True, computer_enabled=True)
    identifier = str(uuid4())
    context = {"api": manager.load_api_settings(), "messages": [],
               "conversation_id": identifier, "gui_api_key": manager.get_api_key()}
    service = AgentService()
    report = {"real_provider": True, "workspace": str(workspace), "turns": [],
              "desktop_actions": 0, "cleanup_performed": False, "model_selected_tools": True}
    tasks = [
        ("帮我清理一下C盘内存，告诉我有哪些可以清理的，先不要自己清理", "inspect_disk_usage"),
        ("再检查一下电脑的实际内存和各个本地硬盘还有多少空间，只读取并告诉我结果。", "inspect_system"),
        (f"列一下这个文件夹里有什么，只看名称和文件大小，不要读取内容或改动：{fixture}", "inspect_directory"),
    ]
    passed = True
    try:
        with patch.object(host, "_WORKER_PATH", harness):
            for index, (task, required_tool) in enumerate(tasks):
                events = []
                old_count = len(service._sessions.get(identifier, []))
                started = time.monotonic()
                result = service.execute(settings, manager.get_api_key(), AgentRequest(task, "agent"),
                                         context, event_callback=events.append)
                records = service._sessions.get(identifier, [])[old_count:]
                observations = [r for r in records if r.get("role") == "tool"]
                names = [r.get("name") for r in observations]
                data = [json.loads(r["content"]) for r in observations if r.get("name") == required_tool]
                verified = bool(data and data[-1].get("read_only") and data[-1].get("deleted_files") == 0)
                if required_tool == "inspect_disk_usage" and verified:
                    disk = data[-1].get("disk") or {}
                    verified = disk.get("drive") == "C:\\" and disk.get("total_bytes", 0) > 0
                    report["disk_capacity_verified"] = verified
                elif required_tool == "inspect_system" and verified:
                    verified = bool(data[-1].get("memory") and data[-1].get("fixed_local_drives"))
                elif required_tool == "inspect_directory" and verified:
                    verified = {e["name"] for e in data[-1].get("entries", [])} == set(before_files)
                row = {"turn": index + 1, "status": result.status,
                       "duration_seconds": round(time.monotonic() - started, 3),
                       "tool_names": names, "actual_observation_verified": verified,
                       "reply_nonempty": bool(result.reply.strip()),
                       "gui_tool_offered": bool(settings.gui_model and context["gui_api_key"])}
                report["turns"].append(row)
                passed = passed and verified and result.status == "completed" and not guard.exists()
    except AgentError as exc:
        report.update(error_code=exc.code)
        passed = False
    except Exception as exc:
        report.update(exception_type=type(exc).__name__)
        passed = False
    finally:
        service.shutdown()
        report["fixture_bytes_unchanged"] = before_files == {p.name: p.read_bytes() for p in fixture.iterdir()}
        report["production_settings_unchanged"] = before_settings == hashlib.sha256(settings_path.read_bytes()).hexdigest()
        report["unexpected_action_requested"] = guard.exists()
        report["passed"] = passed and report["fixture_bytes_unchanged"] and report["production_settings_unchanged"]
        output = ROOT / "artifacts/companion-1.7.0/real-readonly-agent.json"
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
