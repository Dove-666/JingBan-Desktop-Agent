"""Camera chat metadata and settings use synthetic Qt WebEngine messages only."""

import json
import unittest

from PySide6.QtCore import QEvent, QObject, Signal
from PySide6.QtWidgets import QApplication

from tests.test_assistant_ui import AssistantUiChat, click_dom, eval_js, make_panel, wait_js, wait_until


class CameraSettingsNotifier(QObject):
    settings_requested = Signal()


class CameraWebUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.chat = AssistantUiChat()
        self.camera = CameraSettingsNotifier()
        self.opened = []
        self.camera.settings_requested.connect(lambda: self.opened.append("camera"))
        self.chat.camera_controller = self.camera
        self.panel = make_panel(self.chat)

    def tearDown(self):
        self.panel.hide()
        self.panel.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def publish_event(self, source, source_kind=""):
        self.chat.messages = [{
            "id": "camera-test-event", "role": "assistant", "source": source,
            "content": "合成摄像头识别评论。",
            "screen_event": {"source_kind": source_kind, "app_name": "合成设备",
                             "observed_at": "2026-10-31 20:00", "event_type": "camera_context",
                             "context_summary": "仅用于测试的合成场景，不含实际画面。"},
        }]
        self.chat.messages_changed.emit()
        wait_js(self.panel, "Boolean(document.querySelector('[data-message-id=camera-test-event]'))")

    def test_proactive_camera_badge_and_inline_context_are_camera_specific(self):
        self.publish_event("proactive_camera")
        selector = "[data-message-id=camera-test-event]"
        wait_js(self.panel, f"document.querySelector('{selector} .event-toggle')?.textContent.includes('来自摄像头识别')")
        self.assertEqual(eval_js(self.panel, f"document.querySelector('{selector} .screen-event').dataset.sourceKind"), "camera")
        click_dom(self.panel, selector + " .event-toggle")
        wait_js(self.panel, f"Boolean(document.querySelector('{selector} .event-details'))")
        header = eval_js(self.panel, f"document.querySelector('{selector} .event-app').textContent")
        self.assertIn("摄像头情境", header)
        self.assertIn("合成设备", header)
        self.assertNotIn("屏幕情境", header)

    def test_legacy_screen_event_source_kind_camera_is_preserved_through_bridge(self):
        self.publish_event("proactive_screen", "camera")
        wait_js(self.panel, "document.querySelector('.event-toggle')?.textContent.includes('来自摄像头识别')")
        event = self.panel.web_state()["messages"][0]["screen_event"]
        self.assertEqual(event["source_kind"], "camera")
        self.assertEqual(eval_js(self.panel, "document.querySelector('.screen-event').dataset.sourceKind"), "camera")

    def test_screen_origin_keeps_existing_screen_label(self):
        self.publish_event("proactive_screen", "screen")
        wait_js(self.panel, "document.querySelector('.event-toggle')?.textContent.includes('来自屏幕感知')")
        self.assertEqual(eval_js(self.panel, "document.querySelector('.screen-event').dataset.sourceKind"), "screen")
        click_dom(self.panel, ".event-toggle")
        wait_js(self.panel, "Boolean(document.querySelector('.event-details'))")
        self.assertNotIn("摄像头情境", eval_js(self.panel, "document.querySelector('.event-app').textContent"))

    def test_camera_tools_entry_only_emits_settings_requested(self):
        click_dom(self.panel, "[data-testid=tools-button]")
        wait_js(self.panel, "[...document.querySelectorAll('.tools-popover button')].some(button=>button.textContent==='摄像头识别')")
        eval_js(self.panel, "[...document.querySelectorAll('.tools-popover button')].find(button=>button.textContent==='摄像头识别').click()")
        wait_until(lambda: self.opened == ["camera"])
        self.assertEqual(self.chat.sent, [])
        self.assertFalse(self.chat.busy)

    def test_camera_capability_absent_hides_entry(self):
        self.chat.camera_controller = None
        self.panel._queue_web_state()
        click_dom(self.panel, "[data-testid=tools-button]")
        wait_js(self.panel, "Boolean(document.querySelector('.tools-popover'))")
        self.assertNotIn("摄像头识别", eval_js(self.panel, "document.querySelector('.tools-popover').textContent"))
        self.assertFalse(json.loads(self.panel.bridge.dispatch("cameraSettings", "{}"))["ok"])


if __name__ == "__main__":
    unittest.main()
