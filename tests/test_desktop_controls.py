"""Tray commands and runtime privacy use isolated state, never physical input."""

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtCore import QEvent, QThreadPool
from PySide6.QtWidgets import QApplication, QSystemTrayIcon
from PySide6.QtTest import QSignalSpy

from desktop_pet.application_controller import ApplicationController
from desktop_pet.config import PetConfig
from tests.test_application import MemoryCredentialBackend, isolated_settings, spin_until
from tests.test_perception_integration import SyntheticScene


class DesktopControlsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="aura-desktop-controls-")
        self.settings = isolated_settings(Path(self.directory.name) / "settings.ini", MemoryCredentialBackend())
        with patch.object(QSystemTrayIcon, "isSystemTrayAvailable", return_value=False):
            self.session = ApplicationController(PetConfig(), settings=self.settings)
        self.session.pet_controller.asset_player.directory = Path(self.directory.name) / "empty-animations"
        self.session.show()

    def tearDown(self):
        self.session.shutdown()
        spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
        for widget in (self.session.debug_panel, self.session.chat_window, self.session.pet):
            if widget is not None:
                widget.deleteLater()
        self.session.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.directory.cleanup()

    def test_hide_has_recovery_and_keeps_current_conversation(self):
        original_id = self.session.chat.current_conversation_id
        self.session.toggle_pet_visibility()
        self.assertTrue(self.session.pet.isVisible(), "No hiding without a recovery tray")
        self.session.tray.available = True
        self.session.tray.visibility_action.trigger()
        self.assertFalse(self.session.pet.isVisible())
        self.assertEqual(self.session.tray.visibility_action.text(), "显示桌宠")
        self.session.tray.visibility_action.trigger()
        self.assertTrue(self.session.pet.isVisible())
        self.assertEqual(self.session.chat.current_conversation_id, original_id)

    def test_tray_opens_chat_and_reuses_it_after_close(self):
        self.session.tray.chat_action.trigger()
        first = self.session.chat_window
        self.assertTrue(first.isVisible())
        first.close()
        self.session.tray._activated(QSystemTrayIcon.ActivationReason.DoubleClick)
        self.assertIs(self.session.chat_window, first)
        self.assertTrue(first.isVisible())

    def test_runtime_pause_blocks_capture_clears_preview_and_preserves_consent(self):
        perception = self.session.perception
        scene = SyntheticScene()
        perception.observer = scene.observer()
        preferences = replace(perception.settings, enabled=True, allow_remote=False)
        self.assertTrue(perception.update_settings(preferences))
        self.assertTrue(perception.observe_now())
        self.assertFalse(perception.preview.isNull())
        calls = scene.capture_calls
        self.session.tray.pause_action.trigger()
        self.assertTrue(perception.paused)
        self.assertFalse(perception._timer.isActive())
        self.assertTrue(perception.preview.isNull())
        self.assertFalse(perception.observe_now())
        self.assertFalse(perception.poll())
        self.assertFalse(perception.begin_local_observation())
        self.assertEqual(scene.capture_calls, calls)
        self.assertEqual(self.settings.load_perception_settings(), preferences)
        self.session.tray.pause_action.trigger()
        self.assertFalse(perception.paused)
        self.assertTrue(perception._timer.isActive())
        self.assertTrue(perception.observe_now())

    def test_memory_entry_reuses_panel_and_closing_keeps_pet(self):
        spin_until(lambda: not self.session.memory.busy)
        self.session.tray.memory_action.trigger()
        first = self.session.memory_panel
        self.assertTrue(first.isVisible())
        first.close()
        self.assertTrue(self.session.pet.isVisible())
        self.session.tray.memory_action.trigger()
        self.assertIs(self.session.memory_panel, first)
        self.assertTrue(first.isVisible())

    def test_shutdown_hides_tray_and_is_idempotent(self):
        self.session.shutdown()

    def test_failed_service_shutdown_still_closes_other_components_and_finishes_once(self):
        original = self.session.perception.shutdown
        def failing_service():
            original()
            raise RuntimeError("sensitive-provider-payload-must-not-escape")
        finished = QSignalSpy(self.session.finished)
        with patch.object(self.session.perception, "shutdown", side_effect=failing_service):
            self.session.shutdown()
        self.assertTrue(self.session.chat._closed)
        self.assertTrue(self.session.memory._closed)
        self.assertFalse(self.session.pet.isVisible())
        self.assertEqual(self.session.shutdown_errors, ["perception"])
        self.assertEqual(finished.count(), 1)
        self.session.shutdown()
        self.assertEqual(finished.count(), 1)
        self.assertFalse(self.session.tray.icon.isVisible())
        self.session.tray.pause_action.trigger()
        self.assertFalse(self.session.perception.paused)
        self.session.shutdown()


if __name__ == "__main__":
    unittest.main()
