import io
import logging
import socket
import time
import traceback
import unittest

from desktop_pet.api_client import ApiClient, ApiError, ChatReply, parse_reply
from desktop_pet.settings_manager import ApiSettings
from tests.mock_api import MockApiServer


KEY = "local-only-test-token"


class ReplyParsingTests(unittest.TestCase):
    def test_json_and_fenced_json(self):
        for text in (
            '{"reply":"你好！","emotion":"happy"}',
            '```json\n{"reply":"你好！","emotion":"happy"}\n```',
        ):
            self.assertEqual(parse_reply(text), ChatReply("你好！", "happy"))

    def test_plain_text_is_compatible(self):
        self.assertEqual(parse_reply("  普通中文回复。  "), ChatReply("普通中文回复。"))

    def test_invalid_json_or_unknown_emotion_falls_back_to_original_text(self):
        for text in (
            '{"reply":"你好","emotion":"run_shell"}',
            '{"reply":"你好","emotion":["happy"]}',
            '{"reply":42,"emotion":"happy"}',
            '{"reply":" ","emotion":"happy"}',
            '["hello"]', '{not valid}',
            '__import__("os").system("do not execute")',
        ):
            self.assertEqual(parse_reply(text), ChatReply(text))

    def test_missing_emotion_defaults_to_neutral(self):
        self.assertEqual(parse_reply('{"reply":"你好"}'), ChatReply("你好"))


class ApiClientTests(unittest.TestCase):
    def test_real_sdk_request_uses_configured_endpoint_model_context(self):
        with MockApiServer() as server:
            server.enqueue('{"reply":"你好呀","emotion":"surprised"}')
            settings = ApiSettings(base_url=server.base_url, model="test-model")
            messages = [{"role": "user", "content": "你好"}]
            reply = ApiClient().complete(settings, KEY, messages)
            self.assertEqual(reply, ChatReply("你好呀", "surprised"))
            request = server.requests[0]
            self.assertEqual(request["path"], "/v1/chat/completions")
            self.assertEqual(request["authorization"], "Bearer " + KEY)
            self.assertEqual(request["body"]["model"], "test-model")
            self.assertEqual(request["body"]["messages"][0]["role"], "system")
            self.assertEqual(request["body"]["messages"][-1], messages[0])
            self.assertNotIn("response_format", request["body"])
            self.assertNotIn("temperature", request["body"])
            self.assertEqual(len(messages), 1)

    def test_plain_text_provider_and_echoed_secret_are_safe(self):
        with MockApiServer() as server:
            server.enqueue("普通回复 " + KEY)
            reply = ApiClient().complete(ApiSettings(server.base_url, "test-model"), KEY,
                                         [{"role": "user", "content": "我的密钥 " + KEY}])
            self.assertEqual(reply.emotion, "neutral")
            self.assertNotIn(KEY, reply.reply)
            self.assertIn("[已隐藏密钥]", reply.reply)
            self.assertNotIn(KEY, server.requests[0]["body"]["messages"][-1]["content"])

    def test_connection_test_really_requests_a_chat_completion(self):
        with MockApiServer() as server:
            server.enqueue("连接成功")
            status = ApiClient().test_connection(ApiSettings(server.base_url, "test-model"), KEY)
            self.assertIn("连接成功", status)
            self.assertEqual(len(server.requests), 1)
            self.assertEqual(server.requests[0]["path"], "/v1/chat/completions")

    def test_json_unicode_escaped_key_is_redacted_after_parsing(self):
        with MockApiServer() as server:
            escaped_key = "".join(f"\\u{ord(character):04x}" for character in KEY)
            server.enqueue('{"reply":"密钥回显 ' + escaped_key + '","emotion":"happy"}')
            reply = ApiClient().complete(ApiSettings(server.base_url, "test-model"), KEY,
                                         [{"role": "user", "content": "hi"}])
            self.assertNotIn(KEY, reply.reply)
            self.assertIn("[已隐藏密钥]", reply.reply)
            self.assertEqual(reply.emotion, "happy")

    def test_status_errors_are_classified_without_echoing_remote_body(self):
        cases = [(401, "authentication"), (403, "permission"), (404, "model_not_found"),
                 (429, "rate_limit"), (400, "provider"), (500, "provider")]
        with MockApiServer() as server:
            client = ApiClient()
            settings = ApiSettings(server.base_url, "test-model")
            for status, code in cases:
                with self.subTest(status=status):
                    server.enqueue("sensitive-remote-body " + KEY, status=status)
                    with self.assertRaises(ApiError) as caught:
                        client.complete(settings, KEY, [{"role": "user", "content": "hi"}])
                    self.assertEqual(caught.exception.code, code)
                    self.assertNotIn(KEY, str(caught.exception))
                    self.assertNotIn("sensitive-remote-body", repr(caught.exception))
            self.assertEqual(len(server.requests), len(cases))  # No automatic retries.

    def test_remote_error_traceback_and_debug_logs_do_not_expose_secret(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        root = logging.getLogger()
        root.addHandler(handler)
        # Simulate a developer enabling HTTP/SDK debug output before a request.
        logger_states = {}
        for name in ("openai", "openai._base_client", "httpx", "httpcore.connection"):
            logger = logging.getLogger(name)
            logger_states[name] = (logger.disabled, logger.level)
            logger.disabled = False
            logger.setLevel(logging.DEBUG)
        try:
            with MockApiServer() as server:
                server.enqueue("secret-provider-body " + KEY, status=401)
                with self.assertRaises(ApiError) as caught:
                    ApiClient().complete(ApiSettings(server.base_url, "test-model"), KEY,
                                         [{"role": "user", "content": "hi"}])
                formatted = "".join(traceback.format_exception(type(caught.exception), caught.exception,
                                                               caught.exception.__traceback__))
                self.assertNotIn(KEY, formatted + stream.getvalue())
                self.assertNotIn("secret-provider-body", formatted + stream.getvalue())
        finally:
            root.removeHandler(handler)
            for name, (disabled, level) in logger_states.items():
                logger = logging.getLogger(name)
                logger.disabled = disabled
                logger.setLevel(level)

    def test_empty_or_incompatible_response_is_actionable(self):
        with MockApiServer() as server:
            for body in ({"choices": []}, {"choices": [{"message": {"content": None}}]}):
                with self.subTest(body=body):
                    server.enqueue(body=body)
                    with self.assertRaises(ApiError) as caught:
                        ApiClient().complete(ApiSettings(server.base_url, "test-model"), KEY,
                                             [{"role": "user", "content": "hi"}])
                    self.assertIn(caught.exception.code, {"empty_response", "invalid_response"})

    def test_timeout_bounds_whole_request(self):
        with MockApiServer() as server:
            server.enqueue(delay=2)
            start = time.monotonic()
            with self.assertRaises(ApiError) as caught:
                ApiClient().complete(ApiSettings(server.base_url, "test-model", timeout_seconds=1), KEY,
                                     [{"role": "user", "content": "hi"}])
            self.assertEqual(caught.exception.code, "timeout")
            self.assertLess(time.monotonic() - start, 1.7)

    def test_connection_refused_is_network_error(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        with self.assertRaises(ApiError) as caught:
            ApiClient().complete(ApiSettings(f"http://127.0.0.1:{port}/v1", "test-model", 5), KEY,
                                 [{"role": "user", "content": "hi"}])
        self.assertEqual(caught.exception.code, "network")

    def test_missing_key_does_not_make_a_request(self):
        with MockApiServer() as server:
            with self.assertRaises(ApiError) as caught:
                ApiClient().complete(ApiSettings(server.base_url, "test-model"), "", [])
            self.assertEqual(caught.exception.code, "not_configured")
            self.assertFalse(server.requests)

    def test_shutdown_before_worker_registers_prevents_request(self):
        with MockApiServer() as server:
            client = ApiClient()
            client.shutdown()
            with self.assertRaises(ApiError) as caught:
                client.complete(ApiSettings(server.base_url, "test-model"), KEY,
                                [{"role": "user", "content": "hi"}])
            self.assertEqual(caught.exception.code, "cancelled")
            self.assertFalse(server.requests)


if __name__ == "__main__":
    unittest.main()
