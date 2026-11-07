"""Actual SDK recovery from malformed model calls; synthetic HTTP only.

These faults are not evidence of the cause of an earlier real-model failure.
They verify that invalid tool arguments cannot acquire effects or prevent a
later corrected call from being independently checked.
"""
from pathlib import Path
import json
import sys
import tempfile
import time
import unittest
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_goal_contract_worker as support
from test_harness_worker import tool
from session_history import close_interrupted_tools, native_text_record, text_record


class ToolArgumentRecoveryTests(unittest.TestCase):
    run_case = support.GoalContractWorkerTests.run_case

    def test_nonobject_arguments_do_not_execute_or_stop_corrected_task(self):
        for malformed in (None, [], "not an argument object"):
            with self.subTest(argument_type=type(malformed).__name__):
                result, files, snapshots = self.run_case([
                    ("write_file", malformed),
                    tool("write_file", path="report.txt", content="HELLO"),
                    tool("read_file", path="report.txt"),
                    tool("finish_task", status="completed", summary=support.DONE),
                    support.DONE])
                self.assertEqual(result["status"], "completed", result)
                self.assertEqual(files, {"report.txt": "HELLO"})
                writes = [row for row in snapshots[-1]["facts"]["observed"]["observations"]
                          if row["tool"] == "write_file"]
                self.assertEqual(len(writes), 1)

    def test_unknown_tool_has_no_effect_and_corrected_call_can_complete(self):
        result, files, snapshots = self.run_case([
            tool("not_a_registered_tool", path="unintended.txt", content="never execute"),
            tool("write_file", path="report.txt", content="HELLO"),
            tool("read_file", path="report.txt"),
            tool("finish_task", status="completed", summary=support.DONE), support.DONE])
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(files, {"report.txt": "HELLO"})
        writes = [item for item in snapshots[-1]["facts"]["observed"]["observations"] if item["tool"] == "write_file"]
        self.assertEqual(len(writes), 1)

    def observed_run(self, workspace, steps, *, batch=None, history=(), task=support.FILE_TASK, judge_mode="file"):
        model = support.SeparateGoalModel(steps, judge_mode=judge_mode, workspace=workspace)
        if batch is not None:
            parent = model.server.RequestHandlerClass
            class BatchHandler(parent):
                def do_POST(self):
                    if model.planner_calls:
                        return super().do_POST()
                    body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    model.planner_calls.append(body)
                    calls = [{"id": identifier, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}} for identifier, name, arguments in batch]
                    if body.get("stream"):
                        delta = {"role": "assistant", "tool_calls": [{"index": i, **call} for i, call in enumerate(calls)]}
                        chunks = [{"id": "qa-batch", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                                  {"id": "qa-batch", "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}]
                        raw = ("".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n").encode()
                        content_type = "text/event-stream"
                    else:
                        raw = json.dumps({"id": "qa-batch", "choices": [{"index": 0, "message": {"role": "assistant", "content": None, "tool_calls": calls}, "finish_reason": "tool_calls"}]}).encode()
                        content_type = "application/json"
                    self.send_response(200); self.send_header("Content-Type", content_type); self.send_header("Content-Length", str(len(raw))); self.end_headers()
                    self.wfile.write(raw)
            model.server.RequestHandlerClass = BatchHandler
        client = support.Client()
        try:
            client.send({"type": "run", "id": "tool-recovery-observed", "kind": "agent", "conversation_id": str(uuid4()),
                "session_history": list(history), "task": task, "context": "",
                "config": {"base_url": model.base_url, "model": "offline-goal-fixture", "api_key": support.KEY},
                "options": {"workspace": str(workspace), "max_steps": 12, "timeout_seconds": 25,
                    "tools_enabled": True, "computer_enabled": False, "checkpoint_required": True, "include_activity": True}})
            deadline = time.monotonic() + 40
            result = None
            while time.monotonic() < deadline:
                frame = client.frames.get(timeout=max(.1, deadline - time.monotonic()))
                if frame["type"] == "checkpoint":
                    client.send({"type": "checkpoint_ack", "id": frame["id"], "barrier_id": frame["barrier_id"]})
                if frame["type"] in {"final", "error"}:
                    result = frame; break
            self.assertIsNotNone(result)
            self.assertEqual(result["type"], "final", result)
            self.assertEqual(result["status"], "completed", result)
            self.assertTrue(model.judge_calls)
            self.assertEqual(model.errors, [])
            self.assertEqual(len(model.planner_calls), len(steps))
            self.assertNotIn(support.KEY, json.dumps(client.all_frames))
            return result, list(client.all_frames), list(model.planner_calls), [json.loads(row["messages"][-1]["content"]) for row in model.judge_calls]
        finally:
            self.assertEqual(client.close(), "")
            model.close()

    def test_real_sdk_unknown_secret_name_fixed_result_and_known_write_is_once(self):
        root = support.REPO / "agent_workspace"; root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="tool-recovery-observed-", dir=root) as directory:
            workspace = Path(directory)
            unknown = "unavailable_" + support.KEY
            def corrected(body):
                outcomes = [row for row in body["messages"] if row.get("role") == "tool" and row.get("name") == unknown]
                self.assertEqual(len(outcomes), 1)
                content = outcomes[0]["content"]
                self.assertIn("TOOL_NOT_AVAILABLE", content)
                self.assertIn("No action was executed", content)
                self.assertLess(len(content), 512)
                self.assertNotIn(support.KEY, content)
                self.assertNotIn(unknown, json.dumps(body.get("tools", [])))
                return tool("write_file", path="report.txt", content="HELLO")
            result, frames, calls, snapshots = self.observed_run(workspace, [
                tool(unknown, path="unintended.txt", content=support.KEY), corrected,
                tool("read_file", path="report.txt"), tool("finish_task", status="completed", summary=support.DONE), support.DONE])
            self.assertEqual([path.name for path in workspace.iterdir() if path.is_file()], ["report.txt"])
            self.assertEqual((workspace / "report.txt").read_text(), "HELLO")
            self.assertNotIn(unknown, result["tools_used"])
            self.assertEqual(sum(row.get("type") == "activity" and row.get("name") == "write_file" and row.get("status") == "running" for row in frames), 1)
            observations = snapshots[-1]["facts"]["observed"]["observations"]
            self.assertEqual(sum(row["tool"] == "write_file" for row in observations), 1)
            self.assertFalse(any(row["tool"] == unknown for row in observations))
            saved = [row["message"] for row in frames if row["type"] == "session"]
            self.assertTrue(any("Host rejected an unavailable tool call" in row.get("content", "") for row in saved))
            for row in saved: self.assertEqual(text_record(row), row)

    def test_real_sdk_mixed_batch_archive_preserves_known_pair_without_replay(self):
        root = support.REPO / "agent_workspace"; root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="tool-recovery-batch-", dir=root) as directory:
            workspace = Path(directory)
            unknown = "unavailable_" + support.KEY
            result, frames, calls, snapshots = self.observed_run(workspace,
                ["batch response supplied by fixture", tool("read_file", path="report.txt"), tool("finish_task", status="completed", summary=support.DONE), support.DONE],
                batch=[("qa-unknown-call", unknown, {"path": "unintended.txt", "content": support.KEY}),
                       ("qa-known-write", "write_file", {"path": "report.txt", "content": "HELLO"})])
            saved = [row["message"] for row in frames if row["type"] == "session"]
            known = [call for row in saved for call in row.get("tool_calls", []) if call["function"]["name"] == "write_file"]
            self.assertEqual([call["id"] for call in known], ["qa-known-write"])
            outcomes = [row for row in saved if row.get("role") == "tool" and row.get("name") == "write_file"]
            self.assertEqual([row["tool_call_id"] for row in outcomes], ["qa-known-write"])
            self.assertFalse(any(row.get("tool_call_id") == "qa-unknown-call" for row in saved))
            self.assertEqual(close_interrupted_tools(saved), saved)
            for row in saved: self.assertEqual(text_record(row), row)
            self.assertEqual(sum(row["tool"] == "write_file" for row in snapshots[-1]["facts"]["observed"]["observations"]), 1)
            # A fresh SDK task receives only validated archive data. It performs
            # the newly requested read, never the old confirmed write or the
            # unavailable call that has no executable archive identity.
            replay, replay_frames, replay_calls, replay_snapshots = self.observed_run(workspace,
                [tool("read_file", path="report.txt"), "The file contains HELLO."], history=saved,
                task="Read report.txt and tell me its contents.", judge_mode="read_answer")
            self.assertEqual(replay["tools_used"], ["read_file"])
            self.assertEqual((workspace / "report.txt").read_text(), "HELLO")
            self.assertFalse(any(row["tool"] == "write_file" for row in replay_snapshots[-1]["facts"]["observed"]["observations"]))


class NativeRejectedCallProjectionTests(unittest.TestCase):
    def test_unknown_native_calls_are_data_and_imported_native_calls_remain_strict(self):
        unknown = {"id": "u", "type": "function", "function": {"name": "not_registered", "arguments": '{"secret":"NEVER_ARCHIVE"}'}}
        known = {"id": "k", "type": "function", "function": {"name": "write_file", "arguments": '{"path":"report.txt","content":"HELLO"}'}}
        raw = {"role": "assistant", "content": None, "tool_calls": [unknown, known]}
        with self.assertRaises(ValueError): text_record(raw)
        projected = native_text_record(raw, available_tools={"write_file"})
        self.assertEqual(projected["tool_calls"], [known])
        self.assertNotIn("NEVER_ARCHIVE", json.dumps(projected))
        self.assertNotIn("not_registered", json.dumps(projected))
        self.assertEqual(text_record(projected), projected)
        self.assertIsNone(native_text_record({"role": "tool", "tool_call_id": "u", "name": "not_registered", "content": "raw provider error NEVER_ARCHIVE"}, available_tools={"write_file"}))
        only = native_text_record({"role": "assistant", "content": "", "tool_calls": [unknown]}, available_tools={"write_file"})
        self.assertNotIn("tool_calls", only)
        self.assertEqual(only["role"], "assistant")

    def test_catalog_tool_disabled_in_current_registry_is_not_archived_as_executable(self):
        raw = {"role": "assistant", "content": None, "tool_calls": [{"id": "c", "type": "function", "function": {"name": "computer_use", "arguments": "{}"}}]}
        self.assertNotIn("tool_calls", native_text_record(raw, available_tools={"write_file"}))


if __name__ == "__main__":
    unittest.main()
