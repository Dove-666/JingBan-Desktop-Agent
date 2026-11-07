"""Local HTTP fixture exercising the actual SDK without external API calls."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from queue import Queue
import threading
import time


class MockApiServer:
    def __init__(self):
        self.requests = []
        self.metadata_requests = []
        self.plans = Queue()
        self.received = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                data = json.loads(self.rfile.read(length))
                request = {"path": self.path, "body": data, "authorization": self.headers.get("Authorization")}
                is_metadata = any("AURA_SESSION_METADATA" in str(item.get("content", ""))
                                  for item in data.get("messages", []) if item.get("role") == "system")
                if is_metadata:
                    # Track auxiliary real HTTP separately, without consuming
                    # the deterministic plans intended for user chat replies.
                    owner.metadata_requests.append(request)
                    plan = {"delay": 0, "status": 200, "body": None,
                            "content": '{"title":"讨论桌宠功能","summary":"用户与鲸鱼娘讨论了提供的会话内容。"}'}
                else:
                    owner.requests.append(request)
                    owner.received.set()
                    plan = owner.plans.get(timeout=2)
                time.sleep(plan["delay"])
                if plan["status"] != 200:
                    body = {"error": {"message": plan["content"], "type": "test_error", "code": "test_error"}}
                elif plan["body"] is not None:
                    body = plan["body"]
                else:
                    body = {
                        "id": "local-completion", "object": "chat.completion", "created": 0,
                        "model": "test-model", "choices": [{"index": 0, "finish_reason": "stop",
                        "message": {"role": "assistant", "content": plan["content"]}}],
                    }
                payload = json.dumps(body).encode("utf-8")
                stream = data.get("stream") is True and plan["status"] == 200
                if stream:
                    frames = []
                    for choice in body.get("choices", []):
                        message = choice.get("message", {})
                        frame = {"id": "local-stream", "object": "chat.completion.chunk", "created": 0,
                                 "model": "test-model", "choices": [{"index": choice.get("index", 0),
                                 "delta": message, "finish_reason": choice.get("finish_reason")}]}
                        frames.append("data: " + json.dumps(frame) + "\n\n")
                    payload = ("".join(frames) + "data: [DONE]\n\n").encode("utf-8")
                try:
                    self.send_response(plan["status"])
                    self.send_header("Content-Type", "text/event-stream" if stream else "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                    pass  # Timeout/cancellation intentionally closes the socket.

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._server.block_on_close = False
        self.base_url = f"http://127.0.0.1:{self._server.server_port}/v1"
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)

    def enqueue(self, content="你好呀！", *, status=200, delay=0, body=None):
        self.plans.put({"content": content, "status": status, "delay": delay, "body": body})

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *_):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)
