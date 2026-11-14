"""Real SDK worker with separate loopback planner/judge channels.

These are architectural regression fixtures, not a real-model benchmark or a
claim of commercial-agent parity. They use no cloud model or physical desktop.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from uuid import uuid4

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parents[1]
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND / "tests"))
from test_worker import Client, PYTHON
from test_harness_worker import tool, repeat_after_outcome_feedback

KEY = "synthetic-goal-verification-secret"
FILE_TASK = "Create report.txt in the delegated workspace, containing exactly HELLO. Read it back before confirming success."
DONE = "Created report.txt with content HELLO."


class SeparateGoalModel:
    """A protocol fixture whose judge cannot consume planner script steps."""
    def __init__(self, steps, *, judge_mode="file", workspace=None, allow_outcome_feedback=False,
                 outcome_feedback_limit=2):
        self.steps, self.judge_mode = list(steps), judge_mode
        self.allow_outcome_feedback = allow_outcome_feedback
        self.outcome_feedback_limit = outcome_feedback_limit
        self.repair_calls, self.last_answer = 0, None
        self.workspace = workspace
        self.planner_calls, self.judge_calls, self.revision_calls, self.errors = [], [], [], []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                try:
                    snapshot = None
                    revision = None
                    messages = body.get("messages", [])
                    if len(messages) == 2 and messages[-1].get("role") == "user":
                        try:
                            candidate = json.loads(messages[-1].get("content", ""))
                            if isinstance(candidate, dict) and candidate.get("schema") == "aura.goal_snapshot.v1":
                                snapshot = candidate
                            elif isinstance(candidate, dict) and candidate.get("schema") == "aura.goal_revision_snapshot.v1":
                                revision = candidate
                        except (TypeError, ValueError):
                            pass
                    if revision is not None:
                        owner.revision_calls.append(body)
                        if messages[0].get("role") != "system" or body.get("tools"):
                            raise AssertionError("Revision resolver acquired execution authority")
                        content = owner.revise(revision)
                        call = None
                    elif snapshot is not None:
                        owner.judge_calls.append(body)
                        # Independent judge input has no planner conversation,
                        # function tools or authority to execute an action.
                        if messages[0].get("role") != "system" or body.get("tools"):
                            raise AssertionError("Judge acquired execution tools or planner history")
                        if owner.judge_mode == "error":
                            self.send_response(401)
                            payload = json.dumps({"error": {"message": KEY}}).encode()
                            self.send_header("Content-Type", "application/json")
                            self.send_header("Content-Length", str(len(payload)))
                            self.end_headers()
                            self.wfile.write(payload)
                            return
                        content = owner.judge(snapshot)
                        call = None
                    else:
                        index = len(owner.planner_calls)
                        owner.planner_calls.append(body)
                        if index >= len(owner.steps):
                            step = repeat_after_outcome_feedback(owner, body)
                        else:
                            step = owner.steps[index]
                        step = step(body) if callable(step) else step
                        call = None
                        if isinstance(step, tuple):
                            name, arguments = step
                            call = {"id": f"goal-planner-{index}", "type": "function", "function": {
                                "name": name, "arguments": json.dumps(arguments)}}
                        content = None if call else str(step)
                        if not call:
                            owner.last_answer = content
                    message = {"role": "assistant", "content": content}
                    if call:
                        message["tool_calls"] = [call]
                except Exception as exc:
                    owner.errors.append(type(exc).__name__ + ": " + str(exc))
                    self.send_response(500)
                    payload = b'{"error":{"message":"Goal fixture assertion failed"}}'
                    self.send_header("Content-Type", "application/json")
                else:
                    self.send_response(200)
                    reason = "tool_calls" if call else "stop"
                    if body.get("stream"):
                        delta = dict(message)
                        if call:
                            delta["tool_calls"] = [{"index": 0, **call}]
                        chunks = [
                            {"id": "separate-goal-fixture", "object": "chat.completion.chunk", "created": 1,
                             "model": "offline", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                            {"id": "separate-goal-fixture", "object": "chat.completion.chunk", "created": 1,
                             "model": "offline", "choices": [{"index": 0, "delta": {}, "finish_reason": reason}]},
                        ]
                        payload = ("".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
                                   + "data: [DONE]\n\n").encode()
                        self.send_header("Content-Type", "text/event-stream")
                    else:
                        payload = json.dumps({"id": "separate-goal-fixture", "object": "chat.completion", "created": 1,
                            "model": "offline", "choices": [{"index": 0, "message": message, "finish_reason": reason}]}).encode()
                        self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                try:
                    self.end_headers()
                    self.wfile.write(payload)
                except ConnectionError:
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.server.block_on_close = False
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}/v1"

    def judge(self, snapshot):
        receipt = {"schema": "aura.goal_verdict.v1", "goal_id": snapshot["goal_id"],
            "evidence_id": snapshot["evidence_id"], "answer_sha256": snapshot["answer_sha256"],
            "status": "satisfied", "evidence_ids": snapshot["evidence_ids"]}
        if self.judge_mode == "file":
            facts = snapshot["facts"]
            files = facts["files"]
            reads = [item for item in facts["observed"]["observations"] if item["tool"] == "read_file"]
            actual_read = False
            for item in reads:
                result = item.get("observation", {}).get("result")
                result = json.loads(result) if isinstance(result, str) else result
                actual_read |= isinstance(result, dict) and result.get("text") == "HELLO"
            if not (actual_read and any(item.get("text") == "HELLO" for item in files)
                    and all(item["passed"] for item in facts["requirement_checks"])):
                receipt["status"] = "not_satisfied"
        elif self.judge_mode == "answer":
            if snapshot["facts"]["observed"]["observations"]:
                raise AssertionError("Ordinary answer unexpectedly executed tools")
            receipt.update(status="answer_only", evidence_ids=[])
        elif self.judge_mode == "read_answer":
            facts = snapshot["facts"]
            if (facts["observed"]["effect_count"] or not any(item["tool"] == "read_file"
                    for item in facts["observed"]["observations"])):
                raise AssertionError("Read-only answer fixture has no actual source read or performed an effect")
            receipt["status"] = "answer_only"
        elif self.judge_mode == "office_content":
            proofs = snapshot["facts"]["files"]
            if not any(item.get("content_readback", {}).get("status") == "available"
                       and "Actual requested paragraph." in item["content_readback"].get("text", "")
                       and item["content_readback"].get("rendering_performed") is False for item in proofs):
                raise AssertionError("Actual Office content readback was unavailable to the judge")
        elif self.judge_mode == "malformed":
            return "Provider free text that includes " + KEY
        elif self.judge_mode == "stale":
            receipt["evidence_id"] = "0" * 64
        elif self.judge_mode == "invented_id":
            receipt["evidence_ids"] = ["invented-file-report-proof"]
        elif self.judge_mode == "mutate_file":
            (self.workspace / "report.txt").write_text("CHANGED AFTER SNAPSHOT", encoding="utf-8")
        elif self.judge_mode != "optimistic":
            raise AssertionError("Unknown judge fixture mode")
        return json.dumps(receipt)

    def revise(self, snapshot):
        # Default is deliberately conservative. Dedicated revision fixtures
        # override this; ordinary completion tests must never steer implicitly.
        return json.dumps({"schema": "aura.goal_revision_patch.v1", "timeline_id": snapshot["timeline_id"],
            "revision_id": snapshot["revision_id"], "base_requirements_id": snapshot["base_requirements_id"],
            "status": "uncertain", "operations": [{"op": "keep", "requirement_id": item["id"]}
                for item in snapshot["active_requirements"]]})

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


@unittest.skipUnless(PYTHON.is_file(), "isolated nanobot SDK environment missing")
class GoalContractWorkerTests(unittest.TestCase):
    def run_case(self, steps, *, task=FILE_TASK, judge_mode="file", fixtures=None,
                 outcome_feedback_limit=2):
        root = (REPO / "agent_workspace").resolve()
        root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="goal-contract-worker-", dir=root) as owned:
            workspace = Path(owned).resolve()
            self.assertTrue(workspace.is_relative_to(root))
            for name, text in (fixtures or {}).items():
                target = (workspace / name).resolve()
                self.assertTrue(target.is_relative_to(workspace))
                target.write_text(text, encoding="utf-8")
            model = SeparateGoalModel(steps, judge_mode=judge_mode, workspace=workspace,
                                      allow_outcome_feedback=True, outcome_feedback_limit=outcome_feedback_limit)
            client = Client()
            try:
                client.send({"type": "run", "id": "goal-contract-regression", "kind": "agent",
                    "conversation_id": str(uuid4()), "session_history": [], "task": task, "context": "",
                    "config": {"base_url": model.base_url, "model": "offline-goal-fixture", "api_key": KEY},
                    "options": {"workspace": str(workspace), "max_steps": 12, "timeout_seconds": 25,
                        "tools_enabled": True, "computer_enabled": False, "checkpoint_required": False,
                        "include_activity": True}})
                result = client.terminal(timeout=40)
                self.assertEqual(result["type"], "final", (result, model.errors, model.repair_calls))
                self.assertTrue(model.judge_calls, "No independent goal verification request occurred")
                self.assertEqual(len(model.planner_calls), len(steps) + model.repair_calls, model.errors)
                self.assertEqual(model.errors, [])
                self.assertNotIn(KEY, json.dumps(client.all_frames))
                materialized = {}
                for path in workspace.iterdir():
                    if path.is_file():
                        try:
                            materialized[path.name] = path.read_text(encoding="utf-8")
                        except UnicodeError:
                            materialized[path.name] = {"binary": True, "bytes": path.stat().st_size}
                judge_snapshots = [json.loads(body["messages"][-1]["content"]) for body in model.judge_calls]
                # File/plan claims are excluded as evidence; the answer remains
                # a separate candidate whose truth the judge must check.
                for snapshot in judge_snapshots:
                    self.assertFalse(any(item["tool"] in {"update_plan", "finish_task"}
                        for item in snapshot["facts"]["observed"]["observations"]))
                return result, materialized, judge_snapshots
            finally:
                # Close the worker before its owned SQLite/context dir is
                # removed; on Windows a context-manager commit is not a close.
                self.assertEqual(client.close(), "")
                model.close()

    def test_unrelated_system_info_and_false_finish_are_rejected_even_with_optimistic_judge(self):
        result, files, snapshots = self.run_case([
            tool("system_info"), tool("finish_task", status="completed", summary=DONE), DONE], judge_mode="optimistic")
        self.assertEqual(result["status"], "needs_user", result)
        self.assertEqual(files, {})
        self.assertTrue(any(item["tool"] == "system_info"
            for item in snapshots[-1]["facts"]["observed"]["observations"]))
        self.assertFalse(snapshots[-1]["facts"]["requirement_checks"][0]["passed"])

    def test_actual_write_readback_and_independent_judge_complete(self):
        result, files, snapshots = self.run_case([
            tool("write_file", path="report.txt", content="HELLO"),
            tool("read_file", path="report.txt"),
            tool("finish_task", status="completed", summary=DONE), DONE])
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(files, {"report.txt": "HELLO"})
        self.assertEqual(snapshots[-1]["facts"]["files"][0]["text"], "HELLO")
        self.assertEqual(result["completion_scope"], "task")

    def test_ordinary_answer_only_question_needs_no_tool(self):
        result, files, _ = self.run_case(["Four."], task="What is two plus two?", judge_mode="answer")
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["completion_scope"], "answer")
        self.assertEqual(result["tools_used"], [])
        self.assertEqual(files, {})

    def test_judge_fault_malformed_stale_or_fabricated_receipt_fail_closed(self):
        for mode in ("error", "malformed", "stale", "invented_id"):
            with self.subTest(mode=mode):
                result, files, _ = self.run_case([
                    tool("write_file", path="report.txt", content="HELLO"),
                    tool("read_file", path="report.txt"), DONE], judge_mode=mode)
                self.assertEqual(result["status"], "needs_user", result)
                self.assertEqual(files, {"report.txt": "HELLO"})

    def test_external_edit_during_judge_does_not_accept_old_readback(self):
        # The edit introduces distinct stale-receipt and changed-file failures.
        result, files, _ = self.run_case([
            tool("write_file", path="report.txt", content="HELLO"),
            tool("read_file", path="report.txt"), DONE], judge_mode="mutate_file", outcome_feedback_limit=4)
        self.assertEqual(result["status"], "needs_user", result)
        self.assertEqual(files, {"report.txt": "CHANGED AFTER SNAPSHOT"})

    def test_actual_source_reads_allow_answer_only_with_structured_reply(self):
        answer = '{"difference":"2.35"}'
        result, files, snapshots = self.run_case([
            tool("read_file", path="north.txt"), tool("read_file", path="south.txt"),
            tool("finish_task", status="completed", summary=answer), answer],
            task="Read north.txt and south.txt and return their temperature difference as JSON.",
            judge_mode="read_answer", fixtures={"north.txt": "temperature=18.75", "south.txt": "temperature=21.10"})
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["reply"], answer)
        self.assertTrue(snapshots[-1]["requirements"])
        self.assertTrue(all(item["kind"] == "read_file" for item in snapshots[-1]["requirements"]))
        self.assertEqual(files, {"north.txt": "temperature=18.75", "south.txt": "temperature=21.10"})

    def test_generated_office_content_is_judged_without_rendering(self):
        result, _, snapshots = self.run_case([
            tool("create_document", path="report.docx", kind="docx", spec={"title": "Report", "sections": [
                {"heading": "Notes", "paragraphs": ["Actual requested paragraph."]}]}),
            tool("finish_task", status="completed", summary="Created and inspected report.docx."),
            "Created and inspected report.docx."],
            task="Create report.docx with the paragraph Actual requested paragraph. No rendering is requested.",
            judge_mode="office_content")
        self.assertEqual(result["status"], "completed", result)
        self.assertNotIn("render_document", result["tools_used"])
        proof = snapshots[-1]["facts"]["files"][0]["content_readback"]
        self.assertFalse(proof["visual_layout_verified"])


if __name__ == "__main__":
    unittest.main()
