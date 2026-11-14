"""Actual nanobot SDK/localhost model through durable host-worker ACKs."""
from dataclasses import replace
from pathlib import Path
import json
import sys
import tempfile
import threading
import unittest
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "agent_backend" / "nanobot"
sys.path.insert(0, str(BACKEND / "tests"))
sys.path.insert(0, str(BACKEND))
from test_agent_mode import NativeModel, ThreadingHTTPServer, PYTHON, KEY
from desktop_pet.agents import AgentService, AgentSettings, AgentRequest
from desktop_pet.agents.checkpoints import CheckpointStore, scope_fingerprint
from desktop_pet.agents.settings import shared_api_settings


@unittest.skipUnless(PYTHON.is_file(), "isolated SDK environment unavailable")
class CheckpointSdkTests(unittest.TestCase):
    def test_sdk_automatic_user_role_feedback_is_not_a_durable_user_goal(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), NativeModel)
        server.daemon_threads = True
        server.block_on_close = False
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
        thread.start()
        allowed = ROOT / "agent_workspace"
        allowed.mkdir(exist_ok=True)
        directory = tempfile.TemporaryDirectory(prefix="checkpoint-feedback-", dir=allowed)
        workspace = Path(directory.name)
        state_directory = tempfile.TemporaryDirectory(prefix="checkpoint-feedback-db-")
        database = Path(state_directory.name) / "checkpoint.sqlite3"
        service = AgentService(checkpoint_path=database)
        settings = AgentSettings(python_executable=str(PYTHON), workspace=str(workspace), timeout_seconds=30,
                                 max_steps=0)
        context = {"api": {"base_url": f"http://127.0.0.1:{server.server_port}/v1", "model": "offline"},
                   "messages": [], "conversation_id": str(uuid4())}
        task = 'Create report.txt containing exactly "HELLO".'
        try:
            NativeModel.calls, NativeModel.behavior = [], "plain"
            result = service.execute(settings, KEY, AgentRequest(task, "agent"), context)
            self.assertEqual(result.status, "needs_user")
            self.assertFalse((workspace / "report.txt").exists())
            scope = scope_fingerprint(shared_api_settings(settings, context["api"]), context["api"], secrets=(KEY,))
            saved = CheckpointStore(database).load(context["conversation_id"], scope)
            feedback = [row for row in saved["history"] if row["role"] == "user"
                        and row["content"].startswith("HOST OUTCOME CHECK:")]
            self.assertTrue(feedback, "Real SDK continuation must have published automatic user-role feedback")
            self.assertEqual(saved["goal_history"], [task])
            self.assertEqual(service._session_goals[context["conversation_id"]], [task])
            service.shutdown()
            service = AgentService(checkpoint_path=database)
            service.execute(settings, KEY, AgentRequest("Continue", "agent"), context)
            resumed = CheckpointStore(database).load(context["conversation_id"], scope)
            self.assertEqual(resumed["goal_history"], [task, "Continue"])
            self.assertFalse((workspace / "report.txt").exists())
        finally:
            service.shutdown()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            directory.cleanup()
            state_directory.cleanup()

    def test_real_sdk_tool_checkpoints_survive_restart_without_replaying_actions(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), NativeModel)
        server.daemon_threads = True
        server.block_on_close = False
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
        thread.start()
        allowed = ROOT / "agent_workspace"
        allowed.mkdir(exist_ok=True)
        directory = tempfile.TemporaryDirectory(prefix="checkpoint-sdk-", dir=allowed)
        workspace = Path(directory.name)
        self.assertTrue(workspace.resolve().is_relative_to(allowed.resolve()))
        state_directory = tempfile.TemporaryDirectory(prefix="checkpoint-sdk-db-")
        database = Path(state_directory.name) / "checkpoint.sqlite3"
        service = AgentService(checkpoint_path=database)
        settings = AgentSettings(python_executable=str(PYTHON), workspace=str(workspace), timeout_seconds=30)
        context = {"api": {"base_url": f"http://127.0.0.1:{server.server_port}/v1", "model": "offline"},
                   "messages": [], "conversation_id": str(uuid4())}
        try:
            NativeModel.calls, NativeModel.behavior = [], "write_read"
            result = service.execute(settings, KEY, AgentRequest("Create evidence.txt then verify it by reading it", "agent"), context)
            self.assertIn(result.status, ("completed", "needs_user"))
            self.assertEqual((workspace / "evidence.txt").read_text(), "persistent native evidence")
            # Runtime computer routing is derived from the main API, and only
            # the credential actually used for requests participates in scope.
            scope = scope_fingerprint(shared_api_settings(settings, context["api"]),
                                      context["api"], secrets=(KEY,))
            checkpoint = CheckpointStore(database).load(context["conversation_id"], scope)
            self.assertTrue(checkpoint["scope_matches"])
            self.assertGreaterEqual(checkpoint["last_barrier"], 4)
            info = service.checkpoint_info(context["conversation_id"], settings,
                                           context["api"], secrets=(KEY,))
            self.assertEqual(info["last_barrier"], checkpoint["last_barrier"])
            self.assertNotIn("goal_history", info)
            self.assertNotEqual(checkpoint["status"], "running")
            original_bytes = (workspace / "evidence.txt").read_bytes()
            service.shutdown()
            service = AgentService(checkpoint_path=database)
            NativeModel.calls, NativeModel.behavior = [], "plain"
            service.execute(settings, KEY, AgentRequest("What exact content did the prior read return? Do not write again.", "agent"), context)
            history = NativeModel.calls[0]["messages"]
            self.assertTrue(any(row.get("role") == "tool" and "persistent native evidence" in row.get("content", "") for row in history))
            self.assertEqual((workspace / "evidence.txt").read_bytes(), original_bytes)
            goals = CheckpointStore(database).load(context["conversation_id"], scope)["goal_history"]
            self.assertEqual(goals, ["Create evidence.txt then verify it by reading it",
                                    "What exact content did the prior read return? Do not write again."])
            self.assertNotIn(KEY.encode(), database.read_bytes())
        finally:
            service.shutdown()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            self.assertTrue(workspace.resolve().is_relative_to(allowed.resolve()))
            directory.cleanup()
            state_directory.cleanup()


if __name__ == "__main__":
    unittest.main()
