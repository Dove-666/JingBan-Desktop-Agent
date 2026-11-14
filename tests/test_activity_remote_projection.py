"""Specific DSH actions stay portable without exporting desktop commentary."""
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from desktop_pet.agent_presentation import AGENT_ACTIVITY, AGENT_TRACE_LABELS, public_agent_trace
from desktop_pet.agents.activity import ACTION_LABELS, TOOL_LABELS, COMPUTER_ACTION_LABELS, AGENT_ACTION_LABELS
from desktop_pet.conversations.store import ConversationStore, REMOTE_SYNC_SCHEMA_VERSION, remote_sync_projection
from desktop_pet.remote.sync import RemoteSyncStore, sanitize_message


NEW_LABELS = (set(COMPUTER_ACTION_LABELS.values()) | set(AGENT_ACTION_LABELS.values())) - (
    set(ACTION_LABELS.values()) | set(TOOL_LABELS.values()))


def activity_trace():
    labels = sorted(set(COMPUTER_ACTION_LABELS.values()) | set(AGENT_ACTION_LABELS.values()))
    return {"version": 1, "state": "running", "started_at": "2026-11-15T02:00:00+00:00",
            "duration_ms": 1000, "events": [
                {"id": "PRIVATE_ID_" + str(index), "kind": "computer", "label": label,
                 "status": "completed", "elapsed_ms": index, "detail": "PRIVATE_DETAIL"}
                for index, label in enumerate(labels)
            ] + [
                {"id": "PRIVATE_NOTE", "kind": "commentary", "label": "PRIVATE_COMMENTARY",
                 "status": "completed", "elapsed_ms": 900},
                {"id": "PRIVATE_LOOKALIKE", "kind": "commentary", "label": "执行命令",
                 "status": "completed", "elapsed_ms": 901},
            ], "context": {"model": "PRIVATE_MODEL"}}


class ActivityRemoteProjectionTests(unittest.TestCase):
    def test_sql_and_python_publish_only_fixed_actions_and_not_commentary(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConversationStore(Path(directory) / "archive.sqlite3")
            message = store.append_message(str(uuid4()), "user", "Owned task", "user", metadata={
                "execution_mode": "agent", "request_status": "pending", "execution": activity_trace()})
            with store._connection() as connection:
                actual = json.loads(connection.execute("SELECT " + remote_sync_projection("message")
                    + " FROM messages WHERE id=?", (message["id"],)).fetchone()[0])
            expected = sanitize_message(message)
            self.assertEqual(actual["agent_trace"], expected["agent_trace"])
            self.assertEqual(actual["agent_activity"], expected["agent_activity"])
            self.assertEqual(actual["agent_activity"], activity_trace()["events"][-3]["label"])
            self.assertTrue(NEW_LABELS <= {event["label"] for event in actual["agent_trace"]["events"]})
            self.assertNotIn("PRIVATE", json.dumps(actual))
            self.assertNotIn("commentary", json.dumps(actual))
            self.assertNotIn("detail", json.dumps(actual))

    def test_v12_upgrade_refreshes_stale_triggers_and_preserves_archive_and_old_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "archive.sqlite3"
            cid = str(uuid4())
            with patch("desktop_pet.conversations.store.REMOTE_SYNC_SCHEMA_VERSION", 12), \
                    patch("desktop_pet.conversations.store.AGENT_ACTIVITY", AGENT_ACTIVITY - NEW_LABELS), \
                    patch("desktop_pet.agent_presentation.AGENT_TRACE_LABELS", AGENT_TRACE_LABELS - NEW_LABELS):
                store = ConversationStore(path)
                message = store.append_message(cid, "user", "Owned migration task", "user", metadata={
                    "execution_mode": "agent", "request_status": "pending", "execution": activity_trace()})
            with store._connection() as connection:
                archive = [tuple(row) for row in connection.execute("SELECT * FROM messages")]
                journal = [tuple(row) for row in connection.execute("SELECT * FROM remote_sync_changes ORDER BY seq")]
                cursor = connection.execute("SELECT high_seq FROM remote_sync_state").fetchone()[0]
                self.assertEqual(connection.execute("SELECT version FROM remote_sync_schema").fetchone()[0], 12)
                trigger = connection.execute("SELECT sql FROM sqlite_master WHERE name='remote_sync_message_update'").fetchone()[0]
                self.assertNotIn("'向下滚动'", trigger)
            upgraded = ConversationStore(path)
            upgraded.initialize()
            rows = [row["data"] for row in RemoteSyncStore(upgraded).pull(since=cursor)["changes"]
                    if row["id"] == message["id"]]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["agent_trace"], public_agent_trace(activity_trace()))
            self.assertNotIn("PRIVATE", json.dumps(rows[0]))
            with upgraded._connection() as connection:
                self.assertEqual([tuple(row) for row in connection.execute("SELECT * FROM messages")], archive)
                self.assertEqual([tuple(row) for row in connection.execute(
                    "SELECT * FROM remote_sync_changes WHERE seq<=? ORDER BY seq", (cursor,))], journal)
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 3)
                self.assertEqual(connection.execute("SELECT version FROM remote_sync_schema").fetchone()[0], REMOTE_SYNC_SCHEMA_VERSION)
                latest_cursor = connection.execute("SELECT high_seq FROM remote_sync_state").fetchone()[0]
            trace = activity_trace()
            trace["events"].append({"id": "PRIVATE_NEW", "kind": "computer", "label": "向下滚动",
                                    "status": "running", "elapsed_ms": 950, "detail": "PRIVATE_WINDOW"})
            upgraded.update_execution(cid, message["id"], trace)
            fresh = RemoteSyncStore(upgraded).pull(since=latest_cursor)["changes"]
            self.assertEqual(fresh[-1]["data"]["agent_activity"], "向下滚动")
            self.assertNotIn("PRIVATE", json.dumps(fresh))
            settled = fresh[-1]["seq"]
            ConversationStore(path).initialize()
            self.assertEqual(RemoteSyncStore(upgraded).pull(since=settled)["changes"], [])

    def test_android_accepts_every_new_fixed_action_without_arbitrary_text(self):
        source = (Path(__file__).resolve().parents[1] / "android/app/src/main/java/com/aura/companion/data/CompanionDatabase.kt").read_text(encoding="utf-8")
        allowlist = source.split("private val AGENT_ACTIVITY_MESSAGES = setOf(", 1)[1].split("\n)", 1)[0]
        labels = set(re.findall(r'"([^"\\]*)"', allowlist))
        self.assertTrue(set(COMPUTER_ACTION_LABELS.values()) <= labels)
        self.assertTrue(set(AGENT_ACTION_LABELS.values()) <= labels)
        self.assertIn("value?.takeIf { it in AGENT_ACTIVITY_MESSAGES }", source)


if __name__ == "__main__":
    unittest.main()
