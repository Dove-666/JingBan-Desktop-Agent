"""Before contracts: real SDK/worker, fixed loopback planner, zero live input.

The two positive recovery assertions intentionally expose existing behavior.
No production function, authorization gate or real desktop backend is changed.
"""
import json
from pathlib import Path
import sys
import textwrap
import unittest

from agent_backend.nanobot.tests import test_harness_worker as fixture


@unittest.skipUnless(fixture.PYTHON.is_file(), "isolated SDK environment unavailable")
class GuiNoEffectRecoveryBeforeTests(unittest.TestCase):
    setUp = fixture.HarnessWorkerTests.setUp
    tearDown = fixture.HarnessWorkerTests.tearDown
    start = fixture.HarnessWorkerTests.start
    observations = []

    def wrapper(self, mode):
        output = self.workspace / "owned-gui-audit.json"
        wrapper = self.workspace / "owned-no-live-gui-worker.py"
        code = r'''
import asyncio,importlib.util,json,os,re,socket
from pathlib import Path
spec=importlib.util.spec_from_file_location('owned_worker',__WORKER__)
worker=importlib.util.module_from_spec(spec);spec.loader.exec_module(worker)
from gui_verification import report
MODE=__MODE__;OUTPUT=Path(__OUTPUT__)
counts={'input_calls':0,'gui_calls':0,'verification_calls':0,'observation_calls':0,'blocked_network':[]}
for key in list(os.environ):
    if re.search(r'API_?KEY|TOKEN|SECRET|PASSWORD|OPENAI|ANTHROPIC|NANOBOT',key,re.I):os.environ.pop(key,None)
    if key.upper() in {'HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','NO_PROXY'}:os.environ.pop(key,None)
os.environ['NO_PROXY']='*'
def allowed(host):
    host=host.decode('ascii') if isinstance(host,bytes) else host
    if host not in {'127.0.0.1','::1'}:
        counts['blocked_network'].append(str(host)[:160])
        raise OSError('OWNED_NON_LOOPBACK_BLOCKED')
original_dns=socket.getaddrinfo
def dns(host,*args,**kwargs):allowed(host);return original_dns(host,*args,**kwargs)
socket.getaddrinfo=dns
original_connect=socket.socket.connect;original_connect_ex=socket.socket.connect_ex
def connect(sock,address):allowed(address[0]);return original_connect(sock,address)
def connect_ex(sock,address):allowed(address[0]);return original_connect_ex(sock,address)
socket.socket.connect=connect;socket.socket.connect_ex=connect_ex
if os.name=='nt':
    from asyncio.proactor_events import BaseProactorEventLoop
    original_sock_connect=BaseProactorEventLoop.sock_connect
    async def sock_connect(loop,sock,address):allowed(address[0]);return await original_sock_connect(loop,sock,address)
    BaseProactorEventLoop.sock_connect=sock_connect
class OwnedGui(worker.GuiBridge):
    async def _run_once(self, task):
        if self._current_verify_only:
            counts['verification_calls']+=1
            self.verification=report('failed',reason='GOAL_STATE_NOT_MET')
            self.effect_state='uncertain' if self._uncertain_effect else 'not_dispatched'
            return json.dumps({'status':'needs_user','effect_state':self.effect_state,'steps':0,'verification':self.verification})
        counts['gui_calls']+=1
        if MODE=='prior_confirmed' and counts['gui_calls']==1:
            counts['input_calls']+=1;self.action_epoch+=1;self.effect_state='confirmed'
            self.verification=report('failed',reason='GOAL_STATE_NOT_MET')
            self.verification_scope='subgoal'
            return json.dumps({'status':'needs_user','effect_state':'confirmed','steps':1,'verification':self.verification})
        if MODE=='unknown':
            counts['input_calls']+=1;self._uncertain_effect=True;self.effect_state='uncertain'
            error=worker.BridgeFailure('GUI_ACTION_FAILED');error.effect_state='uncertain'
        else:
            self.effect_state='not_dispatched';self._actions_dispatched=0
            error=worker.BridgeFailure('GUI_SCREENSHOT_FAILED');error.effect_state='not_dispatched'
        self.record_failure(error.code)
        raise error
worker.GuiBridge=OwnedGui
import desktop_controls
class OwnedControls:
    def invalidate(self):pass
    async def observe(self,**kwargs):
        counts['observation_calls']+=1
        raise worker.DesktopControlError('DESKTOP_UNAVAILABLE')
    async def act(self,**kwargs):raise AssertionError('UNEXPECTED_INPUT_CALLBACK')
desktop_controls.DesktopControls=OwnedControls
Base=worker.TaskHarness
def audit(harness):
    assessment=harness.assess()
    snapshot=harness.goal_snapshot()
    bridge=worker._running_bridge
    value={'mode':MODE,'synthetic_counts':counts,'live_input_effects':0,
        'original_task':harness.goal_contract.original_task,
        'requirements_kinds':[item['kind'] for item in harness.goal_contract.requirements],
        'requirement_checks_pass':all(item['passed'] for item in snapshot['facts']['requirement_checks']),
        'judge_status':assessment.get('goal',{}).get('judge_status'),
        'original_goal_assessment_pass':assessment.get('goal',{}).get('passed'),
        'issues':assessment['issues'],'gui_required':harness.gui_required,'gui_epoch':harness._gui_epoch,
        'pending_effects':snapshot['facts']['pending_effects'],
        'failed_tools':assessment['failed_tools'],'failure_categories':list(harness.failure_categories.values()),
        'bridge_pending_effect':bool(bridge.pending_effect) if bridge else None,
        'bridge_effect_state':bridge.effect_state if bridge else None}
    OUTPUT.write_text(json.dumps(value),encoding='utf-8')
class AuditedHarness(Base):
    def record_goal_verification(self,value):
        result=super().record_goal_verification(value);audit(self);return result
    def finalize(self,*args,**kwargs):
        result=super().finalize(*args,**kwargs);audit(self);return result
worker.TaskHarness=AuditedHarness
asyncio.run(worker.main())
'''
        code = textwrap.dedent(code).replace("__WORKER__", repr(str(fixture.BACKEND / "worker.py")))
        code = code.replace("__MODE__", repr(mode)).replace("__OUTPUT__", repr(str(output)))
        wrapper.write_text(code, encoding="utf-8")
        return wrapper, output

    def case(self, mode):
        wrapper, output = self.wrapper(mode)
        task = ('Open fixture app and create report.txt containing exactly "DONE".' if mode == "gui_required" else
                'Create report.txt containing exactly "DONE". This is solely a file task; do not interact with desktop windows.')
        steps = [fixture.tool("desktop_observe")] if mode == "observe" else [fixture.tool("computer_use", task="Inspect the task context")]
        if mode in {"unknown", "prior_confirmed"}:steps.append(fixture.tool("computer_use", task="Attempt another desktop subtask"))
        steps += [fixture.tool("write_file", path="report.txt", content="DONE"), fixture.tool("read_file", path="report.txt"),
                  fixture.tool("verify_artifact", path="report.txt", kind="text"),
                  fixture.tool("finish_task", status="completed", summary="The owned report contains exactly DONE and was read back."),
                  "The owned report contains exactly DONE and was read back."]
        client, model = self.start(steps, computer=True, task=task, worker_path=wrapper)
        result = client.terminal()
        self.assertEqual(result["type"], "final", result)
        self.assertEqual((self.workspace / "report.txt").read_bytes(), b"DONE")
        self.assertTrue(output.is_file(), "Actual harness did not publish its metadata audit")
        value = json.loads(output.read_text(encoding="utf-8"))
        value["terminal_status"] = result["status"]
        value["planner_calls"] = len(model.calls)
        self.observations.append(value)
        self.assertEqual(value["synthetic_counts"]["blocked_network"], [])
        self.assertEqual(value["live_input_effects"], 0)
        if mode != "unknown":
            self.assertTrue(value["requirement_checks_pass"])
            self.assertEqual(value["judge_status"], "satisfied")
            self.assertTrue(value["original_goal_assessment_pass"])
        return value

    def test_pure_file_goal_after_zero_input_screenshot_failure_can_finish(self):
        value = self.case("zero")
        self.assertEqual(value["synthetic_counts"]["input_calls"], 0)
        self.assertEqual(value["pending_effects"], [])
        self.assertEqual(value["terminal_status"], "completed")

    def test_pure_file_goal_after_readonly_observation_failure_can_finish(self):
        value = self.case("observe")
        self.assertEqual(value["synthetic_counts"]["input_calls"], 0)
        self.assertFalse(value["gui_required"])
        self.assertEqual(value["terminal_status"], "completed")

    def test_explicit_window_goal_stays_blocked_even_with_optimistic_bound_judge(self):
        value = self.case("gui_required")
        self.assertEqual(value["synthetic_counts"]["input_calls"], 0)
        self.assertTrue(value["original_goal_assessment_pass"], "The negative must not merely rely on a rejecting judge")
        self.assertEqual(value["terminal_status"], "needs_user")
        self.assertTrue(value["gui_required"])

    def test_unknown_input_is_not_replayed_and_cannot_complete_via_file(self):
        value = self.case("unknown")
        self.assertEqual(value["synthetic_counts"]["input_calls"], 1)
        self.assertEqual(value["synthetic_counts"]["gui_calls"], 1)
        self.assertIn("desktop", value["pending_effects"])
        self.assertEqual(value["terminal_status"], "needs_user")

    def test_prior_confirmed_input_obligation_survives_later_zero_input_failure(self):
        value = self.case("prior_confirmed")
        self.assertEqual(value["synthetic_counts"]["input_calls"], 1)
        self.assertEqual(value["synthetic_counts"]["gui_calls"], 2)
        self.assertEqual(value["pending_effects"], [])
        self.assertTrue(value["gui_required"])
        self.assertGreater(value["gui_epoch"], 0)
        self.assertEqual(value["terminal_status"], "needs_user")


if __name__ == "__main__": unittest.main()
