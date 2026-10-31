"""Camera provenance and real v2-to-v3 archive migrations in temporary files."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from desktop_pet.conversation_metadata import update_session_metadata
from desktop_pet.conversations.context import ContextAssembler, ContextBudgetError
from desktop_pet.conversations.export import ExportError, export_conversation_file
from desktop_pet.conversations.store import ConversationStore, StoreError


class CameraConversationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "synthetic-history.sqlite3"
        self.store = ConversationStore(self.path)

    def add_camera(self, cid="one", event_id="camera-one"):
        return self.store.append_screen_comment(
            cid, "桌边有一盆绿色植物。", event_id=event_id, app_name="测试摄像头",
            context_summary="摄像头中能看见桌上的盆栽。", event_type="interesting",
            source_kind="camera", metadata={"image_base64": "SYNTHETIC_FRAME_ONLY"},
        )

    def create_v2_archive(self):
        # These are the actual previously shipped migration functions, not a
        # v3 archive with only its version number changed.
        with sqlite3.connect(self.path) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            ConversationStore._migrate_v1(connection)
            ConversationStore._migrate_v2(connection)
            connection.execute("""INSERT INTO conversations
                (id, title, summary, created_at, updated_at, title_generated,
                 title_manual, summary_through_id)
                VALUES ('legacy', '保留的手动标题', '原有概况', '2025', '2026', 1, 1, 'old-user')""")
            connection.execute("""INSERT INTO messages
                (rowid, id, conversation_id, role, content, created_at, source, metadata)
                VALUES (10, 'old-user', 'legacy', 'user', '旧用户消息', '2026', 'user',
                    '{"request_status":"complete"}')""")
            connection.execute("""INSERT INTO screen_events
                (id, conversation_id, observed_at, app_name, context_summary, event_type, metadata)
                VALUES ('old-event', 'legacy', '2026', 'editor.exe', '旧屏幕情境', 'work',
                    '{"confidence":0.9}')""")
            connection.execute("""INSERT INTO messages
                (rowid, id, conversation_id, role, content, created_at, source, screen_event_id)
                VALUES (30, 'old-comment', 'legacy', 'assistant', '旧屏幕评论', '2026',
                    'proactive_screen', 'old-event')""")
            connection.execute("PRAGMA user_version = 2")

    def test_camera_round_trip_keeps_source_and_event_association(self):
        message = self.add_camera()
        self.assertEqual(message["source"], "proactive_camera")
        self.assertEqual(message["screen_event"]["source_kind"], "camera")
        restarted = ConversationStore(self.path)
        loaded = restarted.load_messages("one")
        self.assertEqual(loaded[0], message)
        event = restarted.get_screen_event("camera-one", "one")
        self.assertEqual(event["message_id"], message["id"])
        self.assertEqual(event["source_kind"], "camera")
        self.assertEqual(restarted.recent_screen_events("one")[0], event)
        self.assertEqual(restarted.load_messages("other"), [])
        self.assertIsNone(restarted.get_screen_event("camera-one", "other"))

    def test_invalid_source_cannot_create_partial_event_or_conversation(self):
        for invalid in ("webcam", "proactive_camera", "", None, []):
            with self.subTest(source=invalid), self.assertRaises(StoreError):
                self.store.append_screen_comment(
                    "one", "comment", event_id="bad", app_name="test",
                    context_summary="test", event_type="other", source_kind=invalid,
                )
        self.assertEqual(self.store.list_conversations(), [])
        with self.assertRaises(StoreError):
            self.store.append_message("one", "assistant", "comment", "proactive_camera")

    def test_real_v2_migration_preserves_order_metadata_counts_and_constraints(self):
        self.create_v2_archive()
        self.store.initialize()
        messages = self.store.load_messages("legacy")
        self.assertEqual([message["id"] for message in messages], ["old-user", "old-comment"])
        self.assertEqual(messages[0]["metadata"], {"request_status": "complete"})
        self.assertEqual(messages[1]["screen_event"]["metadata"], {"confidence": 0.9})
        self.assertEqual(messages[1]["screen_event"]["source_kind"], "screen")
        conversation = self.store.get_conversation("legacy")
        self.assertEqual(conversation["message_count"], 2)
        self.assertEqual(conversation["title"], "保留的手动标题")
        self.assertTrue(conversation["title_manual"])
        self.assertTrue(conversation["title_generated"])
        self.assertEqual(conversation["summary"], "原有概况")
        self.assertEqual(conversation["summary_through_id"], "old-user")
        camera = self.add_camera("legacy")
        self.assertEqual(self.store.get_conversation("legacy")["message_count"], 3)
        self.store.append_message("other", "user", "another session", "user")
        with sqlite3.connect(self.path) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 3)
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            indexes = {row[1] for row in connection.execute("PRAGMA index_list(messages)")}
            self.assertIn("idx_messages_event", indexes)
            self.assertIn("idx_messages_conversation", indexes)
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("""INSERT INTO messages
                    (id, conversation_id, role, content, created_at, source, screen_event_id)
                    VALUES ('forged', 'other', 'assistant', 'forged', '2026',
                        'proactive_camera', 'camera-one')""")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("""INSERT INTO messages
                    (id, conversation_id, role, content, created_at, source)
                    VALUES ('no-event', 'legacy', 'assistant', 'forged', '2026', 'proactive_camera')""")
            connection.execute("DELETE FROM messages WHERE id = ?", (camera["id"],))
        self.assertEqual(self.store.get_conversation("legacy")["message_count"], 2)
        with sqlite3.connect(self.path) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("DELETE FROM screen_events WHERE id = 'old-event'")
        self.assertEqual(self.store.get_conversation("legacy")["message_count"], 1)
        self.store.delete_conversation("legacy")
        self.assertEqual(self.store.load_messages("legacy"), [])
        self.assertEqual(self.store.recent_screen_events("legacy"), [])
        self.assertEqual(len(self.store.load_messages("other")), 1)

    def test_interrupted_migration_rolls_back_schema_rows_and_count_triggers(self):
        self.create_v2_archive()
        migrate = ConversationStore._migrate_v3

        def interrupted(connection):
            migrate(connection)
            raise sqlite3.OperationalError("synthetic interrupted migration")

        with patch.object(ConversationStore, "_migrate_v3", side_effect=interrupted):
            with self.assertRaises(StoreError):
                self.store.initialize()
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(screen_events)")}
            self.assertNotIn("source_kind", columns)
            self.assertEqual(connection.execute("SELECT rowid FROM messages ORDER BY rowid").fetchall(), [(10,), (30,)])
            triggers = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
            self.assertTrue({"message_added_count", "message_removed_count"} <= triggers)
            self.assertEqual(connection.execute("SELECT message_count FROM conversations").fetchone()[0], 2)
        self.store.initialize()
        self.assertEqual(self.store.get_conversation("legacy")["message_count"], 2)

    def test_exports_distinguish_camera_from_screen_and_exclude_frame_metadata(self):
        self.add_camera()
        self.store.append_screen_comment(
            "one", "编辑器里有报错。", event_id="screen-one", app_name="editor.exe",
            context_summary="SCREEN_SUMMARY", event_type="error",
        )
        json_path, md_path = self.root / "camera.json", self.root / "camera.md"
        export_conversation_file(self.path, "one", json_path, "json")
        export_conversation_file(self.path, "one", md_path, "markdown")
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual([item["source"] for item in payload["messages"]], ["proactive_camera", "proactive_screen"])
        self.assertEqual([item["screen_event"]["source_kind"] for item in payload["messages"]], ["camera", "screen"])
        text = md_path.read_text(encoding="utf-8")
        self.assertIn("来自摄像头感知", text)
        self.assertIn("关联摄像头情境", text)
        self.assertIn("设备：测试摄像头", text)
        self.assertIn("来自屏幕感知", text)
        self.assertIn("关联屏幕情境", text)
        for output in (json_path, md_path):
            self.assertNotIn("SYNTHETIC_FRAME_ONLY", output.read_text(encoding="utf-8"))
            self.assertNotIn("image_base64", output.read_text(encoding="utf-8"))

    def test_export_reads_real_v2_archive_without_running_migration(self):
        self.create_v2_archive()
        destination = self.root / "old.json"
        export_conversation_file(self.path, "legacy", destination, "json")
        payload = json.loads(destination.read_text(encoding="utf-8"))
        self.assertEqual(payload["messages"][1]["screen_event"]["source_kind"], "screen")
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)

    def test_export_rejects_mislabeled_camera_event_without_replacing_destination(self):
        self.add_camera()
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE screen_events SET source_kind='screen' WHERE id='camera-one'")
        destination = self.root / "existing.json"
        destination.write_text("keep existing", encoding="utf-8")
        with self.assertRaises(ExportError):
            export_conversation_file(self.path, "one", destination, "json")
        self.assertEqual(destination.read_text(encoding="utf-8"), "keep existing")

    def test_camera_context_keeps_source_and_current_session_isolation(self):
        self.add_camera()
        result = ContextAssembler().assemble(
            system_prompt="按已知资料回答。", user_text="你刚才看到的植物是什么颜色？", recent_messages=[],
            screen_events=self.store.recent_screen_events("one"),
        )
        text = "\n".join(item["content"] for item in result.messages)
        self.assertIn('"source_kind": "camera"', text)
        self.assertIn("摄像头", text)
        self.assertIn("盆栽", text)
        self.assertNotIn("SYNTHETIC_FRAME_ONLY", text)
        with self.assertRaises(ContextBudgetError):
            ContextAssembler().assemble(
                system_prompt="按已知资料回答。", user_text="question",
                recent_messages=[{"role": "user", "content": "other", "conversation_id": "other"}],
                screen_events=self.store.recent_screen_events("one"),
            )

    def test_camera_local_metadata_title_uses_observed_context_without_changing_source(self):
        self.add_camera()
        update_session_metadata(self.store, "one", None, None, "")
        self.assertIn("盆栽", self.store.get_conversation("one")["title"])
        self.assertEqual(self.store.load_messages("one")[0]["source"], "proactive_camera")

    def test_unknown_or_malformed_visual_source_is_rejected_by_context(self):
        for invalid in ("webcam", None, [], {}):
            with self.subTest(source=invalid), self.assertRaises(ContextBudgetError):
                ContextAssembler().assemble(
                    system_prompt="按已知资料回答。", user_text="question", recent_messages=[],
                    screen_events=[{"source_kind": invalid, "context_summary": "untrusted"}],
                )


if __name__ == "__main__":
    unittest.main()
