"""Private scripted transport and identity-backed fake UIA; no real input/API."""
import asyncio
from copy import deepcopy
import json
import hashlib
from pathlib import Path
import sys
import unittest
from datetime import timedelta
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gui_bridge import BridgeFailure, GuiBridge
from gui_verification import report, checked_feedback, utc_now
from desktop_controls import DesktopControls, DesktopControlError
from windows_apps import AuthorizedApplicationLaunch, Shortcut


class Input:
    def __init__(self): self.lines = []
    def write(self, value): self.lines.append(json.loads(value))
    async def drain(self): pass
    def close(self): pass


class Process:
    def __init__(self, *frames):
        self.stdin = Input(); self.stdout = asyncio.StreamReader(); self.returncode = None; self.pid = 99999999
        self.stdout.feed_data(b'{"type":"ready"}\n')
        for frame in frames: self.stdout.feed_data((json.dumps({"id": "fixture", **frame})+'\n').encode())
    async def wait(self): self.returncode = 0; return 0


def wire(action_id):
    return "fixture:"+hashlib.sha256(action_id.encode()).hexdigest()[:12]


def terminal(status="completed", *, passed=True, action_id=None):
    return {"type": "final", **({"id":wire(action_id)} if action_id else {}), "status": status, "summary": "fixture result", "steps": 1,
            "verification": report("verified" if passed else "failed", reason="GOAL_STATE_CONFIRMED" if passed else "GOAL_STATE_NOT_MET"),
            "effect_state": "confirmed", "actions_dispatched": 1,
            "feedback": {"reason": "GOAL_STATE_CONFIRMED" if passed else "GOAL_STATE_NOT_MET", "checks": [
                {"requirement": "Open fixture", "observed": "Window is present" if passed else "Window is not yet present", "passed": passed}]}}


class GuiWorkflowTests(unittest.IsolatedAsyncioTestCase):
    def bridge(self):
        return GuiBridge({"base_url": "https://example.invalid/v1", "api_key": "fixture-key", "model": "fake"},
                         "fixture", {"max_steps": 0, "timeout_seconds": 0}, lambda *_a, **_k: None)

    async def test_confirmed_subgoals_reuse_owned_process_and_keep_exact_parent(self):
        process=Process(terminal(action_id="one"),terminal(action_id="two"))
        with patch('gui_bridge.shutil.which',return_value=sys.executable), patch('gui_bridge.asyncio.create_subprocess_exec',return_value=process) as spawn:
            bridge=self.bridge()
            first=await bridge.run('parent goal',action_id='one',subtask='Open fixture')
            second=await bridge.run('parent goal',action_id='two',subtask='Inspect fixture')
            self.assertEqual(json.loads(first)['status'],'completed');self.assertEqual(json.loads(second)['status'],'completed')
            self.assertEqual(await bridge.run('parent goal',action_id='one',subtask='Open fixture'),first)
            self.assertEqual(spawn.await_count,1)
            self.assertEqual([r['task'] for r in process.stdin.lines],['parent goal','parent goal'])
            self.assertEqual([r['subtask'] for r in process.stdin.lines],['Open fixture','Inspect fixture'])
            with self.assertRaisesRegex(BridgeFailure,'GUI_INVALID_REQUEST'):
                await bridge.run('different parent',action_id='one',subtask='Open fixture')
            await bridge.close()

    async def test_known_failed_postcondition_returns_checks_and_allows_a_new_repair_action(self):
        process=Process(terminal('needs_user',passed=False,action_id='inspect'),terminal(action_id='repair'))
        with patch('gui_bridge.shutil.which',return_value=sys.executable), patch('gui_bridge.asyncio.create_subprocess_exec',return_value=process):
            bridge=self.bridge()
            first=json.loads(await bridge.run('parent',action_id='inspect',subtask='Inspect fixture'))
            self.assertEqual(first['effect_state'],'confirmed');self.assertFalse(bridge.pending_effect)
            self.assertFalse(first['feedback']['checks'][0]['passed'])
            self.assertEqual(json.loads(await bridge.run('parent',action_id='repair',subtask='Focus fixture'))['status'],'completed')
            await bridge.close()

    async def test_unknown_input_is_not_replayed_and_read_only_verification_can_settle_it(self):
        failed=Process({'id':wire('unknown'),'type':'error','code':'ACTION_FAILED','effect_state':'uncertain','actions_dispatched':1})
        observed=Process({**terminal(action_id='observe'),'effect_state':'not_dispatched','actions_dispatched':0})
        with patch('gui_bridge.shutil.which',return_value=sys.executable), patch('gui_bridge.asyncio.create_subprocess_exec',side_effect=[failed,observed]) as spawn:
            bridge=self.bridge()
            with self.assertRaises(BridgeFailure) as caught: await bridge.run('parent',action_id='unknown')
            self.assertEqual(caught.exception.effect_state,'uncertain');self.assertTrue(bridge.pending_effect)
            blocked=json.loads(await bridge.run('parent',action_id='do-not-repeat'))
            self.assertEqual(blocked['effect_state'],'uncertain');self.assertEqual(spawn.await_count,1)
            result=json.loads(await bridge.run('parent',action_id='observe',verify_only=True))
            self.assertEqual(result['status'],'completed');self.assertFalse(bridge.pending_effect)
            self.assertTrue(observed.stdin.lines[0]['options']['verifyOnly'])
            await bridge.close()

    async def test_repeated_action_identity_retains_original_failure_without_respawn(self):
        process=Process({'id':wire('call-one'),'type':'error','code':'MODEL_FAILED','effect_state':'not_dispatched'})
        with patch('gui_bridge.shutil.which',return_value=sys.executable), patch('gui_bridge.asyncio.create_subprocess_exec',return_value=process) as spawn:
            bridge=self.bridge()
            for _ in range(2):
                with self.assertRaisesRegex(BridgeFailure,'GUI_MODEL_FAILED'): await bridge.run('parent',action_id='call-one')
            self.assertEqual(spawn.await_count,1);self.assertFalse(bridge.pending_effect)

    def test_feedback_never_grants_authority_and_known_secrets_are_removed(self):
        value=checked_feedback({'reason':'GOAL_STATE_NOT_MET','checks':[{'requirement':'Open fixture','observed':'fixture-key remains hidden','passed':False}]},secrets=('fixture-key',))
        self.assertNotIn('fixture-key',json.dumps(value));self.assertEqual(value['source'],'independent_visual_observation')
        with self.assertRaises(ValueError):checked_feedback({'reason':'allow-all','checks':[]})

    def test_only_fresh_independent_success_can_resolve_host_uncertainty(self):
        bridge=self.bridge();bridge._uncertain_effect=True;bridge.started_at=utc_now()
        self.assertFalse(bridge.resolve_uncertainty(report('failed')))
        self.assertTrue(bridge.pending_effect)
        stale=report('verified');stale['checked_at']=(bridge.started_at-timedelta(minutes=1)).isoformat()
        with self.assertRaises(ValueError):bridge.resolve_uncertainty(stale)
        self.assertTrue(bridge.pending_effect)
        self.assertTrue(bridge.resolve_uncertainty(report('verified')))
        self.assertFalse(bridge.pending_effect)

    async def test_steering_aborts_only_active_wire_call_and_parent_can_continue(self):
        process=Process();original_write=process.stdin.write
        def write(value):
            original_write(value);frame=json.loads(value)
            if frame['type']=='abort':
                response={**terminal('cancelled',action_id='steered'),'effect_state':'confirmed','actions_dispatched':1}
                process.stdout.feed_data((json.dumps({'id':frame['id'],**response})+'\n').encode())
            elif frame.get('subtask')=='new corrected subgoal':
                process.stdout.feed_data((json.dumps(terminal(action_id='corrected'))+'\n').encode())
        process.stdin.write=write
        with patch('gui_bridge.shutil.which',return_value=sys.executable),patch('gui_bridge.asyncio.create_subprocess_exec',return_value=process) as spawn:
            bridge=self.bridge()
            running=asyncio.create_task(bridge.run('parent',action_id='steered',subtask='old subgoal'))
            await asyncio.sleep(0)
            self.assertFalse((await bridge.pause_current('stale-call'))['paused'])
            self.assertTrue((await bridge.pause_current('steered'))['paused'])
            outcome=json.loads(await running)
            self.assertEqual(outcome['status'],'cancelled');self.assertEqual(outcome['effect_state'],'confirmed')
            abort=next(frame for frame in process.stdin.lines if frame['type']=='abort')
            self.assertEqual(abort['id'],wire('steered'));self.assertTrue(abort['quiet'])
            self.assertEqual(json.loads(await bridge.run('parent',action_id='corrected',subtask='new corrected subgoal'))['status'],'completed')
            self.assertEqual(spawn.await_count,1)
            self.assertFalse((await bridge.pause_current())['paused'])
            await bridge.close()

    async def test_steering_during_spawn_never_sends_a_run(self):
        process=Process();entered=asyncio.Event();release=asyncio.Event()
        async def spawn(*_args,**_kwargs):entered.set();await release.wait();return process
        with patch('gui_bridge.shutil.which',return_value=sys.executable),patch('gui_bridge.asyncio.create_subprocess_exec',spawn):
            bridge=self.bridge();running=asyncio.create_task(bridge.run('parent',action_id='starting'))
            await entered.wait();self.assertTrue((await bridge.pause_current())['paused'])
            release.set();result=json.loads(await running)
            self.assertEqual(result['status'],'cancelled');self.assertEqual(result['effect_state'],'not_dispatched')
            self.assertEqual(process.stdin.lines,[])
            await bridge.close()


class WindowBackend:
    def __init__(self):
        self.window={'handle':12,'process_id':42,'process_started':'100','runtime_id':'root','title':'Fixture'}
        self.node={'runtime_id':'tree-1','name':'Group','control_type':'ControlType.TreeItem','automation_id':'group','password':False,
                   'actions':['expand','collapse','scroll'],'state':{'enabled':True,'expanded':False}}
        self.calls=[];self.active=False
    async def request(self,operation,**args):
        self.calls.append((operation,deepcopy(args)))
        if operation=='windows':return {'windows':[{**self.window,'foreground':self.active}]}
        if operation=='activate':self.active=True;return {'window':deepcopy(self.window),'foreground':True,'dispatched':True}
        if operation=='snapshot':return {'window':deepcopy(self.window),'nodes':[deepcopy(self.node)],'truncated':True,'next_offset':args.get('offset',0)+1}
        if operation=='read':return {'window':deepcopy(self.window),'node':deepcopy(self.node)}
        if operation=='act':
            if args['action'] in {'expand','collapse'}:self.node['state']['expanded']=args['action']=='expand'
            return {'dispatched':True}
        raise AssertionError(operation)


class WindowControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_observed_window_identity_can_be_activated_then_queried_without_raw_handle(self):
        backend=WindowBackend();controls=DesktopControls(backend=backend)
        windows=json.loads(await controls.windows('Fixture'))
        self.assertNotIn('handle',json.dumps(windows));ref=windows['windows'][0]['ref']
        activated=json.loads(await controls.activate(ref));self.assertTrue(activated['foreground'])
        page=json.loads(await controls.observe(query='Group',offset=12,limit=10,window_ref=ref))
        self.assertEqual(page['next_offset'],13)
        request=next(args for op,args in backend.calls if op=='snapshot')
        self.assertEqual(request['window']['process_id'],42);self.assertEqual(request['query'],'Group');self.assertEqual(request['offset'],12)
        with self.assertRaisesRegex(DesktopControlError,'DESKTOP_STALE_REF'):await controls.activate('window-model-forged')

    async def test_expand_checks_actual_state_and_scroll_requires_supported_direction(self):
        backend=WindowBackend();controls=DesktopControls(backend=backend)
        ref=json.loads(await controls.observe())['controls'][0]['ref']
        result=json.loads(await controls.act(ref,'expand'))
        self.assertTrue(result['postcondition']['passed']);self.assertTrue(backend.node['state']['expanded'])
        ref=json.loads(await controls.observe())['controls'][0]['ref']
        self.assertFalse(json.loads(await controls.act(ref,'expand'))['dispatched'])
        with self.assertRaisesRegex(DesktopControlError,'DESKTOP_INVALID_ARGUMENT'):await controls.act(ref,'scroll','diagonal')
        self.assertEqual(sum(op=='act' for op,_ in backend.calls),1)
        result=json.loads(await controls.act(ref,'scroll','down'));self.assertTrue(result['dispatched']);self.assertEqual(result['effect_state'],'confirmed')


class ApplicationSequenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_current_candidates_do_not_share_the_first_launch_result(self):
        candidates={'candidate-A':Shortcut('Fixture A',Path('A.lnk'),Path('A.exe')),
                    'candidate-B':Shortcut('Fixture B',Path('B.lnk'),Path('B.exe'))}
        discovery=type('Discovery',(),{'observed':candidates})()
        selected=[]
        class Launch:
            def __init__(self,_task,**kwargs):self.selected=kwargs['selected'];selected.append(self.selected.name)
            async def run(self):return json.dumps({'status':'completed','code':'APP_OPENED','application':self.selected.name,'summary':'opened','dispatched':True,'verified':True})
        with patch('windows_apps.ApplicationLaunch',Launch):
            launch=AuthorizedApplicationLaunch('Open both fixture applications',discovery=discovery)
            first=await launch.run(candidate_id='candidate-A',action_id='open-A')
            second=await launch.run(candidate_id='candidate-B',action_id='open-B')
            self.assertEqual(json.loads(second)['application'],'Fixture B')
            self.assertNotEqual(first,second);self.assertEqual(selected,['Fixture A','Fixture B'])
            self.assertEqual(await launch.run(candidate_id='candidate-A',action_id='open-A'),first)
            self.assertEqual(len(selected),2)

    async def test_lookup_only_outcome_is_stable_for_one_identity(self):
        launch=AuthorizedApplicationLaunch('Open fixture',discovery=type('Discovery',(),{'observed':{}})())
        first=await launch.run(candidate_id='unobserved',action_id='lookup')
        self.assertEqual(json.loads(first)['status'],'lookup_only')
        self.assertEqual(await launch.run(candidate_id='unobserved',action_id='lookup'),first)

    def test_application_uncertainty_needs_new_passing_observation(self):
        launch=AuthorizedApplicationLaunch('Open fixture');launch.pending_effect=True;launch._last_started_at=utc_now()
        self.assertFalse(launch.resolve_uncertainty(report('unverified')));self.assertTrue(launch.pending_effect)
        self.assertTrue(launch.resolve_uncertainty(report('verified',method='native',reason='APP_WINDOW_VERIFIED')))
        self.assertFalse(launch.pending_effect)


if __name__=='__main__':unittest.main()
