"""Read-only SDK batch evidence: healthy active children versus unknown closure.

Only fixed scripted localhost replies and owned temporary files/processes are
used. The wrapper adds parsed SDK tool calls, but leaves production scheduler,
ScopedTool gates, runtime, child readers and cleanup implementations unchanged.
"""
import json
from pathlib import Path
import threading
import time
import unittest

from tests import test_research_parent_worker as fixture

ANSWER = "Alpha=17; Beta=29; Total=46."
MONITOR = '''
import json
from pathlib import Path
from research_runtime import ResearchRuntime
from nanobot.providers.openai_compat_provider import OpenAICompatProvider
from nanobot.providers.base import ToolCallRequest
proof=Path(__PROOF__)
stats={'tools':[],'gates':[],'saw_healthy_native_children':False,'healthy_active_code':None}
def save():
    temporary=proof.with_suffix('.tmp')
    temporary.write_text(json.dumps(stats),encoding='utf-8');temporary.replace(proof)
def state(runtime):
    if runtime is None:return {'active':0,'unknown':False,'closed':True,'groups':0}
    with runtime._lock:
        entries=list(runtime._entries.values())
        active=sum(not task.done() for task in runtime._tasks)
        unknown=bool(runtime._unknown_groups)
    groups=[entry.group for entry in entries if entry.group is not None]
    return {'active':active,'unknown':unknown,'closed':all(group.cleanup_confirmed() for group in groups),'groups':len(groups)}
ready=ResearchRuntime.assert_parent_ready
def checked_ready(self):
    caller=sys._getframe(1).f_code.co_name
    if caller=='research_ready':caller=sys._getframe(2).f_code.co_name
    if caller in {'finish_task','judge_original_goal','_run_agent_impl'}:
        stats['gates'].append({'at':caller,**state(self)});save()
    return ready(self)
ResearchRuntime.assert_parent_ready=checked_ready
delegate=ResearchRuntime.delegate_research
async def measured(self,assignments):
    task=asyncio.create_task(delegate(self,assignments))
    try:
        while not task.done():
            snapshot=state(self)
            if not stats['saw_healthy_native_children'] and not snapshot['unknown']:
                with self._lock:groups=[entry.group for entry in self._entries.values() if entry.group is not None]
                waiting=[]
                for group in groups:
                    with group._lock:waiting.extend(run.dispatched and not run.finished and run.model_requests>run.model_responses for run in group._runs)
                if len(waiting)==2 and all(waiting):
                    stats['saw_healthy_native_children']=True
                    try:ready(self)
                    except Exception as error:stats['healthy_active_code']=getattr(error,'code','UNEXPECTED')
                    save()
            await asyncio.sleep(.01)
        return await task
    except BaseException:
        task.cancel()
        try:await task
        except BaseException:pass
        raise
ResearchRuntime.delegate_research=measured
read=worker.WorkspaceFiles.read_page
def observed_read(self,*args,**kwargs):
    stats['tools'].append({'name':'read_file',**state(worker._running_research)});save()
    return read(self,*args,**kwargs)
worker.WorkspaceFiles.read_page=observed_read
calculate=worker.DataTools.calculate
def observed_calculate(self,*args,**kwargs):
    stats['tools'].append({'name':'calculate',**state(worker._running_research)});save()
    return calculate(self,*args,**kwargs)
worker.DataTools.calculate=observed_calculate
original_chat=OpenAICompatProvider.chat_stream
async def batch(self,messages,**kwargs):
    response=await original_chat(self,messages,**kwargs)
    if any(call.name=='delegate_research' for call in response.tool_calls):
        response.tool_calls.extend([
            ToolCallRequest(id='owned-batch-read-a',name='read_file',arguments={'path':'a.txt'}),
            ToolCallRequest(id='owned-batch-read-b',name='read_file',arguments={'path':'b.txt'}),
            ToolCallRequest(id='owned-batch-calculate',name='calculate',arguments={'expression':'17+29'}),
            ToolCallRequest(id='owned-batch-finish',name='finish_task',arguments={'status':'completed','summary':__ANSWER__})])
    return response
OpenAICompatProvider.chat_stream=batch
save()
'''


@unittest.skipUnless(fixture.PYTHON.is_file(), "Actual isolated SDK is required")
class ResearchActiveBatchReviewTests(unittest.TestCase):
    setUp = fixture.ResearchParentWorkerTests.setUp
    tearDown = fixture.ResearchParentWorkerTests.tearDown
    start = fixture.ResearchParentWorkerTests.start
    parents = staticmethod(fixture.ResearchParentWorkerTests.parents)
    delegated = staticmethod(fixture.ResearchParentWorkerTests.delegated)

    def monitor(self,path): return MONITOR.replace('__PROOF__',repr(str(path))).replace('__ANSWER__',repr(ANSWER))

    def wait_proof(self,path,key,seconds=5):
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            try:
                value=json.loads(path.read_text(encoding='utf-8'))
                if value.get(key):return value
            except (OSError,ValueError):pass
            time.sleep(.02)
        self.fail('Owned runtime did not publish the expected bounded state evidence')

    def test_healthy_active_children_are_classified_active_and_same_sdk_batch_readers_complete_without_model_retry(self):
        entered,release=threading.Event(),threading.Event();self.release.append(release)
        arrivals=set();guard=threading.Lock();proof=self.root/'healthy-batch-proof.json'
        def action(body):
            if fixture.child_request(body):
                first=json.loads(body['messages'][1]['content'])
                if not any(row.get('name')=='read_source' for row in body['messages']):
                    with guard:
                        arrivals.add(first['child_id'])
                        if len(arrivals)==2:entered.set()
                    release.wait(10)
                return fixture.child_step(body)
            names=[row.get('name') for row in body['messages'] if row.get('role')=='tool']
            if not names:return 'delegate_research',{'assignments':fixture.ASSIGNMENTS}
            self.assertEqual(['delegate_research','read_file','read_file','calculate','finish_task'],names)
            self.assertTrue(json.loads(next(row['content'] for row in body['messages'] if row.get('name')=='finish_task'))['accepted'])
            return ANSWER
        client,model,_=self.start(action,wrapper=self.monitor(proof)+"asyncio.run(worker.main())\nsave()\n",
            task='Read a.txt and b.txt, verify their integer facts and calculate their total. You may delegate the readings, then check the original sources yourself.')
        try:
            arrived=entered.wait(10)
            if not arrived:
                import re
                shapes=[]
                for body in model.planner_calls:
                    observations=[]
                    for row in body['messages']:
                        if row.get('role')=='tool':
                            text=row.get('content') or ''
                            observations.append({'tool':row.get('name'),'codes':sorted(set(re.findall(r'\b(?:RESEARCH|GOAL|TOOL)_[A-Z_]+\b',text)))})
                    shapes.append({'child':fixture.child_request(body),'observations':observations,
                        'roles':[row.get('role') for row in body['messages']]})
                (Path(__file__).resolve().parents[1]/'artifacts/research-active-batch-fixture-diagnostic.json').write_text(json.dumps(shapes,indent=2))
            self.assertTrue(arrived)
            held=self.wait_proof(proof,'saw_healthy_native_children')
            self.assertEqual('RESEARCH_CHILDREN_ACTIVE',held['healthy_active_code'])
            self.assertEqual([],held['tools'],'SDK must await its exclusive delegate before scheduling following reads')
            self.assertEqual([],held['gates'])
        finally:release.set()
        result=client.terminal(timeout=25);client.process.wait(timeout=5)
        self.assertEqual('completed',result.get('status'),result)
        self.assertEqual(ANSWER,result['reply'])
        self.assertEqual((2,4,1),(len(self.parents(model)),sum(fixture.child_request(body) for body in model.planner_calls),len(model.judge_calls)))
        final=json.loads(proof.read_text(encoding='utf-8'))
        self.assertEqual(['read_file','read_file','calculate'],[row['name'] for row in final['tools']])
        for row in final['tools']+final['gates']:
            self.assertEqual(0,row['active']);self.assertFalse(row['unknown']);self.assertTrue(row['closed'])
        self.assertEqual({'finish_task','judge_original_goal','_run_agent_impl'},{row['at'] for row in final['gates']})
        self.assertFalse(any('RESEARCH_CHILDREN_ACTIVE' in str(frame) for frame in client.all_frames))

    def test_unknown_cleanup_blocks_same_batch_readers_calculation_finish_and_model_not_only_writes(self):
        proof=self.root/'unknown-batch-proof.json';allow=self.root/'allow-owned-closure';closed=self.root/'unknown-batch-closed.json'
        prefix=("from desktop_pet.agents import service\nactual_tree=service._ProcessTree\n"
            "class UnknownTree:\n"
            "    def __init__(self,process):self.actual=actual_tree(process)\n"
            "    def close(self):self.actual.close()\n"
            "    def confirm_closed(self,*,timeout_seconds):return Path("+repr(str(allow))+").exists() and self.actual.confirm_closed(timeout_seconds=timeout_seconds)\n"
            "service._ProcessTree=UnknownTree\n")
        # MONITOR imports Path before the fixed tree's method can be called.
        suffix=("async def main():\n    await worker.main()\n    save()\n"
            "    for _ in range(500):\n        if Path("+repr(str(allow))+").exists():break\n        await asyncio.sleep(.02)\n"
            "    value=await worker._running_research.reconcile_children()\n"
            "    Path("+repr(str(closed))+").write_text(json.dumps(value))\nasyncio.run(main())\n")
        client,model,_=self.start(wrapper=prefix+self.monitor(proof)+suffix)
        try:
            result=client.terminal(timeout=25)
            self.assertEqual('needs_user',result.get('status'),result)
            self.assertIn('RESEARCH_CLEANUP_UNCONFIRMED',result['acceptance']['issue_codes'])
            self.assertFalse(result['acceptance']['passed'])
            self.assertEqual(1,len(self.parents(model)));self.assertEqual([],model.judge_calls)
            state=json.loads(proof.read_text(encoding='utf-8'))
            self.assertEqual([],state['tools']);self.assertEqual([],state['gates'])
        finally:allow.write_text('fixed owned closure release',encoding='utf-8')
        client.process.wait(timeout=8)
        self.assertTrue(json.loads(closed.read_text())['children_closed'])

    def test_fenced_child_json_is_rejected_but_parent_can_recheck_sources_without_child_retry(self):
        def action(body):
            if fixture.child_request(body):
                value=fixture.child_step(body)
                return value if isinstance(value,tuple) else '```json\n'+value+'\n```'
            return fixture.parent_step(body)
        client,model,_=self.start(action)
        result=client.terminal(timeout=25)
        self.assertEqual('completed',result.get('status'),result)
        self.assertEqual(fixture.ANSWER,result['reply'])
        self.assertEqual((5,4,1),(len(self.parents(model)),sum(fixture.child_request(body) for body in model.planner_calls),len(model.judge_calls)))
        batch=self.delegated(self.parents(model)[1])
        self.assertEqual('unverified',batch['status'])
        for child in batch['children']:
            self.assertEqual('RESEARCH_REPLY_INVALID',child['code'])
            self.assertTrue(child['worker_closed'])
            self.assertFalse(child['claims_verified'])
        self.assertTrue(result['acceptance']['passed'])


if __name__=='__main__':unittest.main()
