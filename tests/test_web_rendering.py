"""Render bootstrap policy through injected dicts, with no Qt or OS changes."""

import os
from pathlib import Path
import runpy
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from desktop_pet.web_rendering import configure_webengine_rendering


class WebRenderingEntryPointTests(unittest.TestCase):
    def test_script_entry_configures_renderer_before_importing_app_main(self):
        before = dict(os.environ)
        observed = []

        class ApplicationModule(ModuleType):
            def __getattr__(module, name):
                if name != "main":
                    raise AttributeError(name)
                self.assertIn("--disable-gpu", os.environ["QTWEBENGINE_CHROMIUM_FLAGS"])
                observed.append("app_main_imported")

                def fake_main():
                    observed.append("main_called")
                    return 0

                return fake_main

        with patch.dict(os.environ, {"AURA_WEBENGINE_RENDERER": "software",
                                    "QTWEBENGINE_CHROMIUM_FLAGS": "--existing-local-switch"}), \
                patch.dict(sys.modules, {"desktop_pet.app": ApplicationModule("desktop_pet.app")}):
            with self.assertRaises(SystemExit) as result:
                runpy.run_path(str(Path(__file__).resolve().parents[1] / "main.py"), run_name="__main__")
            self.assertEqual(result.exception.code, 0)
            self.assertIn("app_main_imported", observed)
            self.assertEqual(observed[-1], "main_called")
            self.assertTrue(os.environ["QTWEBENGINE_CHROMIUM_FLAGS"].startswith("--existing-local-switch "))
        self.assertEqual(dict(os.environ), before)

    def test_direct_app_entry_configures_before_creating_qapplication(self):
        # Importing Qt classes does not construct QApplication, create a window,
        # or read the user's preferences. All entry-point services below are fake.
        from desktop_pet import app as desktop_app

        before = dict(os.environ)
        observed = []
        application = Mock()
        instance = Mock()
        instance.start.return_value = False

        def existing_application():
            self.assertIn("--disable-gpu", os.environ["QTWEBENGINE_CHROMIUM_FLAGS"])
            observed.append("application_checked")
            return None

        def create_application(_argv):
            self.assertIn("--disable-gpu", os.environ["QTWEBENGINE_CHROMIUM_FLAGS"])
            observed.append("application_created")
            return application

        with patch.dict(os.environ, {"AURA_WEBENGINE_RENDERER": "software",
                                    "QTWEBENGINE_CHROMIUM_FLAGS": "--existing-local-switch"}), \
                patch.object(desktop_app, "QApplication") as application_class, \
                patch.object(desktop_app, "SingleInstance", return_value=instance) as single_instance, \
                patch.object(desktop_app, "SettingsManager") as settings_manager, \
                patch.object(desktop_app, "ApplicationController") as controller:
            application_class.instance.side_effect = existing_application
            application_class.side_effect = create_application
            settings = SimpleNamespace(config_path="synthetic-rendering-test.ini")
            self.assertEqual(desktop_app.main([], settings=settings), 0)
            self.assertEqual(observed, ["application_checked", "application_created"])
            single_instance.assert_called_once_with(settings.config_path, application)
            settings_manager.assert_not_called()
            controller.assert_not_called()
            application.exec.assert_not_called()
            instance.release.assert_called_once_with()
        self.assertEqual(dict(os.environ), before)


class WebRenderingPolicyTests(unittest.TestCase):
    def test_windows_defaults_to_process_local_software_rendering(self):
        values = {}
        self.assertEqual(configure_webengine_rendering(values, "win32"), "software")
        self.assertEqual(values, {"QTWEBENGINE_CHROMIUM_FLAGS": "--disable-gpu"})

    def test_non_windows_default_leaves_environment_unchanged(self):
        for platform in ("linux", "darwin", "freebsd"):
            with self.subTest(platform=platform):
                values = {"QTWEBENGINE_CHROMIUM_FLAGS": '--example="keep this"'}
                before = dict(values)
                self.assertEqual(configure_webengine_rendering(values, platform), "auto")
                self.assertEqual(values, before)

    def test_explicit_auto_preserves_all_windows_flags(self):
        values = {"AURA_WEBENGINE_RENDERER": "auto",
                  "QTWEBENGINE_CHROMIUM_FLAGS": '--use-angle=d3d11 --example="with spaces"'}
        before = dict(values)
        self.assertEqual(configure_webengine_rendering(values, "win32"), "auto")
        self.assertEqual(values, before)

    def test_explicit_software_can_be_used_on_other_platforms(self):
        values = {"AURA_WEBENGINE_RENDERER": "software"}
        self.assertEqual(configure_webengine_rendering(values, "linux"), "software")
        self.assertEqual(values["QTWEBENGINE_CHROMIUM_FLAGS"], "--disable-gpu")

    def test_unknown_modes_fall_back_to_the_platform_default(self):
        for platform, expected in (("win32", "software"), ("linux", "auto")):
            with self.subTest(platform=platform):
                values = {"AURA_WEBENGINE_RENDERER": "unknown-private-value"}
                self.assertEqual(configure_webengine_rendering(values, platform), expected)
                self.assertEqual("QTWEBENGINE_CHROMIUM_FLAGS" in values, expected == "software")

    def test_mode_whitespace_and_case_are_tolerated(self):
        values = {"AURA_WEBENGINE_RENDERER": "  AUTO  "}
        self.assertEqual(configure_webengine_rendering(values, "win32"), "auto")
        self.assertNotIn("QTWEBENGINE_CHROMIUM_FLAGS", values)

    def test_existing_flags_and_quoting_are_retained_verbatim(self):
        flags = '  --example="with spaces" --path="C:\\A Folder\\data"  '
        values = {"QTWEBENGINE_CHROMIUM_FLAGS": flags}
        configure_webengine_rendering(values, "win32")
        self.assertEqual(values["QTWEBENGINE_CHROMIUM_FLAGS"], flags + "--disable-gpu")

    def test_existing_disable_switch_is_not_duplicated_or_normalized(self):
        for flags in ("--disable-gpu", "  --disable-gpu  --example=1 ", "--disable-gpu=1"):
            with self.subTest(flags=flags):
                values = {"QTWEBENGINE_CHROMIUM_FLAGS": flags}
                configure_webengine_rendering(values, "win32")
                self.assertEqual(values["QTWEBENGINE_CHROMIUM_FLAGS"], flags)

    def test_compositing_switch_is_not_mistaken_for_disable_gpu(self):
        flags = "--disable-gpu-compositing"
        values = {"QTWEBENGINE_CHROMIUM_FLAGS": flags}
        configure_webengine_rendering(values, "win32")
        self.assertEqual(values["QTWEBENGINE_CHROMIUM_FLAGS"], flags + " --disable-gpu")

    def test_switch_text_inside_a_quoted_value_is_not_a_real_switch(self):
        flags = '--example="a --disable-gpu b"'
        values = {"QTWEBENGINE_CHROMIUM_FLAGS": flags}
        configure_webengine_rendering(values, "win32")
        self.assertEqual(values["QTWEBENGINE_CHROMIUM_FLAGS"], flags + " --disable-gpu")

    def test_repeated_calls_are_idempotent(self):
        values = {"QTWEBENGINE_CHROMIUM_FLAGS": "--example=1"}
        configure_webengine_rendering(values, "win32")
        first = dict(values)
        configure_webengine_rendering(values, "win32")
        self.assertEqual(values, first)

    def test_empty_injected_dict_never_falls_back_to_real_environment(self):
        with patch.dict(os.environ, {"QTWEBENGINE_CHROMIUM_FLAGS": "real-process-sentinel"}):
            before = dict(os.environ)
            values = {}
            configure_webengine_rendering(values, "win32")
            self.assertEqual(values["QTWEBENGINE_CHROMIUM_FLAGS"], "--disable-gpu")
            self.assertEqual(dict(os.environ), before)

    def test_other_qt_backends_and_settings_are_not_modified(self):
        values = {"QT_QUICK_BACKEND": "existing-backend", "QT_OPENGL": "existing-opengl",
                  "SOME_OTHER_SETTING": "unchanged"}
        configure_webengine_rendering(values, "win32")
        self.assertEqual(values["QT_QUICK_BACKEND"], "existing-backend")
        self.assertEqual(values["QT_OPENGL"], "existing-opengl")
        self.assertEqual(values["SOME_OTHER_SETTING"], "unchanged")

    def test_malformed_existing_quotes_do_not_crash_or_duplicate_on_second_call(self):
        values = {"QTWEBENGINE_CHROMIUM_FLAGS": '--example="unfinished'}
        configure_webengine_rendering(values, "win32")
        first = dict(values)
        configure_webengine_rendering(values, "win32")
        self.assertEqual(values, first)
        self.assertTrue(values["QTWEBENGINE_CHROMIUM_FLAGS"].startswith('--example="unfinished'))


if __name__ == "__main__":
    unittest.main()
