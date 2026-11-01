"""Completion evidence cannot be created by plan text or a dispatched click."""
from datetime import timedelta
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent_backend/nanobot"))
from gui_verification import checked_report, report, result_fields, utc_now, UNCONFIRMED_REPLY, native_launch_report
from task_harness import TaskHarness


class GoalGateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.harness = TaskHarness(Path(self.directory.name))

    def test_click_and_completed_checklist_do_not_verify_goal(self):
        self.harness.begin_gui_action()
        self.harness.record_success("computer_use", {}, '{"status":"completed"}')
        self.harness.update_plan([{"content": "Click like", "status": "completed"}])
        result = json.loads(self.harness.finish_task("completed", "Everything succeeded"))
        self.assertFalse(result["accepted"])
        self.assertIn("GUI_GOAL_UNVERIFIED", result["issues"])
        reply, status, _ = self.harness.finalize("I liked the video successfully")
        self.assertEqual(status, "needs_user")
        self.assertEqual(reply, UNCONFIRMED_REPLY)

    def test_failed_or_unknown_judge_cannot_be_overridden(self):
        for status in ("failed", "unverified"):
            self.harness.begin_gui_action()
            self.harness.record_gui_verification(report(status))
            self.assertFalse(json.loads(self.harness.finish_task("completed"))["accepted"])

    def test_success_is_invalidated_by_later_action(self):
        self.harness.begin_gui_action()
        self.harness.record_gui_verification(report("verified"))
        self.assertTrue(json.loads(self.harness.finish_task("completed"))["accepted"])
        self.harness.begin_gui_action()
        self.assertFalse(self.harness.gui_goal_verified)
        self.assertFalse(json.loads(self.harness.finish_task("completed"))["accepted"])
        self.harness.record_gui_verification(report("verified"))
        self.assertTrue(self.harness.gui_goal_verified)

    def test_local_property_pass_does_not_mean_entire_goal(self):
        self.harness.begin_gui_action()
        self.harness.record_success("desktop_verify_state", {}, '{"passed":true}')
        self.assertFalse(self.harness.assess()["passed"])

    def test_no_gui_normal_tool_work_is_unchanged(self):
        self.assertTrue(self.harness.assess()["passed"])
        self.assertEqual(self.harness.finalize("An ordinary answer")[1], "completed")
        self.assertEqual(result_fields(False, None), {"gui_used": False, "gui_goal_verified": False, "verification": None})

    def test_forged_coerced_or_unscoped_reports_are_rejected(self):
        for value in (True, {}, {**report(), "passed": True}, {**report(), "status": True},
                      {**report(), "method": "model_says_done"}, {**report(), "checked_at": "2026-11-01"}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                checked_report(value)

    def test_old_and_future_reports_are_rejected(self):
        now = utc_now()
        for date in (now - timedelta(hours=1), now + timedelta(minutes=5)):
            with self.subTest(date=date), self.assertRaises(ValueError):
                checked_report({**report("verified"), "checked_at": date.isoformat()}, started_at=now, now=now)

    def test_cancelled_and_pending_results_never_set_verified_flag(self):
        for status in ("needs_user", "cancelled", "timed_out"):
            self.assertFalse(result_fields(True, report("verified"), status=status)["gui_goal_verified"])

    def test_native_wrong_target_or_partial_compound_goal_is_not_verified(self):
        self.assertIsNone(native_launch_report("打开抖音", "Calculator", True))
        self.assertIsNone(native_launch_report("打开抖音并给第一个视频点赞", "抖音", True))
        self.assertIsNone(native_launch_report("打开抖音", "抖音", "true"))
        self.assertIsNone(native_launch_report("打开抖音", "抖音", False))
        self.assertEqual(native_launch_report("打开抖音", "抖音", True)["status"], "verified")


if __name__ == "__main__":
    unittest.main()
