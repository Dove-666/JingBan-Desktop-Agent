"""DSH host transport regressions; fixtures make no provider or desktop calls."""
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from desktop_pet.agents.contracts import AgentError, AgentRequest
from desktop_pet.agents.dsh_storage import DshSessionStore, session_scope
from desktop_pet.agents.service import AgentService, _DSH_VERSION, node_executable, worker_environment
from desktop_pet.agents.artifacts import ArtifactStore
from desktop_pet.agents.settings import AgentSettings
from desktop_pet.agent_presentation import public_agent_trace


FIXTURE = r"""
import {createInterface} from 'node:readline';
import {writeFileSync,readFileSync} from 'node:fs';
import {createHash} from 'node:crypto';
import {join} from 'node:path';
const emit = value => process.stdout.write(JSON.stringify(value)+'\n');
let request;
function final(text, extra={}) {
 emit({type:'stopped',id:request.id,computer_settled:true});
 emit({type:'final',id:request.id,reply:text,status:'completed',...extra});
}
emit({type:'ready',protocol:1,sdk:'deepseek-harness'});
createInterface({input:process.stdin}).on('line', line => {
 const frame = JSON.parse(line);
 if (frame.type === 'run') {
   request=frame;
   emit({type:'session',id:frame.id,dsh_session_id:frame.storage.session_id || 'session-00000000000000000000000000000001'});
   emit({type:'progress',id:frame.id,phase:'planning'});
   emit({type:'activity',id:frame.id,event_id:'dsh-tool-1',kind:'tool',name:'read',status:'completed'});
   if(frame.task.startsWith('cancel')) return;
   if(frame.task==='steer') return;
   if(frame.task==='question') {
     emit({type:'interaction',id:frame.id,interaction_id:'interaction-00000000000000000000000000000001',
           kind:'question',question:'选择一个名称',options:['甲','乙']}); return;
   }
   if(frame.task.startsWith('artifact')) {
     const path=frame.task==='artifact-external'?join(frame.options.workspace,'..','external.txt'):
       frame.task==='artifact-private'?join(frame.options.workspace,'..','private','sensitive-fixture.txt'):
       join(frame.options.workspace,'receipt.txt');
     writeFileSync(path,'delivery');
     const bytes=readFileSync(path),sha256=createHash('sha256').update(bytes).digest('hex');
     if(frame.task==='artifact-changed') writeFileSync(path,'tampered');
     if(frame.task==='artifact-unpresented') {final('文件路径：'+path);return;}
     final('文件已生成',{artifacts:[{path,sha256,size_bytes:bytes.length}]}); return;
   }
   if(frame.task==='context') {final(JSON.stringify(frame.context));return;}
   if(frame.task==='long') {final('长回复。'.repeat(30000));return;}
   final(frame.storage.session_id?'native session restored':'DSH original answer',
         {acceptance:{passed:false},gui_used:true,gui_goal_verified:false});
 } else if(frame.type==='cancel') {
   if(request.task==='cancel-no-ack') return;
   setTimeout(()=> {emit({type:'stopped',id:request.id,computer_settled:true});process.exit(0);},
       request.task==='cancel-delayed' ? 300 : 0);
 } else if(frame.type==='steer') final('received steering: '+frame.text);
 else if(frame.type==='interaction_reply') final('received answer: '+frame.answer.text);
});
"""


class DshHostTests(unittest.TestCase):
    def setUp(self):
        if node_executable() is None:
            self.skipTest("Node is required for local protocol fixtures")
        self.temporary = tempfile.TemporaryDirectory(prefix="aura-dsh-host-test-")
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.worker = self.root / "worker.mjs"
        self.worker.write_text(FIXTURE, encoding="utf-8")
        self.patches = [
            patch("desktop_pet.agents.service._WORKER_PATH", self.worker),
            patch("desktop_pet.agents.service.dsh_installation", lambda: {"dsh": _DSH_VERSION, "dsh-sdk-client": _DSH_VERSION}),
            patch("desktop_pet.agents.settings.agent_workspace_root", lambda: self.workspace),
        ]
        for item in self.patches: item.start()
        self.service = AgentService(checkpoint_path=self.root / "private" / "old-checkpoints.sqlite3")
        self.settings = AgentSettings(workspace=str(self.workspace))
        self.cid, self.generation = str(uuid4()), str(uuid4())
        self.context = {"api": {"base_url": "https://example.invalid/v1", "model": "fixture-model"},
                        "conversation_id": self.cid, "conversation_generation": self.generation,
                        "messages": []}

    def tearDown(self):
        self.service.shutdown()
        self.service.reconcile_worker_closures()
        for item in reversed(self.patches): item.stop()
        self.temporary.cleanup()

    def run_task(self, task, **kwargs):
        return self.service.execute(self.settings, "fixture-key-not-real", AgentRequest(task, "agent"), self.context, **kwargs)

    def test_original_final_is_not_overwritten_by_legacy_judge_fields(self):
        events = []
        result = self.run_task("first", event_callback=events.append)
        self.assertEqual(result.reply, "DSH original answer")
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.backend, "deepseek-harness")
        self.assertIsNone(result.acceptance)
        self.assertEqual(events[0]["label"], "读取文件")
        self.assertFalse(self.service.execution_resources_pending())

    def test_native_session_restores_without_legacy_transcript(self):
        self.run_task("first")
        self.assertEqual(self.run_task("second").reply, "native session restored")
        self.context["conversation_generation"] = str(uuid4())
        self.assertEqual(self.run_task("third").reply, "DSH original answer")

    def test_reasoning_reset_gets_fresh_native_session_and_keeps_text_context(self):
        self.context["api"].update(reasoning_profile="openai", reasoning_effort="high")
        self.assertEqual(self.run_task("first").reply, "DSH original answer")
        self.assertEqual(self.run_task("same effort").reply, "native session restored")
        self.context["api"]["reasoning_effort"] = "default"
        self.context["messages"] = [{"role": "assistant", "content": "Earlier report is still available."}]
        self.assertEqual(self.run_task("after reset").reply, "DSH original answer")
        value = json.loads(self.run_task("context").reply)
        self.assertEqual(value["messages"], self.context["messages"])
        self.assertEqual(len(list((self.root / "private" / "dsh_sessions").rglob("session.json"))), 2)

    def test_chat_json_contract_is_not_injected_into_harness(self):
        self.context["messages"] = [{"role": "system", "content": "RETURN JSON ONLY"},
                                    {"role": "user", "content": "earlier"}]
        self.context["personality"] = "温柔简洁"
        self.context["image_catalog"] = [{"attachment_id": "a" * 64, "label": "earlier picture"}]
        value = json.loads(self.run_task("context").reply)
        self.assertEqual(value["messages"], [{"role": "user", "content": "earlier"}])
        self.assertEqual(value["personality"], "温柔简洁")
        self.assertNotIn("current_image_url", value)

    def test_cancellation_closes_real_owned_child_and_preserves_receipt(self):
        cancelled = threading.Event()
        started = threading.Event()
        handles, errors = [], []
        def run():
            try:
                self.run_task("cancel", cancellation_event=cancelled,
                              progress_callback=lambda phase: started.set() if phase == "planning" else None,
                              worker_lifecycle_callback=handles.append)
            except AgentError as exc: errors.append(exc.code)
        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(started.wait(10))
        self.service.cancel_request(cancelled)
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, ["cancelled"])
        receipt = self.service.worker_closure(handles[0])
        self.assertTrue(self.service.validate_worker_closure(handles[0], receipt))
        self.assertFalse(self.service.execution_resources_pending())

    def test_stop_waits_for_private_native_ack_and_forced_kill_stays_unknown(self):
        for task, acknowledged in (("cancel-delayed", True), ("cancel-no-ack", False)):
            with self.subTest(task=task):
                cancel, started = threading.Event(), threading.Event()
                handles, errors = [], []
                def run():
                    try: self.run_task(task, cancellation_event=cancel, worker_lifecycle_callback=handles.append,
                        progress_callback=lambda phase: started.set() if phase == "planning" else None)
                    except AgentError as exc: errors.append(exc.code)
                with patch("desktop_pet.agents.service._GRACEFUL_STOP_SECONDS", .6):
                    thread = threading.Thread(target=run)
                    thread.start()
                    self.assertTrue(started.wait(5))
                    self.service.cancel_request(cancel)
                    time.sleep(.05)
                    self.assertTrue(thread.is_alive())
                    self.assertIsNone(self.service.worker_closure(handles[0]))
                    self.assertTrue(self.service.execution_resources_pending())
                    thread.join(5)
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, ["cancelled" if acknowledged else "stop_unconfirmed"])
                receipt = self.service.worker_closure(handles[0])
                self.assertEqual(receipt is not None, acknowledged)
                if not acknowledged:
                    self.assertIsNotNone(handles[0]._process.poll())
                    self.assertEqual(self.service.reconcile_worker_closures(timeout_seconds=0), ())
                    with self.assertRaises(AgentError) as blocked: self.run_task("first")
                    self.assertEqual(blocked.exception.code, "busy")

    def test_question_and_steering_are_bound_to_active_conversation(self):
        outputs = []
        pending = threading.Event()
        thread = threading.Thread(target=lambda: outputs.append(self.run_task("question",
            interaction_callback=lambda value: pending.set() if value else None)))
        thread.start()
        self.assertTrue(pending.wait(10))
        question = self.service.pending_interaction(self.cid)
        self.assertFalse(self.service.answer_interaction(str(uuid4()), question["id"], {"text": "甲"}))
        self.assertTrue(self.service.answer_interaction(self.cid, question["id"], {"text": "甲"}))
        thread.join(10)
        self.assertEqual(outputs[0].reply, "received answer: 甲")
        self.assertIsNone(self.service.pending_interaction(self.cid))
        pending.clear()
        thread = threading.Thread(target=lambda: outputs.append(self.run_task("steer",
            progress_callback=lambda phase: pending.set() if phase == "planning" else None)))
        thread.start()
        self.assertTrue(pending.wait(10))
        self.assertFalse(self.service.steer(str(uuid4()), "wrong"))
        self.assertTrue(self.service.steer(self.cid, "先检查"))
        thread.join(10)
        self.assertEqual(outputs[-1].reply, "received steering: 先检查")

    def test_presented_artifact_is_downloadable_and_scope_bound(self):
        result = self.run_task("artifact")
        self.assertEqual(len(result.artifacts), 1)
        reference = result.artifacts[0]["reference"]
        page = self.service.artifact_page(self.cid, reference)
        self.assertEqual(page["total_bytes"], 8)
        self.assertEqual(page["verification_scope"], "immutable_byte_snapshot")
        with self.assertRaises(ValueError): self.service.artifact_page(str(uuid4()), reference)
        (self.workspace / "receipt.txt").write_text("changed", encoding="utf-8")
        self.assertEqual(self.service.artifact_page(self.cid, reference), page)

    def test_clear_session_erases_native_state_and_artifact_index(self):
        result = self.run_task("artifact")
        self.service.clear_session(self.cid).result(10)
        self.assertEqual(self.service.artifacts(self.cid), [])
        with self.assertRaises(ValueError): self.service.artifact_page(self.cid, result.artifacts[0]["reference"])
        self.assertEqual(self.run_task("after deletion").reply, "DSH original answer")

    def test_official_presentation_can_deliver_exact_file_outside_workspace(self):
        result = self.run_task("artifact-external")
        self.assertEqual(result.status, "completed")
        self.assertEqual(len(result.artifacts), 1)
        self.assertEqual(self.service._file_grants, {})
        metadata, raw = self.service.artifact_snapshot(self.cid, result.artifacts[0]["reference"])
        self.assertEqual(raw, b"delivery")
        self.assertEqual(metadata["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(metadata["name"], "external.txt")

    def test_changed_or_private_presented_files_cannot_be_downloaded(self):
        for task in ("artifact-changed", "artifact-private"):
            with self.subTest(task=task):
                result = self.run_task(task)
                self.assertEqual(result.status, "needs_user")
                self.assertEqual(result.artifacts, ())
                self.assertTrue(result.reply.startswith("文件已生成"))
                self.assertEqual(self.service.artifacts(self.cid), [])

    def test_model_text_path_does_not_create_a_download(self):
        result = self.run_task("artifact-unpresented")
        self.assertEqual(result.artifacts, ())
        self.assertEqual(self.service.artifacts(self.cid), [])

    def test_artifact_opened_handle_must_match_exact_presented_file(self):
        expected = self.workspace / "expected.txt"
        substitute = self.root / "substitute.txt"
        expected.write_bytes(b"same bytes")
        substitute.write_bytes(b"same bytes")
        original = Path.open
        def replace_open(path, *args, **kwargs):
            return original(substitute if path == expected else path, *args, **kwargs)
        with patch.object(Path, "open", replace_open):
            with self.assertRaisesRegex(ValueError, "ARTIFACT_OUTSIDE_SCOPE"):
                self.service._artifact_store.register_path(self.cid, str(expected), workspace=self.workspace,
                    presented=True, expected_hash=hashlib.sha256(b"same bytes").hexdigest(), expected_size=10)

    def test_packaged_workspace_inside_private_profile_can_deliver_only_task_files(self):
        profile = self.root / "packaged-profile"
        workspace = profile / "agent_workspace"
        workspace.mkdir(parents=True)
        report = workspace / "report.txt"
        report.write_bytes(b"task report")
        private = profile / "settings.ini"
        private.write_bytes(b"task report")
        options = dict(workspace=workspace, presented=True, private_profile=profile,
            expected_hash=hashlib.sha256(b"task report").hexdigest(), expected_size=11)
        store = ArtifactStore(profile / "agent_artifacts")
        self.assertEqual(store.register_path(self.cid, str(report), **options)["bytes"], 11)
        with self.assertRaisesRegex(ValueError, "ARTIFACT_OUTSIDE_SCOPE"):
            store.register_path(self.cid, str(private), **options)

    def test_clear_running_session_waits_for_owned_worker_stop_before_erase(self):
        running = threading.Event()
        errors = []
        def run():
            try: self.run_task("cancel", progress_callback=lambda phase: running.set() if phase == "planning" else None)
            except AgentError as exc: errors.append(exc.code)
        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(running.wait(10))
        self.service.clear_session(self.cid).result(10)
        thread.join(10)
        self.assertEqual(errors, ["cancelled"])
        self.assertFalse(self.service.execution_resources_pending())
        self.assertFalse((self.root / "private" / "dsh_sessions" / self.cid).exists())
        self.assertEqual(self.run_task("fresh").reply, "DSH original answer")

    def test_long_dsh_reply_is_preserved_by_host(self):
        result = self.run_task("long")
        self.assertEqual(result.reply, "长回复。" * 30000)

    def test_api_scope_and_conversation_id_cannot_restore_each_others_session(self):
        self.run_task("first")
        self.context["conversation_id"] = str(uuid4())
        self.assertEqual(self.run_task("different conversation").reply, "DSH original answer")
        self.context["conversation_id"] = self.cid
        self.context["api"]["model"] = "different-fixture-model"
        self.assertEqual(self.run_task("different model").reply, "DSH original answer")

    def test_deleted_conversation_cannot_launch_after_paused_validation(self):
        entered, release = threading.Event(), threading.Event()
        original = self.service._context
        errors = []
        def paused(*args):
            entered.set()
            release.wait(10)
            return original(*args)
        def run():
            try: self.run_task("first")
            except AgentError as exc: errors.append(exc.code)
        with patch.object(self.service, "_context", side_effect=paused), \
                patch("desktop_pet.agents.service.subprocess.Popen") as launch:
            thread = threading.Thread(target=run)
            thread.start()
            self.assertTrue(entered.wait(10))
            deletion = self.service.clear_session(self.cid)
            release.set()
            thread.join(10)
            deletion.result(10)
            self.assertEqual(launch.call_count, 0)
        self.assertEqual(errors, ["cancelled"])

    def test_activity_projects_to_existing_mobile_trace(self):
        events = []
        self.run_task("first", event_callback=events.append)
        trace = public_agent_trace({"version": 1, "state": "completed", "started_at": "2026-11-14T00:00:00Z",
            "duration_ms": 10, "events": [{**events[0], "elapsed_ms": 1}]})
        self.assertEqual(trace["events"][0]["label"], "读取文件")

    def test_readiness_uses_official_dsh_package_metadata(self):
        self.assertTrue(self.service.readiness(self.settings, "fixture-key-not-real", self.context)["available"])
        with patch("desktop_pet.agents.service.dsh_installation", lambda: {"dsh": None, "dsh-sdk-client": None}):
            report = self.service.readiness(self.settings, "fixture-key-not-real", self.context)
        self.assertFalse(report["available"])
        self.assertEqual(report["reason_code"], "missing_sdk")

    def test_missing_node_produces_finished_no_launch_proof(self):
        attempts = []
        with patch("desktop_pet.agents.service.node_executable", return_value=None), \
                patch("desktop_pet.agents.service.subprocess.Popen") as launch:
            with self.assertRaises(AgentError) as raised:
                self.run_task("first", execution_attempt_callback=attempts.append)
        self.assertEqual(raised.exception.code, "gui_runtime_missing")
        launch.assert_not_called()
        receipt = self.service.execution_no_launch(attempts[0])
        self.assertTrue(self.service.validate_execution_no_launch(attempts[0], receipt))

    def test_credentials_do_not_enter_public_event_or_session_mapping(self):
        events = []
        self.run_task("first", event_callback=events.append)
        self.assertNotIn("fixture-key-not-real", json.dumps(events))
        for metadata in (self.root / "private" / "dsh_sessions").rglob("session.json"):
            self.assertNotIn("fixture-key-not-real", metadata.read_text(encoding="utf-8"))

    def test_unadvertised_tool_name_is_not_shown_as_raw_model_text(self):
        from desktop_pet.agents.activity import public_activity
        event = public_activity({"event_id": "safe-1", "kind": "tool", "name": "private-name",
                                 "status": "running", "arguments": "private-arguments"})
        self.assertEqual(event["label"], "使用任务工具")
        self.assertNotIn("private-", json.dumps(event))


class DshRuntimeEnvironmentTests(unittest.TestCase):
    def test_frozen_path_prefers_owned_python_and_node_without_injected_env(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            python = root / "runtime" / "python" / "python.exe"
            node = root / "runtime" / "node" / "node.exe"
            with patch.dict(os.environ, {"Path": "inherited-system-path", "DEEPSEEK_API_KEY": "synthetic-only",
                                         "NODE_OPTIONS": "untrusted-injection"}, clear=True), \
                    patch("desktop_pet.agents.service.sys.frozen", True, create=True), \
                    patch("desktop_pet.agents.service.bundled_python_executable", return_value=python), \
                    patch("desktop_pet.agents.service.bundled_node_executable", return_value=node):
                environment = worker_environment(root / "dsh-home")
            self.assertEqual(environment["PATH"].split(os.pathsep),
                             [str(python.parent), str(node.parent), "inherited-system-path"])
            self.assertFalse("Path" in environment)
            self.assertFalse("DEEPSEEK_API_KEY" in environment)
            self.assertFalse("NODE_OPTIONS" in environment)
            self.assertEqual(environment["DSH_HOME"], str(root / "dsh-home"))

    def test_source_run_keeps_existing_runtime_search_path(self):
        with patch.dict(os.environ, {"PATH": "source-python-and-node"}, clear=True), \
                patch("desktop_pet.agents.service.sys.frozen", False, create=True):
            environment = worker_environment("synthetic-home")
        self.assertEqual(environment["PATH"], "source-python-and-node")


if __name__ == "__main__": unittest.main()
