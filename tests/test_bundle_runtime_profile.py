"""Frozen startup and explicit profiles never use the developer's real data/vault."""
from contextlib import redirect_stderr
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtCore import QSettings

from desktop_pet.app import parse_config
from desktop_pet.runtime_paths import PACKAGED_ENVIRONMENT, PROFILE_ENVIRONMENT, launch_profile_environment, resource_root
from desktop_pet.settings_manager import ApiSettings, CredentialStore, SettingsError, SettingsManager
from desktop_pet.startup_manager import StartupManager
from tests.test_settings import FakeVault
from tests.test_startup import RegistryDouble


class BundleResourceTests(unittest.TestCase):
    def test_frozen_resources_follow_the_relocated_bundle(self):
        with tempfile.TemporaryDirectory(prefix="jingban relocated bundle ") as folder:
            with patch.object(sys, "frozen", True, create=True), patch.object(sys, "_MEIPASS", folder, create=True):
                self.assertEqual(resource_root(), Path(folder).resolve())

    def test_source_resources_ignore_a_stray_bundle_environment(self):
        with patch.object(sys, "frozen", False, create=True), patch.object(sys, "_MEIPASS", "missing-old-build", create=True):
            self.assertEqual(resource_root(), Path(__file__).resolve().parents[1])

    def test_profile_environment_is_child_visible_and_restored_after_failure(self):
        with tempfile.TemporaryDirectory(prefix="jingban profile ") as folder:
            with patch.dict(os.environ, {PROFILE_ENVIRONMENT: "previous-parent-profile"}):
                with self.assertRaises(RuntimeError):
                    with launch_profile_environment(Path(folder)):
                        self.assertEqual(os.environ[PROFILE_ENVIRONMENT], str(Path(folder).resolve()))
                        raise RuntimeError("synthetic failure")
                self.assertEqual(os.environ[PROFILE_ENVIRONMENT], "previous-parent-profile")

    def test_ordinary_launch_does_not_inherit_an_accidental_profile(self):
        with patch.dict(os.environ, {PROFILE_ENVIRONMENT: "previous-parent-profile"}):
            with launch_profile_environment(None):
                self.assertNotIn(PROFILE_ENVIRONMENT, os.environ)
            self.assertEqual(os.environ[PROFILE_ENVIRONMENT], "previous-parent-profile")

    def test_profile_environment_does_not_leave_a_new_global_value(self):
        with patch.dict(os.environ, {}, clear=True):
            with launch_profile_environment(Path(".")):
                self.assertIn(PROFILE_ENVIRONMENT, os.environ)
            self.assertNotIn(PROFILE_ENVIRONMENT, os.environ)

    def test_cli_profile_is_absolute_and_shutdown_wait_is_validated(self):
        config = parse_config(["--profile-dir", "qa profile", "--qa-exit-after-ms", "500"])
        self.assertEqual(config.profile_dir, Path("qa profile").resolve())
        self.assertEqual(config.qa_exit_after_ms, 500)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parse_config(["--qa-exit-after-ms", "1"])

    def test_frozen_launch_marks_child_workers_packaged_and_removes_the_new_flag(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(sys, "frozen", True, create=True):
            with launch_profile_environment(None):
                self.assertEqual(os.environ[PACKAGED_ENVIRONMENT], "1")
            self.assertNotIn(PACKAGED_ENVIRONMENT, os.environ)

    def test_frozen_launch_restores_the_previous_packaged_flag_after_failure(self):
        with patch.dict(os.environ, {PACKAGED_ENVIRONMENT: "previous-parent-value"}), \
                patch.object(sys, "frozen", True, create=True):
            with self.assertRaises(RuntimeError):
                with launch_profile_environment(None):
                    self.assertEqual(os.environ[PACKAGED_ENVIRONMENT], "1")
                    raise RuntimeError("synthetic launch failure")
            self.assertEqual(os.environ[PACKAGED_ENVIRONMENT], "previous-parent-value")


class BundleProfileSettingsTests(unittest.TestCase):
    def test_profiles_keep_preferences_keys_and_instance_identity_separate(self):
        from desktop_pet.single_instance import instance_identity
        vault, original_store = FakeVault(), CredentialStore
        def store_factory(service="AURA Desktop Agent", account="chat-api-key", **_kwargs):
            return original_store(service=service, account=account, backend=vault)
        with tempfile.TemporaryDirectory(prefix="jingban isolated profiles ") as folder:
            first_path, second_path = Path(folder)/"first", Path(folder)/"second"
            with patch("desktop_pet.settings_manager.CredentialStore", side_effect=store_factory):
                first = SettingsManager(profile_dir=first_path)
                second = SettingsManager(profile_dir=second_path)
                legacy = SettingsManager(QSettings(str(Path(folder)/"legacy.ini"), QSettings.Format.IniFormat))
                legacy.save_api_key("synthetic-legacy-key")
                first.save_api_key("synthetic-first-profile-key")
                first.save_api_settings(ApiSettings(model="first-model"))
                self.assertEqual(second.get_api_key(), "")
                self.assertEqual(legacy.get_api_key(), "synthetic-legacy-key")
                self.assertEqual(second.load_api_settings().model, "")
                reloaded = SettingsManager(profile_dir=first_path)
                self.assertEqual(reloaded.get_api_key(), "synthetic-first-profile-key")
                self.assertEqual(reloaded.load_api_settings().model, "first-model")
                self.assertNotEqual(instance_identity(first.config_path), instance_identity(second.config_path))
                self.assertNotEqual(first._credentials.service, legacy._credentials.service)
                self.assertNotIn("synthetic-first-profile-key", (first_path/"settings.ini").read_text())

    def test_profile_directory_failure_preserves_the_existing_file(self):
        with tempfile.TemporaryDirectory(prefix="jingban profile failure ") as folder:
            path = Path(folder)/"cannot-use-as-folder"
            path.write_text("preserve this file", encoding="utf-8")
            with self.assertRaises(SettingsError):
                SettingsManager(profile_dir=path)
            self.assertEqual(path.read_text(), "preserve this file")

    def test_profile_and_injected_settings_cannot_silently_target_different_archives(self):
        with tempfile.TemporaryDirectory(prefix="jingban ambiguous profile ") as folder:
            settings = QSettings(str(Path(folder)/"settings.ini"), QSettings.Format.IniFormat)
            with self.assertRaises(SettingsError):
                SettingsManager(settings, profile_dir=Path(folder)/"other")


class FrozenStartupTests(unittest.TestCase):
    def test_executable_autostart_needs_no_python_or_source_entry(self):
        with tempfile.TemporaryDirectory(prefix="鲸伴 package test ") as folder:
            executable = Path(folder)/"鲸伴.exe"
            executable.write_bytes(b"synthetic executable, never launched")
            profile = Path(folder)/"independent profile"
            registry = RegistryDouble()
            manager = StartupManager(project_root=Path(folder)/"_internal", python_executable=executable,
                                     registry=registry, platform_name="win32", frozen=True, profile_dir=profile)
            manager.set_enabled(True)
            command = registry.values[StartupManager.VALUE_NAME][0]
            self.assertEqual(command, subprocess.list2cmdline(
                [str(executable.resolve()), "--autostart", "--profile-dir", str(profile.resolve())]))
            self.assertNotIn("pythonw.exe", command)
            self.assertNotIn("main.py", command)
            self.assertTrue(manager.is_enabled())
            manager.set_enabled(False)
            self.assertFalse(manager.is_enabled())

    def test_missing_frozen_executable_never_writes_a_registration(self):
        with tempfile.TemporaryDirectory(prefix="jingban missing executable ") as folder:
            registry = RegistryDouble()
            manager = StartupManager(python_executable=Path(folder)/"鲸伴.exe", registry=registry,
                                     platform_name="win32", frozen=True)
            with self.assertRaises(SettingsError):
                manager.set_enabled(True)
            self.assertEqual(registry.writes, [])
