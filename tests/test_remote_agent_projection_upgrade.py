import tempfile
from pathlib import Path
import unittest
from uuid import uuid4

from desktop_pet.conversations.store import ConversationStore, REMOTE_SYNC_SCHEMA_VERSION
from desktop_pet.execution_trace import ExecutionTrace
from desktop_pet.remote.sync import RemoteSyncStore


class AgentProjectionUpgradeTests(unittest.TestCase):
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
