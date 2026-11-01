"""Contextual expression prompts and strict parsers; HTTP stays on localhost."""

import json
from types import SimpleNamespace
import unittest

from desktop_pet.api_client import ApiClient, ChatReply, EMOTIONS, SYSTEM_PROMPT, parse_reply
from desktop_pet.conversations.context import ContextAssembler, ContextBudget, estimate_tokens
from desktop_pet.emotion_policy import EMOTION_CONTEXT_PROMPT, normalize_emotion
from desktop_pet.expression_manager import EXPRESSIONS
from desktop_pet.perception.context_analyzer import (
    CAMERA_CONTEXT_PROMPT, CONTEXT_PROMPT, ContextAnalyzer, ContextEvent, parse_context,
)
from desktop_pet.settings_manager import ApiSettings
from tests.mock_api import MockApiServer
from tests.test_context_analyzer import observation, response


KEY = "synthetic-expression-api-token"


class ContextExpressionPolicyTests(unittest.TestCase):
    def test_policy_vocabulary_matches_real_expression_codes_and_shared_prompts(self):
        self.assertEqual(EMOTIONS, frozenset(EXPRESSIONS))
        for prompt in (SYSTEM_PROMPT, CONTEXT_PROMPT, CAMERA_CONTEXT_PROMPT):
            with self.subTest(prompt=prompt[:20]):
                self.assertEqual(prompt.count(EMOTION_CONTEXT_PROMPT), 1)
                for code in EMOTIONS:
                    self.assertIn(code, prompt)
                self.assertIn("不推断用户情绪", prompt)
                self.assertIn("不对低落用户生气", prompt)
                self.assertIn('"emotion":"neutral"', prompt)
                self.assertNotIn('"emotion":"happy"', prompt)

    def test_contextual_chat_prompt_fits_existing_default_system_budget(self):
        budget = ContextBudget()
        self.assertLessEqual(estimate_tokens(SYSTEM_PROMPT) + 12, budget.system_tokens)
        result = ContextAssembler(budget).assemble(
            system_prompt=SYSTEM_PROMPT, user_text="你好", recent_messages=[],
        )
        self.assertEqual(result.messages[0]["content"], SYSTEM_PROMPT)

    def test_all_seven_emotions_survive_chat_and_visual_parsing(self):
        for emotion in EMOTIONS:
            with self.subTest(emotion=emotion):
                raw = json.dumps({"reply": "根据语境回答", "emotion": emotion}, ensure_ascii=False)
                self.assertEqual(parse_reply(raw), ChatReply("根据语境回答", emotion))
                event = parse_context(response(emotion=emotion, express=True))
                self.assertEqual(event.emotion, emotion)
                self.assertTrue(event.express)

    def test_unknown_emotions_keep_reply_and_context_with_neutral_fallback(self):
        for invalid in ("sad", "run_shell", "", None, [], {}, True, 7):
            with self.subTest(emotion=invalid):
                raw = json.dumps({"reply": "可保留的正常回答", "emotion": invalid}, ensure_ascii=False)
                self.assertEqual(parse_reply(raw), ChatReply("可保留的正常回答", "neutral"))
                event = parse_context(response(emotion=invalid, express=True))
                self.assertEqual(event.kind, "achievement")
                self.assertEqual(event.summary, "编译完成")
                self.assertEqual(event.reply, "成功啦！")
                self.assertEqual(event.confidence, .9)
                self.assertEqual(event.emotion, "neutral")
                self.assertTrue(event.express)

    def test_normalization_accepts_only_canonical_codes_after_case_and_whitespace(self):
        self.assertEqual(normalize_emotion("  HAPPY \n"), "happy")
        self.assertEqual(normalize_emotion("Thinking"), "thinking")
        self.assertEqual(normalize_emotion("happy;run_shell"), "neutral")
        self.assertEqual(parse_reply('{"reply":"hello","emotion":"Surprised"}'),
                         ChatReply("hello", "surprised"))

    def test_plain_text_and_invalid_reply_structures_keep_compatible_fallback(self):
        self.assertEqual(parse_reply("  有点困了，准备休息。  "), ChatReply("有点困了，准备休息。", "neutral"))
        for raw in ('{"emotion":"happy"}', '{"reply":42,"emotion":"happy"}', '["text"]', "{bad json}"):
            with self.subTest(raw=raw):
                self.assertEqual(parse_reply(raw), ChatReply(raw, "neutral"))

    def test_express_defaults_false_for_legacy_json_and_positional_events(self):
        self.assertFalse(parse_context(response()).express)
        event = ContextEvent("interesting", "summary", "reply", "happy", .5, .9, "event-key")
        self.assertFalse(event.express)

    def test_expression_only_visual_event_can_be_valid_without_speech(self):
        event = parse_context(response(kind="interesting", reply="", emotion="surprised", express=True))
        self.assertEqual(event.kind, "interesting")
        self.assertEqual(event.reply, "")
        self.assertEqual(event.emotion, "surprised")
        self.assertTrue(event.express)

    def test_express_requires_a_json_boolean_not_truthy_coercion(self):
        for invalid in (None, 0, 1, "true", "false", [], {}):
            with self.subTest(express=invalid):
                self.assertEqual(parse_context(response(express=invalid)), ContextEvent())
        self.assertFalse(parse_context(response(express=False)).express)

    def test_expression_flag_does_not_relax_kind_score_or_text_validation(self):
        for changes in ({"kind": "execute"}, {"kind": []}, {"confidence": True},
                        {"confidence": -1}, {"importance": float("nan")},
                        {"importance": 2}, {"summary": {}}, {"reply": []}):
            with self.subTest(changes=changes):
                self.assertEqual(parse_context(response(express=True, **changes)), ContextEvent())

    def test_visual_prompts_describe_no_speech_and_unreliable_scene_behavior(self):
        for prompt in (CONTEXT_PROMPT, CAMERA_CONTEXT_PROMPT):
            with self.subTest(prompt=prompt[:20]):
                self.assertIn('"express":false', prompt)
                self.assertIn("reply为空", prompt)
                self.assertIn("express=true", prompt)
                self.assertIn("看不清、敏感或低置信度时必须express=false", prompt)
                self.assertIn("不授权发言或执行操作", prompt)


class ContextExpressionLocalApiTests(unittest.TestCase):
    def test_chat_uses_one_local_request_and_keeps_valid_reply_for_unknown_emotion(self):
        with MockApiServer() as server:
            server.enqueue('{"reply":"有效普通回答","emotion":"invented-emotion"}')
            client = ApiClient()
            try:
                reply = client.complete(ApiSettings(server.base_url, "synthetic-chat-model"), KEY,
                                        [{"role": "user", "content": "请回答"}])
                self.assertEqual(reply, ChatReply("有效普通回答", "neutral"))
                self.assertEqual(len(server.requests), 1)
                self.assertIn(EMOTION_CONTEXT_PROMPT, server.requests[0]["body"]["messages"][0]["content"])
                self.assertNotIn("response_format", server.requests[0]["body"])
            finally:
                client.shutdown()

    def test_screen_and_camera_share_expression_policy_without_additional_requests(self):
        with MockApiServer() as server:
            analyzer = ContextAnalyzer()
            try:
                screen = observation()
                camera = SimpleNamespace(info=screen.info, image=screen.image, blocked_reason="", source="camera")
                for index, visual in enumerate((screen, camera)):
                    with self.subTest(source=getattr(visual, "source", "screen")):
                        server.enqueue(response(kind="interesting", reply="", emotion="thinking", express=True))
                        event = analyzer.analyze(visual, ApiSettings(server.base_url, "synthetic-vision-model"), KEY)
                        self.assertEqual(event.emotion, "thinking")
                        self.assertEqual(event.reply, "")
                        self.assertTrue(event.express)
                        self.assertEqual(len(server.requests), index + 1)
                        body = server.requests[-1]["body"]
                        self.assertIn(EMOTION_CONTEXT_PROMPT, body["messages"][0]["content"])
                        self.assertIn("express=true", body["messages"][0]["content"])
                        self.assertEqual(body["messages"][1]["content"][1]["type"], "image_url")
                        self.assertNotIn("response_format", body)
            finally:
                analyzer.shutdown()


if __name__ == "__main__":
    unittest.main()
