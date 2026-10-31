"""Complete temporary-DB exports and GUI coordination; no production data."""

import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from PySide6.QtCore import QEvent, QThreadPool, QTimer, QUrl, Signal
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from desktop_pet.chat_controller import ChatController
from desktop_pet.conversations import ConversationStore
from desktop_pet.conversations.export import ExportError, export_conversation_file, suggested_export_filename
from desktop_pet.ui.chat_panel import ChatPanel
from tests.test_application import MemoryCredentialBackend, isolated_settings, spin_until
from tests.test_session_ui import SessionChat


class ConversationExportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="aura-export-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.store = ConversationStore(self.directory / "archive.sqlite3")
        self.cid = str(uuid4())

    def populate(self):
        self.store.append_message(self.cid, "user", "请检查 Python 环境。\n完整保留换行。", "user",
                                  metadata={"api_key": "synthetic-secret-only", "screenshot": "data:image/png;base64,SYNTHETIC_ONLY"})
        self.store.append_message(self.cid, "assistant", "先确认虚拟环境。", "assistant")
        self.store.append_screen_comment(self.cid, "发现一个导入错误。", event_id="event-local-only",
                                         app_name="Code.exe", context_summary="编辑器显示 ModuleNotFoundError。",
                                         event_type="coding_error", metadata={"image_base64": "SYNTHETIC_SCREENSHOT"})
        self.store.update_metadata(self.cid, title="调试鲸鱼娘环境", summary="用户正在排查 Python 依赖。")
        self.store.append_message(str(uuid4()), "user", "属于另一个会话，不能导出。", "user")

    def test_json_exports_whitelisted_full_messages_and_linked_event(self):
        self.populate()
        destination = self.directory / "conversation.json"
        result = export_conversation_file(self.store.path, self.cid, destination, "json")
        text = destination.read_text(encoding="utf-8")
        payload = json.loads(text)
        self.assertEqual(result["message_count"], 3)
        self.assertEqual(payload["conversation"]["id"], self.cid)
        self.assertEqual(payload["messages"][0]["content"], "请检查 Python 环境。\n完整保留换行。")
        comment = payload["messages"][-1]
        self.assertEqual(comment["source"], "proactive_screen")
        self.assertEqual(comment["screen_event"]["id"], "event-local-only")
        self.assertEqual(comment["screen_event"]["conversation_id"], self.cid)
        self.assertIn("ModuleNotFoundError", comment["screen_event"]["context_summary"])
        self.assertNotIn("属于另一个会话", text)
        for forbidden in ("metadata", "synthetic-secret-only", "data:image", "SYNTHETIC_SCREENSHOT", "api_key"):
            self.assertNotIn(forbidden, text)

    def test_markdown_contains_summary_sources_readable_context_and_safe_html(self):
        self.populate()
        self.store.append_message(self.cid, "assistant", '<img src="https://example.invalid/a.png">\n**文字格式保留**', "assistant")
        destination = self.directory / "conversation.md"
        export_conversation_file(self.store.path, self.cid, destination)
        text = destination.read_text(encoding="utf-8")
        self.assertIn("# 调试鲸鱼娘环境", text)
        self.assertIn("## 会话概况", text)
        self.assertIn("来自屏幕感知", text)
        self.assertIn("proactive_screen", text)
        self.assertIn("> 应用：Code.exe", text)
        self.assertIn("ModuleNotFoundError", text)
        self.assertIn("请检查 Python 环境。\n完整保留换行。", text)
        self.assertIn("&lt;img", text)
        self.assertNotIn('<img src=', text)
        self.assertIn("**文字格式保留**", text)

    def test_export_reads_all_80_messages_without_context_or_view_limits(self):
        for index in range(80):
            self.store.append_message(self.cid, "user", f"原始消息 {index}", "user")
        destination = self.directory / "full.json"
        export_conversation_file(self.store.path, self.cid, destination, "json")
        messages = json.loads(destination.read_text(encoding="utf-8"))["messages"]
        self.assertEqual(len(messages), 80)
        self.assertEqual(messages[0]["content"], "原始消息 0")
        self.assertEqual(messages[-1]["content"], "原始消息 79")

    def test_inline_image_data_is_omitted_and_markdown_image_is_not_activated(self):
        self.store.append_message(self.cid, "user",
            "看这张图：![截图](data:image/png;base64,SYNTHETIC_IMAGE_ONLY)\n![外部图](https://example.invalid/image.png)", "user")
        for format in ("markdown", "json"):
            destination = self.directory / ("images.md" if format == "markdown" else "images.json")
            export_conversation_file(self.store.path, self.cid, destination, format)
            text = destination.read_text(encoding="utf-8")
            self.assertNotIn("data:image", text)
            self.assertNotIn("SYNTHETIC_IMAGE_ONLY", text)
            self.assertIn("图片数据未导出", text)
            if format == "markdown":
                self.assertIn("&#33;[外部图]", text)

    def test_failed_atomic_replace_keeps_existing_file_archive_and_cleans_temp(self):
        self.populate()
        destination = self.directory / "existing.md"
        destination.write_text("原文件不能损坏", encoding="utf-8")
        before = self.store.load_messages(self.cid)
        with patch("desktop_pet.conversations.export.os.replace", side_effect=OSError("synthetic private path")):
            with self.assertRaises(ExportError) as raised:
                export_conversation_file(self.store.path, self.cid, destination)
        self.assertNotIn("synthetic private path", str(raised.exception))
        self.assertEqual(destination.read_text(encoding="utf-8"), "原文件不能损坏")
        self.assertEqual(self.store.load_messages(self.cid), before)
        self.assertEqual(list(self.directory.glob(".aura-export-*")), [])

    def test_missing_empty_invalid_format_and_extension_leave_no_output(self):
        missing_db = self.directory / "not-created.sqlite3"
        destination = self.directory / "empty.json"
        with self.assertRaises(ExportError):
            export_conversation_file(missing_db, self.cid, destination, "json")
        self.assertFalse(missing_db.exists())
        self.store.initialize()
        with self.assertRaises(ExportError):
            export_conversation_file(self.store.path, self.cid, destination, "json")
        for format, filename in (("xml", "empty.xml"), ("json", "settings.ini")):
            with self.assertRaises(ExportError):
                export_conversation_file(self.store.path, self.cid, self.directory / filename, format)
        self.assertFalse(destination.exists())
        self.assertEqual(self.store.list_conversations(), [])

    def test_single_read_snapshot_survives_concurrent_conversation_delete(self):
        self.populate()
        connect = sqlite3.connect
        deleted = []
        def instrumented_connect(*args, **kwargs):
            connection = connect(*args, **kwargs)
            if kwargs.get("uri"):
                def trace(statement):
                    if "FROM messages" in statement and not deleted:
                        deleted.append(True)
                        self.store.delete_conversation(self.cid)
                connection.set_trace_callback(trace)
            return connection
        destination = self.directory / "consistent.json"
        with patch("desktop_pet.conversations.export.sqlite3.connect", side_effect=instrumented_connect):
            export_conversation_file(self.store.path, self.cid, destination, "json")
        payload = json.loads(destination.read_text(encoding="utf-8"))
        self.assertEqual(len(payload["messages"]), 3)
        self.assertEqual(payload["messages"][-1]["screen_event"]["id"], "event-local-only")
        self.assertIsNone(self.store.get_conversation(self.cid))

    def test_suggested_filename_removes_windows_path_controls_and_reserved_names(self):
        for title in ('../\\AURA:<>"|?*\n\x00', "CON", "LPT1.txt", " . ", "A" * 400):
            filename = suggested_export_filename(title, self.cid)
            self.assertTrue(filename.endswith(".md"))
            self.assertLess(len(filename), 100)
            self.assertNotRegex(filename, r'[<>:"/\\|?*\x00-\x1f]')
            self.assertNotIn(filename.split(".")[0].upper(), {"CON", "LPT1"})


class ControllerExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="aura-controller-export-")
        self.directory = Path(self.temporary.name)
        self.store = ConversationStore(self.directory / "archive.sqlite3")
        self.cid = str(uuid4())
        for index in range(30):
            self.store.append_message(self.cid, "user", f"已保存的原始消息 {index}", "user")
        settings = isolated_settings(self.directory / "settings.ini", MemoryCredentialBackend())
        self.chat = ChatController(settings, store=self.store)
        self.chat.open_conversation(self.cid)
        spin_until(lambda: not self.chat.history_loading)

    def tearDown(self):
        self.chat.shutdown()
        spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
        self.chat.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.temporary.cleanup()

    def test_export_is_async_complete_captured_and_nonduplicated_while_switching(self):
        entered, release = threading.Event(), threading.Event()
        heartbeats = []
        timer = QTimer()
        timer.setInterval(3)
        timer.timeout.connect(lambda: heartbeats.append(True))
        timer.start()
        self.chat._messages = self.chat.messages[-1:]  # A bounded view must not bound the archive export.
        real_export = export_conversation_file
        def delayed_export(*args):
            entered.set()
            release.wait(timeout=3)
            return real_export(*args)
        destination = self.directory / "captured.json"
        finished = QSignalSpy(self.chat.export_finished)
        try:
            with patch("desktop_pet.chat_controller.export_conversation_file", side_effect=delayed_export):
                self.assertTrue(self.chat.export_conversation(self.cid, destination, "json"))
                spin_until(entered.is_set)
                self.assertTrue(self.chat.export_busy)
                self.assertFalse(self.chat.export_conversation(self.cid, destination, "json"))
                other_id = self.chat.new_conversation()
                spin_until(lambda: len(heartbeats) >= 3)
                release.set()
                spin_until(lambda: finished.count() == 1)
            self.assertFalse(self.chat.export_busy)
            self.assertEqual(finished.at(0), [self.cid, str(destination), ""])
            payload = json.loads(destination.read_text(encoding="utf-8"))
            self.assertEqual(len(payload["messages"]), 30)
            self.assertEqual(payload["conversation"]["id"], self.cid)
            self.assertEqual(self.chat.current_conversation_id, other_id)
            self.assertEqual(self.chat.messages, [])
        finally:
            release.set()
            timer.stop()

    def test_export_failure_reports_without_erasing_records_or_chat_error(self):
        before = self.store.load_messages(self.cid)
        finished = QSignalSpy(self.chat.export_finished)
        destination = self.directory / "missing-directory" / "failure.md"
        self.assertTrue(self.chat.export_conversation(self.cid, destination))
        spin_until(lambda: finished.count() == 1)
        self.assertTrue(finished.at(0)[2])
        self.assertFalse(destination.exists())
        self.assertEqual(self.store.load_messages(self.cid), before)
        self.assertFalse(self.chat.error)

    def test_busy_loading_and_closed_controller_refuse_export(self):
        destination = self.directory / "refused.md"
        self.chat._busy = True
        self.assertFalse(self.chat.export_conversation(self.cid, destination))
        self.chat._busy = False
        self.chat._history_loading = True
        self.assertFalse(self.chat.export_conversation(self.cid, destination))
        self.chat._history_loading = False
        self.chat.shutdown()
        self.assertFalse(self.chat.export_conversation(self.cid, destination))
        self.assertFalse(destination.exists())


class ExportUiController(SessionChat):
    export_busy_changed = Signal(bool)
    export_finished = Signal(str, str, str)

    def __init__(self):
        super().__init__()
        self.export_busy = False

    def export_conversation(self, conversation_id, destination, format="markdown"):
        self.calls.append(("export", conversation_id, destination, format))
        self.export_busy = True
        self.export_busy_changed.emit(True)
        return True


class ExportUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.chat = ExportUiController()
        self.settings_calls = []
        self.panel = ChatPanel(self.chat, settings_opener=lambda: self.settings_calls.append("open-api"))
        self.panel.resize(850, 650)
        self.panel.show()
        self.app.processEvents()

    def tearDown(self):
        self.panel.close()
        self.panel.deleteLater()
        self.app.processEvents()

    def test_cancel_file_picker_changes_no_state_or_records(self):
        before = list(self.chat.messages)
        with patch.object(QFileDialog, "getSaveFileName", return_value=("", "")):
            self.panel.export_button.click()
        self.assertFalse(self.chat.calls)
        self.assertEqual(self.chat.messages, before)
        self.assertTrue(self.panel.export_status_label.isHidden())

    def test_picker_captures_session_before_switch_and_json_extension(self):
        def choose(*args):
            self.chat.new_conversation()
            return "temporary-conversation-export", "JSON 对话 (*.json)"
        with patch.object(QFileDialog, "getSaveFileName", side_effect=choose):
            self.panel.export_button.click()
        self.assertEqual(self.chat.calls[-1], ("export", "current", "temporary-conversation-export.json", "json"))
        self.assertEqual(self.chat.current_conversation_id, "new-session")
        self.chat.export_busy = False
        self.chat.export_finished.emit("current", "temporary-conversation-export.json", "")
        self.assertIn("开发桌宠", self.panel.export_status_label.text())
        self.assertNotIn("已导出「新对话」", self.panel.export_status_label.text())

    def test_export_button_disabled_for_empty_loading_busy_and_running_export(self):
        self.assertTrue(self.panel.export_button.isEnabled())
        for property in ("busy", "history_loading", "export_busy"):
            setattr(self.chat, property, True)
            self.panel._update_buttons()
            self.assertFalse(self.panel.export_button.isEnabled(), property)
            setattr(self.chat, property, False)
        self.chat.messages = []
        self.chat.messages_changed.emit()
        self.assertFalse(self.panel.export_button.isEnabled())

    def test_appended_extension_existing_target_cancel_does_not_export_or_change_file(self):
        with tempfile.TemporaryDirectory(prefix="aura-export-picker-") as directory:
            typed = Path(directory) / "existing"
            actual = Path(str(typed) + ".json")
            actual.write_text("保留已有文件", encoding="utf-8")
            with patch.object(QFileDialog, "getSaveFileName", return_value=(str(typed), "JSON 对话 (*.json)")):
                with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No) as confirm:
                    self.panel.export_button.click()
            self.assertEqual(confirm.call_args.args[-1], QMessageBox.StandardButton.No)
            self.assertFalse(self.chat.calls)
            self.assertEqual(actual.read_text(encoding="utf-8"), "保留已有文件")
            self.assertTrue(self.panel.export_status_label.isHidden())

    def test_appended_extension_existing_target_exports_only_after_explicit_yes(self):
        with tempfile.TemporaryDirectory(prefix="aura-export-picker-") as directory:
            typed = Path(directory) / "existing"
            actual = Path(str(typed) + ".md")
            actual.write_text("existing-file", encoding="utf-8")
            with patch.object(QFileDialog, "getSaveFileName", return_value=(str(typed), "Markdown 对话 (*.md)")):
                with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
                    self.panel.export_button.click()
            self.assertEqual(self.chat.calls[-1], ("export", "current", str(actual), "markdown"))

    def test_picker_already_confirmed_exact_target_does_not_ask_twice(self):
        with tempfile.TemporaryDirectory(prefix="aura-export-picker-") as directory:
            actual = Path(directory) / "existing.json"
            actual.write_text("existing-file", encoding="utf-8")
            with patch.object(QFileDialog, "getSaveFileName", return_value=(str(actual), "JSON 对话 (*.json)")):
                with patch.object(QMessageBox, "question") as confirm:
                    self.panel.export_button.click()
            confirm.assert_not_called()
            self.assertEqual(self.chat.calls[-1], ("export", "current", str(actual), "json"))

    def test_export_error_is_plain_visible_and_does_not_offer_chat_retry(self):
        self.chat.export_finished.emit("current", "not-written.md", "导出失败 <b>合成测试</b>")
        self.assertIn("<b>合成测试</b>", self.panel.export_status_label.text())
        self.assertTrue(self.panel.export_status_label.isVisible())
        self.assertTrue(self.panel.retry_button.isHidden())

    def test_api_settings_menu_and_fixed_empty_link_use_opener_only(self):
        self.panel.settings_button.click()
        self.panel._open_event(QUrl("aura-settings:open"))
        self.panel._open_event(QUrl("aura-settings:open?unexpected=1"))
        self.panel._open_event(QUrl("https://example.invalid"))
        self.assertEqual(self.settings_calls, ["open-api", "open-api"])
        self.chat.messages = []
        self.chat.messages_changed.emit()
        self.assertIn('href="aura-settings:open"', self.panel.transcript.toHtml())
        self.chat.messages = [{"role": "assistant", "content": '<a href="aura-settings:open">假冒按钮</a>'}]
        self.chat.messages_changed.emit()
        self.assertNotIn('href="aura-settings:open"', self.panel.transcript.toHtml())


if __name__ == "__main__":
    unittest.main()
