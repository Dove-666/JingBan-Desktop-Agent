"""Visible perception workflow smoke test using generated images and loopback.

Run: .venv\Scripts\python.exe tests\perception_smoke.py
Only synthetic window pixels reach the local fixture. No real desktop capture,
external provider, production settings/credentials, or physical mouse is used.
"""

from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QThreadPool, QTimer
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from desktop_pet.application_controller import ApplicationController
from desktop_pet.config import PetConfig
from desktop_pet.perception.controller import PerceptionController
from desktop_pet.perception.settings import PerceptionSettings
from desktop_pet.settings_manager import ApiSettings
from tests.mock_api import MockApiServer
from tests.test_application import (
    LOCAL_TEST_KEY, MemoryCredentialBackend, isolated_settings, spin_until,
)
from tests.test_perception_integration import AdjustableClock, SyntheticScene, event_json


def run_validation() -> int:
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    artifacts = ROOT / "artifacts"
    artifacts.mkdir(exist_ok=True)
    report = {
        "status": "failed",
        "scope": "Visible Qt application; generated in-memory QImage; real OpenAI SDK to loopback HTTP only.",
        "real_user_desktop_captured": False,
        "external_provider_tested": False,
        "production_settings_or_credentials_used": False,
        "checks": {}, "metrics": {}, "screenshots": [],
        "not_verified": [
            "Actual user-desktop pixel capture or real foreground app metadata",
            "External multimodal model recognition accuracy, authentication and permissions",
            "Exclusive fullscreen, elevated windows, protected video or mixed-DPI capture",
            "Real mouse and keyboard input on the user's desktop",
        ],
    }
    session = None
    controller = None
    timer = None

    def check(name, condition):
        report["checks"][name] = bool(condition)
        if not condition:
            raise AssertionError("Failed check: " + name)

    def capture(widget, filename):
        app.processEvents()
        if not widget.grab().save(str(artifacts / filename), "PNG"):
            raise RuntimeError("Application-widget screenshot could not be saved")
        report["screenshots"].append(filename)

    try:
        with tempfile.TemporaryDirectory(prefix="aura-perception-smoke-") as directory, MockApiServer() as server:
            path = Path(directory) / "settings.ini"
            backend = MemoryCredentialBackend()
            settings = isolated_settings(path, backend)
            session = ApplicationController(PetConfig(), settings=settings)
            session.pet_controller.asset_player.directory = Path(directory) / "empty-animations"
            session.show()
            check("startup_observation_disabled", not session.perception.settings.enabled)
            check("startup_remote_disabled", not session.perception.settings.allow_remote)
            # Replace the disabled observer/controller before any opt-in. This
            # retains the real application composition and all production UI.
            session.perception.shutdown()
            session.perception.deleteLater()
            scene = SyntheticScene()
            clock = AdjustableClock()
            controller = PerceptionController(
                settings, session.pet_controller, session.chat,
                observer=scene.observer(), clock=clock, parent=session,
            )
            session.perception = controller
            controller.poll()
            app.processEvents()
            check("disabled_does_not_read_or_capture", scene.metadata_calls == 0 and scene.capture_calls == 0)
            session.open_debug_panel()
            panel = session.debug_panel
            check("perception_is_independent_debug_tab", panel.tabs.count() >= 4 and panel.parentWidget() is None)
            panel.tabs.setCurrentIndex(3)
            settings.save_api_settings(ApiSettings(server.base_url, "test-model", 5))
            settings.save_api_key(LOCAL_TEST_KEY)
            check("explicit_opt_in_saved", controller.update_settings(PerceptionSettings(enabled=True, allow_remote=True)))
            controller.observe_now()
            spin_until(lambda: not controller.busy)
            check("manual_observation_is_local_only", scene.capture_calls > 0 and not server.requests)
            check("preview_shown_from_synthetic_image", not panel.perception_panel._preview_image.isNull())

            scene.change()
            server.enqueue(event_json(), delay=0.75)
            ticks = []
            frames = []
            timer = QTimer()
            timer.setInterval(15)
            timer.timeout.connect(lambda: ticks.append(time.monotonic()))
            session.pet.frame_tick.connect(lambda elapsed: frames.append(elapsed))
            timer.start()
            controller.poll()
            spin_until(server.received.is_set)
            check("actual_sdk_request_pending", controller.busy and len(server.requests) == 1)
            session.pet_controller.set_scale(125)
            session.pet_controller.set_expression("sleepy")
            check("controls_work_during_analysis", (
                controller.busy and session.pet_controller.scale_percent == 125
                and session.pet_controller.expression == "sleepy"
            ))
            spin_until(lambda: not controller.busy)
            timer.stop()
            check("gui_continues_while_http_waits", len(ticks) >= 20 and len(frames) >= 10)
            report["metrics"]["qt_15ms_timer_ticks_during_slow_request"] = len(ticks)
            report["metrics"]["pet_frames_during_slow_request"] = len(frames)
            if len(ticks) > 1:
                report["metrics"]["largest_qt_tick_gap_ms"] = round(max(b - a for a, b in zip(ticks, ticks[1:])) * 1000, 2)
            parts = server.requests[0]["body"]["messages"][-1]["content"]
            check("sdk_sent_image_content", any(part.get("type") == "image_url" for part in parts))
            check("model_event_drives_unified_character", session.pet_controller.expression == "happy")
            check("proactive_reply_in_pet_bubble", "合成图表" in session.pet.bubble._text)
            check("proactive_event_is_archived_with_explicit_link", (
                len(session.chat.messages) == 1
                and session.chat.messages[0]["source"] == "proactive_screen"
                and bool(session.chat.messages[0].get("screen_event_id"))
                and session.chat.messages[0]["conversation_id"] == session.chat.current_conversation_id
            ))
            check("bounded_sprite_after_scaling", session.pet.rect().toRectF().contains(session.pet.animated_sprite_rect))
            check("debug_event_history_updates", bool(controller.history) and bool(panel.perception_panel.history_view.toPlainText()))
            capture(panel, "perception_smoke_panel.png")
            scrollbar = panel.perception_panel.scroll.verticalScrollBar()
            scrollbar.setValue(scrollbar.maximum())
            capture(panel, "perception_smoke_context.png")
            capture(session.pet, "perception_smoke_pet.png")

            external_info = scene.info
            captures_before = scene.capture_calls
            scene.info = replace(scene.info, pid=os.getpid(), title="Synthetic AURA debug panel")
            controller.poll()
            check("returning_to_aura_retains_labeled_preview", (
                not panel.perception_panel._preview_image.isNull()
                and controller.details.get("preview_retained") is True
                and "上次本地预览" in panel.perception_panel.preview_status_label.text()
            ))
            check("viewing_retained_preview_never_recaptures_or_calls_api", (
                scene.capture_calls == captures_before and len(server.requests) == 1
            ))
            capture(panel, "perception_smoke_retained.png")
            scene.info = external_info

            clock.advance(61)
            controller.poll()
            spin_until(lambda: not controller.busy)
            check("stable_scene_has_no_extra_model_request", len(server.requests) == 1)
            scene.change("#353a64")
            server.enqueue(event_json(key="second-synthetic-event", emotion="surprised", reply="冷却中只做动作。"))
            session.pet.bubble.hide()
            controller.poll()
            spin_until(lambda: not controller.busy)
            check("cooldown_downgrades_to_silent_motion", session.pet_controller.expression == "surprised" and not session.pet.bubble.isVisible())
            scene.change("#417837")
            clock.advance(5)
            controller.poll()
            spin_until(lambda: not controller.busy)
            check("analysis_interval_limits_requests", len(server.requests) == 2)

            scene.change("#000000")
            controller.poll()
            check("black_frame_is_rejected_without_api", (
                len(server.requests) == 2 and not controller.busy
                and "近乎全黑" in controller.status
                and panel.perception_panel._preview_image.isNull()
            ))

            captures_before = scene.capture_calls
            private_title = "SYNTHETIC 密码窗口 - 不应展示或发送"
            scene.info = replace(scene.info, title=private_title)
            controller.poll()
            spin_until(lambda: not controller.busy)
            check("privacy_filter_precedes_capture_and_http", scene.capture_calls == captures_before and len(server.requests) == 2)
            check("private_title_and_preview_hidden", (
                private_title not in repr(controller.details)
                and panel.perception_panel._preview_image.isNull()
            ))
            capture(panel, "perception_smoke_privacy.png")

            scene.change("#e0bf3c", title="Synthetic safe window")
            clock.advance(61)
            server.enqueue("provider should not echo " + LOCAL_TEST_KEY, status=401)
            controller.poll()
            spin_until(lambda: not controller.busy)
            check("actual_api_failure_is_safe", len(server.requests) == 3 and LOCAL_TEST_KEY not in controller.status and session.pet.isVisible())
            text = path.read_text(encoding="utf-8")
            check("config_excludes_screen_and_secret", all(value not in text for value in (LOCAL_TEST_KEY, private_title, "data:image/", "合成图表")))
            reloaded = isolated_settings(path, backend)
            check("perception_preferences_reload", reloaded.load_perception_settings() == controller.settings)
            panel.close()
            session.open_debug_panel()
            check("panel_reopens_without_resetting_perception", session.debug_panel is panel and bool(controller.history))
            check("disable_saved_and_stops_observation", controller.update_settings(replace(controller.settings, enabled=False)))
            captures_before = scene.capture_calls
            controller.poll()
            check("disabled_pipeline_no_capture", scene.capture_calls == captures_before)
            archived = session.chat.messages
            controller.reset_session()
            check("debug_reset_preserves_archived_comments", (
                controller.history == [] and controller.details == {} and session.chat.messages == archived
            ))
            report["metrics"]["actual_loopback_http_requests"] = len(server.requests)
            report["metrics"]["auxiliary_metadata_http_requests"] = len(server.metadata_requests)
            report["metrics"]["synthetic_capture_calls"] = scene.capture_calls
            session.shutdown()
            spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
            check("shutdown_hides_pet_and_panel", not session.pet.isVisible() and not panel.isVisible())
            report["status"] = "passed"
    except Exception as exc:
        report["error"] = (type(exc).__name__ + ": " + str(exc)).replace(LOCAL_TEST_KEY, "[synthetic credential hidden]")
    finally:
        if timer is not None:
            timer.stop()
        if controller is not None:
            controller.shutdown()
        if session is not None:
            session.shutdown()
        try:
            spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
        except AssertionError:
            report["worker_cleanup_failed"] = True
            report["status"] = "failed"
        (artifacts / "perception_smoke_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8",
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(run_validation())
