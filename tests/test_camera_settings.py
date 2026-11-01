"""Real QSettings camera preferences isolated in a temporary INI file."""

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from PySide6.QtCore import QSettings

from desktop_pet.perception.settings import CameraSettings, PerceptionSettings
from desktop_pet.settings_manager import CredentialStore, SettingsManager
from tests.test_settings import FakeVault


class CameraSettingsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="aura-camera-settings-")
        self.path = Path(self.directory.name) / "preferences.ini"
        self.vault = FakeVault()
        self.credentials = CredentialStore(backend=self.vault)
        self.manager = self.make_manager()

    def tearDown(self):
        self.directory.cleanup()

    def make_manager(self):
        return SettingsManager(QSettings(str(self.path), QSettings.Format.IniFormat), self.credentials)

    def raw_settings(self):
        return QSettings(str(self.path), QSettings.Format.IniFormat)

    def test_default_camera_is_off_and_reading_defaults_does_not_store_consent(self):
        self.assertEqual(self.manager.load_camera_settings(), CameraSettings())
        self.assertFalse(self.manager.load_camera_settings().enabled)
        self.assertFalse(self.manager.load_camera_settings().allow_remote)
        self.assertEqual(self.manager.load_camera_settings().device_id, "")
        self.assertEqual(Path(self.manager.config_path), self.path)
        raw = self.raw_settings()
        self.assertFalse(raw.contains("camera/enabled"))
        self.assertFalse(raw.contains("camera/allow_remote"))
        self.assertEqual(self.vault.values, {})

    def test_all_camera_preferences_and_binary_device_id_survive_new_qsettings_instance(self):
        expected = CameraSettings(
            enabled=True, allow_remote=True, device_id=b"synthetic-camera\x00\xff-id".hex(),
            observation_interval_seconds=17, analysis_interval_seconds=121, change_threshold=.23,
            speech_cooldown_seconds=230, max_speeches_per_hour=8, commentary_mode="quiet",
            idle_pause_seconds=900, privacy_keywords=("机密", "private"), excluded_apps=("synthetic.exe",),
        )
        self.manager.save_camera_settings(expected)
        self.assertTrue(self.path.is_file())
        self.assertEqual(self.make_manager().load_camera_settings(), expected)
        self.assertEqual(self.raw_settings().value("camera/device_id"), expected.device_id)
        self.assertEqual(self.vault.values, {})

    def test_screen_and_camera_consent_and_preferences_are_independent_both_directions(self):
        screen = PerceptionSettings(enabled=True, allow_remote=True, commentary_mode="quiet",
                                    observation_interval_seconds=23)
        self.manager.save_perception_settings(screen)
        self.assertEqual(self.make_manager().load_camera_settings(), CameraSettings())
        camera = CameraSettings(enabled=True, allow_remote=False, device_id="73696d756c61746564",
                                observation_interval_seconds=7)
        self.manager.save_camera_settings(camera)
        reloaded = self.make_manager()
        self.assertEqual(reloaded.load_perception_settings(), screen)
        self.assertEqual(reloaded.load_camera_settings(), camera)
        screen_off = replace(screen, enabled=False, allow_remote=False)
        reloaded.save_perception_settings(screen_off)
        self.assertEqual(self.make_manager().load_camera_settings(), camera)
        reloaded.save_camera_settings(replace(camera, enabled=False))
        self.assertEqual(self.make_manager().load_perception_settings(), screen_off)

    def test_malformed_consent_fails_closed_without_changing_screen_preferences(self):
        screen = PerceptionSettings(enabled=True, allow_remote=True)
        self.manager.save_perception_settings(screen)
        for key in ("enabled", "allow_remote"):
            for invalid in (1, 0, None, [], {}, "yes", "invalid-but-truthy", " true "):
                with self.subTest(key=key, invalid=repr(invalid)):
                    self.manager.save_camera_settings(CameraSettings(enabled=True, allow_remote=True, device_id="aabbcc"))
                    raw = self.raw_settings()
                    raw.setValue("camera/" + key, invalid)
                    raw.sync()
                    restarted = self.make_manager()
                    self.assertEqual(restarted.load_camera_settings(), CameraSettings())
                    self.assertEqual(restarted.load_perception_settings(), screen)

    def test_qsettings_boolean_strings_accept_only_explicit_true_or_false(self):
        self.manager.save_camera_settings(CameraSettings(device_id="aabbcc"))
        raw = self.raw_settings()
        raw.setValue("camera/enabled", "TRUE")
        raw.setValue("camera/allow_remote", "False")
        raw.sync()
        loaded = self.make_manager().load_camera_settings()
        self.assertTrue(loaded.enabled)
        self.assertFalse(loaded.allow_remote)
        self.assertEqual(loaded.device_id, "aabbcc")

    def test_damaged_device_or_parameter_does_not_retain_previous_remote_consent(self):
        for key, invalid in (("device_id", "invalid\ndevice"), ("device_id", "x" * 4097),
                             ("observation_interval_seconds", 999), ("change_threshold", float("nan")),
                             ("privacy_keywords", ["", "private"])):
            with self.subTest(key=key):
                self.manager.save_camera_settings(CameraSettings(enabled=True, allow_remote=True))
                raw = self.raw_settings()
                raw.setValue("camera/" + key, invalid)
                raw.sync()
                self.assertEqual(self.make_manager().load_camera_settings(), CameraSettings())

    def test_invalid_save_is_rejected_before_overwriting_good_preferences(self):
        expected = CameraSettings(enabled=True, allow_remote=False, device_id="aabbcc")
        self.manager.save_camera_settings(expected)
        for invalid in (replace(expected, enabled=1), replace(expected, device_id="bad\ndevice"),
                        replace(expected, analysis_interval_seconds=1)):
            with self.subTest(invalid=repr(invalid)), self.assertRaises(ValueError):
                self.manager.save_camera_settings(invalid)
            self.assertEqual(self.make_manager().load_camera_settings(), expected)


if __name__ == "__main__":
    unittest.main()
