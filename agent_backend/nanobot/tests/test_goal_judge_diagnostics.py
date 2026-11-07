"""Fixed-code verifier diagnostics, including real SDK loopback transport.

Only synthetic keys, an owned temporary workspace and a localhost HTTP server
are used. These tests do not call a production model or operate the desktop.
"""
from __future__ import annotations

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from uuid import uuid4

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
from goal_judge import JUDGE_DIAGNOSTIC_CODES, unknown_verdict, verify_goal

SECRET = "synthetic-judge-diagnostic-key-3d91"
PRIVATE = "C:/private/synthetic-not-a-real-user-document.txt"
SNAPSHOT = {"schema": "aura.goal_snapshot.v1", "goal_id": "a" * 64,
    "evidence_id": "b" * 64, "answer_sha256": "c" * 64,
    "answer": "Read back the fixed result.", "evidence_ids": ["owned-readback-1"]}


def receipt(status="unknown", **overrides):
    value = {"schema": "aura.goal_verdict.v1", "goal_id": SNAPSHOT["goal_id"],
        "evidence_id": SNAPSHOT["evidence_id"], "answer_sha256": SNAPSHOT["answer_sha256"],
        "status": status, "evidence_ids": ["owned-readback-1"] if status == "satisfied" else []}
    value.update(overrides)
    return value


class ValueProvider:
    def __init__(self, *, content=None, finish="stop", error=None, **metadata):
        self.calls = []
        self.error = error
        self.response = SimpleNamespace(content=content, finish_reason=finish, tool_calls=[], **metadata)

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


class FixedCodeTests(unittest.IsolatedAsyncioTestCase):
    async def check(self, provider, code, *, snapshot=None, result=None, **kwargs):
        codes = []
        value = await verify_goal(provider, "synthetic-model", snapshot or SNAPSHOT,
                                  diagnostic_callback=codes.append, **kwargs)
        self.assertEqual(codes, [code])
        self.assertLessEqual(set(codes), JUDGE_DIAGNOSTIC_CODES)
        self.assertEqual(value, unknown_verdict(snapshot or SNAPSHOT) if result is None else result)
        self.assertEqual(set(value), set(unknown_verdict(SNAPSHOT)))
        self.assertNotIn(SECRET, json.dumps(codes))
        self.assertNotIn(PRIVATE, json.dumps(codes))
        return value

    async def test_all_four_valid_statuses_have_distinct_fixed_codes_without_receipt_changes(self):
        for status, code in (("satisfied", "SATISFIED"), ("answer_only", "ANSWER_ONLY"),
                             ("not_satisfied", "NOT_SATISFIED"), ("unknown", "EXPLICIT_UNKNOWN")):
            with self.subTest(status=status):
                expected = receipt(status)
                provider = ValueProvider(content=json.dumps(expected))
                await self.check(provider, "GOAL_JUDGE_" + code, result=expected)
                self.assertEqual(len(provider.calls), 1)
                call = provider.calls[0]
                self.assertEqual(call["tools"], [])
                self.assertEqual(call["tool_choice"], "none")
                self.assertEqual(len(call["messages"]), 2)

    async def test_untrusted_receipt_faults_are_not_explicit_unknown(self):
        cases = [
            ("not JSON " + SECRET + PRIVATE, "INVALID_JSON"),
            ('{"status":"satisfied","status":"unknown"}', "DUPLICATE_FIELDS"),
            ('{"nested":{"x":1,"x":2}}', "DUPLICATE_FIELDS"),
            ('{"invalid":NaN}', "INVALID_JSON"),
            ('{"invalid":Infinity}', "INVALID_JSON"),
            ("[[[]]]", "INVALID_SCHEMA"),
            ("[]", "INVALID_SCHEMA"),
            (json.dumps({**receipt(), "reasoning": SECRET + PRIVATE}), "INVALID_SCHEMA"),
            (json.dumps(receipt(schema="wrong")), "INVALID_SCHEMA"),
            (json.dumps(receipt(goal_id=SECRET)), "BINDING_MISMATCH"),
            (json.dumps(receipt(evidence_id=PRIVATE)), "BINDING_MISMATCH"),
            (json.dumps(receipt(answer_sha256="0" * 64)), "BINDING_MISMATCH"),
            (json.dumps(receipt(status=[SECRET])), "INVALID_STATUS"),
            (json.dumps(receipt(status="unknown " + SECRET)), "INVALID_STATUS"),
            (json.dumps(receipt(evidence_ids={"id": SECRET})), "INVALID_EVIDENCE_IDS"),
            (json.dumps(receipt(evidence_ids=[{}])), "INVALID_EVIDENCE_IDS"),
            (json.dumps(receipt(evidence_ids=[str(i) for i in range(129)])), "INVALID_EVIDENCE_IDS"),
            (json.dumps(receipt(evidence_ids=["owned-readback-1"] * 2)), "DUPLICATE_EVIDENCE_IDS"),
            (json.dumps(receipt(evidence_ids=[SECRET + PRIVATE])), "UNKNOWN_EVIDENCE_IDS"),
            (json.dumps(receipt("satisfied", evidence_ids=[])), "MISSING_EVIDENCE_IDS"),
        ]
        for content, code in cases:
            with self.subTest(code=code):
                await self.check(ValueProvider(content=content), "GOAL_JUDGE_" + code)

    async def test_limits_and_invalid_payload_make_no_request(self):
        for extra, code in (("x" * 524289, "PAYLOAD_LIMIT"),
                            (float("nan"), "INVALID_PAYLOAD"), ("\ud800", "INVALID_PAYLOAD")):
            with self.subTest(code=code):
                snapshot = {**SNAPSHOT, "untrusted_data": extra}
                provider = ValueProvider(content=json.dumps(receipt("satisfied")))
                await self.check(provider, "GOAL_JUDGE_" + code, snapshot=snapshot)
                self.assertEqual(provider.calls, [])
        await self.check(ValueProvider(content=SECRET * 2000), "GOAL_JUDGE_CONTENT_LIMIT")
        await self.check(ValueProvider(content=None), "GOAL_JUDGE_INVALID_RESPONSE")

    async def test_structured_provider_errors_are_fixed_and_do_not_read_error_content(self):
        for metadata, code in (({"error_status_code": 401}, "AUTH_FAILED"),
                ({"error_status_code": 403}, "AUTH_FAILED"),
                ({"error_status_code": 429}, "RATE_LIMITED"),
                ({"error_status_code": 408}, "TIMEOUT"),
                ({"error_status_code": 504}, "TIMEOUT"),
                ({"error_kind": "timeout"}, "TIMEOUT"),
                ({"error_kind": "connection"}, "CONNECTION"),
                ({"error_code": "context_length_exceeded", "error_status_code": 400}, "CONTEXT_LIMIT"),
                ({"error_type": "authentication_error"}, "AUTH_FAILED"),
                ({"error_status_code": 502}, "SERVER_ERROR"),
                ({"error_status_code": 400}, "STATUS_ERROR"),
                ({"error_code": SECRET + PRIVATE}, "ERROR"),
                ({"error_status_code": "401", "error_kind": SECRET}, "ERROR")):
            with self.subTest(code=code, metadata=list(metadata)):
                provider = ValueProvider(finish="error", **metadata)
                class ErrorResponse:
                    finish_reason = "error"
                    def __getattr__(self, name):
                        if name == "content":
                            raise AssertionError("Diagnostic read raw error text")
                        return metadata.get(name)
                provider.response = ErrorResponse()
                await self.check(provider, "GOAL_JUDGE_PROVIDER_" + code)
        # Raw error words cannot manufacture a specific cause.
        await self.check(ValueProvider(error=RuntimeError("401 timeout context_length_exceeded " + SECRET + PRIVATE)),
                         "GOAL_JUDGE_PROVIDER_ERROR")
        await self.check(ValueProvider(error=TimeoutError(SECRET)), "GOAL_JUDGE_PROVIDER_TIMEOUT")
        await self.check(ValueProvider(error=ConnectionError(SECRET)), "GOAL_JUDGE_PROVIDER_CONNECTION")

    async def test_rejected_finish_reasons_and_tool_attempt_do_not_gain_authority(self):
        for reason, code in (("length", "FINISH_LENGTH"), ("content_filter", "FINISH_FILTERED")):
            await self.check(ValueProvider(content=json.dumps(receipt("satisfied")), finish=reason),
                             "GOAL_JUDGE_" + code)
        provider = ValueProvider(content=json.dumps(receipt("satisfied")))
        provider.response.tool_calls = [{"name": SECRET, "arguments": {"path": PRIVATE}}]
        await self.check(provider, "GOAL_JUDGE_UNEXPECTED_TOOLS")
        # Preserve existing compatibility: an unknown non-error finish marker
        # does not create a new rejection rule for an otherwise valid receipt.
        expected = receipt("answer_only")
        await self.check(ValueProvider(content=json.dumps(expected), finish="provider_future_marker"),
                         "GOAL_JUDGE_ANSWER_ONLY", result=expected)

    async def test_callback_failure_and_absence_do_not_change_the_receipt(self):
        expected = receipt("satisfied")
        for error in (RuntimeError(SECRET + PRIVATE), asyncio.CancelledError("callback-only")):
            seen = []
            def broken(code):
                seen.append(code)
                raise error
            value = await verify_goal(ValueProvider(content=json.dumps(expected)), "synthetic-model", SNAPSHOT,
                                      diagnostic_callback=broken)
            self.assertEqual(value, expected)
            self.assertEqual(seen, ["GOAL_JUDGE_SATISFIED"])
        self.assertEqual(await verify_goal(ValueProvider(content=json.dumps(expected)),
                                          "synthetic-model", SNAPSHOT), expected)

    async def test_actual_cancellation_preserves_exception_and_terminal_code_with_broken_callback(self):
        marker = asyncio.CancelledError("fixed cancellation marker")
        # Python 3.10 wait_for replaces a child-raised CancelledError; 3.12
        # preserves it. Diagnostics must preserve the native timeout primitive
        # semantics on both supported interpreters, not wrap or swallow them.
        with self.assertRaises(asyncio.CancelledError) as native:
            await asyncio.wait_for(ValueProvider(error=marker).chat(), timeout=90)
        codes = []
        def callback(code):
            codes.append(code)
            raise RuntimeError(SECRET)
        with self.assertRaises(asyncio.CancelledError) as caught:
            await verify_goal(ValueProvider(error=marker), "synthetic-model", SNAPSHOT,
                              diagnostic_callback=callback)
        self.assertEqual(type(caught.exception), type(native.exception))
        self.assertEqual(caught.exception.args, native.exception.args)
        if native.exception is marker:
            self.assertIs(caught.exception, marker)
        self.assertEqual(codes, ["GOAL_JUDGE_CANCELLED"])


class JudgeEndpoint:
    """A real HTTP fixture with no model, filesystem actions or retry behavior."""
    def __init__(self):
        self.mode = "unknown"
        self.judge_count = self.planner_count = 0
        self.output_budgets = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.errors = []
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                mode = owner.mode
                snapshot = None
                try:
                    candidate = json.loads(request["messages"][-1].get("content", ""))
                    if isinstance(candidate, dict) and candidate.get("schema") == "aura.goal_snapshot.v1":
                        snapshot = candidate
                except (ValueError, TypeError):
                    pass
                status, finish = 200, "stop"
                calls = []
                if snapshot is None:
                    owner.planner_count += 1
                    content = "Four."
                else:
                    owner.judge_count += 1
                    owner.output_budgets.append(request.get("max_completion_tokens", request.get("max_tokens")))
                    # The SDK omits tool_choice when there are no tools. Both
                    # omission and explicit none keep the judge tool-less.
                    if request.get("tools") or request.get("tool_choice") not in {None, "none"}:
                        owner.errors.append("JUDGE_AUTHORITY_CHANGED")
                    owner.started.set()
                    if mode == "wait":
                        owner.release.wait(3)
                    value = {"schema": "aura.goal_verdict.v1", "goal_id": snapshot["goal_id"],
                        "evidence_id": snapshot["evidence_id"], "answer_sha256": snapshot["answer_sha256"],
                        "status": mode if mode in {"unknown", "not_satisfied", "satisfied", "answer_only"} else "unknown",
                        "evidence_ids": snapshot["evidence_ids"] if mode == "satisfied" else []}
                    content = json.dumps(value)
                    if mode == "capacity":
                        # A bounded synthetic provider that cannot finish its
                        # strict verdict in the old 2048-token response cap.
                        value.update(status="satisfied", evidence_ids=snapshot["evidence_ids"])
                        content = json.dumps(value)
                        if owner.output_budgets[-1] < 8192:
                            finish = "length"
                    elif mode == "invalid_json":
                        content = "unstructured receipt " + SECRET + PRIVATE
                    elif mode == "duplicate":
                        content = content[:-1] + ',"status":"satisfied"}'
                    elif mode == "invalid_schema":
                        content = json.dumps({**value, "reasoning": SECRET + PRIVATE})
                    elif mode == "binding":
                        content = json.dumps({**value, "evidence_id": SECRET})
                    elif mode == "tools":
                        calls = [{"id": "judge-has-no-tools", "type": "function", "function": {
                            "name": "write_file", "arguments": json.dumps({"path": PRIVATE, "content": SECRET})}}]
                    elif mode in {"length", "content_filter"}:
                        finish = mode
                    if mode.startswith("http"):
                        status = int(mode[4:7])
                if status != 200:
                    result = {"error": {"message": SECRET + PRIVATE,
                        "code": "context_length_exceeded" if mode == "http400context" else "untrusted-" + SECRET}}
                else:
                    message = {"role": "assistant", "content": content}
                    if calls:
                        message["tool_calls"] = calls
                    result = {"id": "fixed-judge-fixture", "object": "chat.completion", "created": 1,
                        "model": "synthetic-model", "choices": [{"index": 0, "message": message, "finish_reason": finish}]}
                if status == 200 and request.get("stream"):
                    delta = dict(message)
                    if calls:
                        delta["tool_calls"] = [{"index": index, **call} for index, call in enumerate(calls)]
                    chunks = [
                        {"id": "fixed-judge-fixture", "object": "chat.completion.chunk", "created": 1,
                         "model": "synthetic-model", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                        {"id": "fixed-judge-fixture", "object": "chat.completion.chunk", "created": 1,
                         "model": "synthetic-model", "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
                    ]
                    payload = ("".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
                               + "data: [DONE]\n\n").encode()
                    content_type = "text/event-stream"
                else:
                    payload = json.dumps(result).encode()
                    content_type = "application/json"
                self.send_response(status)
                self.send_header("Content-Type", content_type)
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
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


try:
    HAS_SDK = importlib.util.find_spec("nanobot.providers.openai_compat_provider") is not None
except ModuleNotFoundError:
    HAS_SDK = False


@unittest.skipUnless(HAS_SDK, "run with the isolated nanobot SDK interpreter")
class LoopbackSDKTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from nanobot.providers.openai_compat_provider import OpenAICompatProvider
        from nanobot.providers.model_api import ModelAPICapabilities
        self.server = JudgeEndpoint()
        self.provider = OpenAICompatProvider(api_key=SECRET, api_base=self.server.url,
            default_model="synthetic-model", model_api=ModelAPICapabilities(), provider_name="custom")

    async def asyncTearDown(self):
        if self.provider._client is not None:
            await self.provider._client.close()
        self.server.close()
        self.assertEqual(self.server.errors, [])

    async def judge(self, mode, *, timeout=3):
        self.server.mode = mode
        before = self.server.judge_count
        codes = []
        value = await verify_goal(self.provider, "synthetic-model", SNAPSHOT,
                                  diagnostic_callback=codes.append, timeout=timeout)
        self.assertEqual(self.server.judge_count, before + 1, "A judge request was replayed")
        self.assertEqual(len(codes), 1)
        self.assertIn(codes[0], JUDGE_DIAGNOSTIC_CODES)
        self.assertNotIn(SECRET, json.dumps([codes, value]))
        self.assertNotIn(PRIVATE, json.dumps([codes, value]))
        return value, codes[0]

    async def test_real_explicit_unknown_and_invalid_receipt_return_same_schema_different_diagnostics(self):
        unknown, unknown_code = await self.judge("unknown")
        malformed, malformed_code = await self.judge("invalid_json")
        self.assertEqual(unknown, malformed)
        self.assertEqual(unknown_code, "GOAL_JUDGE_EXPLICIT_UNKNOWN")
        self.assertEqual(malformed_code, "GOAL_JUDGE_INVALID_JSON")

    async def test_verdict_output_capacity_reaches_wire_once_without_accepting_truncation(self):
        value, code = await self.judge("capacity")
        self.assertEqual(code, "GOAL_JUDGE_SATISFIED")
        self.assertEqual(value, receipt("satisfied"))
        self.assertEqual(self.server.output_budgets, [8192])
        truncated, code = await self.judge("length")
        self.assertEqual(code, "GOAL_JUDGE_FINISH_LENGTH")
        self.assertEqual(truncated, unknown_verdict(SNAPSHOT))
        self.assertEqual(self.server.output_budgets, [8192, 8192])
        self.assertEqual(self.server.planner_count, 0)

    async def test_real_http_status_metadata_is_classified_without_body_or_retries(self):
        for mode, code in (("http401", "AUTH_FAILED"), ("http429", "RATE_LIMITED"),
                ("http500", "SERVER_ERROR"), ("http400context", "CONTEXT_LIMIT"),
                ("http400", "STATUS_ERROR"), ("http504", "TIMEOUT")):
            with self.subTest(mode=mode):
                value, actual = await self.judge(mode)
                self.assertEqual(actual, "GOAL_JUDGE_PROVIDER_" + code)
                self.assertEqual(value, unknown_verdict(SNAPSHOT))

    async def test_real_receipt_protocol_faults_remain_separate_and_do_not_execute_tools(self):
        for mode, code in (("duplicate", "DUPLICATE_FIELDS"), ("invalid_schema", "INVALID_SCHEMA"),
                ("binding", "BINDING_MISMATCH"), ("tools", "UNEXPECTED_TOOLS"),
                ("length", "FINISH_LENGTH"), ("content_filter", "FINISH_FILTERED")):
            with self.subTest(mode=mode):
                value, actual = await self.judge(mode)
                self.assertEqual(actual, "GOAL_JUDGE_" + code)
                self.assertEqual(value, unknown_verdict(SNAPSHOT))
        self.assertEqual(self.server.planner_count, 0)

    async def test_real_timeout_cancel_then_success_reset_diagnostics_and_never_replay(self):
        # Build the SDK client before the small timeout, so this measures the
        # actual blocked HTTP response rather than lazy import/client startup.
        await self.provider._ensure_client()
        value, code = await self.judge("wait", timeout=.15)
        self.assertEqual(value, unknown_verdict(SNAPSHOT))
        self.assertEqual(code, "GOAL_JUDGE_PROVIDER_TIMEOUT")
        self.server.started.clear()
        codes = []
        def broken(code):
            codes.append(code)
            raise RuntimeError(SECRET + PRIVATE)
        task = asyncio.create_task(verify_goal(self.provider, "synthetic-model", SNAPSHOT,
                                   timeout=3, diagnostic_callback=broken))
        for _ in range(200):
            if self.server.started.is_set():
                break
            await asyncio.sleep(.01)
        self.assertTrue(self.server.started.is_set())
        task.cancel("fixed-user-stop")
        with self.assertRaises(asyncio.CancelledError) as stopped:
            await task
        self.assertEqual(stopped.exception.args, ("fixed-user-stop",))
        self.assertEqual(codes, ["GOAL_JUDGE_CANCELLED"])
        self.server.release.set()
        value, code = await self.judge("answer_only")
        self.assertEqual(code, "GOAL_JUDGE_ANSWER_ONLY")
        self.assertEqual(value["status"], "answer_only")
        self.assertEqual(self.server.judge_count, 3)


@unittest.skipUnless((BACKEND / ".venv" / "Scripts" / "python.exe").is_file(), "isolated SDK missing")
class RealWorkerDiagnosticTests(unittest.TestCase):
    def test_real_worker_emits_exact_current_judge_reason_and_no_raw_errors(self):
        sys.path.insert(0, str(BACKEND / "tests"))
        import test_worker as worker_fixture
        server = JudgeEndpoint()
        owned_root = BACKEND.parents[1] / "agent_workspace"
        owned_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="judge-diagnostic-owned-", dir=owned_root) as workspace:
            try:
                for index, (mode, expected, status) in enumerate((
                    ("unknown", "EXPLICIT_UNKNOWN", "needs_user"),
                    ("invalid_json", "INVALID_JSON", "needs_user"),
                    ("http401", "PROVIDER_AUTH_FAILED", "needs_user"),
                    ("answer_only", "ANSWER_ONLY", "completed"))):
                    server.mode = mode
                    # Production workers own one task and then exit. Start a
                    # fresh exact worker for each host task, as service does.
                    client = worker_fixture.Client()
                    try:
                        client.send({"type": "run", "id": "fixed-judge-" + str(index), "kind": "agent",
                            "conversation_id": str(uuid4()), "session_history": [], "task": "What is two plus two?", "context": "",
                            "config": {"base_url": server.url, "model": "synthetic-model", "api_key": SECRET},
                            "options": {"workspace": workspace, "max_steps": 3, "timeout_seconds": 15,
                                "tools_enabled": True, "computer_enabled": False, "checkpoint_required": False,
                                "include_activity": True}})
                        result = client.terminal(timeout=25)
                        self.assertEqual(result["type"], "final", result)
                        self.assertEqual(result["status"], status, result)
                        frames = client.all_frames
                        codes = [frame["data"]["code"] for frame in frames
                            if frame["type"] == "diagnostic" and str(frame.get("data", {}).get("code", "")).startswith("GOAL_JUDGE_")]
                        self.assertEqual(codes, ["GOAL_JUDGE_" + expected])
                        self.assertEqual(result["tools_used"], [])
                        self.assertNotIn(SECRET, json.dumps(frames))
                        self.assertNotIn(PRIVATE, json.dumps(frames))
                    finally:
                        self.assertEqual(client.close(), "")
                self.assertEqual(server.judge_count, 4)
                self.assertEqual(server.planner_count, 4)
                self.assertEqual(list(Path(workspace).iterdir()), [])
            finally:
                server.close()


if __name__ == "__main__":
    unittest.main()
