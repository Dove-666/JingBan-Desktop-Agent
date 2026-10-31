"""Source links use the shared archive, never a new unrelated conversation."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from PySide6.QtCore import QEvent, QThreadPool
from PySide6.QtWidgets import QApplication

from desktop_pet.application_controller import ApplicationController
from desktop_pet.config import PetConfig
from tests.test_application import MemoryCredentialBackend, isolated_settings, spin_until
from tests.test_memory_controller import FakeMemoryService


class MemorySourceNavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="aura-memory-source-")
        settings = isolated_settings(Path(self.directory.name) / "settings.ini", MemoryCredentialBackend())
        with patch("desktop_pet.application_controller.MemoryService", return_value=FakeMemoryService()):
            self.session = ApplicationController(PetConfig(), settings=settings)
            spin_until(lambda: not self.session.memory.busy)
        self.source_id = str(uuid4())
        self.session.chat.store.append_message(self.source_id, "user", "我希望回复简洁。", "user",
                                               metadata={"request_status": "complete"})
        self.session.open_memory_window()
        spin_until(lambda: not self.session.memory.busy)

    def tearDown(self):
        self.session.shutdown()
        spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
        for window in (self.session.pet, self.session.memory_panel, self.session.chat_window):
            if window is not None:
                window.deleteLater()
        self.session.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.directory.cleanup()

    def test_source_signal_opens_exact_archive_without_losing_memory_window(self):
        panel = self.session.memory_panel
        panel.conversation_requested.emit(self.source_id)
        spin_until(lambda: not self.session.chat.history_loading)
        self.assertEqual(self.session.chat.current_conversation_id, self.source_id)
        self.assertEqual(self.session.chat.messages[0]["content"], "我希望回复简洁。")
        self.assertIs(self.session.memory_panel, panel)
        self.assertTrue(panel.isVisible())
        self.assertTrue(self.session.chat_window.isVisible())

    def test_deleted_source_reports_error_and_keeps_current_conversation(self):
        self.session.chat.store.delete_conversation(self.source_id)
        current = self.session.chat.current_conversation_id
        self.session.memory_panel.conversation_requested.emit(self.source_id)
        spin_until(lambda: not self.session.chat.history_loading)
        self.assertEqual(self.session.chat.current_conversation_id, current)
        self.assertTrue(self.session.chat.error)
        self.assertIsNone(self.session.chat.store.get_conversation(self.source_id))


if __name__ == "__main__":
    unittest.main()
