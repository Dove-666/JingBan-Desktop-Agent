"""Actual host + SDK + localhost faults; these are not model benchmarks."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from dataclasses import replace
import base64
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from agent_backend.nanobot.tests.test_worker import PYTHON, BACKEND, REPO
from desktop_pet.agents import AgentError, AgentRequest, AgentService, AgentSettings
from desktop_pet.agents import service as host_runtime

SECRET = "synthetic-fault-body-never-public"
TASK = 'Create report.txt containing exactly "HELLO". Then complete the rest of the task.'


class FaultModel:
    def __init__(self, steps, *, on_fault=None):
        self.steps, self.on_fault, self.calls, self.errors = list(steps), on_fault, [], []
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                index = len(owner.calls)
                owner.calls.append(body)
                if index >= len(owner.steps):
                    owner.errors.append("UNEXPECTED_REQUEST")
                    step = "FAULT"
                else: step = owner.steps[index]
                if callable(step): step = step(body)
                if step == "FAULT":
                    if owner.on_fault: owner.on_fault()
                    payload = json.dumps({"error": {"message": SECRET, "code": "invalid_api_key"}}).encode()
                    self.send_response(401)
                    self.send_header("Content-Type", "application/json")
                else:
                    message = {"role": "assistant", "content": None if isinstance(step, tuple) else step}
                    reason = "stop"
                    if isinstance(step, tuple):
                        name, args = step
                        message["tool_calls"] = [{"id": "owned-" + str(index), "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)}}]
                        reason = "tool_calls"
                    self.send_response(200)
                    if body.get("stream"):
                        if "tool_calls" in message:
                            message["tool_calls"][0]["index"] = 0
                        payload = ("data: " + json.dumps({"id": "owned", "object": "chat.completion.chunk",
                            "model": "offline", "choices": [{"index": 0, "delta": message, "finish_reason": None}]})
                            + "\n\ndata: " + json.dumps({"id": "owned", "object": "chat.completion.chunk", "model": "offline",
                            "choices": [{"index": 0, "delta": {}, "finish_reason": reason}]}) + "\n\ndata: [DONE]\n\n").encode()
                        self.send_header("Content-Type", "text/event-stream")
                    else:
                        payload = json.dumps({"id": "owned", "object": "chat.completion", "model": "offline",
                            "choices": [{"index": 0, "message": message, "finish_reason": reason}]}).encode()
                        self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                try: self.wfile.write(payload)
                except ConnectionError: pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads, self.server.block_on_close = True, False
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}/v1"

    def close(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(2)


@unittest.skipUnless(PYTHON.is_file(), "isolated SDK environment unavailable")
class PartialDeliveryRuntimeTests(unittest.TestCase):
    def setUp(self):
        root = REPO / "agent_workspace"
        root.mkdir(exist_ok=True)
        self.owned = tempfile.TemporaryDirectory(prefix="partial-runtime-", dir=root)
        self.root = Path(self.owned.name)
        self.workspace = self.root / "files"
        self.workspace.mkdir()
        self.service = AgentService(checkpoint_path=self.root / "state" / "checkpoints.sqlite3")
        self.settings = AgentSettings(python_executable=str(PYTHON), workspace=str(self.workspace), timeout_seconds=20)
        self.cid, self.models, self.diagnostics, self.handles = str(uuid4()), [], [], []

    def tearDown(self):
        self.service.shutdown()
        for model in self.models:
            model.close()
            self.assertEqual(model.errors, [])
        self.owned.cleanup()

    def execute(self, steps, *, on_fault=None, wrapper=None, max_steps=None, computer=False, cancel=None):
        model = FaultModel(steps, on_fault=on_fault)
        self.models.append(model)
        if max_steps is not None: self.settings = replace(self.settings, max_steps=max_steps)
        if computer:
            self.settings = replace(self.settings, computer_enabled=True,
                                    gui_base_url=model.base_url, gui_model="offline-owned-gui")
        target = host_runtime._WORKER_PATH
        if wrapper:
            target = self.root / "owned-worker.py"
            target.write_text("import asyncio,importlib.util\n" +
                f"spec=importlib.util.spec_from_file_location('owned_worker',{str(BACKEND / 'worker.py')!r})\n" +
                "worker=importlib.util.module_from_spec(spec);spec.loader.exec_module(worker)\n" +
                wrapper + "\nasyncio.run(worker.main())\n", encoding="utf-8")
        with patch.object(host_runtime, "_WORKER_PATH", target):
            value = self.service.execute(self.settings, "synthetic-loopback-key", AgentRequest(TASK, "agent"),
                {"api": {"base_url": model.base_url, "model": "offline-partial"},
                 "gui_api_key": "synthetic-gui-key" if computer else "",
                 "conversation_id": self.cid, "messages": []}, diagnostic_callback=self.diagnostics.append,
                worker_lifecycle_callback=self.handles.append, cancellation_event=cancel)
        self.assertEqual(len(model.calls), len(steps), "Unexpected model retry or extra request")
        self.assertNotIn(SECRET, json.dumps(self.diagnostics))
        return value

    def write(self):
        return ("write_file", {"path": "report.txt", "content": "HELLO"})

    def assert_partial(self, value, code):
        self.assertEqual(value.status, "needs_user")
        self.assertFalse(value.acceptance["passed"])
        self.assertEqual(len(value.artifacts), 1)
        item = value.artifacts[0]
        self.assertIn("whole_task_incomplete", item["verification_scope"])
        raw = base64.b64decode(self.service.artifact_page(self.cid, item["reference"])["data_base64"])
        self.assertEqual(raw, b"HELLO")
        self.assertIn(code, [item["code"] for item in self.diagnostics])
        self.assertTrue(all(set(item) == {"phase", "code"} and item["phase"] in {"model", "runtime"}
                            for item in self.diagnostics))
        with self.assertRaises(ValueError):
            self.service.artifact_page(str(uuid4()), item["reference"])
        self.assertNotIn(SECRET, value.reply)
        self.assertEqual(len(self.handles), 1)
        receipt = self.service.worker_closure(self.handles[0])
        self.assertIsNotNone(receipt)
        self.assertTrue(receipt.reaped)
        self.assertEqual(receipt.conversation_id, self.cid)
        self.assertTrue(self.service.validate_worker_closure(self.handles[0], receipt))

    def test_stop_beats_a_late_model_reply_and_requires_exact_worker_reaping(self):
        entered, release, cancel = threading.Event(), threading.Event(), threading.Event()
        outcomes = []
        def late_reply(_):
            entered.set()
            release.wait(10)
            return "LATE_UNACCEPTED_REPLY"
        def run():
            try: outcomes.append(self.execute([self.write(), late_reply], cancel=cancel))
            except AgentError as exc: outcomes.append(exc)
        thread = threading.Thread(target=run)
        thread.start()
        try:
            self.assertTrue(entered.wait(10))
            self.service.cancel_request(cancel)
            release.set()
            thread.join(10)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(outcomes), 1)
            self.assertIsInstance(outcomes[0], AgentError)
            self.assertEqual(outcomes[0].code, "cancelled")
            self.assertEqual(self.service.artifacts(self.cid), [])
            handle = self.handles[0]
            receipt = self.service.worker_closure(handle)
            self.assertTrue(self.service.validate_worker_closure(handle, receipt))
            self.assertEqual(self.service._processes, {})
            self.assertFalse(any("LATE_UNACCEPTED_REPLY" in row.get("content", "")
                                 for row in self.service._sessions.get(self.cid, [])))
        finally:
            release.set()
            thread.join(10)

    def test_written_file_survives_real_model_auth_failure_as_partial(self):
        result = self.execute([self.write(), "FAULT"])
        self.assert_partial(result, "MODEL_REQUEST_AUTH_FAILED")
        self.assertEqual((self.workspace / "report.txt").read_text(), "HELLO")

    def test_no_artifact_preserves_original_auth_error(self):
        with self.assertRaises(AgentError) as raised:
            self.execute(["FAULT"])
        self.assertEqual(raised.exception.code, "provider_auth_failed")
        self.assertEqual(self.service.artifacts(self.cid), [])
        self.assertNotIn(SECRET, str(raised.exception))

    def test_noncompleted_sdk_step_stop_keeps_only_partial_verified_file(self):
        # The SDK itself requests one end-of-budget summary. Partial delivery
        # must not add another request or execute another tool after it.
        result = self.execute([self.write(), "Unaccepted budget-stop summary."], max_steps=1)
        self.assert_partial(result, "EXECUTION_STEP_LIMIT")

    def test_sdk_failure_without_outputs_preserves_generic_execution_error(self):
        wrapper = ("import nanobot\nOriginal=nanobot.Nanobot\nclass Broken(Original):\n"
            " async def run(self,*args,**kwargs):\n"
            f"  raise RuntimeError({SECRET!r})\n"
            "nanobot.Nanobot=Broken\n")
        with self.assertRaises(AgentError) as raised:
            self.execute([], wrapper=wrapper)
        self.assertEqual(raised.exception.code, "agent_failed")
        self.assertIn("执行链中断", raised.exception.message)
        self.assertNotIn("模型请求失败", raised.exception.message)
        self.assertEqual(self.diagnostics, [{"phase": "runtime", "code": "SDK_TURN_FAILED_UNKNOWN_CAUSE"}])

    def test_changed_written_version_is_not_delivered(self):
        def mutate(): (self.workspace / "report.txt").write_text("CHANGED", encoding="utf-8")
        with self.assertRaises(AgentError):
            self.execute([self.write(), "FAULT"], on_fault=mutate)
        self.assertEqual(self.service.artifacts(self.cid), [])

    def test_sdk_failure_after_successful_provider_response_is_not_misattributed(self):
        wrapper = ("import nanobot\nOriginal=nanobot.Nanobot\nclass Broken(Original):\n"
            " async def run(self,*args,**kwargs):\n"
            "  await super().run(*args,**kwargs)\n"
            f"  raise RuntimeError({SECRET!r})\n"
            "nanobot.Nanobot=Broken\n")
        result = self.execute([self.write(), "Unaccepted final text."], wrapper=wrapper)
        self.assert_partial(result, "SDK_TURN_FAILED_UNKNOWN_CAUSE")
        self.assertNotIn("MODEL_REQUEST_FAILED", [item["code"] for item in self.diagnostics])

    def test_unknown_gui_effect_is_not_cleared_by_partial_file_delivery(self):
        wrapper = ("import browser_session,json\nclass OwnedBrowser:\n"
            " def available(self):return True\n"
            " async def navigate(self,**kwargs):return json.dumps({'effect_state':'uncertain','pending_effect':True})\n"
            " async def observe(self,**kwargs):return '{}'\n"
            " async def act(self,**kwargs):return '{}'\n"
            " async def tabs(self,**kwargs):return '{}'\n"
            " async def switch(self,**kwargs):return '{}'\n"
            " async def close(self):pass\n"
            "browser_session.BrowserSession=OwnedBrowser\n")
        result = self.execute([self.write(), ("browser_navigate", {"url": "https://fixture.invalid"}), "FAULT"], wrapper=wrapper)
        self.assert_partial(result, "MODEL_REQUEST_AUTH_FAILED")
        self.assertIn("GOAL_EFFECT_UNRESOLVED", result.acceptance["issue_codes"])
        self.assertIn("UNRESOLVED_TOOL_FAILURE", result.acceptance["issue_codes"])

    def test_actual_gui_parent_gate_survives_interrupted_partial_delivery(self):
        wrapper = ("import json\nclass OwnedGui:\n"
            " def __init__(self,*args,**kwargs):self.failed=False;self.failure_code='';self.verification=None;self.verification_scope='parent'\n"
            " async def run(self,*args,**kwargs):return json.dumps({'effect_state':'uncertain','pending_effect':True})\n"
            " async def close(self):pass\n"
            "worker.GuiBridge=OwnedGui\n")
        result = self.execute([self.write(), ("computer_use", {"task": "Inspect the owned fixture"}), "FAULT"],
                              wrapper=wrapper, computer=True)
        self.assert_partial(result, "MODEL_REQUEST_AUTH_FAILED")
        self.assertIn("GUI_GOAL_UNVERIFIED", result.acceptance["issue_codes"])
        self.assertIn("GOAL_EFFECT_UNRESOLVED", result.acceptance["issue_codes"])
        self.assertIn("UNRESOLVED_TOOL_FAILURE", result.acceptance["issue_codes"])

    def test_true_sdk_cleanup_failure_retains_registry_and_partial_files_until_later_proof(self):
        with patch.object(host_runtime._ProcessTree, "confirm_closed", return_value=False):
            result = self.execute([self.write(), "FAULT"])
        self.assertEqual(result.status, "needs_user")
        self.assertEqual(len(result.artifacts), 1)
        self.assertIn("WORKER_CLEANUP_UNCONFIRMED", result.acceptance["issue_codes"])
        self.assertIn("后台进程和工具", result.reply)
        self.assertIsNone(self.service.worker_closure(self.handles[0]))
        self.assertTrue(self.service._processes)
        self.assertIn("WORKER_CLEANUP_UNCONFIRMED", [item["code"] for item in self.diagnostics])
        receipts = self.service.reconcile_worker_closures(timeout_seconds=.2)
        self.assertEqual(len(receipts), 1)
        self.assertTrue(self.service.validate_worker_closure(self.handles[0], receipts[0]))
        self.assertEqual(self.service._processes, {})


if __name__ == "__main__": unittest.main()
