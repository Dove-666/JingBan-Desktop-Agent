"""Actual SDK/worker + loopback vision/planner; synthetic capture only.

The native capture boundary is replaced. The observer, provider transport,
tool registry, parent history, acceptance checks and protocol remain real.
No user desktop input, screenshot, credentials or provider account is used.
"""
from __future__ import annotations

import base64
import io
import json
import os
from pathlib import Path
import shutil
import struct
import sys
import tempfile
import textwrap
import unittest
from uuid import uuid4
import zlib


BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(BACKEND / "tests"))
from test_harness_worker import ScriptedModel, tool
from test_worker import Client, PYTHON
from acceptance_report import checked_assessment
from tests.test_gui_no_effect_recovery_offline_review import seed_owned_tokenizer_cache


KEY = "desktop-view-loopback-fixture-only-key"
OBSERVATION = "The current video has an unfilled heart and a visible Next control."


def fixture_png() -> bytes:
    def chunk(kind, value):
        return (struct.pack(">I", len(value)) + kind + value
                + struct.pack(">I", zlib.crc32(kind + value) & 0xffffffff))
    rows = (b"\x00" + b"\x26\x74\xb1" * 320) * 240
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 320, 240, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


IMAGE_BASE64 = base64.b64encode(fixture_png()).decode("ascii")


class VisionPlannerModel(ScriptedModel):
    """Reuse the existing scripted planner and add an independent vision lane."""

    def __init__(self, steps, *, fail_vision=False):
        super().__init__(steps)
        self.vision_calls = []
        self.fail_vision = fail_vision
        owner = self
        scripted_handler = self.server.RequestHandlerClass

        class Handler(scripted_handler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                body = json.loads(raw)
                images = [part for message in body.get("messages", [])
                          if isinstance(message.get("content"), list)
                          for part in message["content"]
                          if isinstance(part, dict) and part.get("type") == "image_url"]
                if not images:
                    # The parent handler owns planner ordering and independent
                    # goal-judge fixtures. Give it precisely the original body.
                    original = self.rfile
                    try:
                        self.rfile = io.BytesIO(raw)
                        return super().do_POST()
                    finally:
                        self.rfile = original
                owner.vision_calls.append(body)
                if owner.fail_vision:
                    value = {"error": {"message": KEY + " PRIVATE_PROVIDER_BODY",
                                       "type": "invalid_request_error"}}
                    status = 400
                else:
                    value = {"id": "offline-vision", "object": "chat.completion",
                             "created": 1, "model": "offline-vision",
                             "choices": [{"index": 0, "message": {
                                 "role": "assistant", "content": OBSERVATION + " " + KEY},
                                 "finish_reason": "stop"}]}
                    status = 200
                payload = json.dumps(value).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                try:
                    self.wfile.write(payload)
                except ConnectionError:
                    pass

        self.server.RequestHandlerClass = Handler


FIXTURE = r'''
import asyncio, json, os, re, socket, sys
from pathlib import Path
sys.path.insert(0, __BACKEND__)
import worker
import desktop_controls
import desktop_visual
COUNTS = {"captures": 0, "native_inputs": 0, "subprocesses": 0, "blocked_network": []}
OUTPUT = Path(__AUDIT__)
PORT = __PORT__
CAPTURE_FAILURE = __CAPTURE_FAILURE__
# Windows constructs one private socket pair when creating its Proactor loop.
# Create it before the model-port guard, then reuse this exact owned loop.
OWNED_LOOP = asyncio.new_event_loop()
for key in list(os.environ):
    if re.search(r'API_?KEY|TOKEN|SECRET|PASSWORD|OPENAI|ANTHROPIC|NANOBOT', key, re.I):
        os.environ.pop(key, None)
    if key.upper() in {'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY'}:
        os.environ.pop(key, None)
os.environ['NO_PROXY'] = '*'
def check_address(address):
    if (not isinstance(address, tuple) or len(address) < 2
            or address[0] != '127.0.0.1' or address[1] != PORT):
        COUNTS['blocked_network'].append('NON_FIXTURE_NETWORK')
        raise OSError('NON_FIXTURE_NETWORK_BLOCKED')
original_dns = socket.getaddrinfo
def dns(host, port, *args, **kwargs):
    check_address((host, port)); return original_dns(host, port, *args, **kwargs)
socket.getaddrinfo = dns
original_connect, original_connect_ex = socket.socket.connect, socket.socket.connect_ex
def connect(sock, address):
    check_address(address); return original_connect(sock, address)
def connect_ex(sock, address):
    check_address(address); return original_connect_ex(sock, address)
socket.socket.connect, socket.socket.connect_ex = connect, connect_ex
if os.name == 'nt':
    from asyncio.proactor_events import BaseProactorEventLoop
    original_sock_connect = BaseProactorEventLoop.sock_connect
    async def sock_connect(loop, sock, address):
        check_address(address); return await original_sock_connect(loop, sock, address)
    BaseProactorEventLoop.sock_connect = sock_connect
os.environ['TIKTOKEN_CACHE_DIR'] = __CACHE__
import tiktoken
COUNTS['offline_tokenizer_ready'] = tiktoken.get_encoding('cl100k_base').name == 'cl100k_base'
async def no_subprocess(*args, **kwargs):
    COUNTS['subprocesses'] += 1
    raise AssertionError('NATIVE_SUBPROCESS_NOT_ALLOWED')
asyncio.create_subprocess_exec = no_subprocess
asyncio.create_subprocess_shell = no_subprocess
class Controls:
    def invalidate(self): pass
    async def windows(self, *args, **kwargs):
        raise AssertionError('NATIVE_WINDOWS_NOT_ALLOWED')
    async def observe(self, *args, **kwargs):
        raise AssertionError('NATIVE_UIA_NOT_ALLOWED')
    async def verify(self, *args, **kwargs):
        raise AssertionError('NATIVE_UIA_NOT_ALLOWED')
    async def act(self, *args, **kwargs):
        COUNTS['native_inputs'] += 1; raise AssertionError('NATIVE_INPUT_NOT_ALLOWED')
    async def activate(self, *args, **kwargs):
        COUNTS['native_inputs'] += 1; raise AssertionError('NATIVE_INPUT_NOT_ALLOWED')
desktop_controls.DesktopControls = Controls
async def capture():
    COUNTS['captures'] += 1
    if CAPTURE_FAILURE:
        raise desktop_visual.DesktopVisualError(CAPTURE_FAILURE)
    return {'ok': True, 'base64': __IMAGE__, 'width': 320, 'height': 240,
            'bounds': [0, 0, 320, 240], 'monitors': [[0, 0, 320, 240]],
            'primary': True, 'inputBounds': [0, 0, 320, 240],
            'inputMonitors': [[0, 0, 320, 240]], 'dpiAwareness': 2,
            'scaleFactor': 1, 'backend': 'win32-gdi'}
desktop_visual.capture_desktop = capture
async def main():
    try:
        await worker.main()
    finally:
        OUTPUT.write_text(json.dumps(COUNTS), encoding='utf-8')
asyncio.run(main(), loop_factory=lambda: OWNED_LOOP)
'''


@unittest.skipUnless(PYTHON.is_file() and os.name == "nt", "Windows SDK environment required")
class DesktopViewWorkerTests(unittest.TestCase):
    observations = []
    def run_case(self, steps, *, computer=True, fail_vision=False, capture_failure=None,
                 task="Describe the visible foreground application without clicking anything."):
        root = REPO / "agent_workspace"
        root.mkdir(exist_ok=True)
        workspace = Path(tempfile.mkdtemp(prefix="desktop-view-offline-", dir=root))
        model, client = None, None
        try:
            cache = seed_owned_tokenizer_cache(workspace)
            model = VisionPlannerModel(steps, fail_vision=fail_vision)
            audit = workspace / "audit.json"
            script = workspace / "fixture_worker.py"
            code = (FIXTURE.replace("__BACKEND__", repr(str(BACKEND)))
                    .replace("__AUDIT__", repr(str(audit)))
                    .replace("__PORT__", str(model.server.server_port))
                    .replace("__CAPTURE_FAILURE__", repr(capture_failure))
                    .replace("__CACHE__", repr(str(cache)))
                    .replace("__IMAGE__", repr(IMAGE_BASE64)))
            code = ("try:\n" + textwrap.indent(code, "    ")
                    + "\nexcept BaseException as problem:\n"
                    + "    import traceback\n"
                    + "    from pathlib import Path\n"
                    + "    Path(" + repr(str(audit)) + ").write_text("
                    + "__import__('json').dumps({'fixture_error': type(problem).__name__, "
                    + "'traceback': traceback.format_exc()}), encoding='utf-8')\n"
                    + "    raise\n")
            script.write_text(code, encoding="utf-8")
            client = Client.__new__(Client)
            Client.__init__(client, worker_path=script)
            conversation_id = str(uuid4())
            client.send({"type": "run", "id": "desktop-view-fixture", "kind": "agent",
                         "conversation_id": conversation_id, "session_history": [], "task": task,
                         "config": {"base_url": model.base_url, "api_key": KEY,
                                    "model": "fixture-planner-with-vision"},
                         "options": {"workspace": str(workspace), "max_steps": 20,
                                     "timeout_seconds": 25, "tools_enabled": True,
                                     "computer_enabled": computer, "include_activity": True,
                                     "context_store_root": str(workspace / "observations"),
                                     "context_store_namespace": conversation_id + ":" + "a" * 64}})
            result = client.terminal()
            client.process.stdin.close()
            client.process.wait(timeout=10)
            self.assertEqual(client.process.returncode, 0)
            frames = list(client.all_frames)
            self.assertEqual(client.close(), "")
            client = None
            self.assertEqual(model.errors, [])
            self.assertEqual(len(model.calls), len(steps), result)
            counts = json.loads(audit.read_text(encoding="utf-8"))
            self.assertTrue(counts["offline_tokenizer_ready"])
            self.assertEqual(counts["blocked_network"], [])
            self.assertEqual(counts["native_inputs"], 0)
            self.assertEqual(counts["subprocesses"], 0)
            public = json.dumps(frames)
            self.assertNotIn(KEY, public)
            self.assertNotIn(IMAGE_BASE64, public)
            self.assertNotIn("PRIVATE_PROVIDER_BODY", public)
            for path in workspace.rglob("*"):
                if path.is_file() and path != script and path.parent != cache:
                    self.assertNotIn(IMAGE_BASE64.encode(), path.read_bytes(), str(path))
            self.observations.append({"case": self._testMethodName, "counts": counts,
                "planner_requests": len(model.calls), "vision_requests": len(model.vision_calls),
                "status": result.get("status"), "gui_used": result.get("gui_used"),
                "gui_goal_verified": result.get("gui_goal_verified"),
                "scope": result.get("completion_scope"), "image_in_protocol": False,
                "image_in_parent_requests": IMAGE_BASE64 in json.dumps(model.calls),
                "image_in_owned_persistent_files": False})
            return result, list(model.calls), list(model.vision_calls), counts
        except BaseException:
            if model is not None and audit.is_file():
                print("Owned fixture audit: " + audit.read_text(encoding="utf-8"), file=sys.stderr)
            raise
        finally:
            if client is not None:
                self.assertEqual(client.close(), "")
            if model is not None:
                model.close()
            self.assertTrue(workspace.resolve().is_relative_to(root.resolve()))
            shutil.rmtree(workspace)

    @staticmethod
    def observation(body):
        return next(message for message in reversed(body["messages"])
                    if message.get("role") == "tool" and message.get("name") == "desktop_view")

    def test_real_vision_response_reaches_parent_without_gui_executor(self):
        def parent_after_view(body):
            value = json.loads(self.observation(body)["content"])
            self.assertEqual(value["scope"], "visual_observation_only")
            self.assertIs(value["task_verified"], False)
            self.assertIn(OBSERVATION, value["observation"])
            self.assertNotIn(KEY, value["observation"])
            names = {item["function"]["name"] for item in body["tools"]}
            self.assertIn("desktop_view", names)
            self.assertNotIn("computer_use", names)
            return OBSERVATION
        result, calls, vision, counts = self.run_case([
            tool("desktop_view", question="What is the current heart state?"), parent_after_view])
        self.assertEqual(result["type"], "final", result)
        self.assertEqual(result["status"], "completed", result)
        self.assertFalse(result["gui_used"])
        self.assertFalse(result["gui_goal_verified"])
        self.assertEqual(counts["captures"], 1)
        self.assertEqual(len(vision), 1)
        self.assertEqual(vision[0]["model"], "fixture-planner-with-vision")
        self.assertIn("data:image/png;base64," + IMAGE_BASE64, json.dumps(vision[0]))
        self.assertNotIn(IMAGE_BASE64, json.dumps(calls))

    def test_visual_description_cannot_verify_requested_like_action(self):
        result, _, vision, counts = self.run_case([
            tool("desktop_view", question="What is the current heart state?"), "The second video has been liked."],
            task="Like the second video in the foreground application.")
        self.assertEqual(result["status"], "needs_user", result)
        self.assertFalse(result["gui_goal_verified"])
        self.assertEqual(counts["captures"], 1)
        self.assertEqual(len(vision), 1)

    def test_unconfirmed_capture_cleanup_stops_parent_before_another_turn(self):
        result, calls, vision, counts = self.run_case([
            tool("desktop_view", question="Describe the current application.")],
            capture_failure="DESKTOP_VISUAL_CLEANUP_UNCONFIRMED")
        self.assertEqual(result["status"], "needs_user", result)
        self.assertIn("DESKTOP_VISUAL_CLEANUP_UNCONFIRMED", result["acceptance"]["issue_codes"])
        self.assertFalse(checked_assessment(result["acceptance"])["passed"])
        self.assertFalse(result["gui_goal_verified"])
        self.assertEqual(counts["captures"], 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(vision, [])

    def test_view_not_advertised_without_computer_permission(self):
        def parent_without_permission(body):
            names = {item["function"]["name"] for item in body["tools"]}
            self.assertNotIn("desktop_view", names)
            return "Desktop access has not been enabled."
        _, _, vision, counts = self.run_case([parent_without_permission], computer=False)
        self.assertEqual(counts["captures"], 0)
        self.assertEqual(vision, [])

    def test_model_failure_is_fixed_and_never_exposes_provider_body(self):
        def parent_after_failure(body):
            error = self.observation(body)["content"]
            self.assertIn("DESKTOP_VISUAL_", error)
            self.assertNotIn(KEY, error)
            self.assertNotIn("PRIVATE_PROVIDER_BODY", error)
            return "I could not obtain a visual observation."
        result, _, vision, counts = self.run_case([
            tool("desktop_view", question="Describe the current application."), parent_after_failure], fail_vision=True)
        self.assertEqual(result["reply"], "I could not obtain a visual observation.")
        self.assertFalse(result["gui_goal_verified"])
        self.assertEqual(counts["captures"], 1)
        self.assertEqual(len(vision), 1)


if __name__ == "__main__":
    unittest.main()
