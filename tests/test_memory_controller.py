"""Asynchronous memory facade tests, without Mem0, OS credentials or providers."""

from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import unittest

from PySide6.QtCore import QEvent, QThreadPool, QTimer
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication

from desktop_pet.chat_controller import ChatController
from desktop_pet.memory.settings import MemorySettings
from desktop_pet.memory.service import MemoryUnavailable
from desktop_pet.memory_controller import LazyMemoryService, MemoryController
from desktop_pet.settings_manager import ApiSettings
from tests.mock_api import MockApiServer
from tests.test_application import LOCAL_TEST_KEY, MemoryCredentialBackend, isolated_settings, spin_until


class FakeMemoryService:
    def __init__(self):
        self.settings = MemorySettings(user_id="controller-test-user")
        self.memories = [{"id": "memory-1", "memory": "用户偏好简洁解释"}]
        self.calls = []
        self.threads = []
        self.block = False
        self.entered = threading.Event()
        self.release = threading.Event()
        self.fail = False
        self.closed = False
        self.llm_key_saved = True
        self.embedding_key_saved = True

    def _call(self, name, *values):
        self.calls.append((name, *values))
        self.threads.append(threading.get_ident())

    def load_settings(self):
        self._call("load_settings")
        return self.settings

    def credential_status(self):
        self._call("credential_status")
        return (
            "独立提炼密钥：" + ("已保存" if self.llm_key_saved else "未保存")
            + "\nEmbedding 密钥：" + ("已保存" if self.embedding_key_saved else "未保存")
        )

    def save_settings(self, settings, *, llm_key="", embedding_key=""):
        self._call("save_settings", settings, llm_key, embedding_key)
        self.settings = settings

    def list_memories(self, query=""):
        self._call("list_memories", query)
        self.entered.set()
        if self.block:
            if not self.release.wait(5):
                raise AssertionError("Test gate not released")
        if self.fail:
            raise MemoryUnavailable("记忆服务暂不可用。")
        return [dict(item) for item in self.memories]

    def delete(self, memory_id):
        self._call("delete", memory_id)
        self.memories = [item for item in self.memories if item["id"] != memory_id]

    def clear(self):
        self._call("clear")
        self.memories.clear()

    def search(self, query, limit=4):
        self._call("search", query, limit)
        return self.memories[:limit]

    def clear_llm_key(self):
        self._call("clear_llm_key")
        self.llm_key_saved = False

    def clear_embedding_key(self):
        self._call("clear_embedding_key")
        self.embedding_key_saved = False

    def shutdown(self):
        self.closed = True
        self.release.set()


class MemoryControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.service = FakeMemoryService()
        self.controller = MemoryController(self.service)
        spin_until(lambda: not self.controller.busy)

    def tearDown(self):
        self.controller.shutdown()
        spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
        self.controller.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def test_initialization_and_sdk_operations_run_outside_gui_thread(self):
        gui_thread = threading.get_ident()
        self.assertEqual(self.controller.settings.user_id, "controller-test-user")
        callback_threads = []
        self.controller.memories_changed.connect(lambda: callback_threads.append(threading.get_ident()))
        self.assertTrue(self.controller.refresh("解释偏好"))
        spin_until(lambda: not self.controller.busy)
        self.assertTrue(self.service.threads)
        self.assertTrue(all(identifier != gui_thread for identifier in self.service.threads))
        self.assertEqual(callback_threads, [gui_thread])
        self.assertEqual(self.controller.memories[0]["id"], "memory-1")

    def test_deleting_from_filtered_memories_retains_the_same_local_filter(self):
        self.assertTrue(self.controller.refresh("偏好"))
        spin_until(lambda: not self.controller.busy)
        self.assertEqual(self.controller.memory_query, "偏好")
        self.assertTrue(self.controller.delete_memory("memory-1"))
        spin_until(lambda: not self.controller.busy)
        reads = [call for call in self.service.calls if call[0] == "list_memories"]
        self.assertEqual(reads[-1], ("list_memories", "偏好"))

    def test_automatic_save_notification_refreshes_filter_and_coalesces_while_busy(self):
        self.controller.refresh("偏好")
        spin_until(lambda: not self.controller.busy)
        self.service.block = True
        self.service.entered.clear()
        self.service.release.clear()
        self.controller.refresh("偏好")
        spin_until(lambda: self.service.entered.is_set())
        self.assertTrue(self.controller.notify_memories_saved())
        self.assertTrue(self.controller.notify_memories_saved())
        self.service.memories.append({"id": "saved", "memory": "新偏好"})
        self.service.release.set()
        spin_until(lambda: not self.controller.busy and not self.controller._pending_refresh)
        reads = [call for call in self.service.calls if call[0] == "list_memories"]
        self.assertEqual(reads, [("list_memories", "偏好")] * 3)
        self.assertEqual(self.controller.memory_query, "偏好")
        self.assertEqual(self.controller.memories[-1]["id"], "saved")

    def test_page_navigation_preserves_query_and_reports_owned_total(self):
        records = [{"id": str(i), "memory": "偏好"} for i in range(205)]
        def read_page(query, *, offset=0, page_size=100):
            self.service._call("page", query, offset)
            return {"results": records[offset:offset + page_size], "total": 1205, "matched_total": 205,
                    "offset": offset, "has_more": offset + page_size < len(records)}
        self.service.list_memories_page = read_page
        self.controller.refresh("偏好")
        spin_until(lambda: not self.controller.busy)
        self.assertEqual(self.controller.memory_total, 1205)
        self.assertTrue(self.controller.next_page())
        spin_until(lambda: not self.controller.busy)
        self.assertEqual(self.controller.memory_offset, 100)
        self.assertEqual(self.controller.memories[0]["id"], "100")
        self.controller.notify_memories_saved()
        spin_until(lambda: not self.controller.busy)
        self.assertEqual(self.service.calls[-1], ("page", "偏好", 100))
        self.assertTrue(self.controller.next_page())
        spin_until(lambda: not self.controller.busy)
        self.assertEqual(len(self.controller.memories), 5)
        self.assertFalse(self.controller.next_page())
        self.assertTrue(self.controller.previous_page())
        spin_until(lambda: not self.controller.busy)
        self.assertEqual(self.controller.memory_offset, 100)

    def test_credential_summary_survives_local_refresh_without_claiming_connectivity(self):
        previous = self.controller.credentials_status
        self.assertIn("Embedding 密钥：已保存", previous)
        changed = QSignalSpy(self.controller.credentials_status_changed)
        self.controller.refresh()
        spin_until(lambda: not self.controller.busy)
        self.assertEqual(self.controller.credentials_status, previous)
        self.assertEqual(changed.count(), 0)
        self.assertIn("从本地读取", self.controller.status)
        self.assertIn("未验证 Embedding 或提炼连接", self.controller.status)
        self.assertFalse(any(call[0] == "search" for call in self.service.calls))

    def test_enabled_switch_and_saved_credentials_do_not_report_verified_connection(self):
        self.controller.save_settings(replace(self.controller.settings, enabled=True))
        spin_until(lambda: not self.controller.busy)
        self.assertIn("开关已开启", self.controller.status)
        self.assertIn("连接尚未验证", self.controller.status)
        self.assertNotIn("成功", self.controller.status)
        self.assertIn("已保存", self.controller.credentials_status)
        self.assertFalse(any(call[0] == "search" for call in self.service.calls))

    def test_clearing_each_key_refreshes_summary_on_worker_and_delivers_on_gui(self):
        gui_thread = threading.get_ident()
        callbacks = []
        self.controller.credentials_status_changed.connect(
            lambda summary: callbacks.append((summary, threading.get_ident()))
        )
        self.controller.clear_embedding_key()
        spin_until(lambda: not self.controller.busy)
        self.assertEqual([call[0] for call in self.service.calls[-2:]], ["clear_embedding_key", "credential_status"])
        self.assertIn("Embedding 密钥：未保存", self.controller.credentials_status)
        self.assertIn("独立提炼密钥：已保存", self.controller.credentials_status)
        self.controller.clear_llm_key()
        spin_until(lambda: not self.controller.busy)
        self.assertEqual([call[0] for call in self.service.calls[-2:]], ["clear_llm_key", "credential_status"])
        self.assertIn("独立提炼密钥：未保存", self.controller.credentials_status)
        self.assertEqual(len(callbacks), 2)
        self.assertTrue(all(identifier == gui_thread for _, identifier in callbacks))
        self.assertTrue(all(identifier != gui_thread for identifier in self.service.threads))

    def test_slow_memory_call_keeps_qt_timer_running_and_rejects_overlap(self):
        self.service.block = True
        timer = QTimer()
        ticks = []
        timer.setInterval(5)
        timer.timeout.connect(lambda: ticks.append(True))
        timer.start()
        try:
            self.assertTrue(self.controller.refresh("偏好"))
            spin_until(lambda: self.service.entered.is_set() and len(ticks) >= 4)
            self.assertTrue(self.controller.busy)
            self.assertFalse(self.controller.clear_memories())
            self.assertFalse(self.controller.refresh())
            self.service.release.set()
            spin_until(lambda: not self.controller.busy)
            self.assertEqual(len([call for call in self.service.calls if call[0] == "list_memories"]), 1)
        finally:
            self.service.release.set()
            timer.stop()

    def test_failure_keeps_loaded_memories_and_reports_explicit_error(self):
        self.controller.refresh()
        spin_until(lambda: not self.controller.busy)
        previous = list(self.controller.memories)
        self.service.fail = True
        self.controller.refresh("网络失败")
        spin_until(lambda: not self.controller.busy)
        self.assertEqual(self.controller.memories, previous)
        self.assertIn("暂不可用", self.controller.error)
        self.assertIn("聊天与会话记录仍可使用", self.controller.status)

    def test_actual_service_delete_clear_and_credential_operations_are_called(self):
        self.controller.refresh()
        spin_until(lambda: not self.controller.busy)
        self.assertTrue(self.controller.delete_memory("memory-1"))
        spin_until(lambda: not self.controller.busy)
        self.assertIn(("delete", "memory-1"), self.service.calls)
        self.assertEqual(self.controller.memories, [])
        self.service.memories.append({"id": "second", "memory": "项目事实"})
        self.controller.clear_memories()
        spin_until(lambda: not self.controller.busy)
        self.assertIn(("clear",), self.service.calls)
        self.assertEqual(self.service.memories, [])
        self.controller.clear_llm_key()
        spin_until(lambda: not self.controller.busy)
        self.controller.clear_embedding_key()
        spin_until(lambda: not self.controller.busy)
        self.assertIn(("clear_llm_key",), self.service.calls)
        self.assertIn(("clear_embedding_key",), self.service.calls)

    def test_confirmed_delete_removes_cached_row_when_following_refresh_fails(self):
        self.service.memories = [{"id": str(index), "memory": "偏好"} for index in range(3)]
        self.controller.refresh("偏好")
        spin_until(lambda: not self.controller.busy)
        self.service.fail = True
        self.controller.delete_memory("1")
        spin_until(lambda: not self.controller.busy)
        self.assertEqual([row["id"] for row in self.service.memories], ["0", "2"])
        self.assertEqual([row["id"] for row in self.controller.memories], ["0", "2"])
        self.assertEqual(self.controller.memory_preferred_id, "2")
        self.assertTrue(self.controller.memory_list_stale)
        self.assertEqual((self.controller.memory_total, self.controller.memory_matched_total), (2, 2))
        self.assertIn("已经删除", self.controller.status)
        self.assertIn("缓存", self.controller.status)
        self.service.fail = False
        self.controller.refresh("偏好")
        spin_until(lambda: not self.controller.busy)
        self.assertFalse(self.controller.memory_list_stale)

    def test_real_delete_failure_keeps_the_row_and_does_not_claim_success(self):
        self.controller.refresh()
        spin_until(lambda: not self.controller.busy)
        def fail(_memory_id):
            raise MemoryUnavailable("合成删除失败。")
        self.service.delete = fail
        self.controller.delete_memory("memory-1")
        spin_until(lambda: not self.controller.busy)
        self.assertEqual(self.controller.memories[0]["id"], "memory-1")
        self.assertFalse(self.controller.memory_list_stale)
        self.assertNotIn("已经删除", self.controller.status)
        self.assertIn("删除失败", self.controller.error)

    def test_deleting_last_pages_only_row_focuses_preceding_pages_last_neighbor(self):
        self.service.memories = [{"id": str(index), "memory": "偏好"} for index in range(3)]
        self.controller.memory_page_size = 2
        def read(query, *, offset=0, page_size=2):
            rows = self.service.memories
            return {"results": rows[offset:offset + page_size], "total": len(rows), "matched_total": len(rows),
                    "offset": offset, "has_more": offset + page_size < len(rows)}
        self.service.list_memories_page = read
        self.controller.refresh("偏好", offset=2)
        spin_until(lambda: not self.controller.busy)
        self.controller.delete_memory("2")
        spin_until(lambda: not self.controller.busy)
        self.assertEqual(self.controller.memory_offset, 0)
        self.assertEqual(self.controller.memory_preferred_id, "1")
        self.assertEqual(self.controller.memory_query, "偏好")

    def test_pause_and_settings_are_saved_without_mutating_user_id(self):
        configuration = replace(self.controller.settings, enabled=True, automatic=True, embedding_model="independent")
        changed = QSignalSpy(self.controller.settings_changed)
        self.controller.save_settings(configuration, llm_key="test-only-llm", embedding_key="test-only-embedding")
        spin_until(lambda: not self.controller.busy)
        self.assertEqual(changed.count(), 1)
        self.assertEqual(self.controller.settings, configuration)
        self.assertEqual(self.service.calls[-3][0], "save_settings")
        self.assertEqual(self.service.calls[-3][2:], ("test-only-llm", "test-only-embedding"))
        self.controller.save_settings(replace(configuration, automatic=False))
        spin_until(lambda: not self.controller.busy)
        self.assertFalse(self.controller.settings.automatic)
        self.assertTrue(self.controller.settings.enabled)
        self.assertEqual(self.controller.settings.user_id, "controller-test-user")

    def test_connection_uses_real_service_search_and_disabled_configuration_is_not_success(self):
        self.controller.test_connection()
        spin_until(lambda: not self.controller.busy)
        self.assertIn("请先启用", self.controller.error)
        self.assertFalse(any(call[0] == "search" for call in self.service.calls))
        self.service.settings = replace(self.service.settings, enabled=True)
        self.controller.test_connection()
        spin_until(lambda: not self.controller.busy)
        self.assertEqual(self.controller.error, "")
        self.assertIn(("search", "记忆连接测试", 1), self.service.calls)
        self.assertIn("检索请求成功", self.controller.status)
        self.assertIn("需实际提炼验证", self.controller.status)

    def test_readiness_separates_local_configuration_from_actual_successful_checks(self):
        self.service.configuration_status = lambda: {"chat_configured": True, "embedding_configured": True,
            "extraction_configured": True, "embedding_provider": "openai", "uses_chat_model": True}
        self.controller.save_settings(replace(self.controller.settings, enabled=True))
        spin_until(lambda: not self.controller.busy)
        self.assertIn("配置完整", self.controller.readiness)
        self.assertIn("本面板未测试聊天连接", self.controller.readiness)
        self.assertIn("检索尚未测试", self.controller.readiness)
        self.assertIn("尚无成功提炼记录", self.controller.readiness)
        self.controller.test_connection()
        spin_until(lambda: not self.controller.busy)
        self.assertIn("检索已验证", self.controller.readiness)
        self.assertIn("尚无成功提炼记录", self.controller.readiness)
        self.controller.notify_memories_saved()
        spin_until(lambda: not self.controller.busy)
        self.assertIn("最近成功提炼", self.controller.readiness)
        self.controller.save_settings(replace(self.controller.settings, embedding_model="changed-model"))
        spin_until(lambda: not self.controller.busy)
        self.assertIn("检索尚未测试", self.controller.readiness)
        self.assertIn("尚无成功提炼记录", self.controller.readiness)

    def test_update_result_signal_keeps_failure_distinct_from_successful_retry(self):
        changed = QSignalSpy(self.controller.memory_update_finished)
        def update(memory_id, text, *, expected_text=None):
            if self.service.fail:
                raise MemoryUnavailable("Embedding 暂时不可用。")
            self.service.memories[0]["memory"] = text
        self.service.update = update
        self.service.fail = True
        self.controller.update_memory("memory-1", "新偏好", expected_text="用户偏好简洁解释")
        spin_until(lambda: not self.controller.busy)
        self.assertEqual(changed.at(0)[0:2], ["memory-1", False])
        self.assertIn("暂时不可用", changed.at(0)[2])
        self.service.fail = False
        self.controller.update_memory("memory-1", "新偏好", expected_text="用户偏好简洁解释")
        spin_until(lambda: not self.controller.busy)
        self.assertEqual(changed.at(1), ["memory-1", True, ""])
        self.assertEqual(self.controller.memories[0]["memory"], "新偏好")

    def test_completed_update_is_not_reported_unsaved_when_following_list_refresh_fails(self):
        changed = QSignalSpy(self.controller.memory_update_finished)
        self.service.update = lambda *args, **kwargs: self.service.memories[0].update(memory="已保存的新偏好")
        self.service.fail = True
        self.controller.update_memory("memory-1", "已保存的新偏好")
        spin_until(lambda: not self.controller.busy)
        self.assertEqual(changed.at(0), ["memory-1", True, ""])
        self.assertIn("纠正已经保存", self.controller.status)
        self.assertIn("列表刷新未完成", self.controller.status)

    def test_lazy_service_factory_is_created_once_on_worker(self):
        calls = []
        lazy = LazyMemoryService(lambda: (calls.append(threading.get_ident()), FakeMemoryService())[1])
        controller = MemoryController(lazy)
        try:
            spin_until(lambda: not controller.busy)
            controller.refresh()
            spin_until(lambda: not controller.busy)
            self.assertEqual(len(calls), 1)
            self.assertNotEqual(calls[0], threading.get_ident())
        finally:
            controller.shutdown()
            spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
            controller.deleteLater()

    def test_shutdown_cancels_ui_delivery_and_releases_slow_service(self):
        self.service.block = True
        changed = QSignalSpy(self.controller.memories_changed)
        self.controller.refresh()
        spin_until(lambda: self.service.entered.is_set())
        self.controller.shutdown()
        spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
        self.app.processEvents()
        self.assertEqual(changed.count(), 0)
        self.assertTrue(self.service.closed)
        self.assertFalse(self.controller.refresh())

    def test_memory_failure_does_not_disable_normal_chat(self):
        with tempfile.TemporaryDirectory(prefix="aura-memory-unavailable-") as directory, MockApiServer() as server:
            settings = isolated_settings(Path(directory) / "settings.ini", MemoryCredentialBackend())
            settings.save_api_settings(ApiSettings(server.base_url, "test-model", 5))
            settings.save_api_key(LOCAL_TEST_KEY)
            chat = ChatController(settings)
            try:
                self.service.fail = True
                self.controller.refresh()
                spin_until(lambda: not self.controller.busy)
                server.enqueue("记忆服务不可用时，我仍然可以聊天。")
                self.assertTrue(chat.send("你好"))
                spin_until(lambda: not chat.busy)
                self.assertFalse(chat.error, chat.error)
                self.assertIn("仍然可以聊天", chat.messages[-1]["content"])
                self.assertTrue(self.controller.error)
            finally:
                chat.shutdown()
                spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
                chat.deleteLater()


if __name__ == "__main__":
    unittest.main()
