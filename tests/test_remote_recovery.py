"""Startup/recovery through real Qt timers, temporary settings and fake transport.

No live socket, credential vault, clipboard, registry or user archive is used.
"""

import json
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from PySide6.QtCore import QEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from desktop_pet.remote.controller import RemoteController
from desktop_pet.remote.settings import RemotePreferences
from desktop_pet.ui.remote_panel import RemotePanel
from tests import test_remote_controller as controller_fixture
from tests.test_remote_controller import FakeTransport, PEER, spin_until


READY = {"state": "listening", "endpoint_id": "c" * 64, "ticket": "synthetic-local-ticket",
         "local_available": True, "relay_available": False}
OFFLINE = {"state": "error", "error_code": "no_usable_address",
           "error": "synthetic private error must never reach UI"}


class PlannedFactory:
    def __init__(self, *statuses):
        self.statuses = statuses
        self.instances = []

    def __call__(self, **kwargs):
        index = len(self.instances)
        planned = self.statuses[min(index, len(self.statuses) - 1)]

        class Transport(FakeTransport):
            def start(self):
                self.status_callback(dict(planned))

        transport = Transport(**kwargs)
        self.instances.append(transport)
        return transport


class RemoteRecoveryTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.fixture = controller_fixture.RemoteControllerTests("runTest")
        self.fixture.setUp()
        self.remote = self.fixture.remote
        self.panels = []

    def tearDown(self):
        for panel in self.panels:
            panel.hide()
            panel.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.fixture.tearDown()

    def replace(self, factory, *, saved_enabled=False):
        self.remote.shutdown()
        self.remote.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        RemotePreferences(self.fixture.settings).set_enabled(saved_enabled)
        self.remote = RemoteController(self.fixture.session, transport_factory=factory)
        self.fixture.remote = self.remote
        return self.remote

    def start_error(self, factory):
        self.replace(factory)
        self.assertTrue(self.remote.set_enabled(True))
        spin_until(lambda: self.remote.status["state"] == "error")

    def test_saved_permission_starts_without_opening_settings_or_clicking_retry(self):
        factory = PlannedFactory(READY)
        self.replace(factory, saved_enabled=True)
        spin_until(lambda: self.remote.status["state"] == "listening")
        self.assertTrue(self.remote.enabled)
        self.assertTrue(RemotePreferences(self.fixture.settings).enabled)
        self.assertEqual(len(factory.instances), 1)
        self.assertTrue(self.remote.status["local_available"])
        self.assertFalse(self.remote.status["relay_available"])

    def test_startup_before_network_ready_automatically_recovers_with_real_timer(self):
        factory = PlannedFactory(OFFLINE, READY)
        with patch("desktop_pet.remote.controller.RETRY_DELAYS_SECONDS", (0.08, 0.1)):
            self.replace(factory, saved_enabled=True)
            spin_until(lambda: self.remote.status["state"] == "error")
            self.assertTrue(self.remote.status["auto_retry"])
            self.assertTrue(self.remote._retry_timer.isActive())
            self.assertTrue(self.remote.enabled)
            self.assertNotIn(OFFLINE["error"], json.dumps(self.remote.status))
            spin_until(lambda: self.remote.status["state"] == "listening")
        self.assertEqual(len(factory.instances), 2)
        self.assertTrue(factory.instances[0].stopped)
        self.assertFalse(self.remote._retry_timer.isActive())
        self.assertEqual(factory.instances[0].secret, factory.instances[1].secret)

    def test_retry_backoff_uses_2_5_10_30_60_seconds_and_stays_bounded(self):
        factory = PlannedFactory(OFFLINE)
        self.start_error(factory)
        for index, seconds in enumerate((2, 5, 10, 30, 60, 60)):
            with self.subTest(attempt=index + 1):
                self.assertEqual(self.remote._retry_timer.interval(), seconds * 1000)
                self.assertTrue(self.remote.status["auto_retry"])
                self.assertTrue(self.remote._retry_timer.isSingleShot())
                if index < 5:
                    count = len(factory.instances)
                    # Deliver the owned timer's callback without sleeping for a
                    # minute; the first test above exercises real timed expiry.
                    self.remote._retry_automatically()
                    spin_until(lambda: len(factory.instances) > count and self.remote.status["state"] == "error")

    def test_success_resets_backoff_and_preserves_identity_and_authorized_phone(self):
        factory = PlannedFactory(OFFLINE, OFFLINE, READY)
        self.replace(factory)
        self.remote.preferences.add_device(PEER, "Synthetic phone", "2026-11-01T00:00:00+00:00")
        devices = self.remote.devices
        self.remote.set_enabled(True)
        spin_until(lambda: self.remote.status["state"] == "error")
        self.remote._retry_automatically()
        spin_until(lambda: len(factory.instances) == 2 and self.remote.status["state"] == "error")
        self.assertEqual(self.remote._retry_timer.interval(), 5000)
        self.remote._retry_automatically()
        spin_until(lambda: self.remote.status["state"] == "listening")
        self.assertEqual(self.remote.devices, devices)
        self.assertTrue(self.remote.preferences.authorized(PEER))
        self.assertEqual(len({transport.secret for transport in factory.instances}), 1)
        factory.instances[-1].status_callback(dict(OFFLINE))
        spin_until(lambda: self.remote.status["state"] == "error")
        self.assertEqual(self.remote._retry_timer.interval(), 2000)
        self.assertTrue(self.remote.preferences.enabled)

    def test_manual_retry_does_not_wait_for_the_pending_backoff(self):
        factory = PlannedFactory(OFFLINE, READY)
        self.start_error(factory)
        self.remote.retry()
        spin_until(lambda: self.remote.status["state"] == "listening")
        self.assertEqual(len(factory.instances), 2)
        self.assertFalse(self.remote._retry_timer.isActive())

    def test_disable_cancels_recovery_and_late_callbacks_cannot_enable_it(self):
        factory = PlannedFactory(OFFLINE, READY)
        self.start_error(factory)
        old_callback = factory.instances[-1].status_callback
        self.assertTrue(self.remote.set_enabled(False))
        old_callback(dict(READY))
        old_callback(dict(OFFLINE))
        self.remote._retry_timer.timeout.emit()
        self.app.processEvents()
        self.assertFalse(self.remote.enabled)
        self.assertFalse(self.remote.preferences.enabled)
        self.assertEqual(self.remote.status["state"], "disabled")
        self.assertFalse(self.remote._retry_timer.isActive())
        self.assertEqual(len(factory.instances), 1)

    def test_shutdown_cancels_recovery_but_keeps_permission_for_next_launch(self):
        factory = PlannedFactory(OFFLINE, READY)
        self.start_error(factory)
        old_callback = factory.instances[-1].status_callback
        self.remote.shutdown()
        old_callback(dict(OFFLINE))
        self.remote._retry_timer.timeout.emit()
        self.app.processEvents()
        self.assertFalse(self.remote._retry_timer.isActive())
        self.assertTrue(RemotePreferences(self.fixture.settings).enabled)
        self.assertEqual(len(factory.instances), 1)
        self.assertEqual(self.remote.status["state"], "disabled")

    def test_old_generation_error_cannot_stop_a_recovered_listener(self):
        factory = PlannedFactory(OFFLINE, READY)
        self.start_error(factory)
        old_callback = factory.instances[0].status_callback
        self.remote.retry()
        spin_until(lambda: self.remote.status["state"] == "listening")
        old_callback(dict(OFFLINE))
        self.app.processEvents()
        self.assertEqual(self.remote.status["state"], "listening")
        self.assertFalse(self.remote._retry_timer.isActive())
        self.assertEqual(len(factory.instances), 2)

    def test_initial_callback_and_repeated_retry_cannot_start_duplicate_transports(self):
        factory = PlannedFactory(READY)
        self.replace(factory, saved_enabled=True)
        self.remote.retry()
        self.remote.retry()
        spin_until(lambda: self.remote.status["state"] == "listening")
        QTest.qWait(20)
        self.assertEqual(len(factory.instances), 1)

    def test_invalid_identity_is_clear_and_does_not_enter_retry_loop(self):
        factory = PlannedFactory({"state": "error", "error_code": "invalid_identity",
                                  "error": "do not show this synthetic credential"})
        self.start_error(factory)
        self.assertFalse(self.remote._retry_timer.isActive())
        self.assertFalse(self.remote.status["auto_retry"])
        self.assertIn("身份无效", self.remote.status["message"])
        self.assertNotIn("synthetic credential", json.dumps(self.remote.status))

    def test_missing_dependency_and_vault_failure_do_not_schedule_network_attempts(self):
        factory = PlannedFactory(READY)
        self.replace(factory)
        with patch.object(self.remote, "_dependency_available", return_value=False):
            self.remote.set_enabled(True)
            self.assertEqual(self.remote.status["error_code"], "dependency_unavailable")
            self.assertFalse(self.remote._retry_timer.isActive())
        self.assertEqual(factory.instances, [])
        self.fixture.vault.fail = True
        self.remote.retry()
        self.assertEqual(self.remote.status["error_code"], "credentials_unavailable")
        self.assertFalse(self.remote._retry_timer.isActive())
        self.assertEqual(factory.instances, [])
        self.assertNotIn(self.fixture.vault.secret_for_exception, json.dumps(self.remote.status))

    def test_damaged_saved_settings_do_not_auto_enable_or_overwrite_allowlist(self):
        self.remote.shutdown()
        self.remote.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        preferences = RemotePreferences(self.fixture.settings)
        preferences.directory.mkdir(parents=True, exist_ok=True)
        invalid = '{"version":1,"enabled":"true","devices":[]}'
        preferences.path.write_text(invalid, encoding="utf-8")
        factory = PlannedFactory(READY)
        self.remote = RemoteController(self.fixture.session, transport_factory=factory)
        self.fixture.remote = self.remote
        QTest.qWait(20)
        self.assertFalse(self.remote.enabled)
        self.assertFalse(self.remote._retry_timer.isActive())
        self.assertEqual(factory.instances, [])
        self.assertIn("设置损坏", self.remote.status["message"])
        self.assertEqual(preferences.path.read_text(encoding="utf-8"), invalid)

    def test_unknown_transport_details_cannot_escape_the_error_whitelist(self):
        factory = PlannedFactory({"state": "error", "error_code": ["untrusted"],
                                  "error": "secret URL and synthetic key"})
        self.start_error(factory)
        self.assertEqual(self.remote.status["error_code"], "transport_failed")
        self.assertNotIn("secret URL", json.dumps(self.remote.status))
        self.assertTrue(self.remote._retry_timer.isActive())

    def test_real_panel_exposes_auto_retry_then_neutral_local_ready_status(self):
        factory = PlannedFactory(OFFLINE, READY)
        self.start_error(factory)
        panel = RemotePanel(self.remote)
        self.panels.append(panel)
        panel.resize(740, 760)
        panel.show()
        self.app.processEvents()
        self.assertTrue(panel.enabled_checkbox.isChecked())
        self.assertIn("自动重试", panel.status_label.text())
        self.assertEqual(panel.retry_button.text(), "立即重试")
        self.assertTrue(panel._recovery_timer.isActive())
        panel.retry_button.click()
        spin_until(lambda: self.remote.status["state"] == "listening")
        self.assertEqual(panel.status_label.text(), "局域网已就绪 · 公网中继未连接")
        self.assertFalse(panel.error_label.isVisible())
        self.assertFalse(panel._recovery_timer.isActive())
        self.assertTrue(panel.pair_button.isEnabled())
        self.assertTrue(panel.copy_connection_button.isEnabled())
        self.assertIn("自动恢复", panel.enabled_checkbox.toolTip())
        self.assertNotIn("设备已连接", panel.status_label.text())

    def test_full_application_silent_autostart_restores_remote_identity_and_devices(self):
        from PySide6.QtCore import QThreadPool
        from PySide6.QtGui import QColor, QImage
        from desktop_pet.application_controller import ApplicationController
        from desktop_pet.config import PetConfig
        from desktop_pet.expression_manager import ExpressionManager
        from tests.test_application import isolated_settings
        from tests.test_memory_controller import FakeMemoryService
        from tests.test_startup_settings_ui import FakeStartupManager

        base = self.fixture.path / "full-autostart-session"
        base.mkdir()
        image = QImage(80, 100, QImage.Format.Format_ARGB32)
        image.fill(QColor("#649ed0"))
        image_path = base / "synthetic-character.png"
        self.assertTrue(image.save(str(image_path)))
        settings = isolated_settings(base / "settings.ini", self.fixture.vault)
        preferences = RemotePreferences(settings)
        preferences.set_enabled(True)
        preferences.add_device(PEER, "Synthetic saved phone", "2026-11-01T00:00:00+00:00")
        identity = preferences.identity_secret()
        saved_preferences = preferences.path.read_bytes()
        saved_vault = dict(self.fixture.vault.values)
        factory = PlannedFactory(READY)

        def transport_factory(**kwargs):
            authorization = kwargs.pop("is_authorized")
            self.assertTrue(callable(authorization))
            self.assertTrue(authorization(PEER))
            return factory(**kwargs)

        tray = SimpleNamespace(available=True, refresh=Mock(), shutdown=Mock())
        startup = FakeStartupManager(enabled=True)
        camera_start = Mock(return_value=False)
        session = None
        with patch("desktop_pet.application_controller.StartupManager", return_value=startup), \
                patch("desktop_pet.application_controller.DesktopTray", return_value=tray), \
                patch("desktop_pet.application_controller.MemoryService", return_value=FakeMemoryService()), \
                patch("desktop_pet.pet_controller.ExpressionManager", return_value=ExpressionManager(
                    base / "expressions", character_directory=base / "character")), \
                patch("desktop_pet.perception.camera_observer.CameraObserver.start", camera_start), \
                patch("desktop_pet.remote.transport.IrohTransport", side_effect=transport_factory):
            try:
                session = ApplicationController(PetConfig(image_path=image_path, autostart=True,
                    silent_start=True), settings=settings)
                session.pet_controller.asset_player.directory = base / "animations"
                session.pet.refresh_timer.stop()
                session.show()
                spin_until(lambda: session.remote.status["state"] == "listening"
                           and not session.chat.history_loading)
                self.assertFalse(session.pet.isVisible())
                self.assertIsNone(session.chat_window)
                self.assertIsNone(session.debug_panel)
                self.assertTrue(session.remote.enabled)
                self.assertTrue(session.remote.preferences.authorized(PEER))
                self.assertEqual(len(factory.instances), 1)
                self.assertEqual(factory.instances[0].secret, identity)
                self.assertEqual(startup.changes, [])
                camera_start.assert_not_called()
                session.shutdown()
                self.assertEqual(session.shutdown_errors, [])
                self.assertTrue(RemotePreferences(settings).enabled)
                self.assertEqual(preferences.path.read_bytes(), saved_preferences)
                self.assertEqual(self.fixture.vault.values, saved_vault)
            finally:
                if session is not None:
                    session.shutdown()
                    spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
                    session.pet.deleteLater()
                    session.deleteLater()
                    self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                    self.app.processEvents()
