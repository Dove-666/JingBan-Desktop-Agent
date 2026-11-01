"""Fixed GUI verdict diagnostics and neutral Qt/SQLite result presentation."""
from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent_backend/nanobot"))
from gui_verification import report, unconfirmed_reply, UNCONFIRMED_REPLY
from task_harness import TaskHarness
from tests import test_agent_chat_routing as fixtures
from desktop_pet.agents import AgentResult


class VerificationReasonTests(unittest.TestCase):
    def test_actual_unknown_result_does_not_claim_missed_click_or_handoff(self):
        text = unconfirmed_reply(report("unverified", reason="GOAL_NOT_OBSERVABLE"))
        self.assertIn("当前画面不足以确认", text)
        self.assertIn("操作是否生效仍待确认", text)
        self.assertNotIn("未达到要求", text)
        self.assertNotIn("接手", text)

    def test_failed_visible_state_is_distinct_from_unknown(self):
        failed = unconfirmed_reply(report("failed", reason="GOAL_STATE_NOT_MET"))
        unknown = unconfirmed_reply(report("unverified", reason="GOAL_NOT_OBSERVABLE"))
        self.assertNotEqual(failed, unknown)
        self.assertIn("目标状态仍未达到要求", failed)
        self.assertIn("任务未标记为完成", failed)

    def test_identity_and_order_missing_have_specific_scope(self):
        identity = unconfirmed_reply(report("unverified", reason="TARGET_IDENTITY_UNCONFIRMED"))
        order = unconfirmed_reply(report("unverified", reason="TEMPORAL_EVIDENCE_MISSING"))
        self.assertIn("你指定的对象", identity)
        self.assertIn("目标顺序", order)
        self.assertNotIn("未达到要求", identity + order)

    def test_service_format_capture_and_not_run_are_distinguished(self):
        reasons = {
            "VERIFICATION_SERVICE_UNAVAILABLE": "服务暂时不可用",
            "VERIFICATION_RESPONSE_INVALID": "没有得到有效答复",
            "VERIFICATION_CAPTURE_FAILED": "未能取得可用的新画面",
            "VERIFICATION_NOT_RUN": "尚未完成独立",
        }
        outputs = []
        for reason, expected in reasons.items():
            with self.subTest(reason=reason):
                text = unconfirmed_reply(report("unverified", reason=reason))
                self.assertIn(expected, text)
                self.assertNotIn("接手", text)
                outputs.append(text)
        self.assertEqual(len(set(outputs)), len(reasons))

    def test_untrusted_reason_and_extra_data_never_reach_public_reply(self):
        secret = "private-fake-key-never-show"
        values = [None, {}, True, secret,
                  report("unverified", reason=secret),
                  {**report("unverified", reason="GOAL_NOT_OBSERVABLE"), "details": secret},
                  {**report("unverified", reason="GOAL_NOT_OBSERVABLE"), "method": secret},
                  {**report("unverified", reason="GOAL_NOT_OBSERVABLE"), "checked_at": secret}]
        for value in values:
            with self.subTest(value=value):
                text = unconfirmed_reply(value)
                self.assertEqual(text, UNCONFIRMED_REPLY)
                self.assertNotIn(secret, text)

    def test_success_and_contradictory_reason_never_render_a_pass(self):
        for value in (report("verified", reason="GOAL_STATE_CONFIRMED"),
                      report("verified", reason="GOAL_NOT_OBSERVABLE"),
                      report("unverified", reason="GOAL_STATE_NOT_MET"),
                      report("failed", reason="VERIFICATION_SERVICE_UNAVAILABLE")):
            self.assertEqual(unconfirmed_reply(value), UNCONFIRMED_REPLY)

    def test_harness_retains_fixed_reason_and_removes_optimistic_model_prose(self):
        with tempfile.TemporaryDirectory() as directory:
            harness = TaskHarness(Path(directory))
            harness.begin_gui_action()
            harness.record_gui_verification(report("unverified", reason="GOAL_NOT_OBSERVABLE"))
            reply, status, assessment = harness.finalize("已完成，已经给第二个视频点赞。")
            self.assertEqual(status, "needs_user")
            self.assertFalse(assessment["passed"])
            self.assertIn("当前画面不足以确认", reply)
            self.assertNotIn("已经给第二个视频点赞", reply)
            self.assertNotIn("接手", reply)

    def test_harness_failure_reason_does_not_turn_into_generic_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            harness = TaskHarness(Path(directory))
            harness.begin_gui_action()
            harness.record_gui_verification(report("failed", reason="GOAL_STATE_NOT_MET"))
            reply, status, _ = harness.finalize("已完成")
            self.assertEqual(status, "needs_user")
            self.assertIn("目标状态仍未达到要求", reply)


class VerificationReplyPresentationTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.AgentChatRoutingTests.setUpClass.__func__)
    setUp = fixtures.AgentChatRoutingTests.setUp
    tearDown = fixtures.AgentChatRoutingTests.tearDown
    controller = fixtures.AgentChatRoutingTests.controller
    spin = fixtures.AgentChatRoutingTests.spin
    finish = fixtures.AgentChatRoutingTests.finish

    def configure_computer(self):
        settings = replace(self.settings.load_agent_settings(), computer_enabled=True, gui_model="fixture-gui")
        self.settings.save_agent_settings(settings)
        self.settings.save_agent_gui_api_key(fixtures.GUI_KEY)
        self.chat.refresh_agent_settings()

    def test_unknown_reply_is_saved_as_pending_without_invented_handoff(self):
        self.configure_computer()
        detail = unconfirmed_reply(report("unverified", reason="GOAL_NOT_OBSERVABLE"))
        self.agent.result = AgentResult(detail, kind="agent", status="needs_user")
        self.assertTrue(self.chat.send("打开测试应用并给第二项点赞", agent_mode=True))
        self.finish()
        saved = self.chat.store.load_messages(self.chat.current_conversation_id)[-1]
        self.assertTrue(saved["content"].startswith("任务尚未确认完成。\n"))
        self.assertIn(detail, saved["content"])
        self.assertNotIn("需要你接手", saved["content"])
        self.assertEqual(saved["metadata"]["execution"]["state"], "needs_user")
        self.assertEqual(len(self.agent.calls), 1)

    def test_real_manual_instruction_survives_neutral_prefix(self):
        self.configure_computer()
        detail = "登录页已打开，请在窗口中完成验证。"
        self.agent.result = AgentResult(detail, kind="agent", status="needs_user")
        self.assertTrue(self.chat.send("打开需要登录的测试页面", agent_mode=True))
        self.finish()
        saved = self.chat.store.load_messages(self.chat.current_conversation_id)[-1]
        self.assertIn(detail, saved["content"])
        self.assertNotIn("需要你接手完成当前步骤", saved["content"])
        self.assertEqual(saved["metadata"]["execution"]["state"], "needs_user")


if __name__ == "__main__":
    unittest.main()
