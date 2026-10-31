"""Exports order legacy mixed-offset timestamps by instant, without rewriting.

Raw SQLite updates deliberately simulate an existing archive.  Normalizing only
new writes cannot make these tests pass.  All data and exports are temporary.
"""

from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from desktop_pet.conversations import ConversationStore
from desktop_pet.conversations.export import export_conversation_file


class MixedTimezoneExportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="aura-mixed-timezone-export-")
        self.path = Path(self.directory.name)
        self.store = ConversationStore(self.path / "archive.sqlite3")
        self.cid = "mixed-timezone-conversation"

    def tearDown(self):
        self.directory.cleanup()

    def raw_timestamps(self, values):
        with closing(sqlite3.connect(self.store.path)) as connection:
            for message_id, timestamp in values.items():
                connection.execute("UPDATE messages SET created_at = ? WHERE id = ?", (timestamp, message_id))
                connection.execute("""UPDATE screen_events SET observed_at = ?
                    WHERE id = (SELECT screen_event_id FROM messages WHERE id = ?)""", (timestamp, message_id))
            connection.commit()

    def seed_legacy_conversation(self):
        # Commit order is deliberately different from observation chronology.
        self.store.append_message(self.cid, "user", "USER_FOLLOWUP", "user", message_id="question")
        self.store.append_screen_comment(self.cid, "SCREEN_EARLY", event_id="screen-event",
                                         app_name="synthetic-editor", context_summary="SCREEN_CONTEXT",
                                         event_type="interesting", message_id="screen", source_kind="screen")
        self.store.append_message(self.cid, "assistant", "ASSISTANT_REPLY", "assistant", message_id="reply")
        self.store.append_screen_comment(self.cid, "CAMERA_BETWEEN", event_id="camera-event",
                                         app_name="synthetic-camera", context_summary="CAMERA_CONTEXT",
                                         event_type="interesting", message_id="camera", source_kind="camera")
        self.raw_timestamps({
            "screen": "2026-10-31T09:00:00+08:00",
            "camera": "2026-10-30T18:00:30-07:00",
            "question": "2026-10-31T01:01:00.000000+00:00",
            "reply": "2026-10-31T01:02:00Z",
        })

    def export_json(self):
        destination = self.path / "conversation.json"
        export_conversation_file(self.store.path, self.cid, destination, "json")
        return json.loads(destination.read_text(encoding="utf-8"))

    def test_json_chronology_handles_local_utc_z_and_negative_offsets(self):
        self.seed_legacy_conversation()
        messages = self.export_json()["messages"]
        self.assertEqual([message["id"] for message in messages], ["screen", "camera", "question", "reply"])
        instants = [datetime.fromisoformat(message["created_at"].replace("Z", "+00:00")).astimezone(timezone.utc)
                    for message in messages]
        self.assertEqual(instants, sorted(instants))
        self.assertEqual(messages[0]["screen_event"]["context_summary"], "SCREEN_CONTEXT")
        self.assertEqual(messages[1]["screen_event"]["source_kind"], "camera")

    def test_markdown_uses_same_chronology_and_export_never_rewrites_legacy_times(self):
        self.seed_legacy_conversation()
        with closing(sqlite3.connect(self.store.path)) as connection:
            before = connection.execute("SELECT id,created_at FROM messages ORDER BY rowid").fetchall()
        destination = self.path / "conversation.md"
        export_conversation_file(self.store.path, self.cid, destination, "markdown")
        content = destination.read_text(encoding="utf-8")
        positions = [content.index("\n" + marker + "\n") for marker in ("SCREEN_EARLY", "CAMERA_BETWEEN", "USER_FOLLOWUP", "ASSISTANT_REPLY")]
        self.assertEqual(positions, sorted(positions))
        with closing(sqlite3.connect(self.store.path)) as connection:
            after = connection.execute("SELECT id,created_at FROM messages ORDER BY rowid").fetchall()
        self.assertEqual(after, before)

    def test_submillisecond_timestamps_keep_true_order_instead_of_commit_order(self):
        self.store.append_message(self.cid, "assistant", "LATER_COMMITTED_FIRST", "assistant", message_id="later")
        self.store.append_message(self.cid, "user", "EARLIER_COMMITTED_SECOND", "user", message_id="earlier")
        self.raw_timestamps({"earlier": "2026-10-31T09:00:00.000100+08:00",
                             "later": "2026-10-31T01:00:00.000200+00:00"})
        self.assertEqual([message["id"] for message in self.export_json()["messages"]], ["earlier", "later"])

    def test_equal_instants_with_different_offsets_preserve_rowid_tie_break(self):
        for message_id in ("first", "second", "third"):
            self.store.append_message(self.cid, "user", message_id, "user", message_id=message_id)
        self.raw_timestamps({"first": "2026-10-31T09:00:00.123456+08:00",
                             "second": "2026-10-31T01:00:00.123456Z",
                             "third": "2026-10-30T20:00:00.123456-05:00"})
        self.assertEqual([message["id"] for message in self.export_json()["messages"]], ["first", "second", "third"])


if __name__ == "__main__":
    unittest.main()
