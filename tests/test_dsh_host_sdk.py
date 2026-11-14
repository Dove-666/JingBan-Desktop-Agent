"""Actual AgentService -> official DSH -> loopback model transport acceptance.

The deterministic provider is local and synthetic. No user API, private data,
screenshots or desktop input is used by this test.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from desktop_pet.agents.contracts import AgentRequest, AgentError
from desktop_pet.agents.service import AgentService, dsh_installation, node_executable, _DSH_VERSION
from desktop_pet.agents.settings import AgentSettings


class DshOfficialHostTests(unittest.TestCase):
    def test_official_provider_wait_cancels_through_sdk_before_host_closure(self):
        if node_executable() is None or any(value != _DSH_VERSION for value in dsh_installation().values()):
            self.skipTest("Pinned official DSH runtime is not installed")
        entered, release, cancel = threading.Event(), threading.Event(), threading.Event()
        handles, failures = [], []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                entered.set()
                release.wait(35)
                self.close_connection = True
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            with tempfile.TemporaryDirectory(prefix="aura-dsh-sdk-stop-") as temporary:
                root = Path(temporary)
                workspace = root / "workspace"
                workspace.mkdir()
                service = AgentService(checkpoint_path=root / "private" / "legacy.sqlite3")
                settings = AgentSettings(workspace=str(workspace), computer_enabled=True)
                context = {"api": {"base_url": f"http://127.0.0.1:{server.server_port}/v1", "model": "fixture-model"},
                    "conversation_id": str(uuid4()), "conversation_generation": str(uuid4()), "messages": []}
                def run():
                    try: service.execute(settings, "synthetic-stop-test-key", AgentRequest("Wait for the local fixture.", "agent"),
                        context, cancellation_event=cancel, worker_lifecycle_callback=handles.append)
                    except AgentError as exc: failures.append(exc.code)
                worker = threading.Thread(target=run)
                try:
                    with patch("desktop_pet.agents.settings.agent_workspace_root", lambda: workspace):
                        worker.start()
                        self.assertTrue(entered.wait(20))
                        service.cancel_request(cancel)
                        worker.join(30)
                        self.assertFalse(worker.is_alive())
                        self.assertEqual(failures, ["cancelled"])
                        receipt = service.worker_closure(handles[0])
                        self.assertTrue(service.validate_worker_closure(handles[0], receipt))
                        self.assertFalse(service.execution_resources_pending())
                finally:
                    release.set()
                    service.shutdown()
                    worker.join(10)
                    service.reconcile_worker_closures()
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            server_thread.join(5)

    def test_official_loop_writes_presents_downloads_and_resumes(self):
        self.official_loop(external=False)

    def test_official_loop_delivers_external_file_without_legacy_grant(self):
        self.official_loop(external=True)

    def test_official_reasoning_reset_does_not_resume_previous_high_effort(self):
        self.official_loop(external=False, reasoning_reset=True)

    def official_loop(self, *, external, reasoning_reset=False):
        if node_executable() is None or any(value != _DSH_VERSION for value in dsh_installation().values()):
            self.skipTest("Pinned official DSH runtime is not installed")
        requests = []
        errors = []
        file_name = "host-result.txt"
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass

            def do_POST(self):
                try:
                    if self.path != "/v1/chat/completions": raise ValueError("unexpected route")
                    if self.headers.get("Authorization") != "Bearer synthetic-host-test-key": raise ValueError("missing fixture auth")
                    body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    requests.append(body)
                    count = len(requests)
                    names = {tool["function"]["name"] for tool in body.get("tools", [])}
                    if not {"write", "present"}.issubset(names): raise ValueError("missing official tools")
                    if count == 1:
                        tool, args = "write", {"file_path": file_name, "content": "Written by the official DSH loop.\n"}
                    elif count == 2:
                        tool, args = "present", {"files": [{"path": file_name}]}
                    else:
                        tool, args = None, None
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    def emit(delta, finish=None):
                        frame = {"id": "chatcmpl-host-fixture", "object": "chat.completion.chunk", "created": 1,
                                 "model": "fixture-model", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
                        self.wfile.write(("data: " + json.dumps(frame) + "\n\n").encode("utf-8"))
                    emit({"role": "assistant"})
                    if tool:
                        emit({"tool_calls": [{"index": 0, "id": f"call-{count}", "type": "function",
                            "function": {"name": tool, "arguments": json.dumps(args)}}]})
                    else:
                        emit({"content": "报告已保存。" if count == 3 else "继续使用刚才的报告。"})
                    emit({}, "tool_calls" if tool else "stop")
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                    self.close_connection = True
                except Exception as exc:
                    errors.append(type(exc).__name__ + ":" + str(exc))
                    self.send_error(500)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory(prefix="aura-dsh-sdk-host-") as temporary:
                root = Path(temporary)
                workspace = root / "workspace"
                workspace.mkdir()
                target = root / "external-report.txt" if external else workspace / file_name
                if external:
                    file_name = str(target)
                service = AgentService(checkpoint_path=root / "private" / "legacy.sqlite3")
                settings = AgentSettings(workspace=str(workspace), computer_enabled=False, timeout_seconds=60)
                cid = str(uuid4())
                context = {"api": {"base_url": f"http://127.0.0.1:{server.server_port}/v1", "model": "fixture-model"},
                    "conversation_id": cid, "conversation_generation": str(uuid4()),
                    "messages": [{"role": "system", "content": "Return an old JSON reply."}],
                    "personality": "Use concise Chinese replies."}
                if reasoning_reset:
                    context["api"].update(reasoning_profile="openai", reasoning_effort="high")
                events = []
                try:
                    with patch("desktop_pet.agents.settings.agent_workspace_root", lambda: workspace):
                        result = service.execute(settings, "synthetic-host-test-key", AgentRequest("Create a report and deliver it.", "agent"),
                                                 context, event_callback=events.append)
                        self.assertEqual(result.reply, "报告已保存。")
                        self.assertEqual(result.status, "completed")
                        self.assertEqual(result.backend, "deepseek-harness")
                        self.assertEqual(len(result.artifacts), 1)
                        self.assertEqual(target.read_text(), "Written by the official DSH loop.\n")
                        self.assertEqual(service._file_grants, {})
                        page = service.artifact_page(cid, result.artifacts[0]["reference"])
                        self.assertEqual(page["bytes"], len(b"Written by the official DSH loop.\n"))
                        context["personality"] = "Speak as the synthetic character Beta."
                        if reasoning_reset:
                            context["api"]["reasoning_effort"] = "default"
                            context["messages"] = [{"role": "assistant", "content": "Written by the official DSH loop."}]
                        again = service.execute(settings, "synthetic-host-test-key", AgentRequest("Remember that report.", "agent"), context)
                        self.assertEqual(again.reply, "继续使用刚才的报告。")
                        self.assertFalse(service.execution_resources_pending())
                finally:
                    service.shutdown()
                    service.reconcile_worker_closures()
                self.assertFalse(errors, errors)
                self.assertEqual(len(requests), 4)
                self.assertIn("Written by the official DSH loop.", json.dumps(requests[-1]["messages"]))
                self.assertNotIn("Return an old JSON reply.", json.dumps(requests))
                first_system = json.dumps([item for item in requests[0]["messages"] if item["role"] in {"system", "developer"}])
                next_system = json.dumps([item for item in requests[-1]["messages"] if item["role"] in {"system", "developer"}])
                self.assertIn("Use concise Chinese replies.", first_system)
                self.assertIn("synthetic character Beta", next_system)
                self.assertNotIn("Use concise Chinese replies.", next_system)
                if reasoning_reset:
                    self.assertEqual(requests[0].get("reasoning_effort"), "high")
                    self.assertNotEqual(requests[-1].get("reasoning_effort"), "high")
                    self.assertFalse(any(item["role"] == "tool" for item in requests[-1]["messages"]))
                self.assertEqual(len([event for event in events if event["kind"] == "tool" and event["status"] == "completed"]), 2)
                metadata_files = list((root / "private" / "dsh_sessions").rglob("session.json"))
                self.assertEqual(len(metadata_files), 2 if reasoning_reset else 1)
                for metadata in metadata_files:
                    self.assertNotIn("synthetic-host-test-key", metadata.read_text())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)


if __name__ == "__main__": unittest.main()
