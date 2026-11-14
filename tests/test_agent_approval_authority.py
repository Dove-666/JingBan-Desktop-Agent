"""Approval settings, native-session isolation and controller authority, offline."""
from dataclasses import replace
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, QThreadPool
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication

from desktop_pet.agents.contracts import AgentValidationError
from desktop_pet.agents.coordinator import settings_identity
from desktop_pet.agents.dsh_storage import session_scope
from desktop_pet.agents.settings import AgentSettings, load_agent_settings, save_agent_settings
from desktop_pet.chat_controller import ChatController
from tests.test_chat_controller import MemorySettings, RecordingClient


class AgentPreferences(MemorySettings):
    def __init__(self):
        super().__init__()
        self.agent = AgentSettings()
        self.saved = []
        self.fail_save = False

    def load_agent_settings(self):
        return self.agent

    def save_agent_settings(self, settings):
        if self.fail_save:
            raise OSError("private-storage-detail-do-not-show")
        settings.validate()
        self.agent = settings
        self.saved.append(settings)


class ApprovalPreferencesTests(unittest.TestCase):
    def test_existing_configuration_defaults_to_auto_and_each_mode_survives_reload(self):
        with tempfile.TemporaryDirectory(prefix="jingban-approval-settings-") as directory:
            path = str(Path(directory) / "preferences.ini")
            store = QSettings(path, QSettings.Format.IniFormat)
            self.assertEqual(load_agent_settings(store).approval_mode, "auto")
            original = replace(AgentSettings(), computer_enabled=False, tools_enabled=False)
            for mode in ("ask", "auto", "full"):
                with self.subTest(mode=mode):
                    save_agent_settings(store, replace(original, approval_mode=mode))
                    reopened = QSettings(path, QSettings.Format.IniFormat)
                    loaded = load_agent_settings(reopened)
                    self.assertEqual(loaded.approval_mode, mode)
                    self.assertFalse(loaded.computer_enabled)
                    self.assertFalse(loaded.tools_enabled)

    def test_unrecognized_modes_cannot_enable_full_access(self):
        for mode in ("", "FULL", "danger-full-access", True, None, []):
            with self.subTest(mode=mode), self.assertRaises(AgentValidationError):
                replace(AgentSettings(), approval_mode=mode).validate()
        with tempfile.TemporaryDirectory(prefix="jingban-approval-invalid-") as directory:
            store = QSettings(str(Path(directory) / "preferences.ini"), QSettings.Format.IniFormat)
            store.setValue("agents/approval_mode", "unknown")
            loaded = load_agent_settings(store)
            self.assertFalse(loaded.enabled)
            self.assertEqual(loaded.approval_mode, "auto")

    def test_native_sessions_and_queued_authority_are_separate_for_each_mode(self):
        settings = AgentSettings()
        api = {"base_url": "https://example.invalid/v1", "model": "fixture"}
        native, queued = set(), set()
        for mode in ("ask", "auto", "full"):
            current = replace(settings, approval_mode=mode)
            native.add(session_scope(current, api, generation="fixture-generation", key="local-fixture"))
            queued.add(settings_identity({"agent": current, "api": api, "key": "local-fixture"},
                                         peer="fixture-peer", authorization="fixture-auth", generation="fixture-generation"))
        self.assertEqual(len(native), 3)
        self.assertEqual(len(queued), 3)


class ApprovalControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.settings = AgentPreferences()
        self.client = RecordingClient()
        self.controller = ChatController(self.settings, self.client, metadata_client_factory=lambda: None)
        self._wait_workers()

    def _wait_workers(self):
        deadline = time.monotonic() + 5
        while QThreadPool.globalInstance().activeThreadCount() and time.monotonic() < deadline:
            QTest.qWait(10)
            time.sleep(.002)
        self.app.processEvents()

    def tearDown(self):
        self.controller._active_request = None
        self.controller._busy = False
        self.controller._history_loading = False
        self.controller.shutdown()
        self._wait_workers()

    def test_mode_changes_are_persisted_published_and_do_not_send_a_model_request(self):
        changed = QSignalSpy(self.controller.agent_settings_changed)
        self.assertTrue(self.controller.set_agent_approval_mode("full"))
        self.assertEqual(self.settings.agent.approval_mode, "full")
        self.assertEqual(self.controller.agent_approval_mode, "full")
        self.assertEqual(self.controller._current_agent_queue_policy()["agent"].approval_mode, "full")
        self.assertEqual(changed.count(), 1)
        self.assertEqual(self.client.requests, [])
        self.assertTrue(self.controller.set_agent_approval_mode("full"))
        self.assertEqual(len(self.settings.saved), 1)

    def test_busy_or_loading_cannot_change_authority(self):
        for field in ("_busy", "_history_loading"):
            with self.subTest(field=field):
                setattr(self.controller, field, True)
                self.assertFalse(self.controller.set_agent_approval_mode("full"))
                setattr(self.controller, field, False)
        self.assertEqual(self.settings.saved, [])
        self.assertEqual(self.controller.agent_approval_mode, "auto")

    def test_invalid_and_failed_saves_do_not_change_the_published_mode(self):
        for mode in (True, {}, "full ", "unexpected"):
            self.assertFalse(self.controller.set_agent_approval_mode(mode))
        self.settings.fail_save = True
        self.assertFalse(self.controller.set_agent_approval_mode("full"))
        self.assertEqual(self.controller.agent_approval_mode, "auto")
        self.assertEqual(self.settings.agent.approval_mode, "auto")
        self.assertIn("未能保存", self.controller.error)
        self.assertNotIn("private-storage", self.controller.error)

    def test_external_permission_change_cancels_an_existing_task(self):
        for kind in ("agent", "tools", "computer"):
            with self.subTest(kind=kind):
                self.controller._active_request = {"agent_kind": kind, "conversation_id": "fixture-conversation"}
                next_mode = "ask" if self.settings.agent.approval_mode != "ask" else "auto"
                self.settings.agent = replace(self.settings.agent, approval_mode=next_mode)
                with patch.object(self.controller, "stop_request") as stop:
                    self.controller.refresh_agent_settings()
                self.assertTrue(stop.called)
                self.assertEqual(self.controller.agent_approval_mode, next_mode)
        self.assertEqual(self.client.requests, [])


if __name__ == "__main__":
    unittest.main()
