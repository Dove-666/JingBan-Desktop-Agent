import io
import logging
import socket
import time
import traceback
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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

    def test_invalid_json_or_reply_falls_back_to_original_text(self):
        for text in (
            '{"reply":42,"emotion":"happy"}',
            '{"reply":" ","emotion":"happy"}',
            '["hello"]', '{not valid}',
            '__import__("os").system("do not execute")',
        ):
            self.assertEqual(parse_reply(text), ChatReply(text))

    def test_unknown_or_malformed_emotion_keeps_valid_reply_with_neutral(self):
        for text in ('{"reply":"你好","emotion":"run_shell"}',
                     '{"reply":"你好","emotion":["happy"]}'):
            self.assertEqual(parse_reply(text), ChatReply("你好", "neutral"))

    def test_missing_emotion_defaults_to_neutral(self):
        self.assertEqual(parse_reply('{"reply":"你好"}'), ChatReply("你好"))


class ApiClientTests(unittest.TestCase):
    def test_budgeted_context_sends_one_legacy_output_cap_to_compatible_server(self):
        with MockApiServer() as server:
            server.enqueue('{"reply":"使用预算后的回复。","emotion":"happy"}')
            messages = [{"role": "system", "content": "既有角色设定"},
                        {"role": "user", "content": "完整问题 " + KEY}]
            reply = ApiClient().complete_context(ApiSettings(server.base_url, "compatible-model"),
                                                  KEY, messages, output_tokens=256)
            self.assertEqual(reply, ChatReply("使用预算后的回复。", "happy"))
            self.assertEqual(len(server.requests), 1)
            body = server.requests[0]["body"]
            self.assertEqual(body["max_tokens"], 256)
            self.assertNotIn("max_completion_tokens", body)
            self.assertEqual(len(body["messages"]), len(messages))
            self.assertEqual(body["messages"][0], messages[0])
            self.assertNotIn(KEY, body["messages"][-1]["content"])
            self.assertIn(KEY, messages[-1]["content"])

    def test_known_reasoning_model_names_use_completion_cap_in_single_real_http_request(self):
        with MockApiServer() as server:
            cases = (("o1-preview", "max_completion_tokens"),
                     ("o3-mini", "max_completion_tokens"),
                     ("openai/o4-mini", "max_completion_tokens"),
                     ("gpt-5.4", "max_completion_tokens"),
                     ("openai/gpt-6-astra", "max_completion_tokens"),
                     ("qwen3-thinking", "max_tokens"),
                     ("o10-custom", "max_tokens"),
                     ("gpt-50-custom", "max_tokens"))
            for index, (model, parameter) in enumerate(cases):
                with self.subTest(model=model):
                    server.enqueue("预算参数检查")
                    ApiClient().complete_context(ApiSettings(server.base_url, model), KEY,
                        [{"role": "system", "content": "测试角色"}, {"role": "user", "content": "你好"}],
                        output_tokens=192)
                    self.assertEqual(len(server.requests), index + 1)
                    body = server.requests[-1]["body"]
                    self.assertEqual(body[parameter], 192)
                    self.assertNotIn("max_tokens" if parameter == "max_completion_tokens"
                                     else "max_completion_tokens", body)
                    self.assertNotIn("reasoning_effort", body)

    def test_official_endpoint_uses_exact_hostname_not_lookalike_suffix(self):
        for base_url, parameter in (
                ("https://api.openai.com/v1", "max_completion_tokens"),
                ("https://API.OPENAI.COM:443/v1", "max_completion_tokens"),
                ("https://api.openai.com.evil.example/v1", "max_tokens"),
                ("https://not-api.openai.com/v1", "max_tokens"),
                ("https://compatible.example/api.openai.com/v1", "max_tokens")):
            with self.subTest(base_url=base_url):
                create = AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(
                    message=SimpleNamespace(content="stub response"), finish_reason="stop")]))
                sdk = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
                with patch("desktop_pet.api_client.AsyncOpenAI") as constructor:
                    constructor.return_value.__aenter__ = AsyncMock(return_value=sdk)
                    constructor.return_value.__aexit__ = AsyncMock(return_value=False)
                    ApiClient().complete_context(ApiSettings(base_url, "deployment-alias"), KEY,
                        [{"role": "user", "content": "测试"}], output_tokens=64)
                create.assert_awaited_once()
                arguments = create.await_args.kwargs
                self.assertEqual(arguments[parameter], 64)
                self.assertNotIn("max_tokens" if parameter == "max_completion_tokens"
                                 else "max_completion_tokens", arguments)

    def test_rejected_output_limit_never_retries_or_sends_second_billable_completion(self):
        with MockApiServer() as server:
            server.enqueue("provider rejects parameter " + KEY, status=400)
            with self.assertRaises(ApiError) as caught:
                ApiClient().complete_context(ApiSettings(server.base_url, "o3-mini"), KEY,
                    [{"role": "user", "content": "测试"}], output_tokens=128)
            self.assertEqual(caught.exception.code, "provider")
            self.assertNotIn(KEY, str(caught.exception))
            self.assertEqual(len(server.requests), 1)
            self.assertTrue(server.plans.empty())

    def test_reasoning_budget_exhaustion_without_text_has_actionable_error(self):
        with MockApiServer() as server:
            server.enqueue(body={"id": "test-limit", "object": "chat.completion", "created": 0,
                "model": "o3-mini", "choices": [{"index": 0, "finish_reason": "length",
                    "message": {"role": "assistant", "content": ""}}]})
            with self.assertRaises(ApiError) as caught:
                ApiClient().complete_context(ApiSettings(server.base_url, "o3-mini"), KEY,
                    [{"role": "user", "content": "测试"}], output_tokens=128)
            self.assertEqual(caught.exception.code, "output_limit")
            self.assertIn("提高输出预留", str(caught.exception))
            self.assertEqual(len(server.requests), 1)

    def test_invalid_transport_output_budget_is_rejected_before_network(self):
        with MockApiServer() as server:
            for budget in (True, 0, 31, 64.5, "128"):
                with self.subTest(budget=budget):
                    with self.assertRaises(ApiError) as caught:
                        ApiClient().request_text(ApiSettings(server.base_url, "test-model"), KEY,
                            [{"role": "user", "content": "测试"}], output_tokens=budget)
                    self.assertEqual(caught.exception.code, "invalid_messages")
            self.assertFalse(server.requests)

    def test_unbudgeted_legacy_sdk_stub_receives_no_new_keyword_arguments(self):
        class LegacySdk:
            def __init__(self, **kwargs):
                self.chat = SimpleNamespace(completions=self)
                self.calls = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def create(self, *, model, messages):
                self.calls.append((model, messages))
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="旧适配正常"))])

        with patch("desktop_pet.api_client.AsyncOpenAI", LegacySdk):
            reply = ApiClient().complete(ApiSettings("http://localhost:9876/v1", "legacy-model"), KEY,
                                         [{"role": "user", "content": "你好"}])
        self.assertEqual(reply.reply, "旧适配正常")

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
