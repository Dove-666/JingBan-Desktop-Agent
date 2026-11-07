"""Public mobile process views keep actual events and exclude private traces."""
from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from desktop_pet.agent_presentation import MAX_AGENT_TRACE_MS, public_agent_trace
from desktop_pet.conversations.store import ConversationStore, REMOTE_SYNC_SCHEMA_VERSION, remote_sync_projection
from desktop_pet.remote.sync import RemoteSyncStore, SyncResetRequired, sanitize_message


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

    def test_delegation_sources_remain_public_with_private_fields_filtered(self):
        value = execution("completed")
        labels = ("任务分派说明（模型公开回复）", "任务分派说明（直接请求）")
        value["events"] = [
            {"id": "PRIVATE_" + str(index), "kind": "summary", "label": label,
             "status": "completed", "elapsed_ms": index, "detail": "PRIVATE_DETAIL"}
            for index, label in enumerate((*labels, "PRIVATE_UNKNOWN_LABEL"))]
        message = self.add(value, status="complete")
        trace = self.assert_parity(message)
        self.assertEqual([event["label"] for event in trace["events"]], list(labels))
        self.assertEqual([event["id"] for event in trace["events"]], ["step-0", "step-1"])
        self.assertNotIn("PRIVATE", json.dumps(trace))
        sync = RemoteSyncStore(self.store)
        for public in (next(item["data"] for item in sync.snapshot()["items"] if item["id"] == message["id"]),
                       next(item["data"] for item in sync.pull()["changes"] if item["id"] == message["id"])):
            self.assertEqual(public["agent_trace"], trace)

    def test_real_v10_upgrade_republishes_completed_trace_and_preserves_history(self):
        fixture = Path(__file__).parent / "fixtures/remote_sync_v10_delegation.sqlite3"
        self.assertEqual(hashlib.sha256(fixture.read_bytes()).hexdigest(),
                         "1248413634aced32a3419f1fbdf0aed8f8886e2e31ca3e6061080036738ea59f")
        path = self.path.with_name("real-v10.sqlite3")
        shutil.copyfile(fixture, path)
        preserved_tables = ("conversations", "messages", "screen_events", "remote_sync_operations", "remote_sync_tombstones")
        with closing(sqlite3.connect(path)) as connection:
            connection.row_factory = sqlite3.Row
            self.assertEqual(connection.execute("SELECT version FROM remote_sync_schema").fetchone()[0], 10)
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 3)
            preserved = {table: [dict(row) for row in connection.execute("SELECT * FROM " + table + " ORDER BY rowid")]
                         for table in preserved_tables}
            journal = [dict(row) for row in connection.execute("SELECT * FROM remote_sync_changes ORDER BY seq")]
            cursor = connection.execute("SELECT high_seq FROM remote_sync_state").fetchone()[0]
            stale_snapshot = connection.execute("SELECT id FROM remote_sync_snapshots").fetchone()[0]
            old_triggers = [row[0] for row in connection.execute("SELECT sql FROM sqlite_master WHERE type='trigger'")]
        direct = next(row for row in preserved["messages"] if row["content"] == "Owned direct request")
        label = "任务分派说明（直接请求）"
        self.assertIn(label, direct["metadata"])
        self.assertTrue(all(label not in sql for sql in old_triggers))
        self.assertFalse(any(label in row["payload"] for row in journal))
        self.assertTrue(preserved["remote_sync_operations"])
        self.assertTrue(preserved["remote_sync_tombstones"])
        upgraded = ConversationStore(path)
        upgraded.initialize()
        sync = RemoteSyncStore(upgraded)
        changes = sync.pull(since=cursor)["changes"]
        direct_changes = [item["data"] for item in changes if item["entity"] == "message" and item["id"] == direct["id"]]
        self.assertEqual(len(direct_changes), 1)
        self.assertIn(label, [event["label"] for event in direct_changes[0]["agent_trace"]["events"]])
        self.assertEqual(direct_changes[0]["request_status"], "complete")
        self.assertNotIn("PRIVATE", json.dumps(direct_changes[0]["agent_trace"]))
        with closing(sqlite3.connect(path)) as connection:
            connection.row_factory = sqlite3.Row
            for table, rows in preserved.items():
                self.assertEqual([dict(row) for row in connection.execute("SELECT * FROM " + table + " ORDER BY rowid")], rows)
            self.assertEqual([dict(row) for row in connection.execute("SELECT * FROM remote_sync_changes WHERE seq <= ? ORDER BY seq", (cursor,))], journal)
            self.assertEqual(connection.execute("SELECT count(*) FROM remote_sync_snapshots").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT version FROM remote_sync_schema").fetchone()[0], REMOTE_SYNC_SCHEMA_VERSION)
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 3)
            self.assertIn(label, connection.execute("SELECT sql FROM sqlite_master WHERE name='remote_sync_message_insert'").fetchone()[0])
            self.assertIn(label, connection.execute("SELECT sql FROM sqlite_master WHERE name='remote_sync_message_update'").fetchone()[0])
        with self.assertRaises(SyncResetRequired):
            sync.snapshot(cursor=stale_snapshot + ":0")
        fresh = sync.snapshot()
        public = next(item["data"] for item in fresh["items"] if item["entity"] == "message" and item["id"] == direct["id"])
        self.assertEqual(public["agent_trace"], direct_changes[0]["agent_trace"])
        trace = json.loads(direct["metadata"])["execution"]
        trace["duration_ms"] += 100
        upgraded.update_execution(direct["conversation_id"], direct["id"], trace)
        followup = sync.pull(since=fresh["watermark"])
        self.assertIn(label, [event["label"] for event in followup["changes"][-1]["data"]["agent_trace"]["events"]])
        inserted = upgraded.append_message(direct["conversation_id"], "user", "Owned post-upgrade insert", "user",
            metadata={"execution_mode": "agent", "request_status": "complete", "execution": trace})
        insert_change = sync.pull(since=followup["next_since"])["changes"]
        self.assertIn(label, [event["label"] for item in insert_change if item["id"] == inserted["id"]
                             for event in item["data"]["agent_trace"]["events"]])
        with closing(sqlite3.connect(path)) as connection:
            settled = connection.execute("SELECT * FROM remote_sync_changes ORDER BY seq").fetchall()
            snapshot_count = connection.execute("SELECT count(*) FROM remote_sync_snapshots").fetchone()[0]
        upgraded.initialize()
        ConversationStore(path).initialize()
        with closing(sqlite3.connect(path)) as connection:
            self.assertEqual(connection.execute("SELECT * FROM remote_sync_changes ORDER BY seq").fetchall(), settled)
            self.assertEqual(connection.execute("SELECT count(*) FROM remote_sync_snapshots").fetchone()[0], snapshot_count)

    def test_real_v10_refresh_respects_retention_and_filters_non_agent_rows(self):
        path = self.path.with_name("real-v10-retention.sqlite3")
        shutil.copyfile(Path(__file__).parent / "fixtures/remote_sync_v10_delegation.sqlite3", path)
        with closing(sqlite3.connect(path)) as connection:
            connection.row_factory = sqlite3.Row
            direct = dict(connection.execute("SELECT * FROM messages WHERE content='Owned direct request'").fetchone())
            model = dict(connection.execute("SELECT * FROM messages WHERE content='Owned model request'").fetchone())
            ordinary = json.loads(model["metadata"])
            ordinary["execution_mode"] = "ordinary"
            connection.execute("UPDATE messages SET metadata=? WHERE id=?", (json.dumps(ordinary), model["id"]))
            connection.execute("UPDATE messages SET metadata='broken PRIVATE_METADATA' WHERE content='Ordinary owned history'")
            connection.commit()
            tables = ("conversations", "messages", "screen_events", "remote_sync_operations", "remote_sync_tombstones")
            preserved = {table: [dict(row) for row in connection.execute("SELECT * FROM " + table + " ORDER BY rowid")]
                         for table in tables}
            journal = {row["seq"]: dict(row) for row in connection.execute("SELECT * FROM remote_sync_changes ORDER BY seq")}
            cursor = connection.execute("SELECT high_seq FROM remote_sync_state").fetchone()[0]
        # Exercise the production compaction boundary with small owned limits.
        with patch("desktop_pet.conversations.store.REMOTE_SYNC_MAX_CHANGES", len(journal) + 1), \
                patch("desktop_pet.conversations.store.REMOTE_SYNC_PRUNE_BATCH", 4):
            upgraded = ConversationStore(path)
            upgraded.initialize()
            sync = RemoteSyncStore(upgraded)
            with self.assertRaises(SyncResetRequired):
                sync.pull(since=0)
            changes = sync.pull(since=cursor)["changes"]
            self.assertEqual([item["id"] for item in changes if item["entity"] == "message"], [direct["id"]])
            with closing(sqlite3.connect(path)) as connection:
                connection.row_factory = sqlite3.Row
                live = [dict(row) for row in connection.execute("SELECT * FROM remote_sync_changes ORDER BY seq")]
                state = dict(connection.execute("SELECT * FROM remote_sync_state").fetchone())
                self.assertGreater(state["floor_seq"], 0)
                self.assertEqual(state["floor_seq"], live[0]["seq"] - 1)
                self.assertEqual(state["high_seq"], live[-1]["seq"])
                self.assertEqual(state["entry_count"], len(live))
                self.assertEqual(state["payload_bytes"], sum(len(row["payload"].encode("utf-8")) for row in live))
                self.assertLessEqual(len(live), len(journal) + 1)
                for row in live:
                    if row["seq"] in journal:
                        self.assertEqual(row, journal[row["seq"]])
                for table, rows in preserved.items():
                    self.assertEqual([dict(row) for row in connection.execute("SELECT * FROM " + table + " ORDER BY rowid")], rows)
            snapshot = sync.snapshot()
        public = {item["id"]: item["data"] for item in snapshot["items"] if item["entity"] == "message"}
        labels = [event["label"] for event in public[direct["id"]]["agent_trace"]["events"]]
        self.assertIn("任务分派说明（直接请求）", labels)
        self.assertIsNone(public[model["id"]]["agent_trace"])
        self.assertTrue(all(item["agent_trace"] is None for item in public.values() if item["id"] != direct["id"]))
        self.assertNotIn("PRIVATE", json.dumps(public))

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
            self.assertEqual(connection.execute("SELECT version FROM remote_sync_schema").fetchone()[0], REMOTE_SYNC_SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
