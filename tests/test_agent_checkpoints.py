"""Actual SQLite, restart and subprocess barriers using synthetic data only."""
from dataclasses import replace
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from desktop_pet.agents import AgentError, AgentRequest, AgentService, AgentSettings
from desktop_pet.agents import service as runtime
from desktop_pet.agents.checkpoints import CheckpointError, CheckpointStore, SCHEMA_VERSION, sanitized_history, scope_fingerprint


KEY = "checkpoint-test-synthetic-secret"
GUI_KEY = "checkpoint-test-synthetic-gui-secret"


@contextmanager
def database(path):
    connection = sqlite3.connect(path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def transcript(secret=KEY):
    return [
        {"role": "user", "content": "保留测试 " + secret},
        {"role": "assistant", "content": "", "reasoning_content": "HIDDEN_REASONING_PRIVATE",
         "tool_calls": [{"id": "call-1", "type": "function", "function": {
             "name": "write_file", "arguments": json.dumps({"path": "test.txt", "content": secret})}}]},
        {"role": "tool", "name": "write_file", "tool_call_id": "call-1", "content": '{"written":"test.txt"}'},
        {"role": "assistant", "content": "写入完成"},
    ]


class CheckpointStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="aura-checkpoint-store-")
        self.root = Path(self.directory.name)
        self.path = self.root / "nested" / "checkpoints.sqlite3"
        self.store = CheckpointStore(self.path)
        self.cid, self.task = str(uuid4()), uuid4().hex
        self.scope = "a" * 64

    def tearDown(self):
        self.directory.cleanup()

    def test_construction_performs_no_disk_access(self):
        self.assertFalse(self.path.parent.exists())
        with patch.object(subprocess, "Popen") as spawn, patch.object(threading.Thread, "start") as start:
            service = AgentService(checkpoint_path=self.path)
            service.shutdown()
        self.assertFalse(self.path.parent.exists())
        spawn.assert_not_called()
        start.assert_not_called()

    def test_completed_history_and_task_metadata_survive_new_store(self):
        records = transcript()
        self.store.begin(self.cid, self.scope, self.task, records, secrets=(KEY,))
        self.store.save(self.cid, self.scope, self.task, records, status="completed", barrier=1, secrets=(KEY,))
        recovered = CheckpointStore(self.path).load(self.cid, self.scope, secrets=(KEY,))
        self.assertEqual(recovered["status"], "completed")
        self.assertEqual(recovered["last_barrier"], 1)
        self.assertFalse(recovered["goal_verified"])
        self.assertEqual(recovered["history"], sanitized_history(records, (KEY,)))
        self.assertIsNone(CheckpointStore(self.path).load(str(uuid4()), self.scope))

    def test_verified_goal_is_separate_from_completed_flow(self):
        self.store.begin(self.cid, self.scope, self.task, transcript(), secrets=(KEY,))
        self.store.save(self.cid, self.scope, self.task, transcript(), status="verified", goal_verified=True, secrets=(KEY,))
        self.assertTrue(CheckpointStore(self.path).load(self.cid, self.scope)["goal_verified"])

    def test_running_crash_is_interrupted_and_pending_outcome_is_not_retried(self):
        records = transcript()[:2]
        self.store.begin(self.cid, self.scope, self.task, records, secrets=(KEY,))
        new = CheckpointStore(self.path)
        recovered = new.load(self.cid, self.scope)
        self.assertEqual(recovered["status"], "interrupted")
        self.assertTrue(recovered["recovered_running"])
        self.assertEqual(len([r for r in recovered["history"] if r["role"] == "tool"]), 1)
        self.assertIn("Do not repeat automatically", next(r["content"] for r in recovered["history"] if r["role"] == "tool"))
        self.assertIn("Only the latest user message", recovered["history"][-1]["content"])
        again = new.load(self.cid, self.scope)
        self.assertFalse(again["recovered_running"])
        self.assertEqual(again["history"], recovered["history"])

    def test_scope_change_cannot_restore_or_resurrect_old_native_history(self):
        self.store.begin(self.cid, self.scope, self.task, transcript(), secrets=(KEY,))
        different = "b" * 64
        mismatch = self.store.load(self.cid, different)
        self.assertFalse(mismatch["scope_matches"])
        self.assertEqual(mismatch["history"], [])
        self.store.begin(self.cid, different, uuid4().hex, [{"role": "user", "content": "新权限任务"}])
        self.assertFalse(self.store.load(self.cid, self.scope)["scope_matches"])
        self.assertEqual(self.store.load(self.cid, different)["history"], [{"role": "user", "content": "新权限任务"}])

    def test_scope_binds_workspace_permissions_credentials_but_preserves_model_route_changes(self):
        settings = AgentSettings()
        api = {"base_url": "https://test.invalid/v1", "model": "model-a"}
        baseline = scope_fingerprint(settings, api, secrets=(KEY, GUI_KEY))
        for changed, config, secrets in (
                (replace(settings, computer_enabled=not settings.computer_enabled), api, (KEY, GUI_KEY)),
                (replace(settings, workspace=str(Path(settings.workspace) / "other")), api, (KEY, GUI_KEY)),
                (settings, api, ("other-test-key", GUI_KEY))):
            self.assertNotEqual(baseline, scope_fingerprint(changed, config, secrets=secrets))
        self.assertEqual(len(baseline), 64)
        self.assertEqual(baseline, scope_fingerprint(settings, {**api, "model": "model-b"}, secrets=(KEY, GUI_KEY)))
        self.assertEqual(baseline, scope_fingerprint(replace(settings, gui_model="another-vision-model"), api, secrets=(KEY, GUI_KEY)))
        self.assertNotEqual(scope_fingerprint(settings, api, secrets=(KEY,), generation=str(uuid4())),
                            scope_fingerprint(settings, api, secrets=(KEY,), generation=str(uuid4())))

    def test_secrets_hidden_fields_and_image_payloads_never_enter_database(self):
        records = transcript()
        records[0]["content"] += " data:image/png;base64,QUJDREVGRw=="
        records.append({"role": "user", "content": [{"type": "text", "text": GUI_KEY},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,UElYRUxT"}}]})
        self.store.begin(self.cid, self.scope, self.task, records, secrets=(KEY, GUI_KEY))
        data = self.path.read_bytes()
        for private in (KEY, GUI_KEY, "HIDDEN_REASONING_PRIVATE", "QUJDREVGRw==", "UElYRUxT"):
            self.assertNotIn(private.encode(), data)
        self.assertIn(b"[redacted]", data)

    def test_clear_deletes_only_selected_conversation_and_related_tasks(self):
        other = str(uuid4())
        self.store.begin(self.cid, self.scope, self.task, transcript(), secrets=(KEY,))
        self.store.begin(other, self.scope, uuid4().hex, [{"role": "user", "content": "other"}])
        self.store.clear(self.cid)
        self.assertIsNone(self.store.load(self.cid, self.scope))
        self.assertIsNotNone(self.store.load(other, self.scope))
        with database(self.path) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM agent_task_checkpoints WHERE conversation_id=?", (self.cid,)).fetchone()[0], 0)
        self.store.clear()
        self.assertIsNone(self.store.load(other, self.scope))

    def test_transaction_rejects_late_task_and_preserves_prior_checkpoint(self):
        self.store.begin(self.cid, self.scope, self.task, transcript(), secrets=(KEY,))
        with self.assertRaises(CheckpointError):
            self.store.save(self.cid, self.scope, uuid4().hex, [], status="completed")
        self.assertEqual(self.store.load(self.cid, self.scope)["task_id"], self.task)
        self.store.save(self.cid, self.scope, self.task, transcript(), barrier=2, secrets=(KEY,))
        with self.assertRaises(CheckpointError):
            self.store.save(self.cid, self.scope, self.task, [], barrier=1)
        self.assertEqual(self.store.load(self.cid, self.scope)["last_barrier"], 2)

    def test_bad_database_and_future_version_are_not_overwritten(self):
        self.path.parent.mkdir()
        self.path.write_bytes(b"not-sqlite-private-driver-info")
        before = self.path.read_bytes()
        with self.assertRaises(CheckpointError) as caught:
            self.store.load(self.cid, self.scope)
        self.assertNotIn("private-driver", str(caught.exception))
        self.assertEqual(self.path.read_bytes(), before)
        self.path.unlink()
        with database(self.path) as db:
            db.execute("PRAGMA user_version=99")
            db.execute("CREATE TABLE preserved(value TEXT)")
            db.execute("INSERT INTO preserved VALUES('kept')")
        with self.assertRaises(CheckpointError):
            self.store.load(self.cid, self.scope)
        with database(self.path) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 99)
            self.assertEqual(db.execute("SELECT value FROM preserved").fetchone()[0], "kept")

    def test_history_size_and_whole_turn_budget_are_retained(self):
        messages = []
        for number in range(20):
            messages += [{"role": "user", "content": f"turn {number}"}, {"role": "assistant", "content": "x" * 24000}]
        self.store.begin(self.cid, self.scope, self.task, messages)
        loaded = self.store.load(self.cid, self.scope)["history"]
        self.assertLessEqual(len(json.dumps(loaded).encode()), 260096)
        self.assertLessEqual(sum(r["role"] == "user" for r in loaded), 12)
        self.assertEqual(loaded[0]["role"], "user")

    def test_new_calculation_plan_and_verification_tools_are_replayable(self):
        records = [{"role": "user", "content": "synthetic harness tools"}]
        names = ("calculate", "analyze_csv", "update_plan", "verify_artifact", "finish_task")
        for number, name in enumerate(names):
            identifier = "harness-" + str(number)
            records.append({"role": "assistant", "content": "", "tool_calls": [{"id": identifier,
                "type": "function", "function": {"name": name, "arguments": "{}"}}]})
            records.append({"role": "tool", "name": name, "tool_call_id": identifier,
                            "content": '{"synthetic":true}'})
        self.store.begin(self.cid, self.scope, self.task, records)
        loaded = CheckpointStore(self.path).load(self.cid, self.scope)["history"]
        self.assertEqual([row["name"] for row in loaded if row["role"] == "tool"], list(names))

    def test_task_metadata_retention_is_bounded_and_abandoned_task_not_left_running(self):
        for number in range(40):
            self.store.begin(self.cid, self.scope, uuid4().hex, [{"role": "user", "content": f"task {number}"}])
        with database(self.path) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM agent_task_checkpoints").fetchone()[0], 32)
            self.assertEqual(db.execute("SELECT count(*) FROM agent_task_checkpoints WHERE status='running'").fetchone()[0], 1)

    def test_large_single_turn_cannot_erase_latest_pending_intent_at_ack(self):
        records = [{"role": "user", "content": "large owned task"}]
        for index in range(16):
            identifier = "read-" + str(index)
            records += [{"role": "assistant", "content": "", "tool_calls": [{"id": identifier,
                "type": "function", "function": {"name": "read_file", "arguments": '{"path":"owned.txt"}'}}]},
                {"role": "tool", "name": "read_file", "tool_call_id": identifier, "content": "x" * 24000}]
        records.append({"role": "assistant", "content": "", "tool_calls": [{"id": "critical-write-intent",
            "type": "function", "function": {"name": "write_file", "arguments": '{"path":"owned-result.txt","content":"test"}'}}]})
        self.store.begin(self.cid, self.scope, self.task, records[:1])
        self.store.save(self.cid, self.scope, self.task, records, barrier=1)
        with database(self.path) as db:
            history, frontier = db.execute("SELECT history_json,frontier_json FROM agent_checkpoints").fetchone()
            # Large observations are now recallable references, so a pending
            # call can remain intact in hot replay as well as the safety ledger.
            self.assertIn("critical-write-intent", history)
            pending = json.loads(frontier)["pending_calls"]
            self.assertEqual(pending[0]["id"], "critical-write-intent")
            self.assertEqual(pending[0]["function"]["name"], "write_file")
        recovered = CheckpointStore(self.path).load(self.cid, self.scope)
        self.assertTrue(recovered["history_omitted"])
        self.assertEqual(recovered["pending_tool_count"], 1)
        self.assertIn("critical-write-intent", json.dumps(recovered["history"]))
        self.assertIn("do not repeat automatically", json.dumps(recovered["history"]))
        self.assertTrue(any(record.get("tool_calls") for record in recovered["history"]))
        self.assertTrue(any(record.get("content", "").startswith("INTERRUPTED:") for record in recovered["history"]))

    def test_schema_one_migration_preserves_existing_native_history(self):
        self.store.begin(self.cid, self.scope, self.task, transcript(), secrets=(KEY,))
        self.store.save(self.cid, self.scope, self.task, transcript(), status="completed", secrets=(KEY,))
        with database(self.path) as db:
            db.execute("ALTER TABLE agent_checkpoints DROP COLUMN frontier_json")
            db.execute("PRAGMA user_version=1")
        loaded = CheckpointStore(self.path).load(self.cid, self.scope)
        self.assertEqual(loaded["history"], sanitized_history(transcript(), (KEY,)))
        with database(self.path) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            self.assertIn("frontier_json", [row[1] for row in db.execute("PRAGMA table_info(agent_checkpoints)")])

    def test_failed_sqlite_erasure_leaves_durable_marker_for_new_process(self):
        self.store.begin(self.cid, self.scope, self.task, transcript(), secrets=(KEY,))
        with patch("desktop_pet.agents.checkpoints.sqlite3.connect", side_effect=sqlite3.OperationalError("locked")):
            with self.assertRaises(CheckpointError):
                self.store.clear(self.cid)
        self.assertTrue(list(self.store._deletion_directory.iterdir()))
        self.assertIsNone(CheckpointStore(self.path).load(self.cid, self.scope))
        self.assertEqual(list(self.store._deletion_directory.iterdir()), [])

    def test_malformed_persisted_frontier_is_rejected_without_reflecting_content(self):
        self.store.begin(self.cid, self.scope, self.task, transcript(), secrets=(KEY,))
        with database(self.path) as db:
            db.execute("UPDATE agent_checkpoints SET frontier_json=?", ('{"pending_calls":[{"private":"DO_NOT_REFLECT"}]}',))
        with self.assertRaises(CheckpointError) as caught:
            self.store.load(self.cid, self.scope)
        self.assertNotIn("DO_NOT_REFLECT", str(caught.exception))
        self.store.clear(self.cid)
        self.assertIsNone(self.store.load(self.cid, self.scope))


class CheckpointHostTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="aura-checkpoint-host-")
        self.root = Path(self.directory.name)
        self.database = self.root / "agent.sqlite3"
        self.worker = self.root / "worker.py"
        self.capture = self.root / "capture.json"
        self.effect = self.root / "side-effect.txt"
        self.service = AgentService(checkpoint_path=self.database)
        self.cid = str(uuid4())
        self.settings = AgentSettings(enabled=True, python_executable=sys.executable)
        self.context = {"api": {"base_url": "http://127.0.0.1:1/v1", "model": "fixture"},
                        "messages": [], "conversation_id": self.cid}

    def tearDown(self):
        self.service.shutdown()
        self.directory.cleanup()

    def worker_body(self, *, barrier=1, wait_for_ack=True):
        prefix = (
            "import sys,json\nfrom pathlib import Path\n"
            "print(json.dumps({'type':'ready','protocol':1,'sdk':'nanobot-ai','sdk_version':'0.4.0'}),flush=True)\n"
            "raw=sys.stdin.readline();assert raw.endswith('\\n');request=json.loads(raw);tid=request['id']\n"
            f"Path({str(self.capture)!r}).write_text(json.dumps(request),encoding='utf-8')\n"
            "def send(kind,**values):\n    print(json.dumps(dict(type=kind,id=tid,**values)),flush=True)\n"
            "send('session',message={'role':'user','content':request['task']})\n"
            "send('session',message={'role':'assistant','content':'','tool_calls':[{'id':'call-1','type':'function','function':{'name':'write_file','arguments':'{\"path\":\"sentinel.txt\",\"content\":\"synthetic\"}'}}]})\n"
            f"send('checkpoint',barrier_id='checkpoint-{barrier}')\n"
        )
        if wait_for_ack:
            prefix += "ack=json.loads(sys.stdin.readline());assert ack==dict(type='checkpoint_ack',id=tid,barrier_id='checkpoint-1')\n"
        prefix += (
            f"Path({str(self.effect)!r}).write_text('effect',encoding='utf-8')\n"
            "send('session',message={'role':'tool','tool_call_id':'call-1','name':'write_file','content':'{\"written\":\"sentinel.txt\"}'})\n"
            "send('final',reply='synthetic result',status='completed')\n"
        )
        self.worker.write_text(prefix, encoding="utf-8")

    def execute(self, **kwargs):
        with patch.object(runtime, "_WORKER_PATH", self.worker):
            return self.service.execute(self.settings, KEY, AgentRequest("owned fixture", "agent"), self.context, **kwargs)

    def test_ack_after_commit_and_completed_history_restore_after_service_restart(self):
        self.worker_body()
        self.assertEqual(self.execute().status, "completed")
        self.assertTrue(self.effect.exists())
        first = json.loads(self.capture.read_text(encoding="utf-8"))
        self.assertTrue(first["options"]["checkpoint_required"])
        self.service.shutdown()
        self.service = AgentService(checkpoint_path=self.database)
        self.execute()
        second = json.loads(self.capture.read_text(encoding="utf-8"))
        self.assertTrue(any(r.get("role") == "tool" for r in second["session_history"]))
        self.assertNotIn(KEY.encode(), self.database.read_bytes())

    def test_failed_barrier_does_not_ack_or_allow_tool_body(self):
        self.worker_body()
        with patch.object(self.service._checkpoint_store, "save", side_effect=CheckpointError()):
            with self.assertRaises(AgentError) as caught:
                self.execute()
        self.assertEqual(caught.exception.code, "checkpoint_unavailable")
        self.assertFalse(self.effect.exists())
        self.assertIn("检查点", self.service.checkpoint_status)

    def test_stop_during_commit_prevents_ack_and_new_tool_dispatch(self):
        self.worker_body()
        cancelled = threading.Event()
        save = self.service._checkpoint_store.save
        def stop_after_commit(*args, **kwargs):
            value = save(*args, **kwargs)
            cancelled.set()
            return value
        with patch.object(self.service._checkpoint_store, "save", side_effect=stop_after_commit):
            with self.assertRaises(AgentError) as caught:
                self.execute(cancellation_event=cancelled)
        self.assertEqual(caught.exception.code, "cancelled")
        self.assertFalse(self.effect.exists())

    def test_corrupt_store_prevents_model_or_process_dispatch(self):
        self.worker_body()
        self.database.write_bytes(b"private-invalid-db")
        with patch.object(subprocess, "Popen") as spawn:
            with self.assertRaises(AgentError) as caught:
                self.execute()
        self.assertEqual(caught.exception.code, "checkpoint_unavailable")
        spawn.assert_not_called()

    def test_clear_is_nonblocking_and_disk_erasure_finishes_before_next_task(self):
        self.worker_body()
        self.execute()
        future = self.service.clear_session(self.cid)
        self.assertIsNotNone(future)
        future.result(timeout=2)
        self.execute()
        self.assertEqual(json.loads(self.capture.read_text(encoding="utf-8"))["session_history"], [])

    def test_different_permissions_do_not_replay_in_memory_or_disk(self):
        self.worker_body()
        self.execute()
        self.settings = replace(self.settings, tools_enabled=False)
        self.execute()
        self.assertEqual(json.loads(self.capture.read_text(encoding="utf-8"))["session_history"], [])

    def test_chat_generation_blocks_resurrection_after_erase_cannot_be_persisted(self):
        self.worker_body()
        self.context["conversation_generation"] = str(uuid4())
        self.execute()
        with patch.object(self.service._checkpoint_store, "_record_deletion", side_effect=CheckpointError()):
            future = self.service.clear_session(self.cid)
            with self.assertRaises(CheckpointError):
                future.result(timeout=2)
        self.service.shutdown()
        self.service = AgentService(checkpoint_path=self.database)
        self.context["conversation_generation"] = str(uuid4())
        self.execute()
        request = json.loads(self.capture.read_text(encoding="utf-8"))
        self.assertEqual(request["session_history"], [])
        self.assertNotIn("conversation_generation", request)
        self.assertNotIn("conversation_generation", request["config"])

    def test_invalid_chat_generation_is_rejected_before_dispatch(self):
        self.worker_body()
        for value in (False, 0, [], "not-a-uuid"):
            self.context["conversation_generation"] = value
            with patch.object(subprocess, "Popen") as spawn:
                with self.assertRaises(Exception):
                    self.execute()
                spawn.assert_not_called()

    def test_checkpoint_frame_rejects_extra_fields_bad_ids_and_replays(self):
        tid = uuid4().hex
        for frame in ({"type": "checkpoint", "id": tid, "barrier_id": "checkpoint-0"},
                      {"type": "checkpoint", "id": tid, "barrier_id": "checkpoint-1", "data": "private"},
                      {"type": "checkpoint", "id": tid, "barrier_id": "checkpoint-1\n"}):
            with self.assertRaises(AgentError):
                runtime.AgentService._parse_frame(json.dumps(frame).encode(), tid)
        self.worker_body()
        text = self.worker.read_text(encoding="utf-8")
        text = text.replace("ack=json.loads", "send('checkpoint',barrier_id='checkpoint-1')\nack=json.loads")
        self.worker.write_text(text, encoding="utf-8")
        with self.assertRaises(AgentError) as caught:
            self.execute()
        self.assertEqual(caught.exception.code, "protocol")

    def test_crashed_worker_native_call_is_recovered_as_unknown_not_replayed(self):
        self.worker_body()
        text = self.worker.read_text(encoding="utf-8")
        text = text[:text.index(f"Path({str(self.effect)!r})")]
        text += "raise SystemExit(3)\n"
        self.worker.write_text(text, encoding="utf-8")
        with self.assertRaises(AgentError):
            self.execute()
        self.assertFalse(self.effect.exists())
        self.service.shutdown()
        self.service = AgentService(checkpoint_path=self.database)
        self.worker_body()
        self.execute()
        history = json.loads(self.capture.read_text(encoding="utf-8"))["session_history"]
        pending = [record for record in history if record.get("role") == "tool"]
        self.assertEqual(len(pending), 1)
        self.assertIn("Outcome was not received", pending[0]["content"])
        self.assertTrue(any("Only the next user turn" in record.get("content", "") for record in history))

    def test_oversized_turn_ack_is_backed_by_committed_frontier_before_tool_body(self):
        proof = self.root / "durable-frontier-proof.json"
        script = (
            "import sys,json,sqlite3\nfrom pathlib import Path\n"
            "print(json.dumps({'type':'ready','protocol':1,'sdk':'nanobot-ai','sdk_version':'0.4.0'}),flush=True)\n"
            "raw=sys.stdin.readline();assert raw.endswith('\\n');request=json.loads(raw);tid=request['id']\n"
            "def send(kind,**values):\n    print(json.dumps(dict(type=kind,id=tid,**values)),flush=True)\n"
            "send('session',message={'role':'user','content':request['task']})\n"
            "for index in range(16):\n"
            "    identifier='read-'+str(index)\n"
            "    send('session',message={'role':'assistant','content':'','tool_calls':[{'id':identifier,'type':'function','function':{'name':'read_file','arguments':'{\"path\":\"owned.txt\"}'}}]})\n"
            "    send('session',message={'role':'tool','tool_call_id':identifier,'name':'read_file','content':'x'*24000})\n"
            "send('session',message={'role':'assistant','content':'','tool_calls':[{'id':'critical-write-intent','type':'function','function':{'name':'write_file','arguments':'{\"path\":\"owned-result.txt\",\"content\":\"test\"}'}}]})\n"
            "send('checkpoint',barrier_id='checkpoint-1')\n"
            "ack=json.loads(sys.stdin.readline());assert ack['type']=='checkpoint_ack'\n"
            f"with sqlite3.connect({str(self.database)!r}) as db:\n"
            "    data=json.loads(db.execute('SELECT frontier_json FROM agent_checkpoints').fetchone()[0])\n"
            "assert data['pending_calls'][0]['id']=='critical-write-intent'\n"
            f"Path({str(proof)!r}).write_text(json.dumps(data),encoding='utf-8')\n"
            "raise SystemExit(3)\n"
        )
        self.worker.write_text(script, encoding="utf-8")
        with self.assertRaises(AgentError):
            self.execute()
        self.assertTrue(proof.exists(), "worker must observe its pending tool intent durably before ACK")
        self.assertFalse(self.effect.exists())
        self.service.shutdown()
        self.service = AgentService(checkpoint_path=self.database)
        self.worker_body()
        self.execute()
        history = json.loads(self.capture.read_text(encoding="utf-8"))["session_history"]
        self.assertIn("critical-write-intent", json.dumps(history))
        self.assertIn("do not repeat automatically", json.dumps(history))

    def test_unrepresentable_pending_frontier_is_never_acknowledged(self):
        self.worker_body()
        text = self.worker.read_text(encoding="utf-8")
        insertion = (
            "calls=[{'id':'extra-'+str(i),'type':'function','function':{'name':'write_file','arguments':'{}'}} for i in range(32)]\n"
            "send('session',message={'role':'assistant','content':'','tool_calls':calls})\n"
        )
        text = text.replace("send('checkpoint',barrier_id='checkpoint-1')", insertion + "send('checkpoint',barrier_id='checkpoint-1')")
        self.worker.write_text(text, encoding="utf-8")
        with self.assertRaises(AgentError) as caught:
            self.execute()
        self.assertEqual(caught.exception.code, "checkpoint_unavailable")
        self.assertFalse(self.effect.exists())

    def test_transient_erasure_failure_is_retried_before_next_dispatch(self):
        self.worker_body()
        self.execute()
        store = self.service._checkpoint_store
        original = store.clear
        with patch.object(store, "clear", side_effect=CheckpointError()):
            future = self.service.clear_session(self.cid)
            with self.assertRaises(CheckpointError):
                future.result(timeout=2)
        with patch.object(store, "clear", wraps=original) as retry:
            self.execute()
        retry.assert_called_once_with(self.cid)
        self.assertEqual(json.loads(self.capture.read_text(encoding="utf-8"))["session_history"], [])


if __name__ == "__main__":
    unittest.main()
