"""Mixed-offset visual/manual timelines using temporary SQLite and real Qt jobs."""

from __future__ import annotations

from copy import deepcopy
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from uuid import uuid4

from PySide6.QtCore import QEvent, QThreadPool
from PySide6.QtWidgets import QApplication

from desktop_pet.api_client import ChatReply
from desktop_pet.chat_controller import ChatController
from desktop_pet.conversations import ConversationStore
from desktop_pet.conversations.export import export_conversation_file
from desktop_pet.settings_manager import ApiSettings
from tests.test_application import LOCAL_TEST_KEY, MemoryCredentialBackend, isolated_settings, spin_until


class ImmediateClient:
    """No network: exercise the ordinary request worker and reply transaction."""

    def complete_context(self, settings, key, messages, *, output_tokens):
        return ChatReply("合成普通回复", "neutral")

    def shutdown(self):
        pass


class VisualChatPersistenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="aura-visual-manual-order-")
        self.root = Path(self.directory.name)
        self.settings = isolated_settings(self.root / "settings.ini", MemoryCredentialBackend())
        self.settings.save_api_settings(ApiSettings("https://example.invalid/v1", "synthetic-model", 5))
        self.settings.save_api_key(LOCAL_TEST_KEY)
        self.store = ConversationStore(self.root / "synthetic-archive.sqlite3")
        self.chat = ChatController(self.settings, api_client=ImmediateClient(), store=self.store)
        self.cid = self.chat.current_conversation_id
        spin_until(lambda: not self.chat._runner._tasks)

    def tearDown(self):
        self.chat.shutdown()
        spin_until(lambda: QThreadPool.globalInstance().activeThreadCount() == 0)
        self.chat.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.directory.cleanup()

    def seed_timeline(self, timestamps, source_kind="screen"):
        visual = self.store.append_screen_comment(
            self.cid, "合成主动评论", event_id=str(uuid4()), app_name="synthetic-device",
            context_summary="合成可见情境", event_type="interesting", source_kind=source_kind,
            observed_at=timestamps[0],
        )
        user = self.store.append_message(
            self.cid, "user", "合成手动问题", "user", created_at=timestamps[1],
            metadata={"request_status": "complete"},
        )
        reply = self.store.append_message(
            self.cid, "assistant", "合成已保存回答", "assistant", created_at=timestamps[2],
            metadata={"reply_to": user["id"]},
        )
        # Preserve actual legacy representations even if new writes start
        # normalizing timestamps. Reading old archives must work independently.
        with closing(sqlite3.connect(self.store.path)) as connection, connection:
            for row, timestamp in zip((visual, user, reply), timestamps):
                connection.execute("UPDATE messages SET created_at=? WHERE id=?", (timestamp, row["id"]))
                row["created_at"] = timestamp
        return [visual, user, reply]

    def test_read_orders_cross_date_local_visual_before_later_utc_manual_turn(self):
        expected = self.seed_timeline([
            "2026-10-31T03:12:00+08:00", "2026-10-30T19:15:00+00:00",
            "2026-10-30T19:15:01.200000+00:00",
        ])
        actual = self.store.load_messages(self.cid)
        self.assertEqual(len(actual), 3, "The manual turn is persisted, not discarded")
        self.assertEqual([row["id"] for row in actual], [row["id"] for row in expected])

    def test_read_orders_same_date_camera_offset_before_later_zulu_manual_turn(self):
        expected = self.seed_timeline([
            "2026-10-31T11:00:00+08:00", "2026-10-31T03:05:00Z",
            "2026-10-31T03:05:01.000000+00:00",
        ], source_kind="camera")
        actual = self.store.load_messages(self.cid)
        self.assertEqual([row["id"] for row in actual], [row["id"] for row in expected])

    def test_equal_instants_keep_insertion_order_across_precision_and_offset_spelling(self):
        timestamps = ["2026-10-31T03:00:00Z", "2026-10-31T03:00:00.000000+00:00",
                      "2026-10-31T11:00:00+08:00", "2026-10-31T03:00:00+00:00"]
        ids = []
        for index, timestamp in enumerate(timestamps):
            message = self.store.append_message(
                self.cid, "user", "same instant " + str(index), "user", created_at=timestamp,
            )
            ids.append(message["id"])
        with closing(sqlite3.connect(self.store.path)) as connection, connection:
            for message_id, timestamp in zip(ids, timestamps):
                connection.execute("UPDATE messages SET created_at=? WHERE id=?", (timestamp, message_id))
        self.assertEqual([row["id"] for row in self.store.load_messages(self.cid)], ids)

    def test_delayed_raw_visual_snapshot_preserves_manual_reply_in_chronological_order(self):
        expected = self.seed_timeline([
            "2026-10-31T03:12:00+08:00", "2026-10-30T19:15:00+00:00",
            "2026-10-30T19:15:01.200000+00:00",
        ])
        # The visual save read its one-row snapshot before the ordinary reply
        # callback installed the newer rows. Deliver that old snapshot last.
        self.chat._messages = deepcopy(expected[1:])
        self.chat._install_snapshot(self.cid, self.store.get_conversation(self.cid), expected[:1])
        self.assertEqual({row["id"] for row in self.chat.messages}, {row["id"] for row in expected})
        self.assertEqual([row["id"] for row in self.chat.messages], [row["id"] for row in expected])

    def test_reloading_legacy_mixed_offset_conversation_keeps_manual_answer_last(self):
        expected = self.seed_timeline([
            "2026-10-31T03:12:00+08:00", "2026-10-30T19:15:00+00:00",
            "2026-10-30T19:15:01.200000+00:00",
        ], source_kind="camera")
        self.assertTrue(self.chat.open_conversation(self.cid))
        spin_until(lambda: not self.chat.history_loading)
        self.assertFalse(self.chat.error)
        self.assertEqual([row["id"] for row in self.chat.messages], [row["id"] for row in expected])

    def test_real_manual_send_after_local_visual_comment_is_last_before_and_after_reload(self):
        observed = datetime.now(timezone.utc) - timedelta(minutes=2)
        local_observed = observed.astimezone(timezone(timedelta(hours=8))).isoformat(timespec="seconds")
        reference = self.chat.record_screen_comment(
            self.cid, "合成摄像头评论", event_id=str(uuid4()), app_name="synthetic-camera",
            context_summary="合成相机画面", event_type="interesting", source_kind="camera",
            observed_at=local_observed,
        )
        self.assertIsNotNone(reference)
        spin_until(lambda: len(self.chat.messages) == 1)
        self.assertTrue(self.chat.send("这条手动问题应该显示在评论之后"))
        spin_until(lambda: not self.chat.busy)
        self.assertFalse(self.chat.error)
        archived = self.store.load_messages(self.cid)
        self.assertEqual(len(archived), 3)
        self.assertEqual(sum(row["source"] == "user" for row in archived), 1)
        self.assertEqual(sum(row["source"] == "assistant" for row in archived), 1)
        expected_sources = ["proactive_camera", "user", "assistant"]
        self.assertEqual([row["source"] for row in self.chat.messages], expected_sources)
        self.assertEqual([row["source"] for row in archived], expected_sources)
        self.assertTrue(self.chat.open_conversation(self.cid))
        spin_until(lambda: not self.chat.history_loading)
        self.assertEqual([row["source"] for row in self.chat.messages], expected_sources)

    def test_conversation_updated_at_advances_to_later_utc_instant_across_local_date(self):
        self.store.append_screen_comment(
            self.cid, "old visual", event_id="old-local-event", app_name="synthetic",
            context_summary="old visual", event_type="interesting",
            observed_at="2026-10-31T03:12:00+08:00",
        )
        self.store.append_message(
            self.cid, "user", "later manual", "user", created_at="2026-10-30T19:15:00+00:00",
        )
        self.assertEqual(self.store.get_conversation(self.cid)["updated_at"], "2026-10-30T19:15:00+00:00")

    def test_delayed_local_visual_does_not_move_updated_at_before_newer_utc_manual(self):
        self.store.append_message(
            self.cid, "user", "newer manual", "user", created_at="2026-10-30T19:15:00+00:00",
        )
        self.store.append_screen_comment(
            self.cid, "delayed camera", event_id="delayed-local-event", app_name="synthetic-camera",
            context_summary="old camera frame", event_type="interesting", source_kind="camera",
            observed_at="2026-10-31T03:12:00+08:00",
        )
        self.assertEqual(self.store.get_conversation(self.cid)["updated_at"], "2026-10-30T19:15:00+00:00")

    def test_history_list_and_literal_search_sort_by_instant_instead_of_local_date(self):
        older, newer = "local-older-conversation", "utc-newer-conversation"
        self.store.append_message(
            older, "user", "order-probe older", "user", created_at="2026-10-31T03:12:00+08:00",
        )
        self.store.append_message(
            newer, "user", "order-probe newer", "user", created_at="2026-10-30T19:15:00+00:00",
        )
        for query in ("", "order-probe"):
            with self.subTest(query=query):
                self.assertEqual([row["id"] for row in self.store.list_conversations(query)], [newer, older])

    def test_recent_events_sort_by_instant_before_limiting_thirty_candidates(self):
        # More than thirty lexically larger local timestamps must not evict
        # the actually newest UTC event before the bounded ranking step runs.
        oldest = datetime(2026, 10, 30, 19, 0, tzinfo=timezone.utc)
        for index in range(31):
            observed = (oldest + timedelta(seconds=index)).astimezone(timezone(timedelta(hours=8)))
            self.store.append_screen_comment(
                self.cid, "old visual " + str(index), event_id="old-event-" + str(index), app_name="synthetic",
                context_summary="old visual", event_type="interesting",
                observed_at=observed.isoformat(timespec="seconds"),
            )
        self.store.append_screen_comment(
            self.cid, "latest camera", event_id="newest-utc-event", app_name="synthetic-camera",
            context_summary="actually newest frame", event_type="interesting", source_kind="camera",
            observed_at="2026-10-30T19:15:00+00:00",
        )
        self.assertEqual(
            [row["id"] for row in self.store.recent_screen_events(self.cid, limit=3)],
            ["newest-utc-event", "old-event-30", "old-event-29"],
        )

    def test_exports_use_same_chronological_order_for_legacy_mixed_offset_rows(self):
        expected = self.seed_timeline([
            "2026-10-31T03:12:00+08:00", "2026-10-30T19:15:00+00:00",
            "2026-10-30T19:15:01.200000+00:00",
        ], source_kind="camera")
        json_path, markdown_path = self.root / "timeline.json", self.root / "timeline.md"
        export_conversation_file(self.store.path, self.cid, json_path, "json")
        export_conversation_file(self.store.path, self.cid, markdown_path, "markdown")
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual([row["id"] for row in payload["messages"]], [row["id"] for row in expected])
        markdown = markdown_path.read_text(encoding="utf-8")
        self.assertLess(markdown.index("合成主动评论"), markdown.index("合成手动问题"))
        self.assertLess(markdown.index("合成手动问题"), markdown.index("合成已保存回答"))


if __name__ == "__main__":
    unittest.main()
