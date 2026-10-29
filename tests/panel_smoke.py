"""Visible full-session smoke test using the real SDK against localhost only.

Run: .venv\Scripts\python.exe tests\panel_smoke.py
Uses temporary INI settings and an in-memory credential backend. Never reads or
writes production credentials, captures the desktop, or moves the real mouse.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QThreadPool, QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from desktop_pet.application_controller import ApplicationController
from desktop_pet.config import PetConfig
from tests.mock_api import MockApiServer
from tests.test_application import (
    LOCAL_TEST_KEY, MemoryCredentialBackend, configure_through_panel,
    isolated_settings, spin_until,
)


def run_validation() -> int:
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    artifacts = ROOT / "artifacts"
    artifacts.mkdir(exist_ok=True)
    report = {
        "status": "failed",
        "scope": "Visible real PySide6 application session; real OpenAI SDK requests to localhost fixture only.",
        "external_provider_tested": False,
        "credential_storage": "Isolated in-memory test backend; production Windows credentials untouched.",
        "checks": {},
        "metrics": {},
        "screenshots": [],
        "not_verified": [
            "External provider authentication, model permission and billing",
            "Production Windows credential-vault persistence (this script uses an isolated backend)",
            "Real mouse, native hit testing and mixed-DPI/multi-monitor behavior",
        ],
    }
    session = None
    other = None
    timer = None

    def check(name: str, passed: bool) -> None:
        report["checks"][name] = bool(passed)
        if not passed:
            raise AssertionError("Failed check: " + name)

    def capture(widget, name: str) -> None:
        app.processEvents()
        path = artifacts / name
        if not widget.grab().save(str(path), "PNG"):
            raise RuntimeError("Unable to save the application-widget screenshot")
        report["screenshots"].append(name)

    try:
        with tempfile.TemporaryDirectory(prefix="aura-panel-smoke-") as directory, MockApiServer() as server:
            path = Path(directory) / "settings.ini"
            backend = MemoryCredentialBackend()
            settings = isolated_settings(path, backend)
            session = ApplicationController(PetConfig(), settings=settings)
            session.show()
            app.processEvents()
            check("pet_startup", session.pet.isVisible())
            check("transparent_topmost", bool(
                session.pet.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
                and session.pet.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
            ))
            configure_through_panel(session, server.base_url)
            panel = session.debug_panel
            check("independent_panel", panel.parentWidget() is None and panel.isVisible())
            check("no_topmost_panel", not bool(panel.windowFlags() & Qt.WindowType.WindowStaysOnTopHint))
            check("secret_field_empty_after_save", panel.key_input.text() == "")
            check("no_secret_in_ini", LOCAL_TEST_KEY not in path.read_text(encoding="utf-8"))
            server.enqueue("本地连接测试实际回复")
            panel.test_connection_button.click()
            spin_until(lambda: not session.chat.busy)
            check("actual_sdk_http_connection", len(server.requests) == 1 and "连接成功" in panel.connection_label.text())
            check("configured_model_and_endpoint", (
                server.requests[0]["path"] == "/v1/chat/completions"
                and server.requests[0]["body"]["model"] == "test-model"
            ))
            panel.tabs.setCurrentIndex(1)
            capture(panel, "panel_smoke_api.png")

            server.received.clear()
            server.enqueue(json.dumps({"reply": "你好呀！本地测试接口已收到消息，鲸鱼娘现在很开心。", "emotion": "happy"}, ensure_ascii=False), delay=0.75)
            ticks = []
            frames = []
            timer = QTimer()
            timer.setInterval(15)
            timer.timeout.connect(lambda: ticks.append(time.monotonic()))
            session.pet.frame_tick.connect(lambda elapsed: frames.append(elapsed))
            timer.start()
            panel.show_chat_tab()
            panel.chat_panel.input.setPlainText("你好鲸鱼娘，等待回复时我会调整你的大小。")
            QTest.keyClick(panel.chat_panel.input, Qt.Key.Key_Return)
            check("thinking_on_request", session.chat.busy and session.pet_controller.expression == "thinking")
            spin_until(server.received.is_set)
            check("request_pending_before_controls", session.chat.busy)
            panel.tabs.setCurrentIndex(0)
            panel.scale_slider.setValue(125)
            panel.expression_combo.setCurrentIndex(panel.expression_combo.findData("sleepy"))
            check("controls_respond_during_request", (
                session.pet_controller.scale_percent == 125
                and session.pet_controller.expression == "sleepy"
                and settings.load_scale() == 125
                and session.chat.busy
            ))
            spin_until(lambda: not session.chat.busy)
            timer.stop()
            check("gui_timers_keep_running", len(ticks) >= 20 and len(frames) >= 10)
            report["metrics"]["qt_15ms_timer_ticks_during_slow_request"] = len(ticks)
            report["metrics"]["pet_frames_during_slow_request"] = len(frames)
            if len(ticks) > 1:
                report["metrics"]["largest_tick_gap_ms"] = round(max(b - a for a, b in zip(ticks, ticks[1:])) * 1000, 2)
            check("parsed_emotion_drives_pet", session.pet_controller.expression == "happy")
            check("missing_art_fallback_visible", "尚未提供" in panel.expression_status.text())
            check("sprite_within_window", session.pet.rect().toRectF().contains(session.pet.animated_sprite_rect))
            capture(panel, "panel_smoke_controls.png")
            capture(session.pet, "panel_smoke_pet.png")
            panel.tabs.setCurrentIndex(2)
            capture(panel, "panel_smoke_chat.png")

            saved_messages = session.chat.messages
            panel.close()
            check("panel_close_keeps_pet", not panel.isVisible() and session.pet.isVisible())
            session.open_debug_panel()
            check("panel_reopen_retains_instance_and_state", (
                session.debug_panel is panel and panel.isVisible()
                and session.pet_controller.scale_percent == 125
                and session.pet_controller.expression == "happy"
                and session.chat.messages == saved_messages
            ))
            session.open_chat_window()
            separate = session.chat_window
            check("independent_chat_shares_session", (
                separate.parentWidget() is None
                and "本地测试接口已收到消息" in separate.chat_panel.transcript.toPlainText()
            ))
            server.enqueue("普通文本回复也能正常显示。")
            separate.chat_panel.input.setPlainText("再发一条，验证共享上下文。")
            separate.chat_panel.send_button.click()
            spin_until(lambda: not session.chat.busy)
            check("plain_text_reply_and_context", (
                "普通文本回复也能正常显示。" in panel.chat_panel.transcript.toPlainText()
                and len(server.requests[-1]["body"]["messages"]) == 4
                and session.pet_controller.expression == "neutral"
            ))
            capture(separate, "panel_smoke_separate_chat.png")
            separate.close()
            session.open_chat_window()
            check("chat_reopen_retains_instance", session.chat_window is separate and separate.isVisible())

            reloaded = isolated_settings(path, backend)
            check("settings_reload", reloaded.load_scale() == 125 and reloaded.load_api_settings().base_url == server.base_url)
            check("isolated_credential_reload", reloaded.get_api_key() == LOCAL_TEST_KEY)
            panel.clear_key_button.click()
            check("credential_delete_verified", reloaded.get_api_key() == "")
            # Restore only the isolated test vault so the following HTTP checks run.
            settings.save_api_key(LOCAL_TEST_KEY)
            server.enqueue("untrusted body echo " + LOCAL_TEST_KEY, status=401)
            panel.test_connection_button.click()
            spin_until(lambda: not session.chat.busy)
            check("actual_authentication_failure", "认证失败" in panel.connection_label.text())
            check("authentication_error_redacts_secret", LOCAL_TEST_KEY not in panel.connection_label.text())

            other_settings = isolated_settings(Path(directory) / "unconfigured.ini", MemoryCredentialBackend())
            other = ApplicationController(PetConfig(), settings=other_settings)
            other.show()
            other.open_debug_panel()
            other.debug_panel.chat_panel.input.setPlainText("未配置时也应正常运行")
            other.debug_panel.chat_panel.send_button.click()
            check("unconfigured_chat_is_safe", (
                not other.chat.busy and bool(other.chat.error) and other.pet.isVisible()
                and not other.debug_panel.chat_panel.error_label.isHidden()
            ))
            other.shutdown()

            server.received.clear()
            server.enqueue('{"reply":"退出后不能再显示此条","emotion":"happy"}', delay=1.5)
            check("inflight_exit_request_started", session.chat.send("开始最后一次慢请求，然后退出。"))
            spin_until(server.received.is_set)
            before = session.chat.messages
            started = time.monotonic()
            session.shutdown()
            shutdown_ms = (time.monotonic() - started) * 1000
            report["metrics"]["inflight_shutdown_call_ms"] = round(shutdown_ms, 2)
            spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0, timeout=2)
            check("inflight_exit_is_nonblocking", shutdown_ms < 300)
            check("inflight_exit_hides_windows_and_ignores_reply", (
                not session.pet.isVisible() and not panel.isVisible()
                and not separate.isVisible() and session.chat.messages == before
            ))
            report["metrics"]["actual_local_http_requests"] = len(server.requests)
            report["status"] = "passed"
    except Exception as exc:
        report["error"] = (type(exc).__name__ + ": " + str(exc)).replace(LOCAL_TEST_KEY, "[test credential hidden]")
    finally:
        if timer is not None:
            timer.stop()
        for active in (session, other):
            if active is not None:
                active.shutdown()
        try:
            spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0, timeout=4)
        except AssertionError:
            report["worker_cleanup_failed"] = True
            report["status"] = "failed"
        path = artifacts / "panel_smoke_report.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(run_validation())
