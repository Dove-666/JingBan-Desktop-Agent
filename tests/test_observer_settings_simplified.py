"""Settings disclosures retain observer state without activating any device."""

from dataclasses import replace
import unittest

from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication

from desktop_pet.ui.camera_panel import CameraPanel
from desktop_pet.ui.perception_panel import PerceptionPanel
from tests.test_camera_ui import FakeCameraController
from tests.test_perception_ui import FakePerceptionController


class SimplifiedObserverSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.panels = []

    def tearDown(self):
        for panel in self.panels:
            panel.hide()
            panel.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def make_panel(self, kind):
        controller = FakeCameraController() if kind == "camera" else FakePerceptionController()
        controller.settings = replace(controller.settings, analysis_interval_seconds=167,
                                      speech_cooldown_seconds=259, max_speeches_per_hour=11)
        panel_type = CameraPanel if kind == "camera" else PerceptionPanel
        panel = panel_type(controller, embedded=True)
        panel.resize(620, 760)
        panel.show()
        self.panels.append(panel)
        self.app.processEvents()
        return panel, controller

    def test_open_and_disclose_do_not_capture_or_save_or_change_permissions(self):
        for kind in ("camera", "screen"):
            with self.subTest(kind=kind):
                panel, controller = self.make_panel(kind)
                original = controller.settings
                self.assertFalse(panel.frequency_section.isExpanded())
                self.assertFalse(panel.diagnostics_section.isExpanded())
                self.assertFalse(panel.remote_checkbox.isHidden())
                self.assertFalse(panel.preview_label.isHidden())
                self.assertFalse(panel.save_button.isHidden())
                self.assertFalse(panel.commentary_mode_combo.isHidden())
                for section in (panel.frequency_section, panel.diagnostics_section):
                    section.setExpanded(True)
                    section.setExpanded(False)
                self.assertEqual(controller.settings, original)
                self.assertEqual(controller.saved_settings, [])
                self.assertEqual(controller.observe_count, 0)
                self.assertEqual(controller.comment_count, 0)
                if kind == "camera":
                    self.assertEqual(controller.enable_calls, [])
                    self.assertEqual(controller.remote_calls, [])

    def test_saving_style_preserves_hidden_frequency_values(self):
        for kind in ("camera", "screen"):
            with self.subTest(kind=kind):
                panel, controller = self.make_panel(kind)
                panel.commentary_mode_combo.setCurrentIndex(panel.commentary_mode_combo.findData("quiet"))
                panel.save_button.click()
                saved = controller.settings
                self.assertEqual(saved.commentary_mode, "quiet")
                self.assertEqual(saved.analysis_interval_seconds, 167)
                self.assertEqual(saved.speech_cooldown_seconds, 259)
                self.assertEqual(saved.max_speeches_per_hour, 11)
                self.assertFalse(saved.enabled)
                self.assertFalse(saved.allow_remote)

    def test_hidden_diagnostics_do_not_hide_real_errors(self):
        panel, controller = self.make_panel("camera")
        controller.runtime_changed.emit({"enabled": True, "analysisError": "连接失败"})
        self.assertFalse(panel.diagnostics_section.isExpanded())
        self.assertFalse(panel.runtime_error_label.isHidden())
        self.assertIn("连接失败", panel.runtime_error_label.text())
        panel, _ = self.make_panel("screen")
        panel._runtime_changed({"analysisError": "连接失败"})
        self.assertFalse(panel.diagnostics_section.isExpanded())
        self.assertFalse(panel.runtime_error_label.isHidden())
        self.assertIn("连接失败", panel.runtime_error_label.text())

    def test_screen_privacy_disclosure_retains_unsaved_exclusions(self):
        panel, controller = self.make_panel("screen")
        self.assertFalse(panel.privacy_section.isExpanded())
        panel.privacy_section.setExpanded(True)
        panel.excluded_apps_input.setPlainText("private-app.exe")
        panel.privacy_section.setExpanded(False)
        self.assertEqual(controller.saved_settings, [])
        panel.save_button.click()
        self.assertEqual(controller.settings.excluded_apps, ("private-app.exe",))


if __name__ == "__main__":
    unittest.main()
