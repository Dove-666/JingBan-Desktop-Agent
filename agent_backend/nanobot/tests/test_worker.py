"""Offline acceptance: real nanobot SDK with a loopback model stub, no GUI input."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from uuid import uuid4

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parents[1]
sys.path.insert(0, str(BACKEND))
from safe_files import WorkspaceFiles

PYTHON = BACKEND / ".venv" / "Scripts" / "python.exe"
KEY = "dummy-worker-secret-7d8351"


class ModelHandler(BaseHTTPRequestHandler):
    calls = []
    behavior = "normal"

    def log_message(self, *_):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).calls.append(body)
        behavior = self.behavior
        turns = [m for m in body["messages"] if m["role"] == "tool"]
        if behavior == "hang":
            time.sleep(8)
        if behavior in {"error", "context_error"} or (behavior == "computer_error" and turns):
            result = {"error": {"message": KEY, "type": "invalid_request_error",
                                **({"code": "context_length_exceeded"} if behavior == "context_error" else {})}}
            self.send_response(400 if behavior == "context_error" else 401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())
            return
        tool = None
        if behavior in {"computer", "computer_error", "computer_twice"}:
            if not turns or behavior == "computer_twice" and len(turns) < 2:
                tool = {"id": f"call_gui_{len(turns)}", "type": "function", "function": {"name": "computer_use", "arguments": json.dumps({"task": "model-authored replacement " + str(len(turns))})}}
        elif behavior == "plain":
            pass
        elif not turns:
            tool = {"id": "call_write", "type": "function", "function": {"name": "write_file", "arguments": json.dumps({"path": "result.txt", "content": "Z" * 20000 if behavior == "long_result" else "AURA offline test"})}}
        elif (len(turns) == 1 or behavior == "endless_tools"
              or behavior == "long_tools" and len(turns) < 45):
            tool = {"id": f"call_read_{len(turns)}", "type": "function", "function": {"name": "read_file", "arguments": json.dumps({"path": "result.txt"})}}
        message = {"role": "assistant", "content": None if tool else "已创建并读取 result.txt。 " + KEY}
        if tool:
            message["tool_calls"] = [tool]
        result = {"id": "local-test", "object": "chat.completion", "created": 1, "model": "test-model",
                  "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if tool else "stop"}],
                  "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}}
        self.send_response(200)
        if body.get("stream"):
            delta = dict(message)
            if tool:
                delta["tool_calls"] = [{"index": 0, **tool}]
            first = {"id": "local-test", "object": "chat.completion.chunk", "created": 1, "model": "test-model", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
            last = {"id": "local-test", "object": "chat.completion.chunk", "created": 1, "model": "test-model", "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if tool else "stop"}]}
            data = ("data: " + json.dumps(first) + "\n\ndata: " + json.dumps(last) + "\n\ndata: [DONE]\n\n").encode()
            self.send_header("Content-Type", "text/event-stream")
        else:
            data = json.dumps(result).encode()
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except ConnectionError:
            pass


class Client:
    def __init__(self, env=None, *, worker_path=None):
        self.process = subprocess.Popen([str(PYTHON), "-I", "-u", str(worker_path or BACKEND / "worker.py")], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                        creationflags=0x08000000 if os.name == "nt" else 0, env=env)
        self.frames = queue.Queue()
        self.all_frames = []
        def read():
            for line in self.process.stdout:
                frame = json.loads(line)
                self.all_frames.append(frame)
                self.frames.put(frame)
        threading.Thread(target=read, daemon=True).start()
        assert self.frames.get(timeout=15)["type"] == "ready"

    def send(self, frame):
        self.process.stdin.write(json.dumps(frame) + "\n")
        self.process.stdin.flush()

    def terminal(self, timeout=35):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            frame = self.frames.get(timeout=max(0.1, deadline - time.monotonic()))
            if frame["type"] in {"final", "error"} and frame.get("code") != "BUSY":
                return frame
        raise AssertionError("no terminal frame")

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
        self.process.wait(timeout=10)
        if self.process.stdin:
            self.process.stdin.close()
        stderr = self.process.stderr.read()
        self.process.stderr.close()
        self.process.stdout.close()
        return stderr


class FileTests(unittest.TestCase):
    def test_workspace_guard_and_no_default_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = WorkspaceFiles(Path(tmp))
            files.write("a.txt", "hello")
            self.assertEqual(files.read("a.txt"), "hello")
            with self.assertRaises(FileExistsError):
                files.write("a.txt", "replacement")
            with self.assertRaises(ValueError):
                files.read("../outside.txt")
            self.assertIn('"path": "a.txt"', files.search("hello"))


@unittest.skipUnless(PYTHON.is_file(), "isolated environment missing")
class WorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), ModelHandler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        ModelHandler.calls = []
        ModelHandler.behavior = "normal"
        root = REPO / "agent_workspace"
        root.mkdir(exist_ok=True)
        self.workspace = Path(tempfile.mkdtemp(prefix="worker-test-", dir=root))
        self.client = Client()

    def tearDown(self):
        self.assertEqual(self.client.close(), "")
        target = self.workspace.resolve()
        self.assertTrue(target.is_relative_to((REPO / "agent_workspace").resolve()))
        shutil.rmtree(target)

    def payload(self, **options):
        return {"type": "run", "id": "test-run", "kind": "tools", "task": "创建并读取工作目录里的 result.txt",
                "config": {"base_url": f"http://127.0.0.1:{self.server.server_port}/v1", "api_key": KEY, "model": "test-model"},
                "options": {"workspace": str(self.workspace), "max_steps": 5, "timeout_seconds": 30, **options}}

    def test_real_sdk_tools_loop_allowlist_and_redaction(self):
        payload = self.payload()
        payload["config"]["reasoning_effort"] = "default"
        payload["context"] = "x" * 64000
        self.client.send(payload)
        result = self.client.terminal()
        self.assertEqual(result["type"], "final", result)
        self.assertEqual(result["status"], "completed")
        self.assertEqual((self.workspace / "result.txt").read_text(), "AURA offline test")
        self.assertEqual(set(result["tools_used"]), {"write_file", "read_file"})
        self.assertEqual({tool["function"]["name"] for tool in ModelHandler.calls[0]["tools"]},
                         {"read_file", "write_file", "search_files", "web_fetch", "calculate", "analyze_csv",
                          "verify_artifact", "update_plan", "finish_task"})
        self.assertIn("one user-authorized task", ModelHandler.calls[0]["messages"][0]["content"])
        self.assertNotIn("Dream", ModelHandler.calls[0]["messages"][0]["content"])
        self.assertNotIn(KEY, json.dumps(self.client.all_frames))
        self.assertFalse(any(self.workspace.rglob("MEMORY.md")))

    def test_absent_iteration_limit_defaults_to_unlimited(self):
        payload = self.payload()
        payload["options"].pop("max_steps")
        self.client.send(payload)
        result = self.client.terminal()
        self.assertEqual(result.get("status"), "completed", result)

    def test_absent_task_timeout_defaults_to_unlimited(self):
        payload = self.payload()
        payload["options"].pop("timeout_seconds")
        self.client.send(payload)
        result = self.client.terminal()
        self.assertEqual(result.get("status"), "completed", result)

    def test_zero_task_timeout_allows_real_sdk_tool_loop_to_finish(self):
        ModelHandler.behavior = "long_tools"
        self.client.send(self.payload(timeout_seconds=0, max_steps=0))
        result = self.client.terminal()
        self.assertEqual(result.get("status"), "completed", result)
        self.assertEqual(len(ModelHandler.calls), 46)
        self.assertEqual((self.workspace / "result.txt").read_text(), "AURA offline test")

    def test_invalid_task_timeouts_are_rejected_before_model(self):
        for index, limit in enumerate((-1, True, False, 0.5, float("nan"), float("inf"), None, 1801)):
            with self.subTest(limit=limit):
                if index:
                    self.assertEqual(self.client.close(), "")
                    self.client = Client()
                self.client.send(self.payload(timeout_seconds=limit))
                self.assertEqual(self.client.terminal()["code"], "INVALID_REQUEST")
        self.assertEqual(ModelHandler.calls, [])

    def test_model_context_and_public_tool_events_have_no_application_small_caps(self):
        ModelHandler.behavior = "long_result"
        payload = self.payload(include_activity=True)
        payload["context"] = "reference " + "x" * 1_100_000
        self.client.send(payload)
        result = self.client.terminal()
        self.assertEqual(result.get("status"), "completed", result)
        self.assertIn(payload["context"], ModelHandler.calls[0]["messages"][-1]["content"])
        for call in ModelHandler.calls:
            self.assertFalse({"max_tokens", "max_completion_tokens", "max_output_tokens"} & call.keys())
        tool_messages = [item for call in ModelHandler.calls for item in call["messages"] if item["role"] == "tool"]
        self.assertTrue(any("Z" * 20000 in item["content"] for item in tool_messages))
        events = [item for item in self.client.all_frames if item["type"] == "activity"]
        self.assertEqual([(item["name"], item["status"]) for item in events],
                         [("write_file", "running"), ("write_file", "completed"), ("read_file", "running"), ("read_file", "completed")])
        self.assertTrue(all(set(item) <= {"type", "id", "kind", "name", "event_id", "status"} for item in events))
        self.assertNotIn(KEY, json.dumps(events))

    def test_service_context_capacity_error_is_specific_and_not_retried(self):
        ModelHandler.behavior = "context_error"
        self.client.send(self.payload())
        result = self.client.terminal()
        self.assertEqual(result.get("code"), "MODEL_CONTEXT_EXCEEDED")
        self.assertEqual(len(ModelHandler.calls), 1)
        self.assertNotIn(KEY, json.dumps(self.client.all_frames))

    def test_invalid_iteration_limits_are_rejected_before_model(self):
        for index, limit in enumerate((-1, True, "0", 0.0, 101, 10**30)):
            with self.subTest(limit=limit):
                if index:
                    self.assertEqual(self.client.close(), "")
                    self.client = Client()
                self.client.send(self.payload(max_steps=limit))
                self.assertEqual(self.client.terminal()["code"], "INVALID_REQUEST")
        self.assertEqual(ModelHandler.calls, [])

    def test_provider_error_is_static(self):
        ModelHandler.behavior = "error"
        self.client.send(self.payload())
        result = self.client.terminal()
        self.assertEqual(result, {"type": "error", "id": "test-run", "code": "AGENT_FAILED"})
        self.assertNotIn(KEY, json.dumps(self.client.all_frames))

    def test_cancel_and_busy_do_not_start_second_task(self):
        ModelHandler.behavior = "hang"
        self.client.send(self.payload(timeout_seconds=0))
        self.client.send({**self.payload(timeout_seconds=0), "id": "other"})
        deadline = time.monotonic() + 12
        while not ModelHandler.calls and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(ModelHandler.calls, "the real SDK request must be in flight")
        cancelled_at = time.monotonic()
        self.client.send({"type": "cancel", "id": "test-run"})
        result = self.client.terminal()
        self.assertEqual(result.get("status"), "cancelled", result)
        self.assertLess(time.monotonic() - cancelled_at, 3.0)
        self.assertTrue(any(frame.get("code") == "BUSY" for frame in self.client.all_frames))

    def test_timeout_returns_static_code(self):
        ModelHandler.behavior = "hang"
        self.client.send(self.payload(timeout_seconds=2))
        self.assertEqual(self.client.terminal()["code"], "TIMEOUT")

    def test_plain_model_completion_without_tool_is_needs_user(self):
        ModelHandler.behavior = "plain"
        self.client.send(self.payload())
        result = self.client.terminal()
        self.assertEqual(result.get("status"), "needs_user", result)
        self.assertEqual(result["tools_used"], [])
        self.assertFalse((self.workspace / "result.txt").exists())

    def test_iteration_limit_is_not_completed_even_with_tool_results(self):
        self.client.send(self.payload(max_steps=2))
        result = self.client.terminal()
        self.assertEqual(result.get("status"), "needs_user", result)
        self.assertTrue((self.workspace / "result.txt").exists())

    def test_unlimited_real_sdk_executes_more_than_forty_tool_rounds_then_finishes(self):
        ModelHandler.behavior = "long_tools"
        self.client.send(self.payload(max_steps=0))
        result = self.client.terminal()
        self.assertEqual(result.get("status"), "completed", result)
        self.assertEqual(len(ModelHandler.calls), 46)
        self.assertEqual(len([message for message in ModelHandler.calls[-1]["messages"]
                              if message.get("role") == "tool"]), 45)
        self.assertEqual((self.workspace / "result.txt").read_text(), "AURA offline test")

    def test_unlimited_real_sdk_can_cancel_after_more_than_twelve_rounds(self):
        ModelHandler.behavior = "endless_tools"
        self.client.send(self.payload(max_steps=0))
        deadline = time.monotonic() + 20
        while len(ModelHandler.calls) < 15 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertGreaterEqual(len(ModelHandler.calls), 15)
        started = time.monotonic()
        self.client.send({"type": "cancel", "id": "test-run"})
        result = self.client.terminal()
        self.assertEqual(result.get("status"), "cancelled", result)
        self.assertLess(time.monotonic() - started, 3)

    def test_unlimited_steps_can_still_use_an_explicit_finite_qa_timeout(self):
        ModelHandler.behavior = "hang"
        self.client.send(self.payload(max_steps=0, timeout_seconds=2))
        self.assertEqual(self.client.terminal()["code"], "TIMEOUT")

    def test_escape_and_credential_url_rejected_before_model(self):
        payload = self.payload(workspace=str(REPO))
        payload["config"]["base_url"] = "https://user:password@example.invalid/v1"
        self.client.send(payload)
        self.assertEqual(self.client.terminal()["code"], "INVALID_REQUEST")
        self.assertEqual(ModelHandler.calls, [])

    def test_computer_requires_separate_gui_credentials(self):
        payload = self.payload()
        payload["kind"] = "computer"
        self.client.send(payload)
        self.assertEqual(self.client.terminal()["code"], "INVALID_REQUEST")
        self.assertEqual(ModelHandler.calls, [])

    def test_single_unknown_application_does_not_call_model_or_gui(self):
        # This unique nonexistent shortcut name is resolved read-only. No real
        # application can be selected or launched by this worker acceptance test.
        payload = self.payload()
        payload["kind"] = "computer"
        payload["task"] = "打开 AURA_Uninstalled_" + uuid4().hex
        payload["config"]["gui"] = {"base_url": "https://example.invalid/v1", "api_key": KEY,
                                     "model": "test-gui-model", "provider": "ui-tars-1.5"}
        self.client.send(payload)
        result = self.client.terminal()
        self.assertEqual(result["type"], "final", result)
        self.assertEqual(result["status"], "needs_user")
        self.assertEqual(result["tools_used"], ["open_application"])
        # A UAC prompt already open on the host takes precedence over even
        # read-only shortcut resolution; both outcomes must avoid any launch.
        self.assertTrue("没有找到" in result["reply"] or "管理员授权" in result["reply"], result)
        self.assertEqual(ModelHandler.calls, [])

    def test_invalid_gui_coordinate_profile_rejected_before_any_tool(self):
        payload = self.payload()
        payload["kind"] = "computer"
        payload["config"]["gui"] = {"base_url": "https://example.invalid/v1", "api_key": KEY,
                                     "model": "test-gui-model", "provider": "ui-tars-1.0",
                                     "coordinate_profile": "pixel"}
        self.client.send(payload)
        self.assertEqual(self.client.terminal()["code"], "INVALID_REQUEST")
        self.assertEqual(ModelHandler.calls, [])

    def test_real_sdk_preserves_missing_gui_runtime_code_without_native_input(self):
        self.check_missing_gui_runtime("computer")

    def test_gui_failure_is_not_masked_by_later_model_failure(self):
        self.check_missing_gui_runtime("computer_error")

    def test_real_sdk_second_computer_call_reuses_terminal_gui_result_without_native_replay(self):
        # The harness replaces only the physical GUI dispatch. It retains the
        # production GuiBridge.run cache, real tool registry and real SDK loop.
        for status in ("completed", "needs_user"):
            with self.subTest(status=status):
                self.assertEqual(self.client.close(), "")
                counter = self.workspace / (status + "-gui-count.txt")
                harness = self.workspace / (status + "-worker-harness.py")
                harness.write_text(
                    "import asyncio,importlib.util,json\nfrom pathlib import Path\n"
                    f"spec=importlib.util.spec_from_file_location('aura_qa_worker',{str(BACKEND / 'worker.py')!r})\n"
                    "worker=importlib.util.module_from_spec(spec)\nspec.loader.exec_module(worker)\n"
                    "from gui_verification import report\n"
                    "class SyntheticGui(worker.GuiBridge):\n"
                    "    async def _run_once(self, task):\n"
                    "        assert self.options['timeout_seconds']==0\n"
                    "        assert self.deadline_monotonic is None\n"
                    "        assert self._timeout_budget_ms()==0\n"
                    f"        counter=Path({str(counter)!r})\n"
                    "        counter.write_text(str(int(counter.read_text())+1) if counter.exists() else '1')\n"
                    f"        self.last_status={status!r}\n        self.steps=2\n"
                    f"        self.verification=report({'verified' if status == 'completed' else 'unverified'!r})\n"
                    f"        return json.dumps({{'status':{status!r},'summary':'verified original GUI outcome','steps':2}})\n"
                    "worker.GuiBridge=SyntheticGui\nasyncio.run(worker.main())\n", encoding="utf-8")
                self.client = Client(worker_path=harness)
                ModelHandler.calls = []
                ModelHandler.behavior = "computer_twice"
                payload = self.payload(timeout_seconds=0)
                payload["kind"] = "computer"
                payload["task"] = "切换到电脑桌面，查看桌面有哪些软件图标"
                payload["config"]["gui"] = {"base_url": "https://example.invalid/v1", "api_key": KEY,
                    "model": "synthetic-no-native-model", "provider": "ui-tars-1.5"}
                self.client.send(payload)
                result = self.client.terminal()
                self.assertEqual(result["type"], "final", result)
                self.assertEqual(result["status"], status)
                self.assertEqual(counter.read_text(), "1")
                self.assertEqual(len(ModelHandler.calls), 3)
                tool_results = [item["content"] for item in ModelHandler.calls[-1]["messages"] if item["role"] == "tool"]
                self.assertEqual(len(tool_results), 2)
                self.assertEqual(tool_results[0], tool_results[1])
                cached = json.loads(tool_results[0])
                self.assertEqual(cached, {"status": status, "summary": "verified original GUI outcome", "steps": 2})
                self.assertFalse(any(item.get("type") == "error" for item in self.client.all_frames))
                self.assertNotIn(KEY, json.dumps(self.client.all_frames))

    def check_missing_gui_runtime(self, behavior):
        # Empty PATH proves no Node process (and therefore no screenshot/input)
        # can start. The actual SDK still receives and executes computer_use.
        self.assertEqual(self.client.close(), "")
        self.client = Client(env={**os.environ, "PATH": ""})
        ModelHandler.behavior = behavior
        payload = self.payload()
        payload["kind"] = "computer"
        payload["config"]["gui"] = {"base_url": "https://example.invalid/v1", "api_key": KEY,
                                     "model": "test-gui-model", "provider": "ui-tars-1.5"}
        self.client.send(payload)
        result = self.client.terminal()
        self.assertEqual(result, {"type": "error", "id": "test-run", "code": "GUI_RUNTIME_MISSING"})
        self.assertEqual(len(ModelHandler.calls), 2)
        self.assertNotIn(KEY, json.dumps(self.client.all_frames))
        self.assertNotIn("example.invalid", json.dumps(self.client.all_frames))


if __name__ == "__main__":
    unittest.main(verbosity=2)
