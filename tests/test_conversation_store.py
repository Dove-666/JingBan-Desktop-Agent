"""Real SQLite round trips, isolation and transactional screen associations."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import tempfile
import unittest
from uuid import uuid4

from desktop_pet.conversations.store import ConversationStore, SCHEMA_VERSION, StoreError


class ConversationStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "data" / "history.sqlite3"
        self.store = ConversationStore(self.path)
        self.cid = str(uuid4())

    def test_construction_is_lazy_and_empty_initialization_creates_no_sessions(self):
        self.assertFalse(self.path.exists())
        self.store.initialize()
        self.assertEqual(self.store.list_conversations(), [])
        self.assertIsNone(self.store.get_conversation(self.cid))
        self.assertEqual(self.store.load_messages(self.cid), [])

    def test_sessions_and_messages_survive_new_store_instance(self):
        first = self.store.append_message(self.cid, "user", "讨论鲸鱼娘 API 调试", "user")
        self.store.append_message(self.cid, "assistant", "可以，我们检查模型配置。", "assistant")
        self.store.close()
        restarted = ConversationStore(self.path)
        messages = restarted.load_messages(self.cid)
        self.assertEqual(messages[0]["id"], first["id"])
        self.assertEqual(len(messages), 2)
        self.assertEqual(restarted.get_conversation(self.cid)["message_count"], 2)
        self.assertEqual(restarted.get_conversation(self.cid)["title"], "讨论鲸鱼娘 API 调试")
        fresh = str(uuid4())
        self.assertNotEqual(fresh, self.cid)
        self.assertEqual(restarted.load_messages(fresh), [])

    def test_different_conversations_never_share_history(self):
        other = str(uuid4())
        self.store.append_message(self.cid, "user", "只属于 A", "user")
        self.store.append_message(other, "user", "只属于 B", "user")
        self.assertEqual([m["content"] for m in self.store.load_messages(self.cid)], ["只属于 A"])
        self.assertEqual([m["content"] for m in self.store.load_messages(other)], ["只属于 B"])

    def test_screen_comment_atomically_creates_event_and_assistant(self):
        message = self.store.append_screen_comment(self.cid, "这个报错可能与虚拟环境有关。",
            event_id="event-python", app_name="editor.exe", context_summary="编辑器显示 ModuleNotFoundError",
            event_type="error", metadata={"confidence": 0.95})
        self.assertEqual(message["source"], "proactive_screen")
        self.assertEqual(message["role"], "assistant")
        self.assertEqual(message["screen_event_id"], "event-python")
        event = self.store.get_screen_event("event-python", self.cid)
        self.assertEqual(event["message_id"], message["id"])
        self.assertEqual(event["comment"], message["content"])
        self.assertEqual(event["metadata"], {"confidence": 0.95})
        self.assertEqual(self.store.load_messages(self.cid)[0]["screen_event"], event)
        self.assertEqual(self.store.get_conversation(self.cid)["message_count"], 1)

    def test_failed_screen_transaction_leaves_no_event_or_empty_session(self):
        self.store.append_message(self.cid, "user", "existing", "user", message_id="duplicate")
        other = str(uuid4())
        with self.assertRaises(StoreError):
            self.store.append_screen_comment(other, "comment", event_id="should-rollback",
                app_name="synthetic", context_summary="summary", event_type="media", message_id="duplicate")
        self.assertIsNone(self.store.get_screen_event("should-rollback", other))
        self.assertIsNone(self.store.get_conversation(other))
        self.assertEqual(self.store.get_conversation(self.cid)["message_count"], 1)

    def test_event_lookup_requires_matching_conversation(self):
        self.store.append_screen_comment(self.cid, "comment", event_id="single-owner", app_name="editor",
            context_summary="an error", event_type="error")
        other = str(uuid4())
        self.assertIsNone(self.store.get_screen_event("single-owner", other))
        self.assertEqual(self.store.recent_screen_events(other), [])
        self.assertEqual(self.store.load_messages(other), [])

    def test_schema_rejects_cross_conversation_event_association(self):
        self.store.append_screen_comment(self.cid, "comment", event_id="event-a", app_name="editor",
            context_summary="an error", event_type="error")
        other = str(uuid4())
        self.store.append_message(other, "user", "other", "user")
        with sqlite3.connect(self.path) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("""INSERT INTO messages
                    (id, conversation_id, role, content, created_at, source, screen_event_id)
                    VALUES ('forged', ?, 'assistant', 'forged', '2026', 'proactive_screen', 'event-a')""",
                    (other,))

    def test_delete_cascades_messages_and_events_and_keeps_other_session(self):
        self.store.append_screen_comment(self.cid, "comment", event_id="deleted-event", app_name="editor",
            context_summary="summary", event_type="error")
        other = str(uuid4())
        self.store.append_message(other, "user", "keep", "user")
        self.store.delete_conversation(self.cid)
        self.assertIsNone(self.store.get_conversation(self.cid))
        self.assertEqual(self.store.load_messages(self.cid), [])
        self.assertEqual(self.store.recent_screen_events(self.cid), [])
        self.assertEqual(len(self.store.load_messages(other)), 1)
        self.store.delete_conversation(self.cid)  # Idempotent, useful during UI refresh races.

    def test_search_title_and_message_content_and_sort_by_latest_activity(self):
        older = str(uuid4())
        self.store.append_message(older, "user", "first", "user", created_at="2026-01-01T00:00:00Z")
        self.store.append_message(self.cid, "user", "鲸鱼娘编程", "user", created_at="2026-01-02T00:00:00Z")
        self.store.append_message(older, "assistant", "独有的 SQLite 索引讨论", "assistant",
                                  created_at="2026-01-03T00:00:00Z")
        self.assertEqual([c["id"] for c in self.store.list_conversations()], [older, self.cid])
        self.assertEqual([c["id"] for c in self.store.list_conversations("鲸鱼娘")], [self.cid])
        self.assertEqual([c["id"] for c in self.store.list_conversations("SQLite 索引")], [older])
        self.assertEqual(self.store.list_conversations("missing"), [])

    def test_search_is_literal_and_query_cannot_modify_sql(self):
        self.store.append_message(self.cid, "user", "contains 100% and under_score", "user")
        other = str(uuid4())
        self.store.append_message(other, "user", "plain", "user")
        self.assertEqual([c["id"] for c in self.store.list_conversations("%")], [self.cid])
        self.assertEqual([c["id"] for c in self.store.list_conversations("_")], [self.cid])
        self.assertEqual(self.store.list_conversations("' OR 1=1 --"), [])
        self.assertEqual(len(self.store.list_conversations()), 2)

    def test_generated_title_is_stable_and_manual_rename_wins(self):
        self.store.append_message(self.cid, "user", "local fallback", "user")
        self.store.update_metadata(self.cid, title="自动标题")
        self.store.update_metadata(self.cid, title="不得反复变化")
        self.assertEqual(self.store.get_conversation(self.cid)["title"], "自动标题")
        self.store.rename_conversation(self.cid, "我手动定的标题")
        self.store.update_metadata(self.cid, title="迟到的模型标题", summary="根据实际消息的概况")
        stored = self.store.get_conversation(self.cid)
        self.assertEqual(stored["title"], "我手动定的标题")
        self.assertTrue(stored["title_manual"])
        self.assertEqual(stored["summary"], "根据实际消息的概况")

    def test_summary_cursor_must_belong_to_same_conversation_and_persists(self):
        message = self.store.append_message(self.cid, "user", "本项目决定使用 SQLite", "user")
        other = str(uuid4())
        other_message = self.store.append_message(other, "user", "other", "user")
        with self.assertRaises(StoreError):
            self.store.update_metadata(self.cid, summary="invalid", summary_through_id=other_message["id"])
        self.store.update_metadata(self.cid, summary="用户决定会话存储采用 SQLite。",
                                   summary_through_id=message["id"])
        reopened = ConversationStore(self.path).get_conversation(self.cid)
        self.assertEqual(reopened["summary_through_id"], message["id"])
        self.assertEqual(reopened["summary"], "用户决定会话存储采用 SQLite。")

    def test_late_metadata_does_not_restore_cleared_session_material(self):
        old = self.store.append_message(self.cid, "user", "清空前的话题", "user")
        self.store.clear_messages(self.cid)
        self.store.append_message(self.cid, "user", "清空后的新话题", "user")
        self.store.update_metadata(self.cid, title="迟到的旧标题", summary="已经被清空的概况",
                                   guard_message_id=old["id"])
        conversation = self.store.get_conversation(self.cid)
        self.assertEqual(conversation["title"], "新对话")
        self.assertEqual(conversation["summary"], "")
        self.assertFalse(conversation["title_generated"])

    def test_metadata_guard_allows_concurrent_new_messages_in_same_chat(self):
        first = self.store.append_message(self.cid, "user", "项目使用 SQLite", "user")
        self.store.append_message(self.cid, "assistant", "继续新消息", "assistant")
        self.store.update_metadata(self.cid, title="讨论 SQLite", summary="用户选择 SQLite。",
                                   guard_message_id=first["id"])
        self.assertEqual(self.store.get_conversation(self.cid)["summary"], "用户选择 SQLite。")

    def test_clear_preserves_manual_title_but_hides_empty_history_entry(self):
        self.store.append_screen_comment(self.cid, "comment", event_id="clear-event", app_name="editor",
            context_summary="summary", event_type="error")
        self.store.rename_conversation(self.cid, "保留标题")
        self.store.update_metadata(self.cid, summary="some summary")
        self.store.clear_messages(self.cid)
        self.assertEqual(self.store.load_messages(self.cid), [])
        self.assertEqual(self.store.recent_screen_events(self.cid), [])
        self.assertEqual(self.store.list_conversations(), [])
        conversation = self.store.get_conversation(self.cid)
        self.assertEqual(conversation["title"], "保留标题")
        self.assertEqual(conversation["summary"], "")
        self.assertEqual(conversation["message_count"], 0)

    def test_migrate_version_one_preserves_data_and_populates_counts(self):
        self.path.parent.mkdir(parents=True)
        with sqlite3.connect(self.path) as connection:
            ConversationStore._migrate_v1(connection)
            connection.execute("INSERT INTO conversations VALUES ('old', '旧标题', '旧摘要', '2025', '2025', 'active')")
            connection.execute("""INSERT INTO messages
                (id, conversation_id, role, content, created_at, source)
                VALUES ('old-message', 'old', 'user', '保留旧数据', '2025', 'user')""")
            connection.execute("PRAGMA user_version = 1")
        self.store.initialize()
        self.assertEqual(self.store.get_conversation("old")["message_count"], 1)
        self.assertEqual(self.store.load_messages("old")[0]["content"], "保留旧数据")
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.store.initialize()
        self.assertEqual(len(self.store.load_messages("old")), 1)

    def test_future_version_is_rejected_without_changing_it(self):
        self.path.parent.mkdir(parents=True)
        with sqlite3.connect(self.path) as connection:
            connection.execute("PRAGMA user_version = 999")
        with self.assertRaisesRegex(StoreError, "更新版本"):
            self.store.initialize()
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 999)

    def test_parallel_workers_initialize_once_and_write_own_connections(self):
        def write(index):
            self.store.append_message(self.cid, "user", f"parallel {index}", "user")
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(write, range(30)))
        self.assertEqual(len(self.store.load_messages(self.cid)), 30)
        self.assertEqual(self.store.get_conversation(self.cid)["message_count"], 30)

    def test_events_are_bounded_ranked_and_only_current_session(self):
        for index in range(5):
            self.store.append_screen_comment(self.cid, "comment", event_id=f"event-{index}", app_name="editor",
                context_summary="Python error" if index == 0 else "ordinary media", event_type="media",
                observed_at=f"2026-01-0{index + 1}")
        self.assertEqual(len(self.store.recent_screen_events(self.cid)), 3)
        self.assertEqual(self.store.recent_screen_events(self.cid)[0]["id"], "event-4")
        self.assertEqual(self.store.recent_screen_events(self.cid, query="Python")[0]["id"], "event-0")

    def test_invalid_data_and_sqlite_errors_are_safe(self):
        with self.assertRaises(StoreError):
            self.store.append_message("", "user", "hello", "user")
        with self.assertRaises(StoreError):
            self.store.append_message(self.cid, "system", "hello", "assistant")
        with self.assertRaises(StoreError):
            self.store.append_message(self.cid, "assistant", "hello", "proactive_screen")
        with self.assertRaises(StoreError):
            self.store.append_message(self.cid, "user", "hello", "user", metadata={"bad": float("nan")})
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(b"PRIVATE-BAD-DATABASE-CONTENT")
        try:
            self.store.list_conversations()
        except StoreError as exc:
            self.assertNotIn("PRIVATE", str(exc))
            self.assertNotIn(str(self.path), str(exc))
        else:
            self.fail("Expected corrupted SQLite data to produce a safe error")


if __name__ == "__main__":
    unittest.main()
