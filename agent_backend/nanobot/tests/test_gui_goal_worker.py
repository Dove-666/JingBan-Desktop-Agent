"""Real nanobot SDK + loopback model + controlled executor/judge fixtures.

Only executor boundaries are replaced; registry, harness and terminal protocol
run unchanged in an isolated child. No model accounts or desktop inputs.
"""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parents[1]
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND / "tests"))
from test_harness_worker import ScriptedModel, tool
from test_worker import Client, PYTHON


FIXTURE = r'''
import asyncio, json, sys
sys.path.insert(0, BACKEND_PATH)
import worker
import desktop_controls
import windows_apps
from gui_verification import report
STATE = {"checked": False, "actions": 0}
class Controls:
    def invalidate(self): pass
    async def observe(self):
        return json.dumps({"status":"observed","controls":[{"ref":"fixture-ref","name":"Example checkbox","state":{"checked":STATE["checked"]}}]})
    async def act(self, ref, action, value=""):
        STATE["actions"] += 1
        if action == "check": STATE["checked"] = True
        if action == "uncheck": STATE["checked"] = False
        return json.dumps({"status":"executed","postcondition":{"passed": None if action=="invoke" else True},"task_verified":False})
    async def verify(self, ref, property, expected):
        return json.dumps({"passed":STATE.get(property)==expected,"task_verified":False})
class Bridge:
    def __init__(self, config, request_id, options, emit, **kwargs):
        self.config, self.options = config, options
        self.called, self.failed, self.failure_code = False, False, None
        self.last_status, self.verification = "completed", None
        self.cached = None
    async def close(self): pass
    def record_failure(self, code): self.failed, self.failure_code = True, code
    async def run(self, task):
        if self.cached is not None: return self.cached
        self.called = True
        passed = self.options.get("verify_only") is True and STATE["checked"]
        self.last_status = "completed" if passed else "needs_user"
        self.verification = report("verified" if passed else "failed")
        self.cached = json.dumps({"status":self.last_status,"summary":"fixture observed result","verification":self.verification,"steps":1})
        return self.cached
class UncertainLaunch:
    def __init__(self, *args, **kwargs):
        self.called, self.last_result = False, None
    async def run(self, **kwargs):
        self.called = True
        self.last_result = {"status":"needs_user","verified":False,"dispatched":True,"summary":"fixture unknown launch"}
        return json.dumps(self.last_result)
worker.GuiBridge = Bridge
desktop_controls.DesktopControls = Controls
windows_apps.AuthorizedApplicationLaunch = UncertainLaunch
asyncio.run(worker.main())
'''


@unittest.skipUnless(PYTHON.is_file() and os.name == "nt", "Windows backend environment required")
class GuiGoalWorkerTests(unittest.TestCase):
    def run_case(self, steps, *, task="将当前窗口里的 Example checkbox 设为勾选，并确认勾选状态。"):
        root = REPO / "agent_workspace"
        root.mkdir(exist_ok=True)
        workspace = Path(tempfile.mkdtemp(prefix="gui-goal-offline-", dir=root))
        model = ScriptedModel(steps)
        client = None
        try:
            script = workspace / "fixture_worker.py"
            script.write_text(FIXTURE.replace("BACKEND_PATH", repr(str(BACKEND))), encoding="utf-8")
            client = Client(worker_path=script)
            client.send({"type": "run", "id": "gui-goal-fixture", "kind": "agent",
                "task": task,
                "config": {"base_url": model.base_url, "api_key": "loopback-fixture-only", "model": "fixture-planner",
                    "gui": {"base_url": model.base_url, "api_key": "loopback-fixture-only", "model": "fixture-gui"}},
                "options": {"workspace": str(workspace), "max_steps": 20, "timeout_seconds": 25,
                    "tools_enabled": True, "computer_enabled": True, "include_activity": True}})
            result = client.terminal()
            self.assertEqual(result["type"], "final", result)
            self.assertEqual(model.errors, [])
            self.assertEqual(len(model.calls), len(steps))
            return result, list(model.calls)
        finally:
            if client is not None:
                self.assertEqual(client.close(), "")
            model.close()
            self.assertTrue(workspace.resolve().is_relative_to(root.resolve()))
            shutil.rmtree(workspace)

    def test_planner_cannot_override_failed_visual_result(self):
        result, calls = self.run_case([
            tool("computer_use", task="a model-rewritten task is not authorization"),
            tool("finish_task", status="completed"), "全部完成，已经勾选。"])
        self.assertEqual(result["status"], "needs_user")
        self.assertTrue(result["gui_used"])
        self.assertFalse(result["gui_goal_verified"])
        self.assertNotIn("全部完成", result["reply"])
        self.assertIn("GUI_GOAL_UNVERIFIED", calls[-1]["messages"][-1]["content"])

    def test_control_property_alone_cannot_complete_entire_task(self):
        result, _ = self.run_case([tool("desktop_observe"),
            tool("desktop_act", ref="fixture-ref", action="check"),
            tool("desktop_verify_state", ref="fixture-ref", property="checked", expected=True),
            tool("finish_task", status="completed"), "全部完成。"])
        self.assertEqual(result["status"], "needs_user")
        self.assertFalse(result["gui_goal_verified"])

    def test_verified_structured_task_can_complete(self):
        result, _ = self.run_case([tool("desktop_observe"),
            tool("desktop_act", ref="fixture-ref", action="check"), tool("verify_desktop_goal"),
            tool("finish_task", status="completed"), "已确认勾选。"])
        self.assertEqual(result["status"], "completed", result)
        self.assertTrue(result["gui_goal_verified"])
        self.assertEqual(result["verification"]["status"], "verified")

    def test_later_action_invalidates_previous_pass(self):
        result, _ = self.run_case([tool("desktop_observe"),
            tool("desktop_act", ref="fixture-ref", action="check"), tool("verify_desktop_goal"),
            tool("desktop_observe"), tool("desktop_act", ref="fixture-ref", action="uncheck"),
            tool("finish_task", status="completed"), "全部完成。"])
        self.assertEqual(result["status"], "needs_user")
        self.assertFalse(result["gui_goal_verified"])

    def test_noop_invoke_and_false_success_are_rejected(self):
        result, _ = self.run_case([tool("desktop_observe"),
            tool("desktop_act", ref="fixture-ref", action="invoke"), tool("verify_desktop_goal"),
            tool("finish_task", status="completed"), "全部完成。"])
        self.assertEqual(result["status"], "needs_user")
        self.assertFalse(result["gui_goal_verified"])

    def test_no_tools_cannot_complete_explicit_like_command(self):
        result, _ = self.run_case(["全部完成，已点赞。"], task="给抖音首页第一个视频点个赞")
        self.assertEqual(result["status"], "needs_user")
        self.assertFalse(result["gui_goal_verified"])
        self.assertNotIn("已点赞", result["reply"])

    def test_explanation_still_needs_no_desktop_tools(self):
        result, _ = self.run_case(["电脑操控会先观察界面，再操作并检查结果。"], task="解释一下电脑操控原理")
        self.assertEqual(result["status"], "completed")
        self.assertFalse(result["gui_used"])

    def test_uncertain_native_launch_cannot_fall_through_to_new_input(self):
        result, calls = self.run_case([tool("open_application", application_name="fixture-app"),
            tool("desktop_observe"), tool("desktop_act", ref="fixture-ref", action="check"),
            tool("desktop_observe"), tool("finish_task", status="completed"), "全部完成。"])
        self.assertEqual(result["status"], "needs_user")
        observations = [m for m in calls[-1]["messages"] if m.get("role") == "tool"]
        self.assertTrue(any("GUI_NATIVE_LAUNCH_UNCONFIRMED" in m["content"] for m in observations))
        snapshots = [json.loads(m["content"]) for m in observations if m.get("name") == "desktop_observe"]
        self.assertFalse(snapshots[-1]["controls"][0]["state"]["checked"])


if __name__ == "__main__":
    unittest.main()
