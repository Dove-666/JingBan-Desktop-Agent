"""Terminal GUI evidence gates using real child pipes and temporary SQLite only."""
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from desktop_pet.agents import AgentError, AgentRequest, AgentService, AgentSettings
from desktop_pet.agents import service as runtime
from desktop_pet.agents.checkpoints import CheckpointStore


def report(*, status="verified", method="visual", seconds_ago=0):
    return {"status": status, "method": method,
            "reason": "The target checkbox is selected in the refreshed observation.",
            "checked_at": (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()}


class GuiCompletionHostTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="aura-gui-goal-host-")
        self.root = Path(self.temporary.name)
        self.worker = self.root / "fixture.py"
        self.database = self.root / "checkpoints.sqlite3"
        self.service = AgentService(checkpoint_path=self.database)
        self.cid = str(uuid4())
        self.settings = AgentSettings(enabled=True, python_executable=sys.executable)
        self.context = {"api": {"base_url": "http://127.0.0.1:1/v1", "model": "fixture"},
                        "messages": [], "conversation_id": self.cid}

    def tearDown(self):
        self.service.shutdown()
        self.temporary.cleanup()

    def make_worker(self, terminal, *, prelude=""):
        self.worker.write_text(
            "import sys,json\n"
            "request=json.loads(sys.stdin.readline())\n"
            "def send(frame):\n"
            "    frame.setdefault('id',request['id'])\n"
            "    print(json.dumps(frame),flush=True)\n"
            "send({'type':'session','message':{'role':'user','content':request['task']}})\n"
            + prelude + f"send({terminal!r})\n", encoding="utf-8")

    def execute(self, **kwargs):
        with patch.object(runtime, "_WORKER_PATH", self.worker):
            return self.service.execute(self.settings, "synthetic-host-key",
                AgentRequest("Select the target checkbox", "agent"), self.context, **kwargs)

    def stored(self):
        with closing(sqlite3.connect(self.database)) as db:
            return db.execute("SELECT status,goal_verified FROM agent_checkpoints").fetchone()

    def test_verified_gui_success_sets_durable_evidence_bit(self):
        for method in ("visual", "uia", "native"):
            with self.subTest(method=method):
                self.make_worker({"type": "final", "reply": "Target confirmed selected.",
                    "status": "completed", "gui_used": True, "gui_goal_verified": True,
                    "verification": report(method=method)})
                self.assertEqual(self.execute().status, "completed")
                self.assertEqual(self.stored(), ("completed", 1))

    def test_missed_click_unknown_or_missing_check_never_returns_green_completion(self):
        for verification in (None, report(status="failed"), report(status="unverified")):
            with self.subTest(verification=verification):
                self.make_worker({"type": "final", "reply": "Clicked; everything succeeded!",
                    "status": "completed", "gui_used": True, "gui_goal_verified": False,
                    "verification": verification})
                progress = []
                result = self.execute(progress_callback=progress.append)
                self.assertEqual(result.status, "needs_user")
                self.assertNotIn("everything succeeded", result.reply)
                self.assertIn("尚未确认", result.reply)
                self.assertNotIn("completed", progress)
                self.assertEqual(self.stored(), ("needs_user", 0))

    def test_stale_or_future_check_cannot_be_reused_for_current_task(self):
        for seconds in (60, -60):
            with self.subTest(seconds=seconds):
                self.make_worker({"type": "final", "reply": "done", "status": "completed",
                    "gui_used": True, "gui_goal_verified": True,
                    "verification": report(seconds_ago=seconds)})
                self.assertEqual(self.execute().status, "needs_user")
                self.assertEqual(self.stored()[1], 0)

    def test_native_gui_tool_call_detected_when_activity_callback_is_disabled(self):
        call = {"role": "assistant", "content": "", "tool_calls": [{"id": "owned-gui",
                "type": "function", "function": {"name": "computer_use", "arguments": "{}"}}]}
        outcome = {"role": "tool", "name": "computer_use", "tool_call_id": "owned-gui",
                   "content": '{"status":"completed"}'}
        prelude = "send(" + repr({"type": "session", "message": call}) + ")\n"
        prelude += "send(" + repr({"type": "session", "message": outcome}) + ")\n"
        self.make_worker({"type": "final", "reply": "I finished!"}, prelude=prelude)
        self.assertEqual(self.execute().status, "needs_user")
        self.assertEqual(self.stored(), ("needs_user", 0))

    def test_non_gui_completed_flow_stays_completed_without_false_evidence(self):
        self.make_worker({"type": "final", "reply": "The calculation is 42.", "status": "completed",
                          "gui_used": False, "gui_goal_verified": False, "verification": None})
        self.assertEqual(self.execute().status, "completed")
        self.assertEqual(self.stored(), ("completed", 0))

    def test_needs_user_does_not_become_verified_even_with_successful_subgoal_check(self):
        self.make_worker({"type": "final", "reply": "One step is still missing.", "status": "needs_user",
            "gui_used": True, "gui_goal_verified": True, "verification": report()})
        self.assertEqual(self.execute().status, "needs_user")
        self.assertEqual(self.stored(), ("needs_user", 0))

    def test_malformed_and_contradictory_envelopes_are_protocol_errors(self):
        base = {"type": "final", "reply": "done", "status": "completed",
                "gui_used": True, "gui_goal_verified": True, "verification": report()}
        candidates = [dict(base, gui_goal_verified="true"), dict(base, gui_used=1),
                      dict(base, verification=None), dict(base, gui_used=False),
                      dict(base, verification=report(status="failed"))]
        for mutation in ({"checked_at": "2026-02-30T12:00:00Z"}, {"checked_at": "2026-01-01"},
                         {"method": "model_said_so"}, {"reason": ""}, {"status": []},
                         {"private_screenshot": "pixels"}):
            candidates.append(dict(base, verification={**report(), **mutation}))
        for value in candidates:
            with self.subTest(value=value):
                with self.assertRaises(AgentError) as caught:
                    runtime.AgentService._parse_frame(json.dumps(dict(value, id="owned")).encode(), "owned")
                self.assertEqual(caught.exception.code, "protocol")

    def test_stop_while_final_is_arriving_cannot_persist_verified_completion(self):
        self.make_worker({"type": "final", "reply": "done", "status": "completed",
            "gui_used": True, "gui_goal_verified": True, "verification": report()})
        cancellation = threading.Event()
        read = runtime._read_available
        def stop_on_final(pipe):
            raw = read(pipe)
            if b'"type": "final"' in raw:
                cancellation.set()
            return raw
        with patch.object(runtime, "_read_available", side_effect=stop_on_final):
            with self.assertRaises(AgentError) as caught:
                self.execute(cancellation_event=cancellation)
        self.assertEqual(caught.exception.code, "cancelled")
        self.assertEqual(self.stored(), ("cancelled", 0))


class CheckpointVerificationInvariantsTests(unittest.TestCase):
    def test_structured_tool_manifest_requires_windows_permission_and_gui_config(self):
        names = {"desktop_observe", "desktop_act", "desktop_verify_state", "verify_desktop_goal"}
        configured = replace(AgentSettings(), computer_enabled=True, gui_model="fixture")
        for platform, settings, key, expected in (
                ("win32", configured, "synthetic-key", True),
                ("linux", configured, "synthetic-key", False),
                ("win32", replace(configured, computer_enabled=False), "synthetic-key", False),
                ("win32", configured, "", False),
                ("win32", replace(configured, enabled=False), "synthetic-key", False)):
            with self.subTest(platform=platform, expected=expected):
                with patch.object(runtime.sys, "platform", platform):
                    manifest = {item["name"]: item for item in AgentService.capabilities(settings, key)}
                self.assertTrue(names <= manifest.keys())
                self.assertTrue(all(manifest[name]["available"] is expected for name in names))

    def test_inconsistent_legacy_bit_never_revives_interrupted_or_cancelled_success(self):
        with tempfile.TemporaryDirectory(prefix="aura-goal-invariant-") as directory:
            path = Path(directory) / "checkpoint.sqlite3"
            store = CheckpointStore(path)
            cid, task, scope = str(uuid4()), uuid4().hex, "a" * 64
            history = [{"role": "user", "content": "owned fixture"}]
            store.begin(cid, scope, task, history)
            for status in ("interrupted", "cancelled", "needs_user", "failed", "running"):
                with self.subTest(status=status):
                    with closing(sqlite3.connect(path)) as db:
                        with db:
                            db.execute("UPDATE agent_checkpoints SET status=?,goal_verified=1", (status,))
                    self.assertFalse(store.load(cid, scope)["goal_verified"])


if __name__ == "__main__":
    unittest.main()
