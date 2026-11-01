"""Repaint work stays out of hidden native controls and duplicate snapshots.

The shipped WebEngine receives synthetic Qt input. Conversations and perception
are in-memory doubles; no real settings, clipboard, credentials or API is used.
These assertions cover work scheduling, not a claim about monitor presentation.
"""
import json
import unittest

from PySide6.QtTest import QTest

from tests import test_assistant_ui as fixture
from tests.test_assistant_ui import bridge_action, eval_js, set_dom_text, wait_js, wait_until
from tests.test_glass_ui import GlassInteractions


class ChatRepaintTests(GlassInteractions, unittest.TestCase):
    setUpClass = fixture.AssistantUiTests.__dict__["setUpClass"]
    tearDown = fixture.AssistantUiTests.tearDown

    def setUp(self):
        fixture.AssistantUiTests.setUp(self)
        self.panel.activateWindow()
        self.wait_for_renderer_size(self.panel)
        QTest.qWait(80)

    def capture_snapshots(self, panel=None):
        panel = panel or self.panel
        # Establish an explicit baseline after initial WebChannel actions settle.
        QTest.qWait(40)
        panel._publish_web_state()
        QTest.qWait(40)
        snapshots = []
        panel.bridge.stateChanged.connect(lambda payload: snapshots.append(json.loads(payload)))
        return snapshots

    def test_real_web_keystrokes_do_not_rebuild_hidden_native_document(self):
        self.click_web(self.panel, "#message-input")
        native_changes = []
        self.panel.input.document().contentsChanged.connect(lambda: native_changes.append(True))
        self.assertFalse(self.panel.input.isVisible())
        native_before = self.panel.input.toPlainText()
        text = "keep native document asleep"
        QTest.keyClicks(self.keyboard_target(self.panel), text, delay=4)
        wait_js(self.panel, "document.querySelector('#message-input').value === " + json.dumps(text))
        wait_until(lambda: self.panel._web_drafts.get("current") == text)
        self.assertEqual(native_changes, [])
        self.assertEqual(self.panel.input.toPlainText(), native_before)
        self.assertEqual(self.chat.sent, [])

    def test_latest_web_draft_restores_in_native_and_native_edits_survive_reload(self):
        draft = "网页中尚未发送的合成草稿"
        set_dom_text(self.panel, "#message-input", draft)
        wait_until(lambda: self.panel._web_drafts.get("current") == draft)
        self.panel._activate_native()
        self.assertTrue(self.panel._native_mode)
        self.assertEqual(self.panel.input.toPlainText(), draft)
        self.panel.input.insertPlainText("，兼容界面继续编辑")
        latest = self.panel.input.toPlainText()
        wait_until(lambda: self.panel._web_drafts.get("current") == latest)
        self.panel._reload_web()
        wait_until(lambda: self.panel._frontend_ready, timeout=20)
        wait_js(self.panel, "document.querySelector('#message-input').value === " + json.dumps(latest))
        self.assertFalse(self.panel._native_mode)
        self.assertEqual(self.chat.sent, [])

    def test_bridge_drafts_still_update_the_visible_native_editor(self):
        self.panel._activate_native()
        self.assertTrue(self.panel.dispatch_web_action("draft", {
            "conversationId": "current", "text": "native-compatible bridge draft"}))
        self.assertEqual(self.panel.input.toPlainText(), "native-compatible bridge draft")
        self.assertEqual(self.panel._web_drafts["current"], "native-compatible bridge draft")

    def test_visual_timers_do_not_publish_history_but_state_reads_stay_fresh(self):
        class TickingVisual(fixture.AssistantUiPerception):
            tick = 0

            def runtime_snapshot(self):
                return {**super().runtime_snapshot(), "nextObservationSeconds": self.tick}

        perception = TickingVisual()
        camera = TickingVisual()
        self.chat.perception_controller = perception
        self.chat.camera_controller = camera
        panel = fixture.make_panel(self.chat)
        self.extra_panels.append(panel)
        snapshots = self.capture_snapshots(panel)
        for _ in range(5):
            perception.runtime_changed.emit()
            self.chat.request_status_changed.emit(self.chat.request_status)
            QTest.qWait(25)
        self.assertEqual(snapshots, [])
        self.assertEqual(panel.web_state()["perception"]["paused"], False)
        # Even changing countdowns must not trigger unused chat-page updates.
        for value in range(1, 6):
            perception.tick = value
            camera.tick = value * 2
            perception.runtime_changed.emit()
            camera.runtime_changed.emit()
            QTest.qWait(25)
        self.assertEqual(snapshots, [])
        latest = json.loads(panel.bridge.getState())
        self.assertEqual(latest["perception"]["nextObservationSeconds"], 5)
        self.assertEqual(latest["camera"]["nextObservationSeconds"], 10)
        # Explicit controls retain the existing bridge acknowledgement/update.
        self.assertTrue(bridge_action(panel, "pauseAllVisual")["ok"])
        wait_until(lambda: len(snapshots) == 1)
        self.assertTrue(snapshots[-1]["perception"]["paused"])
        self.assertTrue(snapshots[-1]["camera"]["paused"])
        perception.runtime_changed.emit()
        QTest.qWait(60)
        self.assertEqual(len(snapshots), 1)

    def test_real_message_status_and_repeated_focus_actions_are_published(self):
        snapshots = self.capture_snapshots()
        self.chat.messages[0]["content"] = "更新后的合成消息"
        self.chat.messages_changed.emit()
        wait_until(lambda: len(snapshots) == 1)
        self.assertEqual(snapshots[-1]["messages"][0]["content"], "更新后的合成消息")
        self.chat.busy = True
        self.chat.request_status = "合成状态：正在准备回复"
        self.chat.busy_changed.emit(True)
        self.chat.request_status_changed.emit(self.chat.request_status)
        wait_until(lambda: len(snapshots) == 2)
        self.assertEqual(snapshots[-1]["status"], self.chat.request_status)
        self.panel.focus_message("qa-assistant-1")
        wait_until(lambda: len(snapshots) == 3)
        first_serial = snapshots[-1]["focusRequest"]["serial"]
        self.panel.focus_message("qa-assistant-1")
        wait_until(lambda: len(snapshots) == 4)
        self.assertGreater(snapshots[-1]["focusRequest"]["serial"], first_serial)

    def test_ready_acknowledgement_forces_one_fresh_snapshot(self):
        snapshots = self.capture_snapshots()
        self.panel._publish_web_state()
        self.assertEqual(snapshots, [])
        self.assertTrue(bridge_action(self.panel, "ready")["ok"])
        wait_until(lambda: len(snapshots) == 1)
        self.panel._publish_web_state()
        self.assertEqual(len(snapshots), 1)

    def test_successful_same_state_navigation_still_gets_acknowledged(self):
        snapshots = self.capture_snapshots()
        self.assertTrue(bridge_action(self.panel, "open", id="current")["ok"])
        wait_until(lambda: len(snapshots) == 1)
        self.assertEqual(snapshots[-1]["conversationId"], "current")
        self.panel._publish_web_state()
        self.assertEqual(len(snapshots), 1)

    def test_page_reload_replays_an_identical_state_and_preserves_web_draft(self):
        draft = "reload keeps this unsent draft"
        set_dom_text(self.panel, "#message-input", draft)
        wait_until(lambda: self.panel._web_drafts.get("current") == draft)
        snapshots = self.capture_snapshots()
        self.panel._reload_web()
        wait_until(lambda: self.panel._frontend_ready, timeout=20)
        wait_js(self.panel, "document.querySelector('#message-input').value === " + json.dumps(draft))
        wait_until(lambda: bool(snapshots))
        self.assertEqual(snapshots[-1]["drafts"]["current"], draft)
        before = len(snapshots)
        self.panel._publish_web_state()
        self.assertEqual(len(snapshots), before)


if __name__ == "__main__":
    unittest.main()
