"""Local acceptance: real host/SDK/UIA/capture/verifier/checkpoints, fake models.

The judge decodes actual PNG pixels from the production capture transport. A
planner saying "completed" cannot choose the judge's answer. Only an isolated,
opaque full-screen fixture is manipulated; no production settings are loaded.
"""
from __future__ import annotations

import base64
import ctypes
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import sys
import threading
import time
from uuid import uuid4

from PySide6.QtCore import QObject, QTimer, Qt, Signal
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QCheckBox, QLabel, QPushButton, QVBoxLayout, QWidget

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from desktop_pet.agents import AgentError, AgentRequest, AgentService, AgentSettings
from desktop_pet.agents.checkpoints import CheckpointStore, scope_fingerprint

KEY = "loopback-fixture-model-key"
LIKE = "AURA QA Like Target"
NOOP = "AURA QA No-op Target"
SUCCESS_TASK = "在 AURA 验证窗口中，将 AURA QA Like Target 设为勾选，并确认绿色状态块出现。"
FAILURE_TASK = "在 AURA 验证窗口中，点击 AURA QA No-op Target，并使点赞状态变为勾选，确认绿色状态块出现。"


def image_evidence(body):
    images = [part["image_url"]["url"] for message in body.get("messages", [])
              if isinstance(message.get("content"), list) for part in message["content"]
              if part.get("type") == "image_url"]
    if not images:
        return {"visible": False, "green": False, "red": False, "error": "no_image"}, None
    try:
        raw = base64.b64decode(images[-1].split(",", 1)[1], validate=True)
        image = QImage.fromData(raw, "PNG")
        if image.isNull():
            raise ValueError("invalid_png")
        counts = {"green": 0, "red": 0, "anchor": 0, "background": 0, "sampled": 0}
        for y in range(0, image.height(), 8):
            for x in range(0, image.width(), 8):
                color = image.pixelColor(x, y)
                r, g, b = color.red(), color.green(), color.blue()
                counts["sampled"] += 1
                counts["green"] += g > 180 and r < 50 and b < 85
                counts["red"] += r > 180 and g < 65 and b < 85
                counts["anchor"] += r > 185 and g < 55 and b > 180
                counts["background"] += 10 <= r <= 28 and 25 <= g <= 50 and 45 <= b <= 70
        visible = counts["anchor"] >= 50 and counts["background"] > counts["sampled"] * .5
        return {"visible": visible, "green": counts["green"] >= 120,
                "red": counts["red"] >= 120, "pixels": counts,
                "width": image.width(), "height": image.height()}, raw if visible else None
    except (ValueError, IndexError):
        return {"visible": False, "green": False, "red": False, "error": "decode_failed"}, None


class LocalModel(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    lock = threading.Lock()
    requests = []
    judges = []
    errors = []
    artifact = None
    behavior = "success"
    foreground_guard = staticmethod(lambda: False)

    def log_message(self, *_):
        pass

    def do_POST(self):
        try:
            if not type(self).foreground_guard():
                raise ValueError("fixture_foreground_lost")
            size = int(self.headers.get("Content-Length", "0"))
            if self.path != "/v1/chat/completions" or not 0 < size <= 32 * 1024 * 1024:
                raise ValueError("invalid_request")
            body = json.loads(self.rfile.read(size))
            messages = body["messages"]
            is_judge = any("independent read-only desktop RESULT verifier" in str(m.get("content", "")) for m in messages if m.get("role") == "system")
            call = None
            if is_judge:
                evidence, png = image_evidence(body)
                passed = evidence["visible"] and evidence["green"] and not evidence["red"]
                outcome = "passed" if passed else "failed" if evidence["visible"] and evidence["red"] else "unknown"
                content = json.dumps({"outcome": outcome,
                    "all_requirements_observed": passed,
                    "target_identity_confirmed": evidence["visible"],
                    "temporal_requirements_observed": True,
                    "confidence": "high" if evidence["visible"] else "low",
                    "checks": [
                        {"requirement": "AURA fixture identity", "observed": "unique violet fixture anchor present" if evidence["visible"] else "fixture identity not visible", "passed": evidence["visible"]},
                        {"requirement": "checked goal and green status", "observed": "green status square visible" if passed else "red unchecked status square visible" if evidence["red"] else "goal state not visible", "passed": passed}]
                }, ensure_ascii=False)
                with self.lock:
                    index = len(self.judges)
                    self.judges.append({"behavior": self.behavior, "evidence": evidence, "outcome": outcome,
                                        "original_task_present": (SUCCESS_TASK if self.behavior == "success" else FAILURE_TASK) in str(messages),
                                        "received_images": len([p for m in messages if isinstance(m.get("content"), list) for p in m["content"] if p.get("type") == "image_url"])})
                if png:
                    (self.artifact / f"judge-{index + 1}-{self.behavior}.png").write_bytes(png)
            else:
                latest = max(i for i, message in enumerate(messages) if message.get("role") == "user")
                observations = [m for m in messages[latest + 1:] if m.get("role") == "tool"]
                if len(observations) == 0:
                    name, arguments = "desktop_observe", {}
                elif len(observations) == 1:
                    snapshot = json.loads(observations[0]["content"])
                    if snapshot.get("window", {}).get("process_id") != os.getpid():
                        raise ValueError("fixture_snapshot_wrong_process")
                    target_name = LIKE if self.behavior == "success" else NOOP
                    desired_action = "check" if self.behavior == "success" else "invoke"
                    choices = [c for c in snapshot["controls"] if c["name"] == target_name and desired_action in c.get("actions", [])]
                    if len(choices) != 1:
                        raise ValueError("fixture_target_not_unique")
                    name, arguments = "desktop_act", {"ref": choices[0]["ref"], "action": "check" if self.behavior == "success" else "invoke"}
                elif len(observations) == 2:
                    name, arguments = "verify_desktop_goal", {}
                elif len(observations) == 3:
                    # Deliberately optimistic, even when verification failed.
                    name, arguments = "finish_task", {"status": "completed", "summary": "规划模型声称操作已完成。"}
                else:
                    name, arguments = None, None
                if name:
                    call = {"id": "pipeline-" + str(len(observations)), "type": "function",
                            "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}
                content = None if call else "规划模型声称已成功点赞，任务已完成。"
                # Keep only projected request data: no screenshots or keys here.
                with self.lock:
                    self.requests.append({"behavior": self.behavior, "observations": len(observations),
                                          "called": call["function"]["name"] if call else None,
                                          "stream": body.get("stream", False)})
            message = {"role": "assistant", "content": content}
            if call:
                message["tool_calls"] = [call]
            finish = "tool_calls" if call else "stop"
            payload = {"id": "local-pipeline-model", "object": "chat.completion", "created": 1,
                       "model": "offline-fixture", "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                       "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}}
            if body.get("stream"):
                delta = dict(message)
                if call:
                    delta["tool_calls"] = [{"index": 0, **call}]
                first = {"id": "local-pipeline-model", "object": "chat.completion.chunk", "created": 1,
                         "model": "offline-fixture", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
                last = {"id": "local-pipeline-model", "object": "chat.completion.chunk", "created": 1,
                        "model": "offline-fixture", "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]}
                data = ("data: " + json.dumps(first) + "\n\ndata: " + json.dumps(last) + "\n\ndata: [DONE]\n\n").encode()
                media = "text/event-stream"
            else:
                data, media = json.dumps(payload).encode(), "application/json"
            self.send_response(200)
            self.send_header("Content-Type", media)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as error:
            type(self).errors.append({"type": type(error).__name__, "message": "fixture_request_rejected"})
            data = json.dumps({"error": {"message": type(error).__name__, "type": "invalid_request_error"}}).encode()
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except ConnectionError:
                pass


class Events(QObject):
    reset = Signal()
    done = Signal(object)


def main():
    if os.name != "nt":
        raise SystemExit("Windows required")
    artifact = ROOT / "artifacts/gui_verified_control_20261101/pipeline"
    artifact.mkdir(parents=True, exist_ok=True)
    workspace = ROOT / "agent_workspace" / ("qa-goal-pipeline-" + uuid4().hex)
    workspace.mkdir(parents=True)
    database = artifact / ("checkpoint-" + uuid4().hex + ".sqlite3")
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
    user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    user32.GetWindowThreadProcessId.restype = ctypes.c_uint32
    user32.AttachThreadInput.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_bool]
    user32.BringWindowToTop.argtypes = [ctypes.c_void_p]
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentThreadId.restype = ctypes.c_uint32
    previous = user32.GetForegroundWindow()
    app = QApplication(sys.argv)
    fixture = QWidget()
    fixture.setWindowTitle("AURA QA isolated goal verification fixture")
    fixture.setWindowFlags(Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
    fixture.setStyleSheet("QWidget { background: #11243a; color: white; font-size: 24px; } QPushButton, QCheckBox { padding: 18px; }")
    layout = QVBoxLayout(fixture)
    layout.setContentsMargins(100, 70, 100, 70)
    anchor = QLabel("AURA QA ISOLATED FIXTURE")
    anchor.setStyleSheet("background: #d311db; color: white; padding: 20px;")
    anchor.setMinimumHeight(110)
    layout.addWidget(anchor)
    layout.addStretch()
    indicator = QLabel("RED / NOT SATISFIED")
    indicator.setFixedSize(420, 190)
    indicator.setAlignment(Qt.AlignCenter)
    layout.addWidget(indicator)
    checkbox = QCheckBox(LIKE)
    checkbox.setAccessibleName(LIKE)
    layout.addWidget(checkbox)
    noop = QPushButton(NOOP)
    noop.setAccessibleName(NOOP)
    layout.addWidget(noop)
    layout.addWidget(QLabel("Local test only · no real account, model or production settings"))
    layout.addStretch()
    counts = {"check_transitions": 0, "noop_clicks": 0}

    def changed(checked):
        counts["check_transitions"] += 1
        indicator.setText("GREEN / SATISFIED" if checked else "RED / NOT SATISFIED")
        indicator.setStyleSheet("background: " + ("#10dc40" if checked else "#ed152c") + "; color: white;")

    changed(False)
    noop.clicked.connect(lambda: counts.__setitem__("noop_clicks", counts["noop_clicks"] + 1))
    checkbox.toggled.connect(changed)
    events = Events()
    reset_done = threading.Event()
    focus_reports = []
    result_code = [1]

    def reset_fixture():
        checkbox.setChecked(False)
        changed(False)
        counts.update(check_transitions=0, noop_clicks=0)
        fixture.raise_()
        fixture.activateWindow()
        handle = int(fixture.winId())
        foreground = user32.GetForegroundWindow()
        foreground_thread = user32.GetWindowThreadProcessId(foreground, None)
        current_thread = kernel32.GetCurrentThreadId()
        attached = False
        try:
            if foreground_thread and foreground_thread != current_thread:
                attached = bool(user32.AttachThreadInput(current_thread, foreground_thread, True))
            user32.BringWindowToTop(handle)
            user32.SetForegroundWindow(handle)
        finally:
            if attached:
                user32.AttachThreadInput(current_thread, foreground_thread, False)
        focus_reports.append({"fixture_handle": handle, "foreground_matches": user32.GetForegroundWindow() == handle,
                              "platform": app.platformName(), "visible": fixture.isVisible()})
        QTimer.singleShot(200, reset_done.set)

    events.reset.connect(reset_fixture)
    service = AgentService(checkpoint_path=database)
    server = ThreadingHTTPServer(("127.0.0.1", 0), LocalModel)
    server.daemon_threads, server.block_on_close = True, False
    fixture_handle = int(fixture.winId())
    LocalModel.foreground_guard = staticmethod(lambda: user32.GetForegroundWindow() == fixture_handle)
    LocalModel.requests, LocalModel.judges, LocalModel.errors, LocalModel.artifact = [], [], [], artifact
    server_thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
    server_thread.start()
    endpoint = f"http://127.0.0.1:{server.server_port}/v1"
    settings = AgentSettings(enabled=True, tools_enabled=True, computer_enabled=True,
        python_executable=str(ROOT / "agent_backend/nanobot/.venv/Scripts/python.exe"),
        workspace=str(workspace), timeout_seconds=0, max_steps=0,
        gui_provider="ui-tars-1.5", gui_coordinate_profile="pixel", gui_base_url=endpoint, gui_model="offline-fixture")

    def run():
        report = {"passed": False, "scope": "real host + nanobot SDK + native UIA + native GDI PNG + Node independent verification + loopback model + SQLite",
                  "models": "loopback scripted planner and PNG-pixel judge; no real-model capability claim", "cases": [], "checks": []}

        def check(name, passed):
            report["checks"].append({"name": name, "passed": bool(passed)})
            if not passed:
                raise AssertionError(name)

        try:
            contexts = []
            for behavior, task in [("success", SUCCESS_TASK), ("missed", FAILURE_TASK)]:
                LocalModel.behavior = behavior
                reset_done.clear()
                events.reset.emit()
                if not reset_done.wait(5):
                    raise RuntimeError("fixture_reset_timeout")
                if user32.GetForegroundWindow() != fixture_handle:
                    raise RuntimeError("fixture_foreground_not_acquired")
                context = {"api": {"base_url": endpoint, "model": "offline-fixture"}, "gui_api_key": KEY,
                           "conversation_id": str(uuid4()), "messages": []}
                contexts.append(context)
                events_seen = []
                phases = []
                started = time.monotonic()
                result = service.execute(settings, KEY, AgentRequest(task, "agent"), context,
                                         event_callback=events_seen.append, progress_callback=phases.append)
                config, _, secrets = service._context(context, KEY, AgentRequest(task, "agent"), settings)
                scope = scope_fingerprint(settings, config, secrets=secrets)
                checkpoint = CheckpointStore(database).load(context["conversation_id"], scope, secrets=secrets)
                records = service._sessions.get(context["conversation_id"], [])
                tools = [row for row in records if row.get("role") == "tool"]
                tools_by_name = {row.get("name"): json.loads(row["content"]) for row in tools}
                row = {"behavior": behavior, "status": result.status, "reply": result.reply,
                       "seconds": round(time.monotonic() - started, 3), "fixture_counts": dict(counts),
                       "checkpoint_status": checkpoint["status"], "checkpoint_goal_verified": checkpoint["goal_verified"],
                       "last_barrier": checkpoint["last_barrier"], "tool_names": [item.get("name") for item in tools],
                       "verification": tools_by_name.get("verify_desktop_goal"), "finish_task": tools_by_name.get("finish_task"),
                       "host_notified_completion": "completed" in phases}
                report["cases"].append(row)
                check(behavior + "_single_action", row["tool_names"].count("desktop_act") == 1)
                check(behavior + "_independent_judge_called", "verify_desktop_goal" in tools_by_name)
                check(behavior + "_checkpoint_durable", row["last_barrier"] >= 4)
                if behavior == "success":
                    check("success_true_host_completion", result.status == "completed" and checkpoint["goal_verified"] is True)
                    check("success_completion_notification", row["host_notified_completion"] is True)
                    check("success_single_native_toggle", counts["check_transitions"] == 1 and counts["noop_clicks"] == 0)
                    check("success_judge_saw_real_green", LocalModel.judges[-1]["outcome"] == "passed" and LocalModel.judges[-1]["evidence"]["green"])
                else:
                    check("missed_not_completed", result.status == "needs_user" and checkpoint["goal_verified"] is False)
                    check("missed_no_completion_notification", row["host_notified_completion"] is False)
                    check("missed_no_optimistic_reply", "规划模型声称" not in result.reply and "未标记为完成" in result.reply)
                    check("missed_single_noop_no_toggle", counts["noop_clicks"] == 1 and counts["check_transitions"] == 0)
                    check("missed_judge_saw_real_red", LocalModel.judges[-1]["outcome"] == "failed" and LocalModel.judges[-1]["evidence"]["red"])
                    check("missed_finish_claim_rejected", tools_by_name["finish_task"].get("accepted") is False)
            check("each_goal_bound_to_original", len(LocalModel.judges) == 2 and all(j["original_task_present"] for j in LocalModel.judges))
            check("judge_receives_fresh_actual_png", all(j["received_images"] == 1 and j["evidence"]["visible"] for j in LocalModel.judges))
            # Restart the host and load evidence, without sending any new task.
            service.shutdown()
            reads = []
            restarted = AgentService(checkpoint_path=database)
            try:
                for context in contexts:
                    config, _, secrets = restarted._context(context, KEY, AgentRequest("Read checkpoint only", "agent"), settings)
                    scope = scope_fingerprint(settings, config, secrets=secrets)
                    saved = CheckpointStore(database).load(context["conversation_id"], scope, secrets=secrets)
                    reads.append((saved["status"], saved["goal_verified"]))
            finally:
                restarted.shutdown()
            check("restart_preserves_distinct_evidence", reads == [("completed", True), ("needs_user", False)])
            with sqlite3.connect(database) as db:
                tables = " ".join(str(row) for row in db.execute("SELECT history_json FROM agent_checkpoints"))
            check("checkpoint_has_no_key_or_pixels", KEY not in tables and "data:image" not in tables)
            report["passed"] = True
        except Exception as error:
            report.update(error_type=type(error).__name__, error=str(error))
        finally:
            service.shutdown()
            server.shutdown()
            server.server_close()
            report.update({"planner_requests": LocalModel.requests, "judge_observations": LocalModel.judges, "fixture_server_errors": LocalModel.errors,
                           "focus_reports": focus_reports, "database": str(database), "workspace": str(workspace),
                           "tested_at": datetime.now(timezone.utc).isoformat()})
            events.done.emit(report)

    def finish(report):
        fixture.grab().save(str(artifact / "fixture-final.png"))
        (artifact / "verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"passed": report["passed"], "checks": report["checks"],
                          "cases": [{"behavior": c["behavior"], "status": c["status"], "goal_verified": c["checkpoint_goal_verified"]} for c in report["cases"]],
                          "error": report.get("error"), "report": str(artifact / "verification.json")}, ensure_ascii=True), flush=True)
        result_code[0] = 0 if report["passed"] else 1
        fixture.close()
        if previous:
            user32.SetForegroundWindow(previous)
        app.quit()

    events.done.connect(finish)
    fixture.showFullScreen()
    QTimer.singleShot(700, lambda: threading.Thread(target=run, daemon=True).start())
    # Fixture cleanup watchdog only, not a production Agent time limit.
    QTimer.singleShot(180_000, lambda: (service.shutdown(), fixture.close(), app.quit()))
    try:
        app.exec()
    finally:
        service.shutdown()
        server.shutdown()
        server.server_close()
        fixture.close()
        if previous:
            user32.SetForegroundWindow(previous)
    return result_code[0]


if __name__ == "__main__":
    raise SystemExit(main())
