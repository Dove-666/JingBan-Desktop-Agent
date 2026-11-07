"""Host recovery of premature answers using the real pinned worker and local model."""
from pathlib import Path
import asyncio
from dataclasses import dataclass
import json
import sys
import tempfile
import unittest

BACKEND = Path(__file__).resolve().parents[1] / "agent_backend/nanobot"
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND / "tests"))
from completion_recovery import CompletionRepair, with_completion_recovery
from task_harness import TaskHarness
from action_intent import requires_visible_desktop
from activity_subject import operation_subject, subject_detail
import test_harness_worker as worker_fixtures

tool = worker_fixtures.tool
KEY = worker_fixtures.KEY


class CompletionRecoveryTests(unittest.TestCase):
    def test_temporary_browser_is_not_a_visible_desktop_deliverable(self):
        for task in ("帮我打开两个视频并播放", "点击我当前浏览器页面的点赞按钮", "打开抖音并给主页第二个视频点赞", "Play the first video",
                     "Open my current browser tab"):
            with self.subTest(task=task):
                self.assertTrue(requires_visible_desktop(task))
        for task in ("查找两个视频并总结内容", "解释为什么打开视频失败", "不要打开视频，打开日志文件",
                     "Research two videos and cite their URLs"):
            with self.subTest(task=task):
                self.assertFalse(requires_visible_desktop(task))

    def test_exploratory_browser_and_desktop_reads_do_not_poison_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            harness = TaskHarness(Path(directory))
            for name in ("browser_observe", "browser_tabs", "desktop_windows", "desktop_observe",
                         "inspect_image", "inspect_document_page"):
                harness.record_failure(name, {"query": "absent candidate"})
            self.assertFalse(harness.failed_tools)
            self.assertNotIn("UNRESOLVED_TOOL_FAILURE", harness.assess()["issues"])
            harness.record_failure("browser_observe", {"query": "required target"}, classification="required")
            self.assertIn("UNRESOLVED_TOOL_FAILURE", harness.assess()["issues"])

    def test_same_failed_check_is_bounded_but_new_work_has_no_iteration_limit(self):
        repair = CompletionRepair()
        state = {"passed": False, "issues": ["PLAN_UNFINISHED"],
                 "plan": [{"content": "Read actual fixture", "status": "in_progress"}]}
        self.assertIn("never replay", repair.feedback(state))
        self.assertIsNotNone(repair.feedback(state))
        self.assertIsNone(repair.feedback(state))
        for index in range(140):
            state["plan"][0]["content"] = f"Process actual page {index}"
            self.assertIsNotNone(repair.feedback(state))

    def test_service_unknown_and_pass_do_not_cause_blind_planner_retries(self):
        repair = CompletionRepair()
        self.assertIsNone(repair.feedback({"passed": True}))
        self.assertIsNone(repair.feedback({"passed": False, "issues": ["GOAL_UNVERIFIED"]}))
        note = repair.feedback({"passed": False, "issues": ["UNRESOLVED_TOOL_FAILURE"]}, uncertain=True)
        self.assertIn("only observe/read back first", note)

    def test_failed_result_replaces_optimistic_draft_without_generic_handoff(self):
        with tempfile.TemporaryDirectory() as directory:
            harness = TaskHarness(Path(directory))
            harness.update_plan([{"content": "Save the requested document", "status": "pending"}])
            reply, state, _ = harness.finalize("Everything was completed perfectly")
            self.assertEqual(state, "needs_user")
            self.assertIn("Save the requested document", reply)
            self.assertNotIn("Everything", reply)
            self.assertNotIn("接手", reply)
            self.assertNotIn("尚未", reply)

    def test_judge_outage_reports_a_specific_cause(self):
        with tempfile.TemporaryDirectory() as directory:
            harness = TaskHarness(Path(directory))
            harness.goal_diagnostic = "GOAL_JUDGE_PROVIDER_CONNECTION"
            self.assertEqual(harness.failure_reply({"issues": []}), "未完成：结果检查服务连接失败。")

    def test_adapter_keeps_the_existing_runner_and_native_continuation(self):
        @dataclass
        class Spec:
            continuation_callback: object = None

        class Runner:
            async def _try_drain_injections(self, spec, messages, assistant_message, cycles, **kwargs):
                note = await spec.continuation_callback()
                if note:
                    messages.extend([assistant_message, {"role": "user", "content": note}])
                return bool(note), cycles

        calls = []

        async def checked(answer):
            calls.append(answer)
            return "Check the missing postcondition"

        base = Runner()
        base.existing_state = object()
        runner = with_completion_recovery(base, checked)
        messages = []
        result = asyncio.run(runner._try_drain_injections(
            Spec(), messages, {"role": "assistant", "content": "Premature draft"}, 0,
            allow_continuation=True))
        self.assertTrue(result[0])
        self.assertIs(runner.existing_state, base.existing_state)
        self.assertEqual(calls, ["Premature draft"])
        self.assertEqual([m["role"] for m in messages], ["assistant", "user"])


class CompletionWorkerTests(unittest.TestCase):
    setUp = worker_fixtures.HarnessWorkerTests.setUp
    tearDown = worker_fixtures.HarnessWorkerTests.tearDown
    start = worker_fixtures.HarnessWorkerTests.start

    def test_visible_playback_cannot_pass_with_only_background_tool_evidence(self):
        client, _ = self.start([tool("calculate", expression="1+1")] + ["Both videos are playing."] * 3,
                               computer=False, task="打开两个视频并播放")
        result = client.terminal()
        self.assertEqual(result["type"], "final", result)
        self.assertEqual(result["status"], "needs_user", result)
        self.assertFalse(result["gui_goal_verified"])
        self.assertNotIn("Both videos are playing", result["reply"])

    def test_premature_final_continues_same_task_and_does_not_replay_the_read(self):
        (self.workspace / "actual.txt").write_text("actual owned content", encoding="utf8")

        def repair_plan(body):
            transcript = json.dumps(body["messages"], ensure_ascii=False)
            self.assertIn("HOST OUTCOME CHECK", transcript)
            self.assertIn("actual owned content", transcript)
            return tool("update_plan", steps=[{"content": "Read actual.txt", "status": "completed"}])

        client, model = self.start([
            tool("update_plan", steps=[{"content": "Read actual.txt", "status": "in_progress"}]),
            tool("read_file", path="actual.txt"), "I have read the file.",
            repair_plan, "I have read the file."],
            steps_limit=0, task="Read actual.txt and report its content.")
        result = client.terminal()
        self.assertEqual(result["type"], "final", result)
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(len(model.calls), 5)
        reads = [f for f in client.all_frames if f.get("type") == "activity"
                 and f.get("name") == "read_file" and f.get("status") == "running"]
        self.assertEqual(len(reads), 1)
        self.assertNotIn(KEY, json.dumps(client.all_frames))


class ActivitySubjectTests(unittest.TestCase):
    def test_subjects_show_the_file_or_site_without_private_path_or_url_parts(self):
        subject = operation_subject("read_file", {"path": "C:/private/account/需求说明.docx"})
        self.assertEqual(subject_detail("read_file", subject), "文件：需求说明.docx")
        subject = operation_subject("browser_navigate", {"url": "https://user:secret@example.com/watch?token=private#fragment"})
        self.assertEqual(subject_detail("browser_navigate", subject), "网站：example.com")
        self.assertNotIn("private", json.dumps(subject))

    def test_arbitrary_detail_control_characters_and_wrong_tool_subjects_are_dropped(self):
        for value in ({"kind": "file", "name": "C:/secret.txt"}, {"kind": "file", "name": "bad\nname"},
                      {"kind": "file", "name": "data:image/png"}, {"kind": "file", "name": "x", "reasoning": "private"}):
            self.assertIsNone(subject_detail("read_file", value))
        self.assertIsNone(subject_detail("computer_use", {"kind": "file", "name": "fake"}))
        self.assertIsNone(operation_subject("calculate", {"expression": "private query"}))

    def test_plan_subject_has_only_the_actual_step_count(self):
        value = operation_subject("update_plan", {"steps": [{"content": "private raw text"}]})
        self.assertEqual(value, {"kind": "plan", "count": 1})
        self.assertEqual(subject_detail("update_plan", value), "1 项步骤")


if __name__ == "__main__":
    unittest.main()
