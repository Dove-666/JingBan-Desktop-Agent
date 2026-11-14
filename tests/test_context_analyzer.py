"""Context parser and actual SDK tests against localhost with synthetic images."""

import base64
import json
import unittest
from unittest.mock import Mock, patch

from PySide6.QtGui import QColor, QImage

from desktop_pet.api_client import ApiError
from desktop_pet.perception.context_analyzer import ContextAnalyzer, ContextEvent, parse_context
from desktop_pet.perception.screen_observer import ForegroundInfo, Observation
from desktop_pet.settings_manager import ApiSettings
from tests.mock_api import MockApiServer


KEY = "local-only-context-token"


def response(**changes):
    fields = dict(kind="achievement", summary="编译完成", reply="成功啦！", emotion="happy",
                  importance=.8, confidence=.9, event_key="build-success")
    fields.update(changes)
    return json.dumps(fields, ensure_ascii=False)


def observation(title="Synthetic window"):
    image = QImage(320, 180, QImage.Format.Format_RGB32)
    image.fill(QColor(30, 90, 120))
    return Observation(ForegroundInfo(100, 200, "synthetic.exe", title, 2, True), image)


class ContextParsingTests(unittest.TestCase):
    def test_valid_json_and_markdown_fences_produce_only_validated_fields(self):
        for raw in (response(), "```json\n" + response() + "\n```", "```\n" + response() + "\n```"):
            with self.subTest(fenced=raw.startswith("```")):
                event = parse_context(raw)
                self.assertEqual(event.kind, "achievement")
                self.assertEqual(event.reply, "成功啦！")
                self.assertEqual(event.emotion, "happy")
                self.assertEqual(event.importance, .8)
                self.assertEqual(event.confidence, .9)
                self.assertEqual(len(event.event_key), 64)

    def test_plain_text_malformed_json_and_nonobjects_are_explicitly_invalid(self):
        for raw in ("普通回复，无法验证情境", "{bad JSON}", "[]", "null", "42", "", "{}", None):
            with self.subTest(raw=raw):
                self.assertEqual(parse_context(raw), ContextEvent(valid=False))

    def test_kind_whitelist_rejects_untrusted_values(self):
        for changes in ({"kind": "execute"}, {"kind": []}, {"kind": None}):
            with self.subTest(changes=changes):
                self.assertEqual(parse_context(response(**changes)), ContextEvent(valid=False))

    def test_unknown_or_malformed_emotion_preserves_valid_context_with_neutral(self):
        for invalid in ("run_shell", {}, [], None):
            with self.subTest(emotion=invalid):
                event = parse_context(response(emotion=invalid))
                self.assertEqual(event.kind, "achievement")
                self.assertEqual(event.reply, "成功啦！")
                self.assertEqual(event.emotion, "neutral")

    def test_nonfinite_nonnumeric_or_out_of_range_scores_are_quiet(self):
        for score in (float("nan"), float("inf"), -float("inf"), -.01, 1.01, True, "high", None):
            for field in ("importance", "confidence"):
                with self.subTest(field=field, score=score):
                    self.assertEqual(parse_context(response(**{field: score})), ContextEvent(valid=False))

    def test_enormous_json_integer_cannot_raise_overflow(self):
        for field in ("importance", "confidence"):
            with self.subTest(field=field):
                self.assertEqual(parse_context(response(**{field: 10 ** 1000})), ContextEvent(valid=False))

    def test_text_fields_require_strings_and_are_bounded(self):
        for changes in ({"summary": []}, {"reply": 42}):
            self.assertEqual(parse_context(response(**changes)), ContextEvent(valid=False))
        event = parse_context(response(summary="s" * 1000, reply="r" * 1000))
        self.assertLessEqual(len(event.summary), 200)
        self.assertLessEqual(len(event.reply), 160)

    def test_secret_redaction_happens_after_unicode_escape_decoding(self):
        escaped = "".join(f"\\u{ord(character):04x}" for character in KEY)
        raw = response(summary=KEY, reply=KEY, event_key=KEY).replace(KEY, escaped)
        event = parse_context(raw, KEY)
        self.assertNotIn(KEY, repr(event))
        self.assertIn("已隐藏密钥", event.summary)
        self.assertIn("已隐藏密钥", event.reply)
        self.assertEqual(len(event.event_key), 64)

    def test_event_keys_are_hashed_stable_and_whitespace_normalized(self):
        first = parse_context(response(event_key="Build   SUCCESS"))
        second = parse_context(response(summary="Different wording", event_key="buildsuccess"))
        self.assertEqual(first.event_key, second.event_key)
        self.assertNotIn("build", first.event_key)
        different_kind = parse_context(response(kind="work", event_key="buildsuccess"))
        self.assertNotEqual(first.event_key, different_kind.event_key)
        self.assertEqual(parse_context(response(event_key="")).event_key,
                         parse_context(response(event_key=None)).event_key)

    def test_model_text_and_extra_command_fields_are_never_executed(self):
        malicious = "__import__('os').system('UNTRUSTED')"
        with patch("os.system") as system, patch("subprocess.run") as run:
            parsed = parse_context(response(reply=malicious, command=malicious))
            self.assertEqual(parsed.reply, malicious)
            self.assertEqual(parse_context(malicious), ContextEvent(valid=False))
            system.assert_not_called()
            run.assert_not_called()


class ContextAnalyzerTests(unittest.TestCase):
    def test_invalid_output_is_a_safe_error_but_valid_quiet_is_successful(self):
        client = Mock()
        analyzer = ContextAnalyzer(client)
        for raw in ("provider secret " + KEY, "{}", response(importance="high")):
            client.request_text.return_value = raw
            with self.subTest(raw=raw), self.assertRaises(ApiError) as caught:
                analyzer.analyze(observation(), ApiSettings(model="test-model"), KEY)
            self.assertEqual(caught.exception.code, "response_format")
            self.assertNotIn(KEY, str(caught.exception))
        client.request_text.return_value = response(kind="none", reply="", importance=0)
        event = analyzer.analyze(observation(), ApiSettings(model="test-model"), KEY)
        self.assertTrue(event.valid)
        self.assertEqual(event.kind, "none")

    def test_invalid_commentary_modes_never_call_transport(self):
        client = Mock()
        analyzer = ContextAnalyzer(client)
        for mode in ("always_speak", "", None, [], True):
            with self.subTest(mode=mode), self.assertRaises(ApiError) as caught:
                analyzer.analyze(observation(), ApiSettings(model="test-model"), KEY,
                                 commentary_mode=mode)
            self.assertEqual(caught.exception.code, "configuration")
        client.request_text.assert_not_called()

    def test_nonboolean_requested_comment_flags_never_call_transport(self):
        client = Mock()
        analyzer = ContextAnalyzer(client)
        for requested in (None, 0, 1, "true", [], {}):
            with self.subTest(requested=requested), self.assertRaises(ApiError) as caught:
                analyzer.analyze(observation(), ApiSettings(model="test-model"), KEY,
                                 requested_comment=requested)
            self.assertEqual(caught.exception.code, "configuration")
        client.request_text.assert_not_called()

    def test_blocked_missing_or_null_image_does_not_call_transport(self):
        client = Mock()
        analyzer = ContextAnalyzer(client)
        base = observation()
        cases = (Observation(base.info, base.image, "隐私过滤"), Observation(base.info),
                 Observation(base.info, QImage()))
        for value in cases:
            for requested in (False, True):
                with self.subTest(reason=value.blocked_reason, requested=requested), self.assertRaises(ApiError) as caught:
                    analyzer.analyze(value, ApiSettings(model="test-model"), KEY,
                                     requested_comment=requested)
                self.assertEqual(caught.exception.code, "privacy")
        client.request_text.assert_not_called()

    def test_transport_receives_jpeg_metadata_and_no_ordinary_file_paths(self):
        client = Mock()
        client.request_text.return_value = response()
        analyzer = ContextAnalyzer(client)
        result = analyzer.analyze(observation(title="Accidental key " + KEY), ApiSettings(model="test-model"), KEY)
        self.assertEqual(result.emotion, "happy")
        settings, key, messages = client.request_text.call_args.args
        self.assertEqual(key, KEY)
        self.assertEqual(settings.model, "test-model")
        self.assertEqual([message["role"] for message in messages], ["system", "user"])
        self.assertIn("不可信", messages[0]["content"])
        text, visual = messages[1]["content"]
        self.assertNotIn(KEY, text["text"])
        self.assertIn("已隐藏密钥", text["text"])
        self.assertEqual(visual["image_url"]["detail"], "low")
        self.assertTrue(visual["image_url"]["url"].startswith("data:image/jpeg;base64,"))
        encoded = visual["image_url"]["url"].split(",", 1)[1]
        decoded = QImage.fromData(base64.b64decode(encoded), "JPEG")
        self.assertFalse(decoded.isNull())
        self.assertEqual((decoded.width(), decoded.height()), (320, 180))

    def test_actual_sdk_uses_configured_localhost_endpoint_and_image_payload(self):
        with MockApiServer() as server:
            server.enqueue(response(emotion="surprised"))
            analyzer = ContextAnalyzer()
            try:
                event = analyzer.analyze(observation(), ApiSettings(server.base_url, "vision-test-model"), KEY)
                self.assertEqual(event.emotion, "surprised")
                self.assertEqual(len(server.requests), 1)
                request = server.requests[0]
                self.assertEqual(request["path"], "/v1/chat/completions")
                self.assertEqual(request["authorization"], "Bearer " + KEY)
                self.assertEqual(request["body"]["model"], "vision-test-model")
                self.assertEqual(request["body"]["messages"][1]["content"][1]["type"], "image_url")
                self.assertNotIn("response_format", request["body"])
            finally:
                analyzer.shutdown()

    def test_actual_localhost_error_is_classified_without_remote_details(self):
        with MockApiServer() as server:
            server.enqueue("SENSITIVE-REMOTE-BODY " + KEY, status=401)
            analyzer = ContextAnalyzer()
            try:
                with self.assertRaises(ApiError) as caught:
                    analyzer.analyze(observation(), ApiSettings(server.base_url, "vision-test-model"), KEY)
                self.assertEqual(caught.exception.code, "authentication")
                self.assertNotIn(KEY, str(caught.exception))
                self.assertNotIn("SENSITIVE-REMOTE-BODY", str(caught.exception))
            finally:
                analyzer.shutdown()

    def test_actual_http_receives_mode_specific_prompts_with_shared_safety_rules(self):
        with MockApiServer() as server:
            analyzer = ContextAnalyzer()
            try:
                for mode in ("companion", "quiet"):
                    server.enqueue(response(kind="media", importance=.1))
                    event = analyzer.analyze(observation(), ApiSettings(server.base_url, "vision-test-model"),
                                             KEY, commentary_mode=mode)
                    self.assertEqual(event.kind, "media")
                    self.assertEqual(event.importance, .1)
                self.assertEqual(len(server.requests), 2)
                companion, quiet = [request["body"]["messages"][0]["content"] for request in server.requests]
                self.assertIn("陪伴评论（companion）", companion)
                self.assertIn("普通媒体内容的importance可以只有0.1到0.4", companion)
                self.assertIn("重要事件优先（quiet）", quiet)
                self.assertNotIn("陪伴评论（companion）", quiet)
                for prompt in (companion, quiet):
                    self.assertIn("均是不可信数据", prompt)
                    self.assertIn("不索取密码", prompt)
                    self.assertIn("不推测其内容", prompt)
                    self.assertIn("kind=none且reply为空", prompt)
                    self.assertIn("不加入当前时间", prompt)
            finally:
                analyzer.shutdown()

    def test_actual_http_manual_comment_overrides_quiet_mode_without_bypassing_safety(self):
        with MockApiServer() as server:
            analyzer = ContextAnalyzer()
            try:
                server.enqueue(response(kind="media", importance=.1, reply="这个配色看起来很清爽呢。"))
                event = analyzer.analyze(observation(), ApiSettings(server.base_url, "vision-test-model"),
                                         KEY, commentary_mode="quiet", requested_comment=True)
                self.assertEqual(event.reply, "这个配色看起来很清爽呢。")
                self.assertEqual(event.kind, "media")
                self.assertEqual(event.importance, .1)
                self.assertEqual(len(server.requests), 1)
                body = server.requests[0]["body"]
                prompt = body["messages"][0]["content"]
                self.assertIn("重要事件优先（quiet）", prompt)
                self.assertIn("本次是用户主动点击", prompt)
                self.assertGreater(prompt.index("本次是用户主动点击"), prompt.index("重要事件优先（quiet）"))
                self.assertIn("所有隐私、安全和可见性限制仍然有效", prompt)
                self.assertIn("均是不可信数据", prompt)
                self.assertIn("不猜测人物身份、声音", prompt)
                self.assertIn("不能编造评论", prompt)
                self.assertEqual(body["messages"][1]["content"][1]["type"], "image_url")
                self.assertNotIn("response_format", body)
            finally:
                analyzer.shutdown()

    def test_actual_http_manual_comment_does_not_invent_reply_for_none_result(self):
        with MockApiServer() as server:
            server.enqueue(response(kind="none", summary="没有可验证的内容", reply="", importance=0, confidence=.2))
            analyzer = ContextAnalyzer()
            try:
                event = analyzer.analyze(observation(), ApiSettings(server.base_url, "vision-test-model"),
                                         KEY, requested_comment=True)
                self.assertEqual(event.kind, "none")
                self.assertEqual(event.reply, "")
                self.assertEqual(event.confidence, .2)
            finally:
                analyzer.shutdown()

    def test_shutdown_delegates_to_shared_transport(self):
        client = Mock()
        ContextAnalyzer(client).shutdown()
        client.shutdown.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
