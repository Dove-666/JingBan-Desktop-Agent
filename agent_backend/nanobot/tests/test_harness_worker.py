"""Real nanobot subprocess against scripted loopback models; no real API/GUI."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import base64
import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import sys
import tempfile
import threading
import time
import unittest
from uuid import uuid4

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parents[1]
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND / "tests"))
from test_worker import Client, PYTHON


KEY = "harness-local-fixture-token-only"


def tool(name, **arguments):
    return name, arguments


def last_observation(body, name):
    matching = [message for message in body["messages"] if message.get("role") == "tool" and message.get("name") == name]
    if not matching:
        raise AssertionError("Expected tool observation was absent: " + name)
    return json.loads(matching[-1]["content"])


class ScriptedModel:
    def __init__(self, steps):
        self.steps = list(steps)
        self.calls, self.errors = [], []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                from fixture_goal_judge import judge_message, send_judge
                judged = judge_message(body)
                if judged is not None:
                    send_judge(self, judged)
                    return
                index = len(owner.calls)
                owner.calls.append(body)
                try:
                    if index >= len(owner.steps):
                        raise AssertionError("Model made an unexpected additional iteration")
                    step = owner.steps[index]
                    step = step(body) if callable(step) else step
                    call = None
                    if isinstance(step, tuple):
                        name, arguments = step
                        call = {"id": "harness-call-" + str(index), "type": "function", "function": {
                            "name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}
                    message = {"role": "assistant", "content": None if call else str(step)}
                    if call:
                        message["tool_calls"] = [call]
                except Exception as exc:
                    owner.errors.append(type(exc).__name__ + ": " + str(exc))
                    self.send_response(500)
                    payload = b'{"error":{"message":"Fixture assertion failed"}}'
                    self.send_header("Content-Type", "application/json")
                else:
                    self.send_response(200)
                    reason = "tool_calls" if call else "stop"
                    if body.get("stream"):
                        delta = dict(message)
                        if call:
                            delta["tool_calls"] = [{"index": 0, **call}]
                        chunks = [
                            {"id": "harness", "object": "chat.completion.chunk", "created": 1, "model": "offline",
                             "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                            {"id": "harness", "object": "chat.completion.chunk", "created": 1, "model": "offline",
                             "choices": [{"index": 0, "delta": {}, "finish_reason": reason}]},
                        ]
                        payload = ("".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
                                   + "data: [DONE]\n\n").encode()
                        self.send_header("Content-Type", "text/event-stream")
                    else:
                        payload = json.dumps({"id": "harness", "object": "chat.completion", "created": 1, "model": "offline",
                                              "choices": [{"index": 0, "message": message, "finish_reason": reason}]}).encode()
                        self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                try:
                    self.wfile.write(payload)
                except ConnectionError:
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.server.block_on_close = False
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}/v1"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


@unittest.skipUnless(PYTHON.is_file(), "isolated nanobot SDK environment missing")
class HarnessWorkerTests(unittest.TestCase):
    def setUp(self):
        root = REPO / "agent_workspace"
        root.mkdir(exist_ok=True)
        self.workspace = Path(tempfile.mkdtemp(prefix="harness-worker-test-", dir=root))
        self.clients, self.models = [], []

    def tearDown(self):
        for client in self.clients:
            self.assertEqual(client.close(), "")
        for model in self.models:
            model.close()
            self.assertEqual(model.errors, [])
        target = self.workspace.resolve()
        self.assertTrue(target.is_relative_to((REPO / "agent_workspace").resolve()))
        shutil.rmtree(target)

    def start(self, steps, *, checkpoint_required=False, worker_path=None, computer=False, tools=True, image=False, steps_limit=20, task=None):
        model = ScriptedModel(steps)
        self.models.append(model)
        client = Client(worker_path=worker_path)
        self.clients.append(client)
        config = {"base_url": model.base_url, "model": "offline-harness-model", "api_key": KEY}
        if computer:
            config["gui"] = {"base_url": model.base_url, "model": "offline-gui", "api_key": "gui-fixture-token",
                             "provider": "ui-tars-1.5", "coordinate_profile": "pixel"}
        payload = {"type": "run", "id": "harness-case", "kind": "agent",
                   "conversation_id": str(uuid4()), "session_history": [],
                   "task": task or "Complete the owned test task, verify actual outputs, and report blockers honestly. Replacing owned fixture outputs is authorized.",
                   "config": config, "context": "", "options": {"workspace": str(self.workspace), "max_steps": steps_limit,
                       "timeout_seconds": 25, "include_activity": True, "tools_enabled": tools,
                       "computer_enabled": computer, "checkpoint_required": checkpoint_required}}
        if image:
            payload["current_image_url"] = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\nsynthetic-qa-image").decode()
        client.send(payload)
        return client, model

    def next_frame(self, client, frame_type, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                frame = client.frames.get(timeout=max(.1, deadline-time.monotonic()))
            except queue.Empty:
                break
            if frame["type"] == frame_type:
                return frame
            if frame["type"] in {"error", "final"}:
                self.fail("Unexpected terminal frame: " + json.dumps(frame))
        self.fail("Timed out awaiting " + frame_type)

    def ack(self, client, frame):
        client.send({"type": "checkpoint_ack", "id": frame["id"], "barrier_id": frame["barrier_id"]})

    def seed_csv(self):
        (self.workspace / "input.csv").write_text("类别,金额\n设备,849.44\n资料,300.15\n交通,83.80\n", encoding="utf-8")

    def test_wrong_total_cannot_be_completed_even_when_model_omits_verification(self):
        self.seed_csv()
        bad = {"category_totals": {"设备": "849.44", "资料": "300.15", "交通": "83.8"},
               "total": "1133.39", "record_count": 3}
        client, model = self.start([tool("read_file", path="input.csv"),
            tool("write_file", path="summary.json", content=json.dumps(bad))] + ["Everything is complete."] * 3)
        result = client.terminal()
        self.assertEqual(result["type"], "final", result)
        self.assertEqual(result["status"], "needs_user")
        self.assertIn("未完成", result["reply"])
        self.assertEqual(json.loads((self.workspace / "summary.json").read_text())["total"], "1133.39")
        self.assertNotIn(KEY, json.dumps(client.all_frames))

    def test_explicit_weaker_text_check_cannot_bypass_json_total_validation(self):
        bad = {"category_totals": {"设备": "849.44", "资料": "300.15", "交通": "83.8"},
               "total": "1133.39", "record_count": 3}
        client, _ = self.start([tool("write_file", path="summary.json", content=json.dumps(bad)),
            tool("verify_artifact", path="summary.json", kind="text")] + ["The text file is readable, so I am done."] * 3)
        result = client.terminal()
        self.assertEqual(result.get("status"), "needs_user", result)

    def test_verification_is_repeated_after_later_overwrite(self):
        self.seed_csv()
        good = {"category_totals": {"设备": "849.44", "资料": "300.15", "交通": "83.8"},
                "total": "1233.39", "record_count": 3}
        bad = {**good, "total": "1133.39"}
        client, _ = self.start([tool("write_file", path="summary.json", content=json.dumps(good)),
            tool("verify_artifact", path="summary.json", kind="csv_summary", source_path="input.csv", value_column="金额", group_by="类别"),
            tool("write_file", path="summary.json", content=json.dumps(bad), overwrite=True)] + ["The earlier check passed."] * 3)
        result = client.terminal()
        self.assertEqual(result.get("status"), "needs_user", result)

    def test_verification_failure_is_model_visible_and_correctable_before_completion(self):
        self.seed_csv()
        good = {"category_totals": {"设备": "849.44", "资料": "300.15", "交通": "83.8"},
                "total": "1233.39", "record_count": 3}
        bad = {**good, "total": "1133.39"}
        verify = tool("verify_artifact", path="summary.json", kind="csv_summary", source_path="input.csv", value_column="金额", group_by="类别")
        def correction(body):
            self.assertFalse(last_observation(body, "verify_artifact")["passed"])
            return tool("write_file", path="summary.json", content=json.dumps(good), overwrite=True)
        def finish(body):
            self.assertTrue(last_observation(body, "verify_artifact")["passed"])
            return tool("finish_task", status="completed", summary="Recomputed source total and corrected output.")
        def reply(body):
            self.assertTrue(last_observation(body, "finish_task")["accepted"])
            return "Verified actual source and corrected total to 1233.39."
        client, model = self.start([
            tool("analyze_csv", path="input.csv", value_column="金额", group_by="类别"),
            tool("write_file", path="summary.json", content=json.dumps(bad)), verify,
            correction, verify, finish, reply])
        result = client.terminal()
        self.assertEqual(result.get("status"), "completed", result)
        self.assertEqual(json.loads((self.workspace / "summary.json").read_text())["total"], "1233.39")
        failed = [frame for frame in client.all_frames if frame["type"] == "activity" and frame.get("status") == "failed"]
        self.assertTrue(any(frame.get("name") == "verify_artifact" for frame in failed))
        self.assertEqual(len(model.calls), 7)

    def test_failed_read_without_explicit_finish_does_not_become_completed(self):
        client, _ = self.start([tool("read_file", path="missing.txt")] + ["All done despite the error."] * 3,
                               task="Read missing.txt and report its exact contents.")
        result = client.terminal()
        self.assertEqual(result.get("status"), "needs_user", result)
        self.assertFalse((self.workspace / "missing.txt").exists())

    def test_exploratory_missing_candidate_does_not_poison_valid_alternative(self):
        (self.workspace / "unrelated.txt").write_text("Not the missing required input", encoding="utf-8")
        client, _ = self.start([tool("read_file", path="missing.txt"),
                               tool("read_file", path="unrelated.txt"), "Pretend the missing input was recovered."])
        self.assertEqual(client.terminal().get("status"), "completed")
        observations = [f for f in client.all_frames if f.get("type") == "activity"]
        self.assertTrue(any(f.get("name") == "read_file" and f.get("status") == "failed" for f in observations))

    def test_changed_source_after_analysis_requires_new_evidence(self):
        self.seed_csv()
        summary = {"category_totals": {"设备": "849.44", "资料": "300.15", "交通": "83.8"},
                   "total": "1233.39", "record_count": 3}
        def change_source(body):
            self.assertEqual(last_observation(body, "analyze_csv")["total"], "1233.39")
            # This is the owned fixture changing between observations, not a
            # model action or personal file modification.
            (self.workspace / "input.csv").write_text("类别,金额\n设备,1\n", encoding="utf-8")
            return tool("write_file", path="summary.json", content=json.dumps(summary))
        client, _ = self.start([tool("analyze_csv", path="input.csv", value_column="金额", group_by="类别"),
                               change_source] + ["An earlier source observation is enough."] * 3)
        self.assertEqual(client.terminal().get("status"), "needs_user")

    def test_missing_material_can_explicitly_end_as_needs_user(self):
        def reply(body):
            value = last_observation(body, "finish_task")
            self.assertTrue(value["accepted"])
            self.assertEqual(value["status"], "needs_user")
            return "Please supply the missing material."
        client, _ = self.start([tool("read_file", path="missing.txt"),
            tool("finish_task", status="needs_user", summary="Input file is absent."), reply])
        self.assertEqual(client.terminal().get("status"), "needs_user")

    def test_normal_text_read_write_remains_compatible_without_finish_tool(self):
        def reply(body):
            values = [m["content"] for m in body["messages"] if m.get("role") == "tool" and m.get("name") == "read_file"]
            page = json.loads(values[-1])
            self.assertEqual(page["text"], "owned中文🙂")
            self.assertFalse(page["truncated"])
            self.assertEqual(page["sha256"], hashlib.sha256("owned中文🙂".encode()).hexdigest())
            return "Created and read the owned file."
        client, _ = self.start([tool("write_file", path="normal.txt", content="owned中文🙂"),
                                tool("read_file", path="normal.txt"), reply])
        self.assertEqual(client.terminal().get("status"), "completed")
        self.assertEqual((self.workspace / "normal.txt").read_text(encoding="utf-8"), "owned中文🙂")

    def test_unfinished_plan_blocks_dishonest_completion(self):
        def reply(body):
            finish = last_observation(body, "finish_task")
            self.assertFalse(finish["accepted"])
            self.assertIn("PLAN_UNFINISHED", finish["issues"])
            return "Pretending the unfinished plan is complete."
        client, _ = self.start([tool("update_plan", steps=[{"content": "Read source and verify totals", "status": "pending"}]),
                                tool("finish_task", status="completed")] + [reply] * 3)
        self.assertEqual(client.terminal().get("status"), "needs_user")

    def test_decimal_and_csv_outputs_reach_the_actual_model_loop(self):
        self.seed_csv()
        def analyze(body):
            result = last_observation(body, "calculate")
            self.assertEqual(result["result"], "1233.39")
            self.assertTrue(result["exact"])
            return tool("analyze_csv", path="input.csv", value_column="金额", group_by="类别")
        def reply(body):
            result = last_observation(body, "analyze_csv")
            self.assertEqual(result["total"], "1233.39")
            self.assertEqual(result["record_count"], 3)
            self.assertEqual(result["category_totals"]["设备"], "849.44")
            self.assertEqual(len(result["source_sha256"]), 64)
            return "Observed exact total: 1233.39."
        client, _ = self.start([tool("calculate", expression="849.44+300.15+83.8"), analyze, reply])
        self.assertEqual(client.terminal().get("status"), "completed")

    def test_checkpoint_ack_precedes_model_dispatch_and_write_side_effect(self):
        client, model = self.start([tool("write_file", path="checkpoint.txt", content="one actual write"),
                                   "File verified."], checkpoint_required=True)
        first = self.next_frame(client, "checkpoint")
        time.sleep(.12)
        self.assertEqual(model.calls, [])
        self.assertFalse((self.workspace / "checkpoint.txt").exists())
        self.ack(client, first)
        second = self.next_frame(client, "checkpoint")
        self.assertEqual(len(model.calls), 1)
        self.assertFalse((self.workspace / "checkpoint.txt").exists())
        prior = client.all_frames[:client.all_frames.index(second)]
        self.assertTrue(any(frame["type"] == "session" and frame.get("message", {}).get("tool_calls") for frame in prior))
        self.ack(client, second)
        third = self.next_frame(client, "checkpoint")
        self.assertEqual((self.workspace / "checkpoint.txt").read_text(), "one actual write")
        self.assertEqual(len(model.calls), 1)
        self.ack(client, third)
        self.assertEqual(client.terminal().get("status"), "completed")

    def test_refused_checkpoint_does_not_create_file(self):
        client, model = self.start([tool("write_file", path="must-not-exist.txt", content="blocked")], checkpoint_required=True)
        first = self.next_frame(client, "checkpoint")
        self.ack(client, first)
        second = self.next_frame(client, "checkpoint")
        client.send({"type": "abort", "id": second["id"]})
        result = client.terminal()
        self.assertEqual(result.get("status"), "cancelled", result)
        self.assertEqual(len(model.calls), 1)
        self.assertFalse((self.workspace / "must-not-exist.txt").exists())

    def test_invalid_checkpoint_ack_never_dispatches_a_model(self):
        client, model = self.start(["This must never be requested."], checkpoint_required=True)
        first = self.next_frame(client, "checkpoint")
        client.send({"type": "checkpoint_ack", "id": first["id"], "barrier_id": "wrong-barrier"})
        result = client.terminal()
        self.assertEqual(result.get("code"), "INVALID_REQUEST", result)
        self.assertEqual(model.calls, [])

    def test_repeated_gui_request_after_uncertain_failure_dispatches_only_once(self):
        counter = self.workspace / "gui-dispatches.json"
        wrapper = self.workspace / "fake-gui-worker.py"
        wrapper.write_text(
            "import asyncio,importlib.util,json\nfrom pathlib import Path\n"
            f"spec=importlib.util.spec_from_file_location('qa_worker',{str(BACKEND/'worker.py')!r})\n"
            "worker=importlib.util.module_from_spec(spec)\nspec.loader.exec_module(worker)\n"
            f"counter=Path({str(counter)!r})\n"
            "async def fail_once(self,task):\n"
            "    if self._current_verify_only:\n"
            "        return json.dumps({'status':'needs_user','effect_state':'uncertain'})\n"
            "    values=json.loads(counter.read_text()) if counter.exists() else []\n"
            "    values.append('dispatch')\n"
            "    counter.write_text(json.dumps(values))\n"
            "    self._uncertain_effect=True\n"
            "    error=worker.BridgeFailure('GUI_ACTION_FAILED')\n"
            "    error.effect_state='uncertain'\n"
            "    raise error\n"
            "worker.GuiBridge._run_once=fail_once\nasyncio.run(worker.main())\n", encoding="utf-8")
        client, model = self.start([tool("computer_use", task="only the owned test window"),
                                   tool("computer_use", task="retry the same side effect"), "I tried again."],
                                  worker_path=wrapper, computer=True)
        result = client.terminal()
        self.assertEqual(result.get("type"), "final", result)
        self.assertEqual(result.get("status"), "needs_user", result)
        self.assertEqual(json.loads(counter.read_text()), ["dispatch"])
        self.assertEqual(len(model.calls), 3)

    def test_all_permissions_off_keeps_plain_agent_answers_tool_free(self):
        def reply(body):
            self.assertFalse(body.get("tools"))
            return "Ordinary explanation without desktop or file tools."
        client, _ = self.start([reply], tools=False, computer=False,
                               task="Explain what a desktop assistant is without taking any actions.")
        result = client.terminal()
        self.assertEqual(result.get("status"), "completed")
        self.assertEqual(result.get("completion_scope"), "answer")
        self.assertIn("Ordinary explanation", result["reply"])
        self.assertEqual(list(self.workspace.iterdir()), [])

    def test_four_identical_read_failures_stop_before_another_model_or_tool_dispatch(self):
        for with_image in (False, True):
            with self.subTest(current_image=with_image):
                calls = [tool("read_file", path=path) for path in
                         ("missing.txt", "./missing.txt", "sub/../missing.txt", "missing.txt")]
                client, model = self.start(calls, image=with_image, steps_limit=20 if with_image else 0)
                result = client.terminal()
                self.assertEqual(result.get("type"), "final", result)
                self.assertEqual(result.get("status"), "needs_user", result)
                self.assertEqual(len(model.calls), 4)
                self.assertFalse((self.workspace / "missing.txt").exists())

    def test_correcting_csv_column_arguments_is_not_an_identical_failure_loop(self):
        self.seed_csv()
        calls = [tool("analyze_csv", path="input.csv", value_column=column, group_by="类别")
                 for column in ("gross", "net", "amount", "price", "金额")]
        def reply(body):
            self.assertEqual(last_observation(body, "analyze_csv")["total"], "1233.39")
            return "The corrected column produced exact results."
        client, model = self.start([*calls, reply])
        self.assertEqual(client.terminal().get("status"), "completed")
        self.assertEqual(len(model.calls), 6)

    def test_gui_needs_user_is_not_overridden_by_later_planner_finish(self):
        wrapper = self.workspace / "needs-user-worker.py"
        wrapper.write_text(
            "import asyncio,importlib.util,json\n"
            f"spec=importlib.util.spec_from_file_location('qa_worker',{str(BACKEND/'worker.py')!r})\n"
            "worker=importlib.util.module_from_spec(spec)\nspec.loader.exec_module(worker)\n"
            "async def needs_user(self,task):\n"
            "    self.last_status='needs_user'\n"
            "    return json.dumps({'status':'needs_user','summary':'Owned fixture needs human input.','steps':1})\n"
            "worker.GuiBridge._run_once=needs_user\nasyncio.run(worker.main())\n", encoding="utf-8")
        client, _ = self.start([tool("computer_use", task="owned task"),
                               tool("finish_task", status="completed")] + ["The planner claims completion."] * 3,
                              worker_path=wrapper, computer=True)
        result = client.terminal()
        self.assertEqual(result.get("type"), "final", result)
        self.assertEqual(result.get("status"), "needs_user", result)


if __name__ == "__main__":
    unittest.main()
