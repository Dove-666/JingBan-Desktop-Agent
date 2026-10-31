"""Browser selection -> explicit question -> shared chat, with isolated state.

All pages are synthetic. The test inbox is local to a temporary settings path;
credentials are in memory, model responses are recorded, and optional remote,
tray and memory services cannot access the user's actual services or data.
"""

from copy import deepcopy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from PySide6.QtCore import QEvent, QThreadPool, Qt
from PySide6.QtNetwork import QLocalSocket
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from desktop_pet.api_client import ChatReply
from desktop_pet.application_controller import ApplicationController
from desktop_pet.config import PetConfig
from desktop_pet.selection.context import SelectionContext
from desktop_pet.selection.ipc import encode_frame
from desktop_pet.settings_manager import ApiSettings
from tests.test_application import LOCAL_TEST_KEY, MemoryCredentialBackend, isolated_settings, spin_until
from tests.test_memory_controller import FakeMemoryService


class RecordingClient:
    def __init__(self):
        self.requests = []
        self.closed = False

    def complete_context(self, settings, key, messages, **options):
        self.requests.append(deepcopy(messages))
        return ChatReply("这是针对合成选区的回答。", "neutral")

    def shutdown(self):
        self.closed = True


class SelectionApplicationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aura-selection-application-")
        self.path = Path(self.temp.name)
        self.settings = isolated_settings(self.path / "settings.ini", MemoryCredentialBackend())
        self.settings.save_api_settings(ApiSettings("http://127.0.0.1:1/v1", "synthetic-model", 3))
        self.settings.save_api_key(LOCAL_TEST_KEY)
        self.client = RecordingClient()
        self.remote = SimpleNamespace(shutdown=Mock())
        self.tray = SimpleNamespace(available=False, refresh=Mock(), shutdown=Mock())
        self.memory = FakeMemoryService()
        patches = (
            patch("desktop_pet.application_controller.RemoteController", return_value=self.remote),
            patch("desktop_pet.application_controller.DesktopTray", return_value=self.tray),
            patch("desktop_pet.application_controller.MemoryService", return_value=self.memory),
        )
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.session = ApplicationController(PetConfig(), settings=self.settings, api_client=self.client)
        self.session.pet_controller.asset_player.directory = self.path / "no-animation-files"
        spin_until(lambda: not self.session.chat.history_loading and not self.session.chat._runner._tasks)
        self.context = SelectionContext(
            text="合成选区：FOREIGN KEY constraint failed",
            title="合成数据库错误页面", url="https://example.invalid/sqlite",
            page_text="合成页面背景：messages 引用 conversations，需要先创建会话。",
            captured_at="2026-10-31T12:00:00+08:00", app_name="Synthetic Browser",
        )
        self.dialogs = []
        self.sockets = []

    def tearDown(self):
        for socket in self.sockets:
            socket.abort()
            socket.deleteLater()
        self.session.shutdown()
        spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
        for window in (self.session.pet, self.session.debug_panel, self.session.chat_window,
                       self.session.memory_panel):
            if window is not None:
                window.deleteLater()
        self.session.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.temp.cleanup()

    def open_dialog(self, request_id=None):
        request_id = request_id or str(uuid4())
        self.assertTrue(self.session.open_selection_question(self.context, request_id))
        dialog = self.session._selection_dialogs[request_id]
        self.dialogs.append(dialog)
        self.app.processEvents()
        return request_id, dialog

    def test_native_menu_inbox_opens_composer_without_request_or_archive_write(self):
        self.assertTrue(self.session.selection_inbox._server.isListening())
        request_id = str(uuid4())
        socket = QLocalSocket()
        self.sockets.append(socket)
        socket.connectToServer(self.session.selection_inbox.name)
        spin_until(lambda: socket.state() == QLocalSocket.LocalSocketState.ConnectedState)
        socket.write(encode_frame({"version": 1, "action": "ask_selection", "request_id": request_id,
                                   "context": self.context.to_dict()}))
        socket.flush()
        spin_until(lambda: request_id in self.session._selection_dialogs)
        dialog = self.session._selection_dialogs[request_id]
        self.assertTrue(dialog.isVisible())
        self.assertEqual(dialog.selection_preview.toPlainText(), self.context.text)
        self.assertEqual(dialog.page_preview.toPlainText(), self.context.page_text)
        self.assertEqual(self.client.requests, [])
        self.assertEqual(self.session.chat.messages, [])
        self.assertEqual(self.session.chat.store.list_conversations(), [])
        self.assertIsNone(self.session.chat_window)

    def test_busy_chat_leaves_question_and_snapshot_available_to_retry(self):
        _, dialog = self.open_dialog()
        draft = "请解释这条合成错误。"
        dialog.input.setPlainText(draft)
        self.session.chat._set_busy(True)
        try:
            QTest.mouseClick(dialog.send_button, Qt.MouseButton.LeftButton)
            self.assertTrue(dialog.isVisible())
            self.assertEqual(dialog.input.toPlainText(), draft)
            self.assertEqual(dialog.context, self.context)
            self.assertTrue(dialog.error_label.isVisible())
            self.assertIn("其他请求", dialog.error_label.text())
            self.assertTrue(dialog.send_button.isEnabled())
            self.assertEqual(self.client.requests, [])
        finally:
            self.session.chat._set_busy(False)

    def test_send_uses_current_conversation_snapshot_and_existing_chat_window(self):
        conversation_id = self.session.chat.current_conversation_id
        self.session.open_chat_window()
        original_window = self.session.chat_window
        original_window.chat_panel._activate_native()
        original_window.close()
        request_id, dialog = self.open_dialog()
        dialog.input.setPlainText("为什么出现这个错误？")
        with patch.object(self.session.chat, "send", wraps=self.session.chat.send) as sent:
            QTest.mouseClick(dialog.send_button, Qt.MouseButton.LeftButton)
            self.assertEqual(sent.call_count, 1)
            self.assertEqual(sent.call_args.kwargs["conversation_id"], conversation_id)
            self.assertEqual(sent.call_args.kwargs["selection_context"], self.context)
        self.assertIs(self.session.chat_window, original_window)
        self.assertTrue(original_window.isVisible())
        self.assertNotIn(request_id, self.session._selection_dialogs)
        spin_until(lambda: not self.session.chat.busy and not self.session.chat._runner._tasks)
        self.assertEqual(len(self.client.requests), 1)
        payload = str(self.client.requests[0])
        self.assertIn("FOREIGN KEY constraint failed", payload)
        self.assertIn("先创建会话", payload)
        self.assertEqual(self.session.chat.current_conversation_id, conversation_id)
        messages = self.session.chat.store.load_messages(conversation_id)
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0]["metadata"]["selection_context"]["text"], self.context.text)
        self.assertEqual(messages[1]["content"], "这是针对合成选区的回答。")

    def test_unconfigured_api_retains_draft_and_shows_actual_rejection(self):
        self.settings.clear_api_key()
        _, dialog = self.open_dialog()
        dialog.input.setPlainText("这是未配置时保留的草稿")
        QTest.mouseClick(dialog.send_button, Qt.MouseButton.LeftButton)
        self.assertTrue(dialog.isVisible())
        self.assertTrue(dialog.error_label.isVisible())
        self.assertEqual(dialog.input.toPlainText(), "这是未配置时保留的草稿")
        self.assertTrue(dialog.send_button.isEnabled())
        self.assertEqual(self.client.requests, [])
        self.assertEqual(self.session.chat.messages, [])

    def test_three_composer_limit_deduplicates_request_and_close_releases_slot(self):
        first_id, first = self.open_dialog()
        self.open_dialog()
        self.open_dialog()
        self.assertEqual(len(self.session._selection_dialogs), 3)
        self.assertTrue(self.session.open_selection_question(self.context, first_id))
        self.assertIs(self.session._selection_dialogs[first_id], first)
        fourth_id = str(uuid4())
        self.assertFalse(self.session.open_selection_question(self.context, fourth_id))
        first.close()
        self.assertNotIn(first_id, self.session._selection_dialogs)
        self.assertTrue(self.session.open_selection_question(self.context, fourth_id))
        self.assertEqual(len(self.session._selection_dialogs), 3)
        self.assertEqual(self.client.requests, [])

    def test_shutdown_closes_inbox_dialogs_and_all_workers_without_accepting_late_open(self):
        self.open_dialog()
        self.open_dialog()
        inbox = self.session.selection_inbox
        self.session.shutdown()
        spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
        self.assertTrue(inbox._closed)
        self.assertFalse(inbox._server.isListening())
        self.assertEqual(inbox._clients, {})
        self.assertEqual(self.session._selection_dialogs, {})
        self.assertFalse(self.session.open_selection_question(self.context, str(uuid4())))
        self.assertTrue(self.client.closed)
        self.remote.shutdown.assert_called_once()
        self.tray.shutdown.assert_called_once()
        self.assertEqual(self.session.shutdown_errors, [])


if __name__ == "__main__":
    unittest.main()
