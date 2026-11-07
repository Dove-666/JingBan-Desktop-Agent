"""Owned native Android QUIC -> host queue -> actual SDK, loopback model only.

This opt-in fixture never loads production settings, keys, history or desktop
pixels. Its separate Android instrumentation exchanges control through private
debug-package files; business RPC stays on actual authenticated native QUIC.
No listener starts on import. Run only against the named QA emulator.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
from queue import Empty, Queue
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

A_TASK = 'Create phone-queue-a.txt containing exactly "A". Read it back before finishing.'
B_SENTINEL = "PHONE_QUEUE_FUTURE_B_SENTINEL_75319"
B_TASK = 'Create phone-queue-b.txt containing exactly "B". Read it back before finishing. ' + B_SENTINEL
A_REPLY = "Created phone-queue-a.txt containing exactly A."
B_REPLY = "Created phone-queue-b.txt containing exactly B."


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adb", type=Path, default=Path("E:/Android/Sdk/platform-tools/adb.exe"))
    parser.add_argument("--serial", default="emulator-5560")
    parser.add_argument("--report", type=Path, default=PROJECT / "artifacts/qa-phone-queue/host-report.json")
    parser.add_argument("--max-seconds", type=int, default=300)
    args = parser.parse_args()
    if args.serial != "emulator-5560" or not args.adb.is_file() or not 30 <= args.max_seconds <= 900:
        parser.error("Use only the owned emulator-5560, an installed adb, and a bounded fixture lifetime")

    from PySide6.QtCore import QSettings, QThreadPool, QTimer
    from PySide6.QtWidgets import QApplication
    from desktop_pet.chat_controller import ChatController
    from desktop_pet.remote.controller import RemoteController, CAPABILITIES
    from desktop_pet.settings_manager import ApiSettings, CredentialStore, SettingsManager
    from tests.test_settings import FakeVault
    from agent_backend.nanobot.tests.test_goal_contract_worker import SeparateGoalModel
    from agent_backend.nanobot.tests.test_worker import PYTHON
    from tools.android_desktop_fixture import AdbBridge, install_local_only_qa, private_json

    if not PYTHON.is_file(): parser.error("The isolated SDK is unavailable")
    # Ephemeral real native endpoint, direct-address QA readiness only. This
    # does not substitute a Python RPC transport or modify production sources.
    install_local_only_qa()
    CAPABILITIES[:] = [name for name in CAPABILITIES if name in {
        "agent.send", "agent.queue.v1", "agent.cancel", "sync.snapshot", "sync.pull",
        "operation.get", "image.list", "agent.artifacts", "agent.artifact"}]
    entered, release = threading.Event(), threading.Event()
    future_clean = []
    def checked(step, *, wait=False):
        def respond(body):
            clean = B_SENTINEL not in json.dumps(body, ensure_ascii=False)
            future_clean.append(clean)
            if not clean: raise AssertionError("Future task contaminated the first context")
            if wait:
                entered.set()
                if not release.wait(args.max_seconds): raise AssertionError("Owned release control timed out")
            return step
        return respond
    model = SeparateGoalModel([
        checked(("write_file", {"path": "phone-queue-a.txt", "content": "A"}), wait=True),
        checked(("read_file", {"path": "phone-queue-a.txt"})),
        checked(("finish_task", {"status": "completed", "summary": A_REPLY})),
        checked("Unchecked final A"),
        ("write_file", {"path": "phone-queue-b.txt", "content": "B"}),
        ("read_file", {"path": "phone-queue-b.txt"}),
        ("finish_task", {"status": "completed", "summary": B_REPLY}), "Unchecked final B",
    ], judge_mode="optimistic")
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    incoming, outgoing = Queue(), Queue()
    bridge = AdbBridge(str(args.adb), args.serial, "com.aura.companion.debug", incoming, outgoing)
    started = time.monotonic()
    package = "com.aura.companion.debug"
    workspace_parent = PROJECT / "agent_workspace"
    workspace_parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="native-phone-queue-", dir=workspace_parent) as owned:
        root = Path(owned)
        settings = SettingsManager(QSettings(str(root / "owned.ini"), QSettings.Format.IniFormat),
                                   CredentialStore(backend=FakeVault()))
        settings.save_api_settings(ApiSettings(model.base_url, "owned-native-queue", 10))
        settings.save_api_key("SYNTHETIC_NATIVE_QUEUE_NO_EXTERNAL_SERVICE")
        workspace = root / "workspace"
        workspace.mkdir()
        settings.save_agent_settings(replace(settings.load_agent_settings(), enabled=True,
            computer_enabled=False, python_executable=str(PYTHON), workspace=str(workspace),
            timeout_seconds=120, max_steps=0))
        chat = ChatController(settings)
        chat._metadata_factory = None
        remote = RemoteController(SimpleNamespace(settings=settings, chat=chat))
        admissions = []
        original_admit = remote._send_agent
        def admitted(peer, request, params):
            value = original_admit(peer, request, params)
            admissions.append(value["job_id"])
            return value
        remote._send_agent = admitted
        published = False
        report = {"isolated_fixture": True, "agent_queue_qa": True, "actual_sdk": True,
                  "model": "synthetic_loopback", "real_api_keys_read": False,
                  "production_data_read": False, "physical_phone_tested": False,
                  "cross_network_tested": False, "relay_tested": False, "ready": False}

        def inspect():
            jobs = chat._agent_queue.queue.list_jobs(limit=10)
            outputs = [(workspace / name).read_bytes() if (workspace / name).is_file() else None
                       for name in ("phone-queue-a.txt", "phone-queue-b.txt")]
            return {"first_planner_entered": entered.is_set(), "released": release.is_set(),
                "planner_calls": len(model.planner_calls), "judge_calls": len(model.judge_calls),
                "fixture_error_count": len(model.errors), "a_context_checks": len(future_clean),
                "a_context_clean": bool(future_clean) and all(future_clean),
                "admission_count": len(admissions), "unique_jobs": len(set(admissions)),
                "jobs": [{"state": job.state, "worker_closed": job.worker_closed_at is not None,
                          "saved_reply": job.reply_id is not None, "artifacts": len(job.artifact_refs)} for job in jobs],
                "actual_a_bytes": outputs[0] == b"A", "actual_b_bytes": outputs[1] == b"B",
                "output_hashes": [hashlib.sha256(value).hexdigest() if value is not None else None for value in outputs]}

        def tick():
            nonlocal published
            if time.monotonic() - started >= args.max_seconds:
                report["stop_reason"] = "fixture_timeout"
                app.quit(); return
            if not published and remote.status["state"] == "listening":
                pairing = remote.begin_pairing()
                configuration = {"isolated_fixture": True, "agent_queue_qa": True,
                    "mock_chat_model": True, "actual_sdk": True, "direct_api": False,
                    "pairing_uri": pairing["uri"], "a_task": A_TASK, "b_task": B_TASK,
                    "a_reply": A_REPLY, "b_reply": B_REPLY}
                outgoing.put(("aura-e2e-config.json", configuration))
                published = True; report["ready"] = True
                private_json(args.report, report)
                print(json.dumps({"ready": True, "actual_sdk": True, "model": "synthetic_loopback"}), flush=True)
            try: command = incoming.get_nowait()
            except Empty: return
            identity, action = command.get("id"), command.get("action")
            if not isinstance(identity, str) or len(identity) > 64: return
            if action == "release": release.set()
            if action in {"inspect", "release", "stop"}:
                result = inspect()
                outgoing.put(("aura-e2e-ack.json", {"id": identity, "ok": True, "result": result}))
                report.update(result); report["config_injected"] = bridge.config_published.is_set()
                private_json(args.report, report)
                if action == "stop": app.quit()
            else:
                outgoing.put(("aura-e2e-ack.json", {"id": identity, "ok": False, "error": "UNKNOWN_QA_CONTROL"}))

        timer = QTimer(); timer.setInterval(100); timer.timeout.connect(tick)
        bridge.start(); remote.set_enabled(True); timer.start()
        try: app.exec()
        finally:
            timer.stop(); release.set()
            report.update(inspect()); report["elapsed_ms"] = round((time.monotonic() - started) * 1000)
            private_json(args.report, report)
            remote.shutdown(); chat.shutdown()
            deadline = time.monotonic() + 10
            while QThreadPool.globalInstance().activeThreadCount() and time.monotonic() < deadline:
                QApplication.processEvents(); time.sleep(.01)
            bridge.stop.set(); bridge.join(timeout=10)
            remote.deleteLater(); chat.deleteLater(); QApplication.processEvents()
            model.close()
    return 0


if __name__ == "__main__": raise SystemExit(main())
