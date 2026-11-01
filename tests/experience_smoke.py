"""Isolated R8 experience smoke; no desktop capture, activation or real vault.

Run: .venv\Scripts\python.exe -m tests.experience_smoke
The entire Qt process is offscreen. HF_HUB_OFFLINE forbids model downloads;
only synthetic chat requests to a loopback fixture are permitted.
"""

from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import time
import traceback
from unittest.mock import patch
from uuid import uuid4

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"

from PySide6.QtCore import QEvent, QPoint, QThreadPool, Qt
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from desktop_pet.application_controller import ApplicationController
from desktop_pet.config import PetConfig
from desktop_pet.memory.service import MemoryService
from desktop_pet.settings_manager import CredentialStore
from tests.mock_api import MockApiServer
from tests.test_application import MemoryCredentialBackend, configure_through_panel, isolated_settings, spin_until


ROOT = Path(__file__).resolve().parents[1]


def run_validation():
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    # The offscreen Windows plugin exposes no system font database. Register
    # existing OS fonts for readable widget-only screenshots; no font is copied
    # into the project, installed, or changed for other applications.
    loaded_fonts = []
    for font_path in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyhbd.ttc", "C:/Windows/Fonts/segoeui.ttf"):
        if Path(font_path).is_file():
            font_id = QFontDatabase.addApplicationFont(font_path)
            if font_id >= 0:
                loaded_fonts.extend(QFontDatabase.applicationFontFamilies(font_id))
    if "Microsoft YaHei UI" in loaded_fonts:
        app.setFont(QFont("Microsoft YaHei UI", 10))
    artifacts = ROOT / "artifacts"
    artifacts.mkdir(exist_ok=True)
    report = {
        "status": "failed", "qt_platform": app.platformName(),
        "scope": "real ApplicationController and Qt rendering; offscreen; temporary INI/SQLite; in-memory credentials; synthetic loopback chat",
        "model_policy": "HF_HUB_OFFLINE=1; existing public model cache only; no production memory enabled",
        "checks": {}, "screenshots": [], "metrics": {},
        "not_verified": ["native tray registration and OS activation", "physical mouse and keyboard", "external LLM or Embedding services", "real monitor hot-plug"],
    }
    session = None

    def check(name, passed):
        report["checks"][name] = bool(passed)
        if not passed:
            raise AssertionError(name)

    def capture(widget, filename):
        app.processEvents()
        path = artifacts / filename
        image = widget.grab()
        check(filename + "_rendered", not image.isNull() and image.width() > 100 and image.height() > 100)
        if not image.save(str(path), "PNG"):
            raise AssertionError("widget_screenshot_save")
        report["screenshots"].append(filename)

    def button_inside(window, button):
        return button.isVisible() and window.rect().contains(button.mapTo(window, QPoint(0, 0))) and window.rect().contains(
            button.mapTo(window, button.rect().bottomRight()))

    try:
        check("offscreen_does_not_activate_desktop", app.platformName() == "offscreen")
        check("offscreen_has_readable_application_fonts", bool(QFontDatabase.families()))
        with tempfile.TemporaryDirectory(prefix="aura-experience-smoke-") as temporary, MockApiServer() as server:
            root = Path(temporary)
            backend = MemoryCredentialBackend()
            settings = isolated_settings(root / "settings.ini", backend)

            def isolated_memory(settings_manager, data_dir):
                return MemoryService(settings_manager, data_dir,
                                     llm_credentials=CredentialStore(account="mem0-llm-key", backend=backend),
                                     embedding_credentials=CredentialStore(account="mem0-embedding-key", backend=backend))

            with patch("desktop_pet.application_controller.MemoryService", side_effect=isolated_memory):
                session = ApplicationController(PetConfig(), settings=settings)
                session.pet_controller.asset_player.directory = root / "empty-animations"
                session.show()
                spin_until(lambda: not session.memory.busy)
                check("pet_started_transparent_topmost", session.pet.isVisible()
                      and session.pet.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
                      and bool(session.pet.windowFlags() & Qt.WindowType.WindowStaysOnTopHint))
                check("memory_and_remote_perception_default_off", not session.memory.settings.enabled
                      and not session.memory.settings.automatic and not session.perception.settings.enabled
                      and not session.perception.settings.allow_remote)

                # Polling while disabled must never reach OS metadata/capture.
                forbidden_observations = []
                def forbidden_observation(*_args, **_kwargs):
                    forbidden_observations.append(True)
                    raise AssertionError("disabled_observation_reached_os_boundary")
                session.perception.observer.observe = forbidden_observation
                session.tray.pause_action.setChecked(True)
                check("tray_pause_stops_perception", session.perception.paused and not session.perception._timer.isActive())
                session.perception.poll()
                session.tray.pause_action.setChecked(False)
                session.perception.poll()
                check("resume_preserves_denied_permissions", not session.perception.paused
                      and not session.perception.settings.enabled and not session.perception.settings.allow_remote
                      and not session.perception._timer.isActive() and forbidden_observations == [])
                saved_perception = settings.load_perception_settings()
                check("runtime_pause_does_not_grant_persisted_consent", not saved_perception.enabled and not saved_perception.allow_remote)

                session.tray.chat_action.trigger()
                chat_window = session.chat_window
                check("tray_chat_action_opens_shared_conversation", chat_window is not None and chat_window.isVisible()
                      and chat_window.chat_panel.controller is session.chat)
                session.tray.settings_action.trigger()
                check("tray_settings_action_opens_panel", session.debug_panel is not None and session.debug_panel.isVisible())
                configure_through_panel(session, server.base_url)
                check("configuration_is_isolated_in_memory_vault", not session.debug_panel.api_error_label.isVisible()
                      and session.debug_panel.key_input.text() == "")
                session.debug_panel.tabs.setCurrentIndex(1)
                capture(session.debug_panel, "r8_experience_api.png")

                server.enqueue(json.dumps({"reply": "可以呀！聊天、历史和长期记忆现在都能从同一个窗口找到。", "emotion": "happy"}, ensure_ascii=False))
                chat_window.chat_panel._activate_native()
                chat_window.chat_panel.input.setPlainText("你好鲸鱼娘，我正在体验新版聊天界面。")
                chat_window.chat_panel.send_button.click()
                spin_until(lambda: not session.chat.busy, timeout=15)
                check("real_sdk_loopback_chat_complete", len(server.requests) == 1 and not session.chat.error
                      and len(session.chat.messages) == 2)
                session.debug_panel.show_chat_tab()
                session.debug_panel.chat_panel._activate_native()
                check("both_chat_views_share_messages", chat_window.chat_panel.transcript.isVisible()
                      and session.debug_panel.chat_panel.transcript.isVisible()
                      and "新版聊天界面" in chat_window.chat_panel.transcript.toPlainText()
                      and "新版聊天界面" in session.debug_panel.chat_panel.transcript.toPlainText())
                current_id = session.chat.current_conversation_id

                reference = session.chat.record_screen_comment(
                    current_id, "这个示例报错看起来与模块路径有关，可以一起检查。",
                    event_id=str(uuid4()), app_name="synthetic-editor.exe",
                    context_summary="合成编辑器示例：Python ModuleNotFoundError；没有采集真实屏幕。",
                    event_type="error", emotion="thinking")
                spin_until(lambda: any(row.get("id") == reference["message_id"] for row in session.chat.messages), timeout=10)
                session.pet_controller.respond(reference["content"], "thinking", conversation_link=reference)
                QTest.mouseClick(session.pet.bubble, Qt.MouseButton.LeftButton, pos=session.pet.bubble.rect().center())
                spin_until(lambda: not session.chat.history_loading)
                check("bubble_opens_archived_event_in_same_conversation", session.chat.current_conversation_id == current_id
                      and chat_window.isVisible() and "来自屏幕感知" in chat_window.chat_panel.transcript.toPlainText())
                check("linked_event_summary_visible", not chat_window.chat_panel.event_details.isHidden()
                      and "没有采集真实屏幕" in chat_window.chat_panel.event_details.text())
                capture(chat_window, "r8_experience_chat.png")

                session.tray.memory_action.trigger()
                spin_until(lambda: not session.memory.busy, timeout=15)
                memory_panel = session.memory_panel
                check("tray_memory_action_opens_single_owned_window", memory_panel is not None and memory_panel.isVisible())
                chat_window.chat_panel.memory_button.click()
                spin_until(lambda: not session.memory.busy, timeout=15)
                session.debug_panel.chat_panel.memory_button.click()
                spin_until(lambda: not session.memory.busy, timeout=15)
                check("both_chat_entries_reuse_same_memory_window", session.memory_panel is memory_panel
                      and chat_window.chat_panel.memory_panel is None and session.debug_panel.chat_panel.memory_panel is None)

                check("memory_ui_has_no_technical_configuration", not hasattr(memory_panel, "tabs")
                      and not hasattr(memory_panel, "embedding_provider_combo")
                      and not hasattr(memory_panel, "credentials_status_label"))
                check("opening_memory_does_not_enable_it", not session.memory.settings.enabled
                      and not session.memory.settings.automatic and not memory_panel.automatic_checkbox.isChecked())
                check("memory_actions_remain_inside_window", button_inside(memory_panel, memory_panel.automatic_checkbox)
                      and button_inside(memory_panel, memory_panel.refresh_button))
                # Developer-only fixture preparation keeps the existing cached
                # ONNX check; ordinary users no longer configure these fields.
                session.memory.save_settings(replace(session.memory.settings, embedding_provider="fastembed"))
                spin_until(lambda: not session.memory.busy, timeout=15)
                started = time.monotonic()
                session.memory.prepare_local_model()
                spin_until(lambda: not session.memory.busy, timeout=35)
                report["metrics"]["cached_model_check_seconds"] = round(time.monotonic() - started, 3)
                check("cached_chinese_model_real_onnx_ready_offline", not session.memory.error
                      and "本地模型已就绪" in session.memory.status and "512" in session.memory.status)
                check("offline_model_check_keeps_memory_disabled", not session.memory_service.load_settings().enabled
                      and not session.memory_service.load_settings().automatic)
                capture(memory_panel, "r8_experience_memory.png")

                # The ordinary windows may close/reopen without resetting the
                # app or creating a second settings/memory service.
                for window, opener in ((session.debug_panel, session.open_debug_panel),
                                       (chat_window, session.open_chat_window),
                                       (memory_panel, session.open_memory_window)):
                    window.close()
                    check(window.objectName() + "_close_hides", not window.isVisible() and session.pet.isVisible())
                    opener()
                    if window is memory_panel:
                        spin_until(lambda: not session.memory.busy, timeout=15)
                    check(window.objectName() + "_reopens", window.isVisible())
                check("reopen_preserves_conversation", session.chat.current_conversation_id == current_id
                      and len(session.chat.messages) == 3 and session.memory_panel is memory_panel)

                # Exercise the action's state wiring without pretending the
                # offscreen plugin registered a native Windows tray icon.
                native_available = session.tray.available
                session.tray.available = True
                session.tray.visibility_action.trigger()
                check("simulated_available_tray_can_hide_pet", not session.pet.isVisible()
                      and session.tray.visibility_action.text() == "显示桌宠")
                session.tray.visibility_action.trigger()
                check("simulated_available_tray_can_restore_pet", session.pet.isVisible())
                session.tray.available = native_available
                session.tray.menu.show()
                capture(session.tray.menu, "r8_experience_tray_menu.png")
                session.tray.menu.hide()

                session.pet.move(-4000, -4000)
                session.pet._placement.fit()
                check("pet_fit_callback_remains_connected", callable(session.pet._placement.fit_to_screen))
                check("pet_fits_work_area_after_restore", any(screen.availableGeometry().toRectF().adjusted(-1e-6, -1e-6, 1e-6, 1e-6).contains(
                          session.pet.character_placement_bounds.translated(session.pet.pos()))
                      for screen in app.screens()) and session.pet.rect().toRectF().contains(session.pet.animated_sprite_rect))
                for window in (session.debug_panel, chat_window, memory_panel):
                    window.move(-4000, -4000)
                    window._placement.fit()
                    check(window.objectName() + "_bounded_to_work_area", any(
                        screen.availableGeometry().contains(window.frameGeometry()) for screen in app.screens()))
                check("chat_send_still_accessible_after_fitting", button_inside(chat_window, chat_window.chat_panel.send_button))
                check("no_screen_analysis_or_image_http", forbidden_observations == [] and all(
                    "image_url" not in json.dumps(row["body"]) for row in server.requests))
                report["metrics"]["chat_http_requests"] = len(server.requests)
                report["metrics"]["metadata_http_requests"] = len(server.metadata_requests)
                report["metrics"]["native_tray_available_offscreen"] = native_available
                session.shutdown()
                spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0, timeout=35)
                check("clean_async_shutdown", session.shutdown_errors == [])
                for widget in (session.debug_panel, chat_window, memory_panel, session.pet):
                    widget.deleteLater()
                session.deleteLater()
                app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                app.processEvents()
                session = None
                report["status"] = "passed"
    except Exception:
        report["error"] = traceback.format_exc()
    finally:
        if session is not None:
            session.shutdown()
            try:
                spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0, timeout=35)
            except Exception:
                pass
        target = artifacts / "r8_experience_smoke.json"
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(run_validation())
