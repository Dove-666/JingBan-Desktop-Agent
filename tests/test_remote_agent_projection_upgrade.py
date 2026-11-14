import tempfile
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import uuid4

from desktop_pet.agent_presentation import AGENT_ACTIVITY, AGENT_TRACE_LABELS
from desktop_pet.conversations.store import ConversationStore, REMOTE_SYNC_SCHEMA_VERSION
from desktop_pet.execution_trace import ExecutionTrace
from desktop_pet.remote.sync import RemoteSyncStore


class AgentProjectionUpgradeTests(unittest.TestCase):
    def test_installed_v11_refreshes_dsh_tools_without_rewriting_user_history(self):
        new_labels = {
            "编辑文件", "搜索文件内容", "执行命令", "查看图片", "查看工作目录", "交付文件",
            "分派任务", "协调子任务", "查看子任务", "等待子任务", "停止子任务", "操作电脑", "使用任务工具",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "archive.sqlite3"
            cid = str(uuid4())
            # Install an actual stale SQL projection: changing only the schema
            # marker would leave the new whitelist embedded in the triggers.
            with patch("desktop_pet.conversations.store.REMOTE_SYNC_SCHEMA_VERSION", 11), \
                    patch("desktop_pet.conversations.store.AGENT_ACTIVITY", AGENT_ACTIVITY - new_labels), \
                    patch("desktop_pet.agent_presentation.AGENT_TRACE_LABELS", AGENT_TRACE_LABELS - new_labels):
                store = ConversationStore(path)
                trace = ExecutionTrace("fixture-model")
                for index, label in enumerate(sorted(new_labels)):
                    trace.event(f"private-{index}", "tool", label, "completed")
                message = store.append_message(cid, "user", "Owned task", "user", metadata={
                    "request_status": "pending", "execution_mode": "agent", "execution": trace.snapshot(),
                    "private_context": "PRIVATE_FIXTURE_ONLY",
                })
            with store._connection() as connection:
                before_messages = [tuple(row) for row in connection.execute("SELECT * FROM messages")]
                before_journal = [tuple(row) for row in connection.execute("SELECT * FROM remote_sync_changes ORDER BY seq")]
                cursor = connection.execute("SELECT high_seq FROM remote_sync_state").fetchone()[0]
                stale_trigger = connection.execute("SELECT sql FROM sqlite_master WHERE name='remote_sync_message_update'").fetchone()[0]
                self.assertNotIn("'交付文件'", stale_trigger)
            old_public = next(change["data"] for change in RemoteSyncStore(store).pull()["changes"]
                              if change["id"] == message["id"])
            self.assertFalse(new_labels & {item["label"] for item in old_public["agent_trace"]["events"]})

            upgraded = ConversationStore(path)
            upgraded.initialize()
            sync = RemoteSyncStore(upgraded)
            refreshed = [change["data"] for change in sync.pull(since=cursor)["changes"]
                         if change["id"] == message["id"]]
            self.assertEqual(len(refreshed), 1)
            self.assertTrue(new_labels <= {item["label"] for item in refreshed[0]["agent_trace"]["events"]})
            self.assertNotIn("PRIVATE", json.dumps(refreshed[0]))
            self.assertEqual(refreshed[0]["agent_trace"]["version"], 1)
            with upgraded._connection() as connection:
                self.assertEqual([tuple(row) for row in connection.execute("SELECT * FROM messages")], before_messages)
                self.assertEqual([tuple(row) for row in connection.execute(
                    "SELECT * FROM remote_sync_changes WHERE seq<=? ORDER BY seq", (cursor,))], before_journal)
                self.assertEqual(connection.execute("SELECT version FROM remote_sync_schema").fetchone()[0], 12)
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 3)
            trace.event("new-present", "tool", "交付文件", "running")
            upgraded.update_execution(cid, message["id"], trace.snapshot())
            latest = [change["data"] for change in sync.pull(since=cursor)["changes"]
                      if change["id"] == message["id"]][-1]
            self.assertEqual(latest["agent_activity"], "交付文件")
            self.assertEqual(latest["agent_trace"]["events"][-1]["label"], "交付文件")
            with upgraded._connection() as connection:
                settled = connection.execute("SELECT count(*) FROM remote_sync_changes").fetchone()[0]
            ConversationStore(path).initialize()
            with upgraded._connection() as connection:
                self.assertEqual(connection.execute("SELECT count(*) FROM remote_sync_changes").fetchone()[0], settled)

    def test_existing_v4_journal_gets_new_capture_errors_and_discovery_activity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "archive.sqlite3"
            store = ConversationStore(path)
            cid = str(uuid4())
            message = store.append_message(cid, "user", "Continue the task", "user",
                metadata={"request_status": "pending", "execution_mode": "agent"})
            # Simulate an installed v4 projection, rather than only testing a
            # fresh database whose triggers already know the new field values.
            with store._connection() as connection:
                connection.execute("UPDATE remote_sync_schema SET version = 4")
                connection.execute("DROP TRIGGER remote_sync_message_update")
                connection.execute("CREATE TRIGGER remote_sync_message_update AFTER UPDATE ON messages BEGIN SELECT 1; END")
            upgraded = ConversationStore(path)
            upgraded.initialize()
            trace = ExecutionTrace("fixture-model")
            trace.event("discover", "tool", "查找已安装应用", "running")
            upgraded.update_execution(cid, message["id"], trace.snapshot())
            changes = RemoteSyncStore(upgraded).pull()["changes"]
            public = [c["data"] for c in changes if c["entity"] == "message"][-1]
            self.assertEqual(public["agent_activity"], "查找已安装应用")
            upgraded.set_message_status(cid, message["id"], "failed", agent_error_code="gui_desktop_locked")
            public = [c["data"] for c in RemoteSyncStore(upgraded).pull()["changes"] if c["entity"] == "message"][-1]
            self.assertEqual(public["agent_error_code"], "gui_desktop_locked")
            with upgraded._connection() as connection:
                self.assertEqual(connection.execute("SELECT version FROM remote_sync_schema").fetchone()[0], REMOTE_SYNC_SCHEMA_VERSION)

    def test_existing_v5_projection_includes_native_system_observations(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "archive.sqlite3"
            store = ConversationStore(path)
            cid = str(uuid4())
            message = store.append_message(cid, "user", "Inspect only", "user",
                metadata={"request_status": "pending", "execution_mode": "agent"})
            with store._connection() as connection:
                connection.execute("UPDATE remote_sync_schema SET version = 5")
                connection.execute("DROP TRIGGER remote_sync_message_update")
                connection.execute("CREATE TRIGGER remote_sync_message_update AFTER UPDATE ON messages BEGIN SELECT 1; END")
            upgraded = ConversationStore(path)
            upgraded.initialize()
            trace = ExecutionTrace("fixture-model")
            for name in ("检查电脑状态", "检查磁盘占用", "查看目录信息"):
                trace.event(name, "tool", name, "running")
                upgraded.update_execution(cid, message["id"], trace.snapshot())
                changes = RemoteSyncStore(upgraded).pull()["changes"]
                public = [c["data"] for c in changes if c["entity"] == "message"][-1]
                self.assertEqual(public["agent_activity"], name)


if __name__ == "__main__":
    unittest.main()
