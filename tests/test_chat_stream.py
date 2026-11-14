"""Real localhost SSE transport tests; never contact a production provider."""
import json
import socket
import threading
import time
import unittest
from unittest.mock import Mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from desktop_pet.redaction import redact_text
from desktop_pet.api_client import ApiClient, ApiError, ChatReply
from desktop_pet.settings_manager import ApiSettings


KEY = 'sk-local-only-"quoted\\credential'


class SseServer:
    def __init__(self, plan, *, status=200, json_response=False):
        self.plan, self.status, self.requests = plan, status, []
        self.release = threading.Event()
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                fixture.requests.append(json.loads(body))
                self.send_response(fixture.status)
                self.send_header("Content-Type", "text/event-stream" if fixture.status == 200 and not json_response else "application/json")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                try:
                    fixture.plan(self)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    self.close_connection = True

            def write_chunk(self, body):
                self.wfile.write(f"{len(body):x}\r\n".encode("ascii") + body + b"\r\n")
                self.wfile.flush()

            def send(self, content=None, *, finish=None, extra=None):
                delta = dict(extra or {})
                if content is not None:
                    delta["content"] = content
                payload = {"id": "local-stream", "object": "chat.completion.chunk", "created": 0,
                           "model": "local-model", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
                frame = ("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode("utf-8")
                # Actual HTTP transfer chunks split the SSE frame and UTF-8;
                # neither the SDK nor our projection can assume frame boundaries.
                for offset in range(0, len(frame), 13):
                    self.write_chunk(frame[offset:offset + 13])

            def done(self, reason="stop"):
                self.send(finish=reason)
                self.write_chunk(b"data: [DONE]\n\n")
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.base_url = f"http://127.0.0.1:{self.server.server_port}/v1"

    def __enter__(self):
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


def run_background(client, server, callback, cancellation=None, **kwargs):
    result = {}
    def target():
        try:
            result["reply"] = client.complete_context(ApiSettings(server.base_url, "local-model"), KEY,
                [{"role": "user", "content": "仅本地测试"}], on_delta=callback,
                cancellation_event=cancellation, **kwargs)
        except BaseException as error:
            result["error"] = error
    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, result


class ChatStreamTransportTests(unittest.TestCase):
    def test_visible_reply_precedes_response_end_and_ignores_other_channels(self):
        first = threading.Event()
        snapshots = []
        def plan(handler):
            handler.send(extra={"reasoning_content": "PRIVATE_REASONING", "tool_calls": [{"index": 0,
                "function": {"name": "private_tool", "arguments": '{"secret":"DO_NOT_SHOW"}'}}]})
            handler.send('{"emotion":"happy","private":{"tool":"WIRE_ONLY"},"reply":"你好')
            server.release.wait(15)
            handler.send(' 🐳\\n第二行\\\"引号\\\"","agent_request":null}')
            handler.done()
        def update(text):
            snapshots.append(text)
            first.set()
        with SseServer(plan) as server:
            thread, result = run_background(ApiClient(), server, update)
            try:
                self.assertTrue(first.wait(8), result)
                self.assertTrue(thread.is_alive(), "First text must arrive while the response is still open")
                self.assertEqual(snapshots[-1], "你好")
            finally:
                server.release.set()
                thread.join(4)
            self.assertNotIn("error", result)
            self.assertEqual(result["reply"], ChatReply('你好 🐳\n第二行"引号"', "happy"))
            self.assertEqual(len(server.requests), 1)
            self.assertTrue(server.requests[0]["stream"])
            for text in snapshots:
                for hidden in ("PRIVATE_REASONING", "DO_NOT_SHOW", "private_tool", "WIRE_ONLY", '"reply"', '"emotion"'):
                    self.assertNotIn(hidden, text)

    def test_unicode_and_json_escaped_secrets_never_cross_chunk_boundary(self):
        escaped = KEY
        for _ in range(4):
            escaped = json.dumps(escaped)[1:-1]
        answer = '鲸鱼🐳：第一行\n"第二行" ' + KEY + " / " + escaped + "，完成。"
        raw = json.dumps({"reply": answer, "emotion": "happy"}, ensure_ascii=True)
        snapshots = []
        def plan(handler):
            for char in raw:
                handler.send(char)
            handler.done()
        with SseServer(plan) as server:
            reply = ApiClient().complete_context(ApiSettings(server.base_url, "local-model"), KEY,
                [{"role": "user", "content": "测试"}], on_delta=snapshots.append)
            self.assertEqual(reply.reply, redact_text(answer, (KEY,), "[已隐藏密钥]"))
            self.assertEqual(len(server.requests), 1)
        for text in snapshots:
            self.assertNotIn(KEY, text)
            self.assertNotIn(escaped, text)
            self.assertNotIn("\ud83d", text)
            self.assertNotIn("\udc33", text)
        self.assertTrue(any("🐳" in text for text in snapshots))

    def test_plain_text_and_split_hidden_tags_preserve_markdown(self):
        raw = '<think>PRIVATE "analysis"</think>普通中文。\n```python\nprint("你好")\n```\n结束。'
        snapshots = []
        def plan(handler):
            for char in raw:
                handler.send(char)
            handler.done()
        with SseServer(plan) as server:
            reply = ApiClient().complete_context(ApiSettings(server.base_url, "local-model"), KEY,
                [{"role": "user", "content": "测试"}], on_delta=snapshots.append)
        self.assertEqual(reply.reply, raw.split("</think>", 1)[1])
        self.assertTrue(snapshots)
        self.assertTrue(all("PRIVATE" not in text and "<think" not in text and "</think" not in text for text in snapshots))

    def test_cancel_closes_open_socket_retains_partial_and_never_retries(self):
        first = threading.Event()
        cancellation = threading.Event()
        def plan(handler):
            handler.send('{"reply":"已收到的一部分')
            server.release.wait(15)
            handler.send('不应完成","emotion":"happy"}')
            handler.done()
        client = ApiClient()
        with SseServer(plan) as server:
            thread, result = run_background(client, server, lambda text: first.set(), cancellation)
            self.assertTrue(first.wait(8), result)
            started = time.monotonic()
            client.cancel_request(cancellation)
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertLess(time.monotonic() - started, 2)
            self.assertNotIn("reply", result)
            self.assertIsInstance(result.get("error"), ApiError)
            self.assertEqual(result["error"].code, "cancelled")
            self.assertEqual(result["error"].partial_reply, "已收到的一部分")
            self.assertEqual(len(server.requests), 1)

    def test_connection_loss_never_promotes_partial_to_success_or_replays(self):
        for graceful in (False, True):
            with self.subTest(graceful=graceful):
                def plan(handler):
                    handler.send('{"reply":"仅收到部分')
                    if graceful:
                        handler.write_chunk(b"data: [DONE]\n\n")
                        handler.wfile.write(b"0\r\n\r\n")
                        handler.wfile.flush()
                    else:
                        handler.connection.shutdown(socket.SHUT_RDWR)
                with SseServer(plan) as server:
                    snapshots = []
                    with self.assertRaises(ApiError) as caught:
                        ApiClient().complete_context(ApiSettings(server.base_url, "local-model"), KEY,
                            [{"role": "user", "content": "测试"}], on_delta=snapshots.append)
                    self.assertEqual(caught.exception.partial_reply, "仅收到部分")
                    self.assertIn(caught.exception.code, ("network", "invalid_response", "stream_interrupted"))
                    self.assertEqual(len(server.requests), 1)
                    self.assertTrue(snapshots)

    def test_output_limit_and_malformed_envelope_do_not_return_success(self):
        for raw, finish, expected in (('{"reply":"保留这部分', "length", "output_limit"),
                                      ('{"reply":"保留这部分","emotion":', "stop", "invalid_response"),
                                      ('{"tool_calls":[{"arguments":"PRIVATE"}]}', "stop", "invalid_response")):
            with self.subTest(raw=raw):
                def plan(handler):
                    handler.send(raw)
                    handler.done(finish)
                with SseServer(plan) as server:
                    snapshots = []
                    with self.assertRaises(ApiError) as caught:
                        ApiClient().complete_context(ApiSettings(server.base_url, "local-model"), KEY,
                            [{"role": "user", "content": "测试"}], on_delta=snapshots.append)
                    self.assertEqual(caught.exception.code, expected)
                    self.assertEqual(len(server.requests), 1)
                    self.assertTrue(all("PRIVATE" not in text and "emotion" not in text for text in snapshots))

    def test_unknown_reasoning_parameter_is_one_safe_failure_not_second_request(self):
        def plan(handler):
            handler.write_chunk(json.dumps({"error": {"param": "reasoning_effort", "message": KEY}}).encode())
            handler.wfile.write(b"0\r\n\r\n")
            handler.wfile.flush()
        with SseServer(plan, status=400) as server:
            with self.assertRaises(ApiError) as caught:
                ApiClient().complete_context(ApiSettings(server.base_url, "local-model", reasoning_effort="high", reasoning_profile="openai"), KEY,
                    [{"role": "user", "content": "测试"}], on_delta=lambda _: self.fail("Error response must never become a delta"))
            self.assertEqual(caught.exception.code, "reasoning_unsupported")
            self.assertNotIn(KEY, str(caught.exception))
            self.assertEqual(caught.exception.partial_reply, "")
            self.assertEqual(len(server.requests), 1)

    def test_gateway_ignoring_stream_is_decoded_in_the_same_http_request(self):
        def plan(handler):
            handler.write_chunk(json.dumps({"id": "same-response", "choices": [{"index": 0,
                "message": {"content": '{"reply":"兼容原样回复","emotion":"happy"}',
                            "reasoning_content": "PRIVATE"}, "finish_reason": "stop"}]}).encode())
            handler.wfile.write(b"0\r\n\r\n")
            handler.wfile.flush()
        with SseServer(plan, json_response=True) as server:
            snapshots = []
            reply = ApiClient().complete_context(ApiSettings(server.base_url, "local-model"), KEY,
                [{"role": "user", "content": "测试"}], on_delta=snapshots.append)
            self.assertEqual(reply, ChatReply("兼容原样回复", "happy"))
            self.assertEqual(snapshots, ["兼容原样回复"])
            self.assertEqual(len(server.requests), 1)

    def test_explicit_pre_output_window_rejection_allows_one_valid_smaller_payload(self):
        current = [{"type": "text", "text": "当前完整问题不能裁剪"},
                   {"type": "image_url", "image_url": {"url": "data:image/png;base64,YWJj"}}]
        messages = [{"role": "system", "content": "保留全部系统设定"},
                    {"role": "user", "content": "以前的长内容" * 80},
                    {"role": "assistant", "content": "以前的回答" * 80},
                    {"role": "user", "content": current}]
        def plan(handler):
            if len(server.requests) == 1:
                server.status = 200
                handler.write_chunk(json.dumps({"error": {"code": "context_length_exceeded"}}).encode())
                handler.wfile.write(b"0\r\n\r\n")
                handler.wfile.flush()
            else:
                handler.send('{"reply":"使用恢复后的上下文回答","emotion":"neutral"}')
                handler.done()
        recover = Mock(side_effect=lambda original: [original[0], original[-1]])
        with SseServer(plan, status=400) as server:
            reply = ApiClient().complete_context(ApiSettings(server.base_url, "local-model"), KEY,
                messages, on_delta=lambda _: None, on_context_exceeded=recover)
            self.assertEqual(reply.reply, "使用恢复后的上下文回答")
            self.assertEqual(len(server.requests), 2)
            self.assertEqual(server.requests[1]["messages"], [messages[0], messages[-1]])
            self.assertEqual(server.requests[0]["messages"], messages)
        recover.assert_called_once()

    def test_second_explicit_window_rejection_stops_without_third_attempt(self):
        def plan(handler):
            handler.write_chunk(json.dumps({"error": {"code": "context_length_exceeded"}}).encode())
            handler.wfile.write(b"0\r\n\r\n")
            handler.wfile.flush()
        recover = Mock(side_effect=lambda original: [original[0], original[-1]])
        with SseServer(plan, status=400) as server:
            with self.assertRaises(ApiError) as caught:
                ApiClient().complete_context(ApiSettings(server.base_url, "local-model"), KEY,
                    [{"role": "system", "content": "系统"}, {"role": "user", "content": "以前" * 20},
                     {"role": "user", "content": "当前完整问题"}],
                    on_delta=lambda _: None, on_context_exceeded=recover)
            self.assertEqual(caught.exception.code, "context_length_exceeded")
            self.assertEqual(len(server.requests), 2)
        recover.assert_called_once()

    def test_recovery_cannot_change_system_current_or_mutate_original_payload(self):
        messages = [{"role": "system", "content": "系统"}, {"role": "user", "content": "历史" * 20},
                    {"role": "user", "content": "当前完整问题"}]
        def plan(handler):
            handler.write_chunk(json.dumps({"error": {"code": "context_length_exceeded"}}).encode())
            handler.wfile.write(b"0\r\n\r\n")
            handler.wfile.flush()
        def invalid(original):
            original[-1]["content"] = "当前"
            return [original[0], original[-1]]
        with SseServer(plan, status=400) as server:
            with self.assertRaises(ApiError):
                ApiClient().complete_context(ApiSettings(server.base_url, "local-model"), KEY,
                    messages, on_delta=lambda _: None, on_context_exceeded=invalid)
            self.assertEqual(len(server.requests), 1)
            self.assertEqual(messages[-1]["content"], "当前完整问题")

    def test_sse_error_after_visible_delta_never_attempts_context_recovery(self):
        def plan(handler):
            handler.send('{"reply":"已开始的回复')
            handler.write_chunk(b'data: {"error":{"code":"context_length_exceeded","message":"PRIVATE"}}\n\n')
            handler.wfile.write(b"0\r\n\r\n")
            handler.wfile.flush()
        recover = Mock()
        with SseServer(plan) as server:
            snapshots = []
            with self.assertRaises(ApiError) as caught:
                ApiClient().complete_context(ApiSettings(server.base_url, "local-model"), KEY,
                    [{"role": "user", "content": "测试"}], on_delta=snapshots.append,
                    on_context_exceeded=recover)
            self.assertEqual(caught.exception.partial_reply, "已开始的回复")
            self.assertEqual(len(server.requests), 1)
            self.assertNotIn("PRIVATE", str(caught.exception))
        recover.assert_not_called()

    def test_fenced_json_streams_only_reply_and_releases_non_secret_suffix_on_finish(self):
        answer = "这是普通末尾 sk-local"
        raw = '```json\n' + json.dumps({"reply": answer, "emotion": "neutral"}) + '\n```'
        def plan(handler):
            for char in raw:
                handler.send(char)
            handler.done()
        with SseServer(plan) as server:
            snapshots = []
            reply = ApiClient().complete_context(ApiSettings(server.base_url, "local-model"), KEY,
                [{"role": "user", "content": "测试"}], on_delta=snapshots.append)
        self.assertEqual(reply.reply, answer)
        self.assertEqual(snapshots[-1], answer)
        self.assertTrue(all("```json" not in text and '"reply"' not in text for text in snapshots))

    def test_legacy_opted_in_routing_wrapper_cannot_stream_tool_arguments(self):
        raw = '好的，我来帮你。\n```json\n' + json.dumps({"reply": "我来执行任务。", "emotion": "neutral",
            "agent_request": {"kind": "computer", "task": "PRIVATE_TASK"}}, ensure_ascii=False) + '\n```'
        def plan(handler):
            for char in raw:
                handler.send(char)
            handler.done()
        with SseServer(plan) as server:
            snapshots = []
            reply = ApiClient().complete_context(ApiSettings(server.base_url, "local-model"), KEY,
                [{"role": "user", "content": "测试"}], on_delta=snapshots.append, allow_agent_routing=True)
        self.assertEqual(reply.reply, "我来执行任务。")
        self.assertEqual(reply.agent_request, {"kind": "computer", "task": "PRIVATE_TASK"})
        self.assertTrue(all(text == "我来执行任务。" for text in snapshots))


if __name__ == "__main__":
    unittest.main()
