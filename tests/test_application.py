"""Real Qt session + real SDK/HTTP tests, using only an isolated local server.

Credentials live in this module's memory backend, never the user's Windows vault.
These integration tests send Qt events; they do not move the system mouse.
"""

import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import QEvent, QSettings, QThreadPool, QTimer, Qt
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QMessageBox

from desktop_pet.application_controller import ApplicationController
from desktop_pet.config import PetConfig
from desktop_pet.settings_manager import ApiSettings, CredentialStore, SettingsManager
from tests.mock_api import MockApiServer


LOCAL_TEST_KEY = "local-only-session-token"


class MemoryCredentialBackend:
    """Implement the credential protocol without accessing OS credentials."""

    def __init__(self):
        self.values = {}

    def get_password(self, service, account):
        return self.values.get((service, account))

    def set_password(self, service, account, value):
        self.values[(service, account)] = value

    def delete_password(self, service, account):
        self.values.pop((service, account), None)


def isolated_settings(path: Path, backend: MemoryCredentialBackend) -> SettingsManager:
    return SettingsManager(
        settings=QSettings(str(path), QSettings.Format.IniFormat),
        credential_store=CredentialStore(backend=backend),
    )


def spin_until(condition, timeout: float = 6.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        QTest.qWait(5)
        # QTest.qWait may retain the GIL on Windows; yield it explicitly so
        # Python HTTP workers receive the time they get under app.exec().
        time.sleep(0.002)
    if not condition():
        raise AssertionError("Qt/HTTP operation exceeded the local integration-test deadline")


def configure_through_panel(session: ApplicationController, base_url: str) -> None:
    session.open_debug_panel()
    panel = session.debug_panel
    panel.base_url_input.setText(base_url)
    panel.model_input.setText("test-model")
    panel.key_input.setText(LOCAL_TEST_KEY)
    panel.save_api_button.click()
    if not panel.api_error_label.isHidden():
        raise AssertionError("The isolated test configuration was not saved")


class ApplicationIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aura-session-tests-")
        self.path = Path(self.temp.name) / "settings.ini"
        self.backend = MemoryCredentialBackend()
        self.settings = isolated_settings(self.path, self.backend)
        self.session = ApplicationController(PetConfig(), settings=self.settings)
        # Chat mocks test logical emotions, not the installed 240 MiB video.
        self.session.pet_controller.asset_player.directory = Path(self.temp.name) / "empty-animations"
        self.session.show()
        self.app.processEvents()

    def tearDown(self):
        self.session.shutdown()
        spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
        for window in (self.session.pet, self.session.debug_panel, self.session.chat_window):
            if window is not None:
                window.deleteLater()
        self.session.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.temp.cleanup()

    def test_startup_transparency_topmost_and_embedded_settings_entry(self):
        pet = self.session.pet
        self.assertTrue(pet.isVisible())
        self.assertTrue(pet.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground))
        self.assertTrue(pet.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
        self.session.pet.debug_requested.emit()
        window = self.session.chat_window
        self.assertTrue(window.isVisible())
        self.assertIsNone(self.session.debug_panel)
        self.assertIs(window.pages.currentWidget(), window.settings_page)
        self.assertEqual(window.settings_page._section_key, "general")
        self.assertFalse(window.settings_page.isWindow())
        self.session.pet.chat_requested.emit()
        self.assertIs(self.session.chat_window, window)
        self.assertIs(window.pages.currentWidget(), window.chat_panel)

        # Explicit diagnostic callers retain the legacy panel, while ordinary
        # settings and chat entries reuse the single main application window.
        self.session.open_debug_panel()
        self.assertTrue(self.session.debug_panel.isVisible())
        self.assertTrue(window.isVisible())
        for utility_window in (self.session.debug_panel, window):
            self.assertFalse(utility_window.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
            self.assertIsNone(utility_window.parentWidget())

    def test_panel_reopen_preserves_instance_scale_and_expression(self):
        self.session.open_debug_panel()
        panel = self.session.debug_panel
        panel.scale_slider.setValue(150)
        panel.expression_combo.setCurrentIndex(panel.expression_combo.findData("sleepy"))
        self.assertEqual(self.session.pet_controller.scale_percent, 150)
        self.assertEqual(self.session.pet_controller.expression, "sleepy")
        expression_status = panel.expression_status.text()
        self.assertEqual(expression_status, self.session.pet_controller.expression_status)
        panel.close()
        self.assertFalse(panel.isVisible())
        self.assertTrue(self.session.pet.isVisible())
        self.session.open_debug_panel()
        self.assertIs(self.session.debug_panel, panel)
        self.assertEqual(panel.scale_slider.value(), 150)
        self.assertEqual(panel.expression_combo.currentData(), "sleepy")
        self.assertEqual(panel.expression_status.text(), expression_status)

    def test_real_local_connection_chat_emotion_and_shared_views(self):
        with MockApiServer() as server:
            configure_through_panel(self.session, server.base_url)
            panel = self.session.debug_panel
            server.enqueue("local connectivity response")
            panel.test_connection_button.click()
            self.assertTrue(self.session.chat.busy)
            spin_until(lambda: not self.session.chat.busy)
            self.assertIn("连接成功", panel.connection_label.text())
            self.assertEqual(server.requests[0]["path"], "/v1/chat/completions")
            server.enqueue(json.dumps({"reply": "本地 HTTP 对话成功。", "emotion": "happy"}, ensure_ascii=False))
            self.session.open_chat_window()
            chat = self.session.chat_window.chat_panel
            chat._activate_native()
            chat.input.setPlainText("你好，鲸鱼娘")
            QTest.keyClick(chat.input, Qt.Key.Key_Return)
            self.assertTrue(self.session.chat.busy)
            self.assertEqual(self.session.pet_controller.expression, "thinking")
            spin_until(lambda: not self.session.chat.busy)
            self.assertEqual(self.session.pet_controller.expression, "happy")
            self.assertEqual(len(self.session.chat.messages), 2)
            for view in (panel.chat_panel, chat):
                view._activate_native()
                self.assertIn("本地 HTTP 对话成功。", view.transcript.toPlainText())
            self.session.chat_window.close()
            self.session.open_chat_window()
            self.assertIn("你好，鲸鱼娘", self.session.chat_window.chat_panel.transcript.toPlainText())
            self.assertEqual(server.requests[-1]["body"]["model"], "test-model")

    def test_slow_http_keeps_timers_scaling_and_expression_responsive(self):
        with MockApiServer() as server:
            configure_through_panel(self.session, server.base_url)
            server.enqueue('{"reply":"慢请求完成","emotion":"surprised"}', delay=0.65)
            ticks = []
            timer = QTimer()
            timer.setInterval(15)
            timer.timeout.connect(lambda: ticks.append(time.monotonic()))
            timer.start()
            frames = QSignalSpy(self.session.pet.frame_tick)
            try:
                chat = self.session.debug_panel.chat_panel
                chat.input.setPlainText("等待时仍能调节")
                chat.send_button.click()
                spin_until(server.received.is_set)
                self.assertTrue(self.session.chat.busy)
                self.session.debug_panel.scale_slider.setValue(175)
                self.session.pet_controller.set_expression("angry")
                self.assertEqual(self.session.pet_controller.scale_percent, 175)
                self.assertEqual(self.settings.load_scale(), 175)
                self.assertEqual(self.session.pet_controller.expression, "angry")
                rect = self.session.pet.animated_sprite_rect
                self.assertTrue(self.session.pet.rect().toRectF().contains(rect))
                spin_until(lambda: not self.session.chat.busy)
                self.assertGreater(len(ticks), 12)
                self.assertGreater(frames.count(), 6)
                self.assertEqual(self.session.pet_controller.expression, "surprised")
            finally:
                timer.stop()

    def test_nonsecret_settings_reload_and_key_stays_out_of_ini(self):
        self.session.open_debug_panel()
        panel = self.session.debug_panel
        panel.base_url_input.setText("https://example.test/v1")
        panel.model_input.setText("restored-model")
        panel.key_input.setText(LOCAL_TEST_KEY)
        panel.save_api_button.click()
        panel.scale_slider.setValue(125)
        reloaded = isolated_settings(self.path, self.backend)
        self.assertEqual(reloaded.load_scale(), 125)
        self.assertEqual(reloaded.load_api_settings().model, "restored-model")
        self.assertEqual(reloaded.get_api_key(), LOCAL_TEST_KEY)
        self.assertNotIn(LOCAL_TEST_KEY, self.path.read_text(encoding="utf-8"))
        self.assertEqual(panel.key_input.text(), "")
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            panel.clear_key_button.click()
        self.assertEqual(reloaded.get_api_key(), "")

    def test_unconfigured_api_reports_error_and_pet_stays_usable(self):
        self.session.open_debug_panel()
        panel = self.session.debug_panel
        panel.chat_panel.input.setPlainText("尚未配置")
        panel.chat_panel.send_button.click()
        self.assertFalse(self.session.chat.busy)
        self.assertTrue(self.session.chat.error)
        self.assertFalse(panel.chat_panel.error_label.isHidden())
        self.assertEqual(self.session.chat.messages, [])
        panel.scale_slider.setValue(75)
        panel.expression_combo.setCurrentIndex(panel.expression_combo.findData("happy"))
        self.assertEqual(self.session.pet_controller.scale_percent, 75)
        self.assertEqual(self.session.pet_controller.expression, "happy")
        self.assertTrue(self.session.pet.isVisible())

    def test_api_save_rejects_new_key_in_plaintext_fields_before_any_write(self):
        self.session.open_debug_panel()
        panel = self.session.debug_panel
        panel.model_input.setText(LOCAL_TEST_KEY)
        panel.key_input.setText(LOCAL_TEST_KEY)
        panel.save_api_button.click()
        self.assertIn("不能包含 API Key", panel.api_error_label.text())
        self.assertNotIn(LOCAL_TEST_KEY, panel.api_error_label.text())
        self.assertEqual(self.settings.get_api_key(), "")
        self.assertEqual(self.settings.load_api_settings().model, "")
        if self.path.exists():
            self.assertNotIn(LOCAL_TEST_KEY, self.path.read_text(encoding="utf-8"))

    def test_actual_http_authentication_failure_is_displayed_without_secret(self):
        with MockApiServer() as server:
            configure_through_panel(self.session, server.base_url)
            server.enqueue("provider echo " + LOCAL_TEST_KEY, status=401)
            self.session.debug_panel.test_connection_button.click()
            spin_until(lambda: not self.session.chat.busy)
            text = self.session.debug_panel.connection_label.text()
            self.assertIn("认证失败", text)
            self.assertNotIn(LOCAL_TEST_KEY, text)
            self.assertTrue(self.session.pet.isVisible())
            self.assertFalse(self.session.debug_panel.chat_panel.error_label.isHidden())

    def test_shutdown_cancels_inflight_http_and_ignores_late_reply(self):
        with MockApiServer() as server:
            configure_through_panel(self.session, server.base_url)
            server.enqueue('{"reply":"关闭后不得显示","emotion":"happy"}', delay=1.5)
            self.assertTrue(self.session.chat.send("关闭时正在请求"))
            spin_until(server.received.is_set)
            before = list(self.session.chat.messages)
            finished = QSignalSpy(self.session.finished)
            started = time.monotonic()
            self.session.shutdown()
            self.assertLess(time.monotonic() - started, 0.3)
            self.assertEqual(finished.count(), 1)
            self.session.shutdown()
            self.assertEqual(finished.count(), 1)
            self.assertFalse(self.session.pet.isVisible())
            self.assertFalse(self.session.debug_panel.isVisible())
            spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0, timeout=2)
            self.assertEqual(self.session.chat.messages, before)
            self.assertFalse(self.session.chat.send("关闭后不得继续请求"))


if __name__ == "__main__":
    unittest.main()
