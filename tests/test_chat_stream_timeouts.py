"""Real localhost checks for long healthy streams, idle timeouts and Stop."""
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from desktop_pet.api_client import ApiClient, ApiError, ChatReply
from desktop_pet.settings_manager import ApiSettings
from tests.test_chat_stream import SseServer


KEY = "synthetic-stream-timeout-credential"
MESSAGES = [{"role": "user", "content": "本地受控测试"}]


class TricklingJsonServer:
    """Send valid JSON whitespace regularly, then a delayed full JSON value."""
    def __init__(self):
        owner = self
        self.requests, self.first_byte = [], threading.Event()

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def respond(self):
                owner.requests.append((self.command, self.path))
                if self.command == "POST":
                    self.rfile.read(int(self.headers.get("Content-Length", "0")))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                try:
                    for _ in range(16):
                        self.wfile.write(b"1\r\n \r\n")
                        self.wfile.flush()
                        owner.first_byte.set()
                        time.sleep(0.15)
                    body = (b'{"object":"list","data":[{"id":"local-model","object":"model"}]}'
                        if self.command == "GET" else
                        b'{"choices":[{"message":{"content":"complete"},"finish_reason":"stop"}]}')
                    self.wfile.write(f"{len(body):x}\r\n".encode() + body + b"\r\n0\r\n\r\n")
                    self.wfile.flush()
                except (OSError, ConnectionResetError):
                    pass
                finally:
                    self.close_connection = True

            do_POST = respond
            do_GET = respond

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.base_url = f"http://127.0.0.1:{self.server.server_port}/v1"

    def __enter__(self):
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


class ChatStreamTimeoutTests(unittest.TestCase):
    def test_nonstream_completion_and_model_list_keep_total_deadline_despite_live_bytes(self):
        for operation in ("completion", "models", "json_fallback"):
            with self.subTest(operation=operation), TricklingJsonServer() as server:
                client = ApiClient()
                settings = ApiSettings(server.base_url, "local-model", timeout_seconds=1)
                started = time.monotonic()
                with self.assertRaises(ApiError) as caught:
                    if operation == "models":
                        client.list_models(settings, KEY)
                    elif operation == "json_fallback":
                        client.complete_context(settings, KEY, MESSAGES,
                            on_delta=lambda _: self.fail("A buffered response is not complete yet"))
                    else:
                        client.complete_context(settings, KEY, MESSAGES)
                self.assertEqual(caught.exception.code, "timeout")
                self.assertLess(time.monotonic() - started, 3)
                self.assertTrue(server.first_byte.is_set(), "The test must reach a live HTTP response")
                self.assertEqual(len(server.requests), 1)

    def test_continuous_chunks_outlive_configured_timeout_and_complete_once(self):
        times, snapshots = {}, []

        def plan(handler):
            times["first"] = time.monotonic()
            handler.send('{"reply":"开始')
            for _ in range(6):
                time.sleep(0.3)
                handler.send("。")
            handler.send('完成🐳","emotion":"happy"}')
            times["end"] = time.monotonic()
            handler.done()

        with SseServer(plan) as server:
            settings = ApiSettings(server.base_url, "local-model", timeout_seconds=1)
            reply = ApiClient().complete_context(settings, KEY, MESSAGES,
                on_delta=lambda text: snapshots.append((time.monotonic(), text)))
            self.assertEqual(reply, ChatReply("开始" + "。" * 6 + "完成🐳", "happy"))
            self.assertGreater(times["end"] - times["first"], settings.timeout_seconds + 0.5)
            self.assertLess(snapshots[0][0], times["end"])
            self.assertEqual(len(server.requests), 1)
            self.assertTrue(server.requests[0]["stream"])
            self.assertEqual(snapshots[-1][1], reply.reply)

    def test_silent_read_idle_still_times_out_and_retains_partial_without_retry(self):
        snapshots = []

        def plan(handler):
            handler.send('{"reply":"已经收到的部分')
            server.release.wait(8)
            handler.send('不可变成成功","emotion":"happy"}')
            handler.done()

        with SseServer(plan) as server:
            with self.assertRaises(ApiError) as caught:
                ApiClient().complete_context(ApiSettings(server.base_url, "local-model", timeout_seconds=1),
                    KEY, MESSAGES, on_delta=lambda text: snapshots.append((time.monotonic(), text)))
            ended = time.monotonic()
            self.assertTrue(snapshots)
            self.assertEqual(caught.exception.code, "timeout")
            self.assertEqual(caught.exception.partial_reply, "已经收到的部分")
            # Measure from the first received text, excluding SDK setup and
            # connection startup. No bytes arrive during the idle interval.
            self.assertGreater(ended - snapshots[0][0], 0.75)
            self.assertLess(ended - snapshots[0][0], 4)
            self.assertEqual(len(server.requests), 1)

    def test_stop_cancels_healthy_stream_after_old_deadline_in_under_two_seconds(self):
        beyond_old_deadline, first = threading.Event(), threading.Event()
        cancelled, result = threading.Event(), {}
        snapshots = []

        def plan(handler):
            started = time.monotonic()
            handler.send('{"reply":"正在继续')
            for _ in range(40):
                if server.release.wait(0.15):
                    return
                handler.send("。")
                if time.monotonic() - started > 1.3:
                    beyond_old_deadline.set()
            handler.send('结束","emotion":"neutral"}')
            handler.done()

        def update(text):
            snapshots.append(text)
            first.set()

        with SseServer(plan) as server:
            client = ApiClient()

            def run():
                try:
                    result["reply"] = client.complete_context(
                        ApiSettings(server.base_url, "local-model", timeout_seconds=1), KEY, MESSAGES,
                        cancellation_event=cancelled, on_delta=update)
                except BaseException as error:
                    result["error"] = error

            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            try:
                self.assertTrue(first.wait(8), result)
                self.assertTrue(beyond_old_deadline.wait(6), result)
                self.assertTrue(thread.is_alive(), result)
                started = time.monotonic()
                client.cancel_request(cancelled)
                thread.join(1.9)
                self.assertFalse(thread.is_alive())
                self.assertLess(time.monotonic() - started, 2)
                self.assertNotIn("reply", result)
                self.assertIsInstance(result.get("error"), ApiError)
                self.assertEqual(result["error"].code, "cancelled")
                self.assertEqual(result["error"].partial_reply, snapshots[-1])
                self.assertTrue(result["error"].partial_reply.startswith("正在继续"))
                self.assertEqual(len(server.requests), 1)
            finally:
                server.release.set()
                thread.join(2)


if __name__ == "__main__":
    unittest.main()
