"""Public mobile process views keep actual events and exclude private traces."""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from uuid import uuid4

from desktop_pet.agent_presentation import MAX_AGENT_TRACE_MS, public_agent_trace
from desktop_pet.conversations.store import ConversationStore, remote_sync_projection
from desktop_pet.remote.sync import RemoteSyncStore, sanitize_message


def execution(state="running"):
    return {"version": 1, "state": state, "started_at": "2026-11-01T10:12:13.123456+00:00",
            "duration_ms": 2500, "events": [
                {"id": "PRIVATE_ID", "kind": "progress", "label": "开始处理请求", "status": "completed", "elapsed_ms": 0},
                {"id": "PRIVATE_ID_2", "kind": "tool", "label": "检查磁盘占用", "status": "running", "elapsed_ms": 2000,
                 "detail": "PRIVATE_ARGUMENT", "reasoning": "PRIVATE_THOUGHT"},
                {"id": "PRIVATE_ID_3", "kind": "tool", "label": "PRIVATE_LABEL", "status": "completed", "elapsed_ms": 2100}],
            "context": {"messages": [{"role": "system", "content": "PRIVATE_CONTEXT"}], "api_key": "PRIVATE_KEY"},
            "reasoning_content": "PRIVATE_THOUGHT"}


class PublicExecutionProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "archive.sqlite3"
        self.store = ConversationStore(self.path)
        self.cid = str(uuid4())

    def add(self, value, *, role="user", mode="agent", status="pending"):
        return self.store.append_message(self.cid, role, "Public fixture", "user" if role == "user" else "assistant",
            metadata={"execution_mode": mode, "request_status": status, "execution": value})

    def sql_public(self, message):
        with self.store._connection() as connection:
            raw = connection.execute("SELECT " + remote_sync_projection("message") + " FROM messages WHERE id = ?",
                                     (message["id"],)).fetchone()[0]
        return json.loads(raw)

    def assert_parity(self, message):
        actual = self.sql_public(message)
        expected = sanitize_message(message)
        self.assertEqual(actual["agent_trace"], expected["agent_trace"])
        return actual["agent_trace"]

    def test_pending_view_has_fixed_events_generated_ids_and_no_private_fields(self):
        message = self.add(execution())
        value = self.assert_parity(message)
        self.assertEqual([event["id"] for event in value["events"]], ["step-0", "step-1"])
        self.assertEqual(value["events"][-1]["label"], "检查磁盘占用")
        self.assertEqual(value["omitted_count"], 0)
        encoded = json.dumps(self.sql_public(message), ensure_ascii=False)
        for private in ("PRIVATE_", "context", "reasoning", "detail"):
            self.assertNotIn(private, encoded)

    def test_terminal_process_survives_snapshot_and_pull_only_on_user_message(self):
        value = execution("completed")
        value["events"][-1] = {"id": "summary:completed", "kind": "summary", "label": "回复已完成",
                               "status": "completed", "elapsed_ms": 2500}
        user = self.add(value, status="complete")
        assistant = self.add(value, role="assistant", status="complete")
        sync = RemoteSyncStore(self.store)
        snapshot = sync.snapshot(limit=100)
        messages = {item["id"]: item["data"] for item in snapshot["items"] if item["entity"] == "message"}
        self.assertEqual(messages[user["id"]]["agent_trace"], self.assert_parity(user))
        self.assertEqual(messages[user["id"]]["agent_trace"]["state"], "completed")
        self.assertIsNone(messages[assistant["id"]]["agent_trace"])
        changes = {item["id"]: item["data"] for item in sync.pull(limit=100)["changes"] if item["entity"] == "message"}
        self.assertEqual(changes[user["id"]]["agent_trace"], messages[user["id"]]["agent_trace"])
        self.assertIsNone(self.assert_parity(assistant))

    def test_mobile_window_bounded_without_truncating_stored_execution(self):
        value = execution()
        value["events"] = [{"id": f"PRIVATE-{i}", "kind": "tool", "label": "读取文件", "status": "completed", "elapsed_ms": i}
                           for i in range(1000)]
        message = self.add(value)
        trace = self.assert_parity(message)
        self.assertEqual(len(trace["events"]), 80)
        self.assertEqual(trace["omitted_count"], 920)
        self.assertEqual(trace["events"][0]["id"], "step-920")
        self.assertEqual(trace["events"][-1]["id"], "step-999")
        self.assertEqual(len(self.store.load_messages(self.cid)[0]["metadata"]["execution"]["events"]), 1000)
        self.assertLess(len(json.dumps(trace, ensure_ascii=False).encode("utf-8")), 32000)

    def test_malformed_scalars_and_dates_are_rejected_identically(self):
        cases = [
            {"version": True}, {"version": 1.0}, {"state": "PRIVATE_STATE"}, {"state": {}},
            {"duration_ms": True}, {"duration_ms": -1}, {"duration_ms": 2.5},
            {"duration_ms": MAX_AGENT_TRACE_MS + 1}, {"events": "PRIVATE_EVENT"},
            {"started_at": "PRIVATE_DATE"}, {"started_at": "2026-11-01T00:00:00"},
            {"started_at": "2026-02-30T00:00:00Z"}, {"started_at": "0000-01-01T00:00:00Z"},
            {"started_at": "2026-11-01T24:00:00Z"}, {"started_at": "2026-11-01T00:00:60Z"},
            {"started_at": "2026-11-01T00:00:00.1234567Z"}, {"started_at": "2026-11-01T00:00:00+14:01"},
        ]
        for updates in cases:
            with self.subTest(updates=updates):
                value = {**execution(), **updates}
                self.assertIsNone(self.assert_parity(self.add(value)))

    def test_valid_timestamp_forms_and_empty_event_list_have_snapshot_parity(self):
        for started in ("2026-11-01T12:30:00Z", "2024-02-29T00:00:00.1-14:00",
                        "0001-01-01T00:00:00+00:00", "2026-11-01T00:00:00.123+05:30"):
            with self.subTest(started=started):
                value = {**execution(), "started_at": started, "events": []}
                trace = self.assert_parity(self.add(value))
                self.assertEqual(trace["started_at"], started)
                self.assertEqual(trace["events"], [])

    def test_malformed_events_are_discarded_without_breaking_archive_writes(self):
        value = execution()
        value["events"].extend([None, "PRIVATE", [], {},
            {"kind": "tool", "label": "读取文件", "status": "completed", "elapsed_ms": True},
            {"kind": "tool", "label": "读取文件", "status": "completed", "elapsed_ms": 2501},
            {"kind": "tool", "label": "读取文件", "status": "completed", "elapsed_ms": -1},
            {"kind": "tool", "label": "读取文件", "status": "completed", "elapsed_ms": 2.2}])
        self.assertEqual(len(self.assert_parity(self.add(value))["events"]), 2)
        ordinary = self.add(execution(), mode="ordinary")
        self.assertIsNone(self.assert_parity(ordinary))
        self.assertIsNone(self.assert_parity(self.add("PRIVATE_EXECUTION")))
        self.assertIsNone(self.assert_parity(self.add(None)))

    def test_malformed_legacy_metadata_does_not_break_projection(self):
        message = self.add(execution())
        with self.store._connection() as connection:
            connection.execute("UPDATE messages SET metadata = 'broken PRIVATE' WHERE id = ?", (message["id"],))
        self.assertIsNone(self.sql_public(message)["agent_trace"])

    def test_v6_trigger_upgrade_emits_live_and_terminal_traces_without_message_replay(self):
        user = self.add(execution())
        with self.store._connection() as connection:
            connection.execute("UPDATE remote_sync_schema SET version = 6")
            connection.execute("DROP TRIGGER remote_sync_message_update")
            connection.execute("CREATE TRIGGER remote_sync_message_update AFTER UPDATE ON messages BEGIN SELECT 1; END")
        updated = ConversationStore(self.path)
        updated.initialize()
        current = execution()
        current["duration_ms"] = 3000
        updated.update_execution(self.cid, user["id"], current)
        latest = RemoteSyncStore(updated).pull(limit=100)["changes"][-1]["data"]
        self.assertEqual(latest["agent_trace"]["duration_ms"], 3000)
        terminal = {**current, "state": "cancelled", "duration_ms": 3100}
        updated.set_message_status(self.cid, user["id"], "cancelled", agent_error_code="cancelled", execution=terminal)
        latest = RemoteSyncStore(updated).pull(limit=100)["changes"][-1]["data"]
        self.assertEqual(latest["agent_trace"]["state"], "cancelled")
        self.assertEqual(len(updated.load_messages(self.cid)), 1)
        with updated._connection() as connection:
            self.assertEqual(connection.execute("SELECT version FROM remote_sync_schema").fetchone()[0], 7)


if __name__ == "__main__":
    unittest.main()
