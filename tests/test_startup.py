"""Startup settings without touching the actual registry or production process."""

from dataclasses import replace
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PySide6.QtWidgets import QApplication

from desktop_pet.app import main, parse_config
from desktop_pet.application_controller import ApplicationController
from desktop_pet.config import PetConfig
from desktop_pet.settings_manager import SettingsError
from desktop_pet.single_instance import SingleInstance
from desktop_pet.startup_manager import StartupManager
from tests import test_settings_navigation as navigation_fixture


class RegistryDouble:
    HKEY_CURRENT_USER = object()
    KEY_QUERY_VALUE = 1
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self):
        self.values = {"AnotherApp": ('"C:\\Some App\\app.exe"', self.REG_SZ)}
        self.reads = []
        self.writes = []
        self.deleted = []
        self.failure = ""
        self.ignore_write = False
        self.ignore_delete = False

    def _maybe_fail(self, phase):
        if self.failure == phase:
            raise PermissionError("backend-detail-with-private-material")

    def OpenKey(self, root, path, reserved=0, access=0):
        self._maybe_fail("open")
        if root is not self.HKEY_CURRENT_USER or path != StartupManager.RUN_KEY:
            raise AssertionError("Unexpected registry location")
        return self

    CreateKeyEx = OpenKey

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def QueryValueEx(self, _key, name):
        self._maybe_fail("read")
        self.reads.append(name)
        if name not in self.values:
            raise FileNotFoundError
        return self.values[name]

    def SetValueEx(self, _key, name, reserved, value_type, value):
        self._maybe_fail("write")
        self.writes.append((name, value, value_type))
        if not self.ignore_write:
            self.values[name] = (value, value_type)

    def DeleteValue(self, _key, name):
        self._maybe_fail("delete")
        self.deleted.append(name)
        if name not in self.values:
            raise FileNotFoundError
        if not self.ignore_delete:
            del self.values[name]


class StartupRegistryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="aura startup test ")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "project with spaces"
        self.root.mkdir()
        (self.root / "main.py").write_text("# synthetic test entry", encoding="utf-8")
        self.python_folder = Path(self.directory.name) / "python with spaces"
        self.python_folder.mkdir()
        self.pythonw = self.python_folder / "pythonw.exe"
        self.pythonw.write_bytes(b"synthetic executable, never launched")
        self.registry = RegistryDouble()
        self.manager = StartupManager(project_root=self.root,
                                      python_executable=self.python_folder / "python.exe",
                                      registry=self.registry, platform_name="win32")

    def test_constructing_service_never_reads_or_writes_registry(self):
        self.assertTrue(self.manager.available)
        self.assertEqual(self.registry.reads, [])
        self.assertEqual(self.registry.writes, [])
        self.assertEqual(self.registry.deleted, [])

    def test_enable_quotes_absolute_project_and_pythonw_paths_and_verifies(self):
        original = self.registry.values["AnotherApp"]
        self.assertFalse(self.manager.is_enabled())
        self.manager.set_enabled(True)
        expected = subprocess.list2cmdline([str(self.pythonw.resolve()),
                                           str((self.root / "main.py").resolve()), "--autostart"])
        self.assertEqual(self.registry.values[StartupManager.VALUE_NAME], (expected, self.registry.REG_SZ))
        self.assertTrue(expected.startswith('"'))
        self.assertTrue(expected.endswith(" --autostart"))
        self.assertTrue(self.manager.is_enabled())
        self.assertEqual(self.registry.values["AnotherApp"], original)
        self.assertEqual({name for name, *_ in self.registry.writes}, {StartupManager.VALUE_NAME})

    def test_disable_only_deletes_own_value_and_is_idempotent(self):
        self.manager.set_enabled(True)
        original = self.registry.values["AnotherApp"]
        self.manager.set_enabled(False)
        self.manager.set_enabled(False)
        self.assertFalse(self.manager.is_enabled())
        self.assertNotIn(StartupManager.VALUE_NAME, self.registry.values)
        self.assertEqual(self.registry.values["AnotherApp"], original)
        self.assertEqual(set(self.registry.deleted), {StartupManager.VALUE_NAME})

    def test_registration_for_an_old_checkout_is_not_reported_enabled(self):
        self.registry.values[StartupManager.VALUE_NAME] = ('"C:\\old checkout\\pythonw.exe" main.py', 1)
        self.assertFalse(self.manager.is_enabled())
        self.manager.set_enabled(False)
        self.assertNotIn(StartupManager.VALUE_NAME, self.registry.values)
        self.assertIn("AnotherApp", self.registry.values)

    def test_missing_runtime_does_not_create_an_invalid_startup_entry(self):
        self.pythonw.unlink()
        with self.assertRaises(SettingsError):
            self.manager.set_enabled(True)
        self.assertEqual(self.registry.writes, [])

    def test_existing_registration_is_detected_then_renamed_without_duplicate(self):
        command = self.manager._command()
        self.registry.values[StartupManager.LEGACY_VALUE_NAME] = (command, self.registry.REG_SZ)
        self.assertTrue(self.manager.is_enabled())
        self.assertNotIn(StartupManager.VALUE_NAME, self.registry.values)
        self.manager.set_enabled(True)
        self.assertEqual(StartupManager.VALUE_NAME, "鲸伴")
        self.assertEqual(self.registry.values["鲸伴"], (command, self.registry.REG_SZ))
        self.assertNotIn(StartupManager.LEGACY_VALUE_NAME, self.registry.values)
        self.assertIn("AnotherApp", self.registry.values)

    def test_disable_removes_matching_old_registration_and_leaves_it_disabled(self):
        self.registry.values[StartupManager.LEGACY_VALUE_NAME] = (self.manager._command(), self.registry.REG_SZ)
        self.assertTrue(self.manager.is_enabled())
        self.manager.set_enabled(False)
        self.assertFalse(self.manager.is_enabled())
        self.assertNotIn(StartupManager.LEGACY_VALUE_NAME, self.registry.values)
        self.assertIn("AnotherApp", self.registry.values)

    def test_brand_migration_preserves_a_different_checkout_legacy_entry(self):
        foreign = ('"C:\\different checkout\\pythonw.exe" main.py --autostart', self.registry.REG_SZ)
        self.registry.values[StartupManager.LEGACY_VALUE_NAME] = foreign
        self.assertFalse(self.manager.is_enabled())
        self.manager.set_enabled(True)
        self.assertEqual(self.registry.values[StartupManager.LEGACY_VALUE_NAME], foreign)
        self.manager.set_enabled(False)
        self.assertEqual(self.registry.values[StartupManager.LEGACY_VALUE_NAME], foreign)

    def test_read_write_delete_failures_have_static_errors(self):
        for phase, action in (("read", self.manager.is_enabled),
                              ("write", lambda: self.manager.set_enabled(True)),
                              ("delete", lambda: self.manager.set_enabled(False))):
            with self.subTest(phase=phase):
                self.registry.failure = phase
                with self.assertRaises(SettingsError) as raised:
                    action()
                self.assertNotIn("private-material", str(raised.exception))
                self.assertNotIn(str(self.root), str(raised.exception))
        self.assertIn("AnotherApp", self.registry.values)

    def test_failed_readback_never_reports_enable_or_disable_success(self):
        self.registry.ignore_write = True
        with self.assertRaises(SettingsError):
            self.manager.set_enabled(True)
        self.registry.ignore_write = False
        self.manager.set_enabled(True)
        self.registry.ignore_delete = True
        with self.assertRaises(SettingsError):
            self.manager.set_enabled(False)
        self.assertTrue(self.manager.is_enabled())

    def test_unsupported_platform_is_unavailable_and_cannot_write(self):
        manager = StartupManager(project_root=self.root, registry=self.registry,
                                 python_executable=self.pythonw, platform_name="linux")
        self.assertFalse(manager.available)
        self.assertFalse(manager.is_enabled())
        with self.assertRaises(SettingsError):
            manager.set_enabled(True)
        self.assertEqual(self.registry.writes, [])


class StartupLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def test_cli_flags_distinguish_explicit_silent_from_saved_preference(self):
        self.assertIsNone(parse_config([]).silent_start)
        self.assertFalse(parse_config([]).autostart)
        self.assertTrue(parse_config(["--silent"]).silent_start)
        auto = parse_config(["--autostart"])
        self.assertTrue(auto.autostart)
        self.assertIsNone(auto.silent_start)
        both = parse_config(["--autostart", "--silent", "--debug-panel"])
        self.assertTrue(both.show_debug_panel)
        with self.assertRaises(ValueError):
            PetConfig(silent_start="yes")

    def test_silent_visibility_requires_reachable_tray_and_explicit_debug_wins(self):
        for config, saved_silent, tray_available, hidden in (
            (PetConfig(), True, True, False),
            (PetConfig(), False, True, False),
            (PetConfig(silent_start=True), False, True, True),
            (PetConfig(silent_start=False), True, True, False),
            (PetConfig(silent_start=True), True, False, False),
            (PetConfig(silent_start=True, show_debug_panel=True), True, True, False),
            (PetConfig(autostart=True), True, True, True),
            (PetConfig(autostart=True), False, True, False),
            (PetConfig(autostart=True), True, False, False),
            (PetConfig(autostart=True, show_debug_panel=True), True, True, False),
            (PetConfig(autostart=True, silent_start=False), True, True, False),
        ):
            with self.subTest(config=config, tray_available=tray_available):
                host = SimpleNamespace(_startup_config=config,
                                       settings=SimpleNamespace(load_silent_start=Mock(return_value=saved_silent)),
                                       pet=Mock(), tray=SimpleNamespace(available=tray_available, refresh=Mock()))
                ApplicationController.show(host)
                if config.silent_start is None and not config.autostart:
                    host.settings.load_silent_start.assert_not_called()
                if hidden:
                    host.pet.hide.assert_called_once()
                    host.pet.show.assert_not_called()
                else:
                    host.pet.show.assert_called_once()
                    host.pet.hide.assert_not_called()
                host.tray.refresh.assert_called_once()

    def test_duplicate_manual_wakes_chat_but_silent_and_autostart_do_not(self):
        for args, expected in (([], "chat"), (["--silent"], "silent"),
                               (["--autostart"], "silent"),
                               (["--debug-panel", "--silent"], "debug")):
            with self.subTest(args=args), \
                    patch("desktop_pet.app.SingleInstance") as instance_factory, \
                    patch("desktop_pet.app.ApplicationController") as controller_factory:
                instance = instance_factory.return_value
                instance.start.return_value = False
                settings = SimpleNamespace(config_path="synthetic-config.ini", load_silent_start=Mock(return_value=True))
                self.assertEqual(main(args, settings=settings), 0)
                instance.start.assert_called_once_with(expected)
                controller_factory.assert_not_called()
                instance.release.assert_called_once()

    def test_fixed_silent_ipc_is_acknowledged_without_activation(self):
        class SocketDouble:
            def __init__(self, frame):
                self.frame = frame
                self.properties = {}
                self.ack = []

            def property(self, name):
                return self.properties.get(name)

            def setProperty(self, name, value):
                self.properties[name] = value

            def read(self, _limit):
                frame, self.frame = self.frame, b""
                return frame

            def write(self, value):
                self.ack.append(value)

            def flush(self):
                pass

            def disconnectFromServer(self):
                pass

            abort = deleteLater = flush

        with tempfile.TemporaryDirectory(prefix="aura-startup-ipc-") as folder:
            owner = SingleInstance(Path(folder) / "settings.ini")
            observed = []
            owner.activation_requested.connect(observed.append)
            for command in ("silent", "chat", "debug"):
                socket = SocketDouble(SingleInstance.COMMANDS[command])
                owner._clients[socket] = (bytearray(), Mock())
                owner._read_command(socket)
                owner._read_command(socket)
                self.assertEqual(socket.ack, [SingleInstance.ACK])
            self.assertEqual(observed, ["chat", "debug"])
            owner.release()
            owner.deleteLater()

    def test_existing_owner_activation_restores_pet_before_opening_window(self):
        for command, window_method in (("chat", "open_chat_window"), ("debug", "open_debug_panel")):
            with self.subTest(command=command), \
                    patch("desktop_pet.app.QApplication") as application_class, \
                    patch("desktop_pet.app.SingleInstance") as instance_factory, \
                    patch("desktop_pet.app.ApplicationController") as controller_factory:
                application = application_class.instance.return_value
                instance = instance_factory.return_value
                instance.start.return_value = True
                session = controller_factory.return_value
                observed = Mock()
                observed.attach_mock(session.show_pet, "show_pet")
                observed.attach_mock(getattr(session, window_method), "open_window")

                def activate_during_event_loop():
                    callback = instance.activation_requested.connect.call_args.args[0]
                    callback(command)
                    return 0

                application.exec.side_effect = activate_during_event_loop
                settings = SimpleNamespace(config_path="synthetic-config.ini")
                self.assertEqual(main([], settings=settings), 0)
                self.assertEqual([call[0] for call in observed.mock_calls], ["show_pet", "open_window"])


class StartupApplicationTests(unittest.TestCase):
    """Real application lifecycle with temporary archives and a registry stub."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.startup_stub = SimpleNamespace(available=True, is_enabled=Mock(return_value=False),
                                            set_enabled=Mock())
        replacement = patch("desktop_pet.application_controller.StartupManager", return_value=self.startup_stub)
        replacement.start()
        self.addCleanup(replacement.stop)
        navigation_fixture.EmbeddedSettingsNavigationTests.setUp(self)

    tearDown = navigation_fixture.EmbeddedSettingsNavigationTests.tearDown

    def test_saved_silent_autostart_leaves_only_reachable_tray(self):
        self.settings.save_silent_start(True)
        self.session._startup_config = replace(self.session._startup_config, autostart=True)
        self.tray.available = True
        self.session.show()
        self.assertFalse(self.session.pet.isVisible())
        self.assertIsNone(self.session.chat_window)
        self.assertIsNone(self.session.debug_panel)
        self.assertIs(self.session.startup_manager, self.startup_stub)
        self.session.pet_controller.respond("隐藏时的合成评论也不能弹出窗口", "neutral")
        self.assertFalse(self.session.pet.bubble.isVisible())
        self.startup_stub.set_enabled.assert_not_called()
        self.camera_start.assert_not_called()

    def test_visible_startup_preference_shows_pet_without_opening_panels(self):
        self.settings.save_silent_start(False)
        self.tray.available = True
        self.session.show()
        self.assertTrue(self.session.pet.isVisible())
        self.assertIsNone(self.session.chat_window)
        self.assertIsNone(self.session.debug_panel)

    def test_manual_launch_shows_pet_even_with_old_silent_preference(self):
        self.settings.save_silent_start(True)
        self.tray.available = True
        self.session.show()
        self.assertTrue(self.session.pet.isVisible())
        self.assertTrue(self.settings.load_silent_start())
        self.assertIsNone(self.session.chat_window)
        self.assertIsNone(self.session.debug_panel)

    def test_explicit_launch_restores_silent_pet_without_replacing_conversation(self):
        self.settings.save_silent_start(True)
        self.session._startup_config = replace(self.session._startup_config, autostart=True)
        self.tray.available = True
        self.session.show()
        self.assertFalse(self.session.pet.isVisible())
        original_id = self.session.chat.current_conversation_id
        self.tray.refresh.reset_mock()
        self.session.show_pet()
        self.assertTrue(self.session.pet.isVisible())
        self.assertEqual(self.session.chat.current_conversation_id, original_id)
        self.assertTrue(self.settings.load_silent_start())
        self.tray.refresh.assert_called_once()
        self.assertIsNone(self.session.chat_window)

    def test_explicit_launch_cannot_revive_shutting_down_pet(self):
        self.session.shutdown()
        self.session.show_pet()
        self.assertFalse(self.session.pet.isVisible())

    def test_unavailable_tray_safely_shows_pet_instead_of_unreachable_process(self):
        self.settings.save_silent_start(True)
        self.session._startup_config = replace(self.session._startup_config, autostart=True)
        self.tray.available = False
        self.session.show()
        self.assertTrue(self.session.pet.isVisible())
        self.assertIsNone(self.session.chat_window)
        self.assertIsNone(self.session.debug_panel)
        self.startup_stub.set_enabled.assert_not_called()

    def test_startup_service_is_shared_with_embedded_settings_without_registry_write(self):
        self.session.open_settings("general")
        page = self.session.chat_window.settings_page
        self.assertIs(page.startup_manager, self.startup_stub)
        self.startup_stub.is_enabled.assert_called()
        self.startup_stub.set_enabled.assert_not_called()
        self.assertIsNone(self.session.debug_panel)


if __name__ == "__main__":
    unittest.main()
