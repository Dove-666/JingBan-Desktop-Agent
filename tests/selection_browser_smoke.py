"""Disposable real-Windows browser-to-pet smoke driver.

Run this separately from unittest discovery. The real browser extension/native
host may address the config_path in browser-smoke.json. All model traffic goes
to a 127.0.0.1 mock; credentials exist only in this process's memory. A synthetic
HTTP fixture is included so no personal page, clipboard or screen capture is
needed. Only this application's own widgets are saved as screenshots.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUTPUT = ROOT / "artifacts" / "selection_ask_20261031"
SELECTION_MARKER = "AURA_BROWSER_SELECTED_FIXTURE"
PAGE_MARKER = "AURA_BROWSER_PAGE_FIXTURE"
QUESTION = "请结合当前页面，解释我选中的这段测试内容。"


class FixturePage:
    def __init__(self):
        html = ("<!doctype html><html lang='zh-CN'><meta charset='utf-8'>"
                "<title>AURA 右键提问 · 本机合成测试页面</title><style>"
                "body{max-width:850px;margin:55px auto;padding:24px;color:#26364a;"
                "font:20px/1.8 'Microsoft YaHei UI',sans-serif;background:#f7f9fc}"
                "article{background:white;padding:30px;border-radius:16px;border:1px solid #dce4ed}"
                "#selection{background:#eaf2ff;padding:14px;border-radius:8px}"
                "small{color:#667085}</style><article><h1>问问鲸鱼娘：本机集成测试</h1>"
                "<p>选中下面的蓝底文字，然后右键选择“问问鲸鱼娘”。</p>"
                f"<p id='selection'>{SELECTION_MARKER}：桌宠右键提问为什么需要保留当前页面背景？</p>"
                f"<p>{PAGE_MARKER}：当前页面是本机生成的测试资料。选中文字用于定位具体问题，"
                "有限的页面正文提供必要背景；用户可以在发送前查看这些资料。</p>"
                "<small>本页面没有个人数据、表单、外部资源或真实 API。测试回复来自本机模拟服务。</small>"
                "</article></html>").encode("utf-8")
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass
            def do_GET(self):
                if self.path != "/fixture":
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(html)))
                self.end_headers()
                self.wfile.write(html)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.server.block_on_close = False
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": .02}, daemon=True)
        self.url = f"http://127.0.0.1:{self.server.server_port}/fixture"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=120)
    parser.add_argument("--auto-submit", action="store_true")
    parser.add_argument("--submit-delay-ms", type=int, default=1500)
    options = parser.parse_args(argv)
    if not 1 <= options.seconds <= 3600 or not 100 <= options.submit_delay_ms <= 30000:
        parser.error("--seconds must be 1..3600; --submit-delay-ms must be 100..30000")
    for name in tuple(os.environ):
        if name.startswith("OPENAI_") or name in {"MEM0_API_KEY", "MEM0_DIR"}:
            os.environ.pop(name, None)
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["MEM0_TELEMETRY"] = "False"

    from PySide6.QtCore import QEvent, QObject, QThreadPool, QTimer
    from PySide6.QtWidgets import QApplication
    from desktop_pet.application_controller import ApplicationController
    from desktop_pet.config import PetConfig
    from desktop_pet.memory.service import MemoryService
    from desktop_pet.settings_manager import ApiSettings, CredentialStore
    from tests.mock_api import MockApiServer
    from tests.test_application import LOCAL_TEST_KEY, MemoryCredentialBackend, isolated_settings

    class NoExtra(QObject):
        """No tray icon and no remote listener in the isolated test instance."""
        available = False
        def __init__(self, *_args, **_kwargs):
            super().__init__()
        def refresh(self):
            pass
        def shutdown(self):
            pass

    OUTPUT.mkdir(parents=True, exist_ok=True)
    state_path = OUTPUT / "browser-smoke.json"
    app = QApplication.instance() or QApplication([])
    app.setApplicationName("AURA isolated browser smoke")
    app.setQuitOnLastWindowClosed(False)
    started = time.monotonic()
    screenshots = {}
    state = {"pid": os.getpid(), "running": True, "auto_submit": options.auto_submit,
             "composer_seen": False, "composer_open": False, "api_call_count": 0,
             "last_request_contains_selection": False, "last_request_contains_page_text": False,
             "last_request_contains_synthetic_selection": False,
             "last_request_contains_synthetic_page": False, "reply_received": False,
             "screenshots": screenshots, "production_data_accessed": False,
             "external_model_api_called": False, "screen_or_camera_captured": False,
             "shutdown_clean": False}

    def save_state():
        pending = state_path.with_suffix(".pending.json")
        try:
            pending.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            pending.replace(state_path)
            return True
        except PermissionError:
            # A Windows inspector can momentarily hold the destination without
            # FILE_SHARE_DELETE. Keep atomic publication; retry the next tick.
            state["status_write_retry_count"] = state.get("status_write_retry_count", 0) + 1
            return False

    with ExitStack() as stack:
        temp = stack.enter_context(tempfile.TemporaryDirectory(prefix="aura-browser-native-smoke-"))
        base = Path(temp)
        api = stack.enter_context(MockApiServer())
        fixture = stack.enter_context(FixturePage())
        for _ in range(12):
            api.enqueue(json.dumps({"reply": "【本机模拟回复】选中文字用于定位问题，当前页面正文补充必要背景。"
                                             "这是一条固定测试回复，没有调用真实模型。", "emotion": "happy"},
                                   ensure_ascii=False), delay=.4)
        backend = MemoryCredentialBackend()
        settings = isolated_settings(base / "settings.ini", backend)
        settings.save_api_settings(ApiSettings(api.base_url, "synthetic-browser-smoke", 5))
        settings.save_api_key(LOCAL_TEST_KEY)
        settings.save_scale(50)
        state.update({"config_path": settings.config_path, "state_file": str(state_path),
                      "fixture_url": fixture.url, "temporary_data_directory": str(base)})
        safe_memory = lambda store, directory: MemoryService(store, directory,
            llm_credentials=CredentialStore(backend=backend, account="smoke-memory-llm"),
            embedding_credentials=CredentialStore(backend=backend, account="smoke-memory-embedding"))
        stack.enter_context(patch("desktop_pet.application_controller.DesktopTray", NoExtra))
        stack.enter_context(patch("desktop_pet.application_controller.RemoteController", NoExtra))
        stack.enter_context(patch("desktop_pet.application_controller.MemoryService", safe_memory))
        capture_guards = []
        for name in ("desktop_pet.perception.screen_observer.ScreenObserver.observe",
                     "desktop_pet.perception.camera_observer.CameraObserver.start",
                     "desktop_pet.perception.camera_observer.CameraObserver.available_devices"):
            capture_guards.append(stack.enter_context(patch(name, side_effect=AssertionError("Capture forbidden in smoke driver"))))
        session = ApplicationController(PetConfig(), settings=settings)
        session.chat._metadata_factory = lambda: None
        session.pet_controller.asset_player.directory = base / "no-video-animation"
        session.show()
        screen = app.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            session.pet.move(area.right() - session.pet.width() - 12,
                             area.bottom() - session.pet.height() - 12)
        if session.selection_inbox is None or not session.selection_inbox._server.isListening():
            session.shutdown()
            raise RuntimeError("The isolated selection inbox could not start")
        state["inbox_listening"] = True
        state["screen_perception_disabled"] = not session.perception.settings.enabled
        state["camera_disabled"] = not session.camera.settings.enabled
        state["memory_disabled"] = not session.memory.settings.enabled
        seen = set()
        auto_submitted = False
        completed_at = None

        def screenshot(widget, name):
            path = OUTPUT / name
            if widget is not None and widget.isVisible() and widget.grab().save(str(path)):
                screenshots[name] = str(path)

        def submit(dialog):
            if not session._shutting_down and dialog.isVisible():
                dialog.input.setPlainText(QUESTION)
                screenshot(dialog, "browser-smoke-composer.png")
                dialog.send_button.click()

        def tick():
            nonlocal auto_submitted, completed_at
            dialogs = [dialog for dialog in session._selection_dialogs.values() if dialog.isVisible()]
            state["composer_open"] = bool(dialogs)
            state["composer_count"] = len(dialogs)
            state["chat_open"] = session.chat_window is not None and session.chat_window.isVisible()
            state["api_call_count"] = len(api.requests)
            state["metadata_api_call_count"] = len(api.metadata_requests)
            state["chat_busy"] = session.chat.busy
            state["error_visible"] = bool(session.chat.error)
            state["elapsed_seconds"] = round(time.monotonic() - started, 1)
            state["capture_guard_called"] = any(guard.called for guard in capture_guards)
            state["screen_or_camera_captured"] = False
            for request_id, dialog in tuple(session._selection_dialogs.items()):
                if request_id in seen or not dialog.isVisible():
                    continue
                seen.add(request_id)
                state["composer_seen"] = True
                state["composer_has_selected_text"] = bool(dialog.context.text)
                state["composer_has_page_text"] = bool(dialog.context.page_text)
                screenshot(dialog, "browser-smoke-composer.png")
                if options.auto_submit and not auto_submitted:
                    auto_submitted = True
                    state["auto_submit_scheduled"] = True
                    QTimer.singleShot(options.submit_delay_ms, lambda dialog=dialog: submit(dialog))
            if api.requests:
                messages = api.requests[-1]["body"].get("messages", [])
                payloads = []
                for item in messages:
                    content = item.get("content", "")
                    if isinstance(content, str) and content.startswith("【问问鲸鱼娘"):
                        try:
                            payloads.append(json.loads(content.split("\n", 2)[2]))
                        except (ValueError, IndexError):
                            pass
                state["last_request_contains_selection"] = any(bool(value.get("selected_text")) for value in payloads)
                state["last_request_contains_page_text"] = any(bool(value.get("page_text")) for value in payloads)
                state["last_request_contains_synthetic_selection"] = any(
                    SELECTION_MARKER in value.get("selected_text", "") for value in payloads)
                state["last_request_contains_synthetic_page"] = any(
                    PAGE_MARKER in value.get("page_text", "") for value in payloads)
            if not session.chat.busy and any(item.get("role") == "assistant" for item in session.chat.messages):
                state["reply_received"] = True
                if completed_at is None:
                    completed_at = time.monotonic()
                if session.chat_window is not None:
                    panel = session.chat_window.chat_panel
                    ready = bool(panel._frontend_ready or panel._native_mode)
                    state["chat_frontend_ready"] = bool(panel._frontend_ready)
                    if (time.monotonic() - completed_at > 1.5 and (ready or time.monotonic() - completed_at > 8)
                            and "browser-smoke-chat.png" not in screenshots):
                        screenshot(session.chat_window, "browser-smoke-chat.png")
            save_state()

        stopping = False
        def stop():
            nonlocal stopping
            if stopping:
                return
            stopping = True
            timer.stop()
            session.shutdown()
            state["running"] = False
            state["inbox_listening"] = False
            deadline = time.monotonic() + 5
            def drain():
                if QThreadPool.globalInstance().activeThreadCount() and time.monotonic() < deadline:
                    QTimer.singleShot(30, drain)
                    return
                state["shutdown_clean"] = (not session.shutdown_errors
                    and QThreadPool.globalInstance().activeThreadCount() == 0)
                state["capture_guard_called"] = any(guard.called for guard in capture_guards)
                if not save_state() and time.monotonic() < deadline:
                    QTimer.singleShot(50, drain)
                    return
                app.quit()
            drain()

        timer = QTimer()
        timer.setInterval(200)
        timer.timeout.connect(tick)
        timer.start()
        session.finished.connect(lambda: QTimer.singleShot(0, stop))
        QTimer.singleShot(round(options.seconds * 1000), stop)
        save_state()
        print(json.dumps({"state_file": str(state_path), "config_path": settings.config_path,
                          "fixture_url": fixture.url}, ensure_ascii=True), flush=True)
        try:
            result = app.exec()
        finally:
            if not stopping:
                stop()
            session.deleteLater()
            app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()
        return result


if __name__ == "__main__":
    raise SystemExit(main())
