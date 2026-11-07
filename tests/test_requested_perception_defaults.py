"""Requested defaults through real isolated preferences and device-free Qt forms."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from PySide6.QtCore import QEvent, QSettings, Qt
from PySide6.QtWidgets import QApplication

from desktop_pet.perception.settings import CameraSettings, PerceptionSettings
from desktop_pet.perception.proactive_agent import ProactiveAgent
from desktop_pet.settings_manager import CredentialStore, SettingsManager
from desktop_pet.ui.agent_panel import AgentSettingsPanel
from desktop_pet.ui.camera_panel import CameraPanel
from desktop_pet.ui.perception_panel import PerceptionPanel
from tests.test_camera_ui import FakeCameraController
from tests.test_perception_ui import FakePerceptionController
from tests.test_perception_core import event
from tests.test_settings import FakeVault


class RequestedPerceptionDefaultsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="jingban-requested-defaults-")
        self.path = Path(self.directory.name) / "settings.ini"
        self.vault = FakeVault()
        self.credentials = CredentialStore(backend=self.vault)
        self.manager = self.make_manager()
        self.panels = []

    def make_manager(self):
        return SettingsManager(QSettings(str(self.path), QSettings.Format.IniFormat), self.credentials)

    def tearDown(self):
        for panel in self.panels:
            panel.hide()
            panel.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.directory.cleanup()

    def test_new_profile_loads_requested_screen_values_without_writing_settings(self):
        actual = self.manager.load_perception_settings()
        self.assertEqual((actual.enabled, actual.allow_remote, actual.commentary_mode,
                          actual.observation_interval_seconds, actual.analysis_interval_seconds,
                          actual.change_threshold, actual.speech_cooldown_seconds,
                          actual.max_speeches_per_hour, actual.idle_pause_seconds),
                         (True, True, "companion", 10, 30, .08, 30, 30, 585))
        self.assertEqual(self.manager.load_scale(), 100)
        self.assertEqual(self.manager.load_theme(), "light")
        agent = self.manager.load_agent_settings()
        self.assertEqual((agent.enabled, agent.tools_enabled, agent.computer_enabled), (True, True, True))
        self.assertEqual(self.manager._settings.allKeys(), [])
        self.assertFalse(self.path.exists())
        self.assertEqual(self.vault.values, {})

    def test_saved_theme_size_and_agent_permissions_are_not_overwritten_by_new_defaults(self):
        self.manager.save_theme("dark")
        self.manager.save_scale(150)
        expected = replace(self.manager.load_agent_settings(), enabled=False, tools_enabled=False,
                           computer_enabled=False)
        self.manager.save_agent_settings(expected)
        before = self.path.read_bytes()
        reloaded = self.make_manager()
        self.assertEqual(reloaded.load_theme(), "dark")
        self.assertEqual(reloaded.load_scale(), 150)
        self.assertEqual(reloaded.load_agent_settings(), expected)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.vault.values, {})

    def test_partial_agent_preferences_preserve_explicit_off_and_show_other_defaults_on(self):
        raw = self.manager._settings
        raw.setValue("agents/computer_enabled", False)
        raw.sync()
        before = self.path.read_bytes()
        panel = AgentSettingsPanel(self.make_manager(), embedded=True)
        panel.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        self.panels.append(panel)
        panel.show()
        self.app.processEvents()
        self.assertTrue(panel.enabled_checkbox.isChecked())
        self.assertTrue(panel.tools_checkbox.isChecked())
        self.assertFalse(panel.computer_checkbox.isChecked())
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(raw.contains("agents/tools_enabled"))
        self.assertEqual(self.vault.values, {})

    def test_fresh_agent_form_loads_all_three_permissions_without_saving(self):
        panel = AgentSettingsPanel(self.manager, embedded=True)
        panel.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        self.panels.append(panel)
        panel.show()
        self.app.processEvents()
        self.assertTrue(all(box.isChecked() for box in
                            (panel.enabled_checkbox, panel.tools_checkbox, panel.computer_checkbox)))
        self.assertFalse(panel._dirty)
        self.assertEqual(self.manager._settings.allKeys(), [])
        self.assertFalse(self.path.exists())
        self.assertEqual(self.vault.values, {})

    def test_camera_retains_its_independent_previous_defaults(self):
        actual = self.manager.load_camera_settings()
        self.assertEqual((actual.enabled, actual.allow_remote, actual.commentary_mode,
                          actual.observation_interval_seconds, actual.analysis_interval_seconds,
                          actual.change_threshold, actual.speech_cooldown_seconds,
                          actual.max_speeches_per_hour, actual.idle_pause_seconds, actual.device_id),
                         (False, False, "companion", 10, 60, .08, 180, 6, 600, ""))
        self.manager.save_perception_settings(PerceptionSettings())
        self.assertEqual(self.make_manager().load_camera_settings(), actual)

    def test_saved_custom_values_and_scale_survive_reload_byte_for_byte(self):
        expected = PerceptionSettings(enabled=False, allow_remote=False, commentary_mode="quiet",
            observation_interval_seconds=17, analysis_interval_seconds=173, change_threshold=.13,
            speech_cooldown_seconds=410, max_speeches_per_hour=8, idle_pause_seconds=1080,
            privacy_keywords=("private", "秘密"), excluded_apps=("custom.exe",))
        self.manager.save_perception_settings(expected)
        self.manager.save_scale(150)
        before = self.path.read_bytes()
        reloaded = self.make_manager()
        self.assertEqual(reloaded.load_perception_settings(), expected)
        self.assertEqual(reloaded.load_scale(), 150)
        self.assertEqual(self.path.read_bytes(), before)

    def test_partial_profile_keeps_explicit_values_and_uses_new_values_only_for_missing_keys(self):
        raw = self.manager._settings
        raw.setValue("perception/enabled", False)
        raw.setValue("perception/allow_remote", False)
        raw.setValue("perception/analysis_interval_seconds", 120)
        raw.sync()
        before = self.path.read_bytes()
        self.assertEqual(self.make_manager().load_perception_settings(),
            replace(PerceptionSettings(), enabled=False, allow_remote=False, analysis_interval_seconds=120))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(raw.contains("perception/speech_cooldown_seconds"))

    def test_damaged_preferences_fail_closed_without_mutating_the_file(self):
        for key, value in (("enabled", "invalid"), ("allow_remote", 1),
                           ("change_threshold", float("nan")), ("observation_interval_seconds", 999)):
            with self.subTest(key=key):
                self.manager.save_perception_settings(PerceptionSettings())
                self.manager._settings.setValue("perception/" + key, value)
                self.manager._settings.sync()
                before = self.path.read_bytes()
                actual = self.make_manager().load_perception_settings()
                self.assertFalse(actual.enabled)
                self.assertFalse(actual.allow_remote)
                self.assertEqual(self.path.read_bytes(), before)

    def test_actual_screen_form_loads_and_saves_defaults_without_capturing(self):
        controller = FakePerceptionController()
        controller.settings = self.manager.load_perception_settings()
        panel = PerceptionPanel(controller, embedded=True)
        panel.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        self.panels.append(panel)
        panel.show()
        self.app.processEvents()
        self.assertTrue(panel.enabled_checkbox.isChecked())
        self.assertTrue(panel.remote_checkbox.isChecked())
        self.assertEqual(panel.commentary_mode_combo.currentData(), "companion")
        self.assertEqual((panel.observation_interval_input.value(), panel.analysis_interval_input.value(),
            panel.change_threshold_input.value(), panel.speech_cooldown_input.value(),
            panel.max_speeches_input.value(), panel.idle_pause_input.value()), (10, 30, .08, 30, 30, 585))
        self.assertEqual(controller.saved_settings, [])
        panel.save_button.click()
        self.assertEqual(controller.saved_settings, [PerceptionSettings()])
        self.assertEqual((controller.observe_count, controller.comment_count), (0, 0))

    def test_actual_camera_form_remains_off_and_never_starts_a_device(self):
        controller = FakeCameraController()
        controller.settings = self.manager.load_camera_settings()
        panel = CameraPanel(controller, embedded=True)
        panel.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        self.panels.append(panel)
        panel.show()
        self.app.processEvents()
        self.assertFalse(panel.enabled_button.isChecked())
        self.assertFalse(panel.remote_checkbox.isChecked())
        self.assertEqual(controller.enable_calls, [])
        self.assertEqual(controller.remote_calls, [])
        self.assertEqual(controller.saved_settings, [])

    def test_loaded_defaults_drive_actual_cooldown_hourly_limit_and_idle_policy(self):
        settings = self.manager.load_perception_settings()
        agent = ProactiveAgent()
        first = agent.decide(event(), 0, settings)
        self.assertEqual(first.action, "speak")
        agent.record(first, 0)
        self.assertEqual(agent.decide(event(), 29, settings).reason, "cooldown")
        for index in range(1, 30):
            now = index * 30
            decision = agent.decide(event(), now, settings)
            self.assertEqual(decision.action, "speak")
            agent.record(decision, now)
        self.assertEqual(agent.decide(event(), 900, settings).reason, "hourly_limit")
        agent.reset()
        self.assertEqual(agent.decide(event(), 0, settings, idle_seconds=585).reason, "user_idle")
        self.assertEqual(agent.decide(event(), 0, settings, idle_seconds=584).action, "speak")


if __name__ == "__main__":
    unittest.main()
