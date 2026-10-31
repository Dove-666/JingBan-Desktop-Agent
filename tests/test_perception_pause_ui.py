"""A tray privacy pause remains visible and recoverable from the control panel."""

from dataclasses import replace
import unittest

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QApplication

from desktop_pet.ui.perception_panel import PerceptionPanel
from tests.test_perception_ui import FakePerceptionController


class PausableController(FakePerceptionController):
    paused_changed = Signal(bool)
    def __init__(self):
        super().__init__()
        self.paused = False

    def set_paused(self, paused):
        self.paused = bool(paused)
        self.cancel_local_observation()
        self.paused_changed.emit(self.paused)


class PerceptionPauseUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_pause_cancels_countdown_and_panel_can_restore_without_changing_consent(self):
        controller = PausableController()
        controller.settings = replace(controller.settings, enabled=True)
        panel = PerceptionPanel(controller)
        try:
            panel.observe_button.click()
            self.assertTrue(panel._observation_timer.isActive())
            controller.set_paused(True)
            self.assertFalse(panel._observation_timer.isActive())
            self.assertFalse(panel.pause_notice.isHidden())
            self.assertFalse(panel.observe_button.isEnabled())
            self.assertIn("恢复", panel.observe_button.toolTip())
            panel.resume_button.click()
            self.assertFalse(controller.paused)
            self.assertTrue(panel.pause_notice.isHidden())
            self.assertTrue(panel.observe_button.isEnabled())
            self.assertFalse(controller.settings.allow_remote)
            self.assertEqual(controller.saved_settings, [])
            self.assertEqual(controller.observe_count, 0)
        finally:
            panel.deleteLater()

    def test_restoring_runtime_pause_does_not_enable_disabled_observation(self):
        controller = PausableController()
        controller.paused = True
        panel = PerceptionPanel(controller)
        try:
            self.assertFalse(panel.pause_notice.isHidden())
            panel.resume_button.click()
            self.assertFalse(controller.settings.enabled)
            self.assertFalse(panel.observe_button.isEnabled())
            self.assertEqual(controller.saved_settings, [])
        finally:
            panel.deleteLater()
