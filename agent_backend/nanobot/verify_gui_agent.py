"""Opt-in real GUI Agent acceptance in a private, opaque QA-only fullscreen.

Uses current configured credentials in memory. Refuses multi-monitor or
non-Windows/offscreen environments, and never changes production settings.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path
import sys
import subprocess
import threading
import time
from uuid import uuid4

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))


def safe_tool_verification_diagnostics(messages):
    """Fixed-value projection of this QA conversation's tool observations.

    Never save tool arguments, prompts, reply/summary/check prose, pixels,
    resource IDs, source paths or unknown strings, even if a provider places a
    credential in a nominally structured status/reason field.
    """
    tools = {"verify_desktop_goal", "computer_use", "finish_task"}
    statuses = {"completed", "needs_user", "cancelled", "timed_out", "failed", "unverified", "verified"}
    reasons = {"GOAL_STATE_CONFIRMED", "GOAL_STATE_NOT_MET", "GOAL_NOT_OBSERVABLE", "TARGET_IDENTITY_UNCONFIRMED",
        "TEMPORAL_EVIDENCE_MISSING", "VERIFICATION_CAPTURE_FAILED", "VERIFICATION_RESPONSE_INVALID",
        "VERIFICATION_SERVICE_UNAVAILABLE", "VERIFICATION_NOT_RUN", "GUI_GOAL_UNVERIFIED", "APP_WINDOW_VERIFIED", "NO_PROGRESS"}
    issues = {"EXECUTION_EVIDENCE_MISSING", "PLAN_UNFINISHED", "UNRESOLVED_TOOL_FAILURE", "ARTIFACT_VERIFICATION_FAILED",
        "GUI_GOAL_UNVERIFIED", "ARTIFACT_CHANGED_AFTER_WRITE_OR_VERIFICATION", "SOURCE_CHANGED_AFTER_VERIFICATION"}
    errors = {"GUI_ALREADY_USED", "GUI_RUNTIME_MISSING", "GUI_START_FAILED", "GUI_EXITED", "GUI_PROTOCOL", "GUI_FAILED",
        "GUI_SDK_UNAVAILABLE", "GUI_UNSUPPORTED_PLATFORM", "GUI_STEP_LIMIT", "GUI_SCREENSHOT_FAILED", "GUI_MODEL_FAILED",
        "GUI_ACTION_FAILED", "GUI_CONTEXT_LENGTH_EXCEEDED", "GUI_DESKTOP_LOCKED", "GUI_DESKTOP_SESSION_UNAVAILABLE",
        "GUI_SCREENSHOT_DISPLAY_UNAVAILABLE", "GUI_SCREENSHOT_BLACK_FRAME", "GUI_UNSUPPORTED_ACTION", "GUI_SDK_FAILED",
        "GUI_INVALID_REQUEST", "GUI_INVALID_PROVIDER", "GUI_BUSY", "GUI_WORKER_FAILED", "GUI_NO_ACTION_TO_VERIFY",
        "ACTION_OUTCOME_UNCERTAIN", "ACTION_ID_CONFLICT", "REQUEST_TIMEOUT", "TOOL_FAILED"}
    result = []
    if not isinstance(messages, (list, tuple)):
        return result
    for index, message in enumerate(messages[-512:]):
        if not isinstance(message, dict) or message.get("role") != "tool" or message.get("name") not in tools:
            continue
        item = {"sequence": index, "tool": message["name"]}
        content = message.get("content")
        try:
            value = json.loads(content) if isinstance(content, str) and len(content) <= 262144 else None
        except (ValueError, RecursionError):
            value = None
        if not isinstance(value, dict):
            item["format"] = "non_json_or_omitted"
            prefix = content.split(":", 1)[0].strip() if isinstance(content, str) else None
            if prefix in errors: item["error_code"] = prefix
            result.append(item)
            continue
        item["format"] = "json_object"
        for field in ("status", "effect_state", "verification_scope"):
            allowed = statuses if field == "status" else {"confirmed", "uncertain", "not_dispatched"} if field == "effect_state" else {"parent", "subgoal"}
            if field in value: item[field] = value[field] if isinstance(value[field], str) and value[field] in allowed else "unrecognized"
        for field in ("accepted", "passed", "gui_goal_verified", "pending_effect"):
            if type(value.get(field)) is bool: item[field] = value[field]
        verification = value.get("verification")
        if isinstance(verification, dict):
            safe = {}
            for field, allowed in (("status", {"verified", "failed", "unverified"}), ("method", {"visual", "native", "uia"}), ("reason", reasons)):
                if field in verification:
                    entry = verification[field]
                    safe[field] = entry if isinstance(entry, str) and entry in allowed else "unrecognized"
            item["verification"] = safe
        for field in ("issues", "failed_tools"):
            values = value.get(field)
            if isinstance(values, list):
                allowed = issues if field == "issues" else tools | {"desktop_act", "desktop_activate", "open_application", "desktop_verify_state", "write_file", "edit_file", "read_file", "verify_artifact", "create_document", "render_document"}
                item[field] = [entry for entry in values[:64] if isinstance(entry, str) and entry in allowed]
                item[field + "_unrecognized_count"] = sum(not isinstance(entry, str) or entry not in allowed for entry in values[:64])
        feedback = value.get("feedback")
        if isinstance(feedback, dict):
            reason = feedback.get("reason")
            item["feedback_reason"] = reason if isinstance(reason, str) and reason in reasons else "unrecognized"
            checks = feedback.get("checks")
            if isinstance(checks, list):
                item["checks_count"] = min(len(checks), 32)
                item["failed_checks_count"] = sum(isinstance(check, dict) and check.get("passed") is False for check in checks[:32])
        result.append(item)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-real-gui", action="store_true", required=True)
    parser.add_argument("--native-typing-probe", action="store_true")
    args = parser.parse_args()
    from PySide6.QtCore import QObject, Qt, QTimer, Signal
    from PySide6.QtWidgets import QApplication, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget
    from desktop_pet.agents import AgentError, AgentRequest, AgentService
    from desktop_pet.settings_manager import SettingsManager

    app = QApplication.instance() or QApplication([])
    app.setApplicationName("AURA Desktop Pet")
    app.setQuitOnLastWindowClosed(False)
    manager = SettingsManager()
    settings_path = Path(manager.config_path)
    settings_before = hashlib.sha256(settings_path.read_bytes()).hexdigest()
    settings = manager.load_agent_settings()
    key, api = manager.get_api_key(), manager.load_api_settings()
    gui_key = key
    def relevant_settings_fingerprint():
        state = {"agent": asdict(manager.load_agent_settings()), "api": asdict(manager.load_api_settings()),
                 "key_hash": hashlib.sha256(manager.get_api_key().encode()).hexdigest()}
        return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()
    relevant_before = relevant_settings_fingerprint()
    identifier = str(uuid4())
    workspace = REPO / "agent_workspace" / ("qa-gui-agent-" + uuid4().hex)
    workspace.mkdir(parents=True)
    token = "AURA_GUI_QA_" + uuid4().hex[:12].upper() + "_中文"
    target = workspace / "fixture-result.txt"
    report = {"workspace": str(workspace), "passed": False, "monitor_count": len(app.screens()), "turns": []}
    service = AgentService()
    stopped = threading.Event()
    events = []
    old_trace_path = os.environ.get("AURA_GUI_QA_TRACE_PATH")
    os.environ["AURA_GUI_QA_TRACE_PATH"] = str(workspace / "safe-action-diagnostics.jsonl")
    native_exception = []
    started = time.monotonic()

    def save_report():
        report["duration_seconds"] = round(time.monotonic() - started, 3)
        report["production_settings_unchanged"] = relevant_before == relevant_settings_fingerprint()
        report["whole_ini_unchanged"] = settings_before == hashlib.sha256(settings_path.read_bytes()).hexdigest()
        if old_trace_path is None:
            os.environ.pop("AURA_GUI_QA_TRACE_PATH", None)
        else:
            os.environ["AURA_GUI_QA_TRACE_PATH"] = old_trace_path
        (workspace / "verification-real-gui.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False), flush=True)

    def record_tool_diagnostics():
        # shutdown() clears _sessions, so take the same-conversation projection
        # first. The lock protects the host's bounded replay snapshot only.
        with service._lock:
            messages = list(service._sessions.get(identifier, []))
        report["tool_verification_diagnostics"] = safe_tool_verification_diagnostics(messages)
        report["tool_verification_diagnostic_scope"] = "same_QA_conversation_fixed_values_only; no prompts/model_prose/pixels/credentials"

    reason = ""
    if sys.platform != "win32" or app.platformName() != "windows":
        reason = "interactive_windows_platform_required"
    elif len(app.screens()) != 1:
        reason = "single_monitor_required_to_avoid_private_pixels"
    elif not settings.enabled or not settings.computer_enabled:
        reason = "configured_computer_permission_disabled"
    elif not key or not gui_key or not settings.gui_model.strip():
        reason = "configured_agent_or_gui_credentials_missing"
    if reason:
        report.update(skipped=True, reason_code=reason)
        save_report()
        return 2

    class Signals(QObject):
        done = Signal(object)
        native_done = Signal(object)
    signals = Signals()
    window = QWidget()
    window.setObjectName("qaFixture")
    window.setWindowTitle("AURA Agent QA Fixture")
    window.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
    window.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
    window.setWindowOpacity(1.0)
    window.setStyleSheet("QWidget#qaFixture { background:#102238; color:#ffffff; } QLabel { color:#ffffff; font-size:28px; } QLineEdit { background:#ffffff; color:#112235; border:4px solid #55c4ff; border-radius:10px; padding:18px; font-size:30px; } QPushButton { background:#48b8f2; color:#082034; border:0; border-radius:12px; padding:22px; font-size:30px; } QPushButton:pressed { background:#2293ce; }")
    layout = QVBoxLayout(window)
    layout.setContentsMargins(100, 80, 100, 80)
    title = QLabel("AURA Agent 临时验证窗口")
    title.setAlignment(Qt.AlignmentFlag.AlignCenter)
    instruction = QLabel("只在此窗口输入验证令牌，再点击下方保存按钮")
    instruction.setAlignment(Qt.AlignmentFlag.AlignCenter)
    textbox = QLineEdit()
    textbox.setAccessibleName("验证输入框")
    textbox.setPlaceholderText("验证输入框")
    button = QPushButton("保存验证结果")
    label = QLabel("尚未保存")
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    layout.addStretch()
    for widget in (title, instruction, textbox, button, label):
        layout.addWidget(widget)
        layout.addSpacing(24)
    layout.addStretch()
    saved_callbacks = []
    def clicked():
        value = textbox.text()
        target.write_bytes(value.encode("utf-8"))
        saved_callbacks.append(value)
        label.setText("保存成功：" + value)
    button.clicked.connect(clicked)
    native_text = None
    native_expected = "  中文🙂 AURA\nsecond line 两行  "
    if args.native_typing_probe:
        for widget in (textbox, button, label):
            widget.hide()
        title.setText("AURA 原生 Unicode 输入验证")
        instruction.setText("仅此临时文本框接收合成验证文字")
        native_text = QPlainTextEdit()
        native_text.setStyleSheet("background:white;color:black;font-size:30px;")
        layout.insertWidget(4, native_text)

    # Check native Z-order as well as Qt geometry before permitting any pixels
    # to leave the process. No window titles or personal content are read.
    def cover_is_private():
        import ctypes
        from ctypes import wintypes
        user = ctypes.WinDLL("user32", use_last_error=True)
        user.GetForegroundWindow.restype = wintypes.HWND
        handle = int(window.winId())
        own_rect = wintypes.RECT()
        user.GetWindowRect(wintypes.HWND(handle), ctypes.byref(own_rect))
        above = []
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        @callback_type
        def inspect(hwnd, _):
            if int(hwnd) == handle:
                return False
            if user.IsWindowVisible(hwnd) and not user.IsIconic(hwnd):
                cloaked = wintypes.DWORD()
                try:
                    if ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked)) == 0 and cloaked.value:
                        return True
                except OSError:
                    pass
                alpha, flags, color = ctypes.c_ubyte(), wintypes.DWORD(), wintypes.DWORD()
                if user.GetLayeredWindowAttributes(hwnd, ctypes.byref(color), ctypes.byref(alpha), ctypes.byref(flags)) and flags.value & 2 and alpha.value == 0:
                    return True
                rect = wintypes.RECT()
                if user.GetWindowRect(hwnd, ctypes.byref(rect)):
                    if min(rect.right, own_rect.right) > max(rect.left, own_rect.left) and min(rect.bottom, own_rect.bottom) > max(rect.top, own_rect.top):
                        above.append(True)
            return True
        user.EnumWindows(inspect, 0)
        screen = app.primaryScreen()
        checks = {"visible": window.isVisible(), "opaque": window.windowOpacity() == 1.0,
                  "covers_screen": window.frameGeometry().contains(screen.geometry()),
                  "overlapping_windows_above": len(above), "foreground_owned": int(user.GetForegroundWindow()) == handle}
        report["cover_checks"] = checks
        return checks["visible"] and checks["opaque"] and checks["covers_screen"] and not above and checks["foreground_owned"]

    def finish(result):
        record_tool_diagnostics()
        service.shutdown()
        report["completed_tool_calls"] = sum(event["kind"] == "tool" and event["status"] == "completed" for event in events)
        report["gui_activity_events"] = sum(event["kind"] in {"computer", "progress"} for event in events)
        report["activity"] = [{"kind": item["kind"], "label": item["label"], "status": item["status"]} for item in events]
        report["save_callback_count"] = len(saved_callbacks)
        report["file_bytes_match"] = target.is_file() and target.read_bytes() == token.encode("utf-8")
        report["widget_text_match"] = textbox.text() == token
        report["widget_label_match"] = label.text() == "保存成功：" + token
        if isinstance(result, AgentError):
            report["error_code"] = result.code
        elif result is not None:
            report["status"] = result.status
        report["passed"] = (getattr(result, "status", None) == "completed" and report["file_bytes_match"]
                            and report["widget_text_match"] and report["widget_label_match"] and len(saved_callbacks) == 1)
        if not report["passed"]:
            # QWidget.grab() renders only this owned opaque fixture.
            window.grab().save(str(workspace / "fixture-diagnostic.png"))
        save_report()
        window.close()
        app.quit()
    signals.done.connect(finish)

    def finish_native(result):
        report["native_typing_probe"] = result
        report["unicode_emoji_multiline_exact"] = native_text.toPlainText() == native_expected
        report["passed"] = bool(result.get("exit_code") == 0 and report["unicode_emoji_multiline_exact"])
        record_tool_diagnostics()
        service.shutdown()
        if not report["passed"]:
            window.grab().save(str(workspace / "fixture-diagnostic.png"))
        save_report()
        window.close()
        app.quit()
    # SendInput enqueues Windows messages; let the owned Qt control consume
    # them before comparing or closing the fixture.
    signals.native_done.connect(lambda result: QTimer.singleShot(500, lambda: finish_native(result)))

    def run_native():
        command = ("for (const key of ['log','info','warn','error','debug']) console[key]=()=>{};"
                   "let data='';for await (const chunk of process.stdin)data+=chunk;"
                   "const {sendWindowsUnicodeText}=await import(" + json.dumps((REPO / 'agent_backend/ui_tars/sdk-adapter.mjs').as_uri()) + ");"
                   "const request=JSON.parse(data);await sendWindowsUnicodeText(request.text,new AbortController().signal,request.pythonExecutable);")
        probe_start = time.monotonic()
        try:
            output = subprocess.run(["node", "--input-type=module", "-e", command],
                                    input=json.dumps({"text": native_expected, "pythonExecutable": sys.executable}).encode(),
                                    cwd=str(REPO / "agent_backend/ui_tars"), stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, timeout=30,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            result = {"exit_code": output.returncode}
        except Exception as exc:
            result = {"exception_type": type(exc).__name__}
        result["duration_seconds"] = round(time.monotonic() - probe_start, 3)
        signals.native_done.emit(result)

    def run():
        nonlocal settings
        try:
            # QA deliberately remains bounded even when production tasks have
            # no total deadline. This is not a runtime cap or saved preference.
            settings = replace(settings, workspace=str(workspace), timeout_seconds=min(settings.timeout_seconds or 110, 110))
            context = {"api": api, "messages": [], "gui_api_key": gui_key, "conversation_id": identifier}
            task = ("当前屏幕是我授权的 AURA Agent 临时验证窗口。请只操作这个验证窗口："
                    "在中间标有“验证输入框”的输入框输入精确文本 " + token +
                    "，然后点击“保存验证结果”按钮。最后查看下方显示“保存成功：" + token +
                    "”才算完成。不得操作其他窗口、打开应用或使用文件工具代替界面输入和按钮点击。")
            result = service.execute(settings, key, AgentRequest(task, "agent"), context,
                                     cancellation_event=stopped, event_callback=events.append)
        except AgentError as exc:
            result = exc
        except Exception as exc:
            native_exception.append(type(exc).__name__)
            result = None
        signals.done.emit(result)

    def begin():
        import ctypes
        from ctypes import wintypes
        user = ctypes.WinDLL("user32", use_last_error=True)
        user.GetForegroundWindow.restype = wintypes.HWND
        user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user.GetWindowThreadProcessId.restype = wintypes.DWORD
        user.SetForegroundWindow.argtypes = [wintypes.HWND]
        user.SetFocus.argtypes = [wintypes.HWND]
        user.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
        handle = wintypes.HWND(int(window.winId()))
        foreground = user.GetForegroundWindow()
        current_thread = ctypes.windll.kernel32.GetCurrentThreadId()
        foreground_thread = user.GetWindowThreadProcessId(foreground, None) if foreground else 0
        attached = bool(foreground_thread and foreground_thread != current_thread
                        and user.AttachThreadInput(current_thread, foreground_thread, True))
        try:
            user.SetWindowPos(handle, wintypes.HWND(-1), 0, 0, 0, 0, 0x0043)
            user.SetForegroundWindow(handle)
            user.SetFocus(handle)
        finally:
            if attached:
                user.AttachThreadInput(current_thread, foreground_thread, False)
        if not cover_is_private():
            report.update(skipped=True, reason_code="opaque_fixture_not_covering_foreground_desktop")
            finish(None)
            return
        report["opaque_cover_verified"] = True
        if args.native_typing_probe:
            native_text.setFocus()
            QTimer.singleShot(250, lambda: threading.Thread(target=run_native, daemon=True).start())
            return
        threading.Thread(target=run, daemon=True, name="aura-real-gui-qa").start()
        QTimer.singleShot(115_000, lambda: service.cancel_request(stopped))

    window.showFullScreen()
    window.raise_()
    window.activateWindow()
    import ctypes
    ctypes.windll.user32.SetForegroundWindow(int(window.winId()))
    QTimer.singleShot(800, begin)
    app.exec()
    return 0 if report.get("passed") and report.get("production_settings_unchanged") else 1


if __name__ == "__main__":
    raise SystemExit(main())
