"""Actual registered SDK tool with trusted QA config injection, no enable file.

All code/model replies are fixed synthetic fixtures. Native positive/EOF cases
use the actual pinned MXC launcher; uncertainty controls use labeled fakes.
No production configuration, cloud provider or real user file is touched.
"""
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from tests import test_research_parent_worker as fixture
from desktop_pet.agents import execution_runtime as host

FIXED_CODE = "import json,pathlib\nnumbers=[int(n) for n in (pathlib.Path(INPUT_DIR)/'source.txt').read_text().split()]\n(pathlib.Path(OUTPUT_DIR)/'result.txt').write_text(str(sum(numbers)))\nprint(json.dumps({'written':'STDOUT_HAS_NO_PUBLICATION_AUTHORITY','sha256':'0'*64,'bytes':999}))"
SUMMARY = 'Created result.txt containing exactly 46 and read it back.'

WRAPPER = r'''
from pathlib import Path
from unittest.mock import patch
from desktop_pet.agents import execution_runtime as host
import project_runtime,mxc_execution,mxc_native
from nanobot.providers.openai_compat_provider import OpenAICompatProvider
from nanobot.providers.base import ToolCallRequest
proof=Path(__PROOF__)
state={'native':[],'payload_started':False,'write_calls':0,'late_call_appended':False,'finished':False}
def save():
    temporary=proof.with_suffix('.tmp');temporary.write_text(json.dumps(state),encoding='utf-8');temporary.replace(proof)
with patch.object(host,'load_execution_runtime',return_value=host._fixed_configuration({'engine':'mxc','enabled':True})):
    snapshot=host.capture_execution_runtime()
host.capture_execution_runtime=lambda:snapshot
actual=mxc_native.run
mode=__MODE__
def transport(argv,**kwargs):
    if mode in {'lost_ack','native_unknown','projection_failure','payload_failed'}:
        kwargs['launch_attempt_callback']()
        with kwargs['authority_guard']():kwargs['before_resume']()
        if argv[1:]==['--version']:
            value=mxc_native.NativeResult('MXC_EXITED',0,mxc_execution.CODEX_VERSION.encode(),limits_verified=True)
        else:
            (kwargs['cwd'].parent/'task/result.txt').write_bytes(b'46')
            lease=mxc_native.retain_failed_close(object()) if mode=='native_unknown' else None
            value=mxc_native.NativeResult('MXC_EXITED',1 if mode=='payload_failed' else 0,b'{}',limits_verified=True,
                resources_closed=lease is None,_cleanup_lease=lease)
    else:
        watch=kwargs['watch']
        def observed():
            if (kwargs['cwd'].parent/'task/started').is_file() and not state['payload_started']:
                state['payload_started']=True;save()
            return watch()
        kwargs['watch']=observed
        value=actual(argv,**kwargs)
    state['native'].append({'code':value.code,'tree_zero':value.tree_reaped,
        'resources_closed':value.resources_closed,'limits':value.limits_verified,'resumed':value.resumed})
    save();return value
mxc_native.run=transport
if mode in {'lost_ack','native_unknown','projection_failure','payload_failed'}:
    mxc_execution.MxcPythonRunner._pins=lambda self:()
    chat=OpenAICompatProvider.chat_stream
    async def append_same_batch(self,*args,**kwargs):
        result=await chat(self,*args,**kwargs)
        if mode!='payload_failed' and any(call.name=='compute_project_artifact' for call in result.tool_calls):
            result.tool_calls.append(ToolCallRequest(id='owned-late-write',name='write_file',arguments={'path':'late.txt','content':'FORBIDDEN'}))
            state['late_call_appended']=True;save()
        return result
    OpenAICompatProvider.chat_stream=append_same_batch
if mode=='lost_ack':
    write=worker.WorkspaceFiles.write_bytes
    def lost_ack(self,path,*args,**kwargs):
        if path=='result.txt':
            state['write_calls']+=1;save();write(self,path,*args,**kwargs)
            raise OSError('SYNTHETIC_PRIVATE_WRITE_ACK')
        return write(self,path,*args,**kwargs)
    worker.WorkspaceFiles.write_bytes=lost_ack
if mode=='projection_failure':
    def failed_projection(*args,**kwargs):raise OSError('SYNTHETIC_PRIVATE_PROJECTION')
    project_runtime.ProjectArtifactResult=failed_projection
save()
try:asyncio.run(worker.main())
finally:
    state['finished']=True
    state['host_connected']=worker._host_connected.is_set()
    state['global_native_pending']=mxc_native.has_pending_cleanup()
    runtime=worker._running_project
    state['runtime_unknown']=runtime._has_unknown() if runtime else False
    state['native_attempt_count']=sum(len(entry.attempts) for entry in runtime._entries.values()) if runtime else 0
    save()
'''


@unittest.skipUnless(os.name=='nt' and fixture.PYTHON.is_file(),'actual Windows isolated SDK required')
class ProjectParentWorkerTests(unittest.TestCase):
    setUp = fixture.ResearchParentWorkerTests.setUp
    tearDown = fixture.ResearchParentWorkerTests.tearDown
    proofs = {}

    def start_project(self,mode='native',*,enabled=True,authority=True,action=None,code_override=None,file_grants=()):
        (self.workspace/'source.txt').write_bytes(b'17 29')
        digest=hashlib.sha256(b'17 29').hexdigest()
        calls={'computed':False}
        def scripted(body):
            names=[row.get('name') for row in body['messages'] if row.get('role')=='tool']
            advertised=[row['function']['name'] for row in body.get('tools',[])]
            if not enabled:
                self.assertNotIn('compute_project_artifact',advertised)
                if 'read_file' not in names:return 'read_file',{'path':'source.txt'}
                if 'finish_task' not in names:return 'finish_task',{'status':'completed','summary':'Read source.txt containing 17 and 29.'}
                return 'Read source.txt containing 17 and 29.'
            self.assertIn('compute_project_artifact',advertised)
            schema=next(row['function']['parameters'] for row in body['tools'] if row['function']['name']=='compute_project_artifact')
            self.assertEqual({'code','input_json','inputs','output'},set(schema['properties']))
            if 'read_file' not in names:return 'read_file',{'path':'source.txt'}
            if 'compute_project_artifact' not in names:
                code=code_override or FIXED_CODE
                if mode=='eof':
                    code="import pathlib,time\n(pathlib.Path(OUTPUT_DIR)/'started').write_text('FIXED_READY')\ntime.sleep(10)\n(pathlib.Path(OUTPUT_DIR)/'result.txt').write_text('46')\nprint('{}')"
                calls['computed']=True
                return 'compute_project_artifact',{'code':code,'inputs':[{'path':'source.txt','expected_sha256':digest}],
                    'output':{'path':'result.txt','target':'result.txt'}}
            if names.count('read_file')<2:return 'read_file',{'path':'result.txt'}
            if 'verify_artifact' not in names:return 'verify_artifact',{'path':'result.txt','kind':'text'}
            if 'finish_task' not in names:return 'finish_task',{'status':'completed','summary':SUMMARY}
            return SUMMARY
        model=fixture.SeparateGoalModel([action or scripted]*30,judge_mode='optimistic')
        self.models.append(model)
        proof=self.root/('proof_'+uuid4().hex+'.json')
        wrapper=self.root/('project_worker_'+uuid4().hex+'.py')
        source="import sys,asyncio,json\nsys.path.insert(0,"+repr(str(fixture.REPO))+ ")\nsys.path.insert(0,"+repr(str(fixture.REPO/'agent_backend/nanobot'))+")\nimport worker\n"
        if enabled:source+=WRAPPER.replace('__PROOF__',repr(str(proof))).replace('__MODE__',repr(mode))
        else:source+='asyncio.run(worker.main())\n'
        wrapper.write_text(source,encoding='utf-8')
        client=fixture.Client(worker_path=wrapper);self.clients.append(client)
        identity,cid=str(uuid4()),str(uuid4())
        payload={'type':'run','id':identity,'conversation_id':cid,'kind':'agent','context':'',
            'task':('Read source.txt and report its two numbers.' if not enabled else
                'Create result.txt containing exactly 46 from the numbers in source.txt. Read back and verify the actual file before completion.'),
            'config':{'base_url':model.base_url,'model':'owned-project-planner','api_key':'SYNTHETIC_PROJECT_PARENT_SECRET'},
            'options':{'workspace':str(self.workspace),'tools_enabled':True,'computer_enabled':False,
                'checkpoint_required':False,'include_activity':True,'max_steps':0,'timeout_seconds':25,
                'file_grants':list(file_grants)}}
        if enabled:
            with patch.object(host,'load_execution_runtime',return_value=host._fixed_configuration({'engine':'mxc','enabled':True})):
                snapshot=host.capture_execution_runtime()
            payload['config'].update(project_execution=snapshot.configuration,project_execution_identity=snapshot.identity)
            if authority:
                payload['execution_authority']={'version':1,'conversation_id':cid,'generation':str(uuid4()),
                    'request_id':identity,'settings_scope':'e'*64}
        client.send(payload)
        return client,model,proof

    def finished_proof(self,client,path):
        client.process.wait(timeout=8)
        value=json.loads(path.read_text(encoding='utf-8'))
        self.assertTrue(value['finished'])
        self.proofs[self._testMethodName] = value
        return value

    def test_actual_parent_fixed_python_artifact_readback_goal_witness_and_real_native_closure(self):
        client,model,proof=self.start_project(code_override=FIXED_CODE+'\n#'+('bounded_fixture_comment_'*1200))
        result=client.terminal(timeout=40)
        self.assertEqual('completed',result.get('status'),result)
        self.assertEqual(SUMMARY,result['reply']);self.assertTrue(result['acceptance']['passed'])
        self.assertEqual(b'46',(self.workspace/'result.txt').read_bytes())
        self.assertEqual(1,len(model.judge_calls))
        snapshot=json.loads(model.judge_calls[0]['messages'][-1]['content'])
        observations=snapshot['facts']['observed']['observations']
        written=[row for row in observations if row['tool']=='compute_project_artifact']
        self.assertEqual(1,len(written));self.assertEqual('result.txt',written[0]['file_write']['path'])
        self.assertTrue(written[0]['truncated'],'Actual large code arguments must keep only the host write witness')
        self.assertEqual(hashlib.sha256(b'46').hexdigest(),written[0]['file_write']['sha256'])
        self.assertFalse(any(row['tool']=='write_file' for row in observations))
        body=next(body for body in model.planner_calls if any(row.get('name')=='compute_project_artifact' for row in body['messages']))
        receipt=json.loads(next(row['content'] for row in body['messages'] if row.get('name')=='compute_project_artifact'))
        self.assertEqual('result.txt',receipt['written']);self.assertFalse(receipt['task_verified'])
        self.assertNotIn('STDOUT_HAS_NO_PUBLICATION_AUTHORITY',json.dumps(receipt))
        state=self.finished_proof(client,proof)
        self.assertEqual(2,len(state['native']));self.assertEqual(2,state['native_attempt_count'])
        self.assertTrue(all(row['tree_zero'] and row['resources_closed'] and row['limits'] for row in state['native']))
        self.assertFalse(state['global_native_pending'] or state['runtime_unknown'])
        self.assertNotIn('SYNTHETIC_PROJECT_PARENT_SECRET',json.dumps(client.all_frames))

    def test_missing_machine_configuration_does_not_register_project_but_text_file_chat_works(self):
        client,model,_=self.start_project(enabled=False)
        result=client.terminal()
        self.assertEqual('completed',result.get('status'),result)
        self.assertFalse((self.workspace/'result.txt').exists())
        self.assertFalse(any(row['function']['name']=='compute_project_artifact' for row in model.planner_calls[0]['tools']))

    def test_enabled_wire_without_host_authority_is_rejected_before_any_model_or_native_call(self):
        client,model,proof=self.start_project(authority=False)
        result=client.terminal();self.assertEqual('INVALID_REQUEST',result.get('code'),result)
        self.assertEqual([],model.planner_calls)
        self.assertEqual([],self.finished_proof(client,proof)['native'])

    def test_actual_parent_stdin_eof_stops_native_and_forbids_late_publication(self):
        client,model,proof=self.start_project(mode='eof')
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            if proof.exists() and json.loads(proof.read_text())['payload_started']:break
            time.sleep(.01)
        else:self.fail('fixed native payload never reached its controlled wait')
        started=time.monotonic();client.process.stdin.close()
        result=client.terminal(timeout=10)
        self.assertEqual('cancelled',result.get('status'),result)
        self.assertLess(time.monotonic()-started,3)
        self.assertFalse((self.workspace/'result.txt').exists())
        self.assertEqual(2,len(model.planner_calls));self.assertEqual([],model.judge_calls)
        state=self.finished_proof(client,proof)
        self.assertFalse(state['host_connected']);self.assertEqual(2,len(state['native']))
        self.assertEqual('MXC_CANCELLED',state['native'][-1]['code'])
        self.assertTrue(all(row['tree_zero'] and row['resources_closed'] for row in state['native']))
        self.assertFalse(state['global_native_pending'] or state['runtime_unknown'])

    def test_write_committed_lost_ack_blocks_same_batch_effect_finish_judge_and_retry(self):
        client,model,proof=self.start_project(mode='lost_ack')
        result=client.terminal(timeout=35)
        self.assertEqual('needs_user',result.get('status'),result)
        self.assertIn('PROJECT_PUBLICATION_UNCONFIRMED',result['acceptance']['issue_codes'])
        self.assertFalse(result['acceptance']['passed']);self.assertEqual([],model.judge_calls)
        self.assertEqual(b'46',(self.workspace/'result.txt').read_bytes())
        self.assertFalse((self.workspace/'late.txt').exists())
        self.assertEqual(2,len(model.planner_calls))
        state=self.finished_proof(client,proof)
        self.assertTrue(state['late_call_appended']);self.assertEqual(1,state['write_calls'])
        self.assertTrue(state['runtime_unknown']);self.assertFalse(state['global_native_pending'])
        self.assertNotIn('SYNTHETIC_PRIVATE_WRITE_ACK',json.dumps(client.all_frames))

    def test_unknown_native_resources_block_same_batch_effects_and_remain_owned(self):
        client,model,proof=self.start_project(mode='native_unknown')
        result=client.terminal(timeout=35)
        self.assertEqual('needs_user',result.get('status'),result)
        self.assertIn('PROJECT_EXECUTION_CLEANUP_UNCONFIRMED',result['acceptance']['issue_codes'])
        self.assertFalse((self.workspace/'result.txt').exists())
        self.assertFalse((self.workspace/'late.txt').exists())
        self.assertEqual(2,len(model.planner_calls));self.assertEqual([],model.judge_calls)
        state=self.finished_proof(client,proof)
        self.assertTrue(state['runtime_unknown'] and state['global_native_pending'])
        self.assertTrue(state['late_call_appended'])

    def test_post_commit_projection_failure_is_unknown_without_a_second_effect(self):
        client,model,proof=self.start_project(mode='projection_failure')
        result=client.terminal(timeout=35)
        self.assertEqual('needs_user',result.get('status'),result)
        self.assertIn('PROJECT_PUBLICATION_UNCONFIRMED',result['acceptance']['issue_codes'])
        self.assertEqual(b'46',(self.workspace/'result.txt').read_bytes())
        self.assertFalse((self.workspace/'late.txt').exists())
        self.assertEqual(2,len(model.planner_calls));self.assertEqual([],model.judge_calls)
        state=self.finished_proof(client,proof)
        self.assertTrue(state['runtime_unknown']);self.assertFalse(state['global_native_pending'])
        self.assertNotIn('SYNTHETIC_PRIVATE_PROJECTION',json.dumps(client.all_frames))

    def fallback_route(self):
        digest=hashlib.sha256(b'17 29').hexdigest()
        def route(body):
            names=[row.get('name') for row in body['messages'] if row.get('role')=='tool']
            if 'read_file' not in names:return 'read_file',{'path':'source.txt'}
            if 'compute_project_artifact' not in names:
                return 'compute_project_artifact',{'code':FIXED_CODE,'inputs':[{'path':'source.txt','expected_sha256':digest}],
                    'output':{'path':'result.txt','target':'result.txt'}}
            if 'write_file' not in names:return 'write_file',{'path':'result.txt','content':'46'}
            if names.count('read_file')<2:return 'read_file',{'path':'result.txt'}
            if 'finish_task' not in names:return 'finish_task',{'status':'completed','summary':SUMMARY}
            return SUMMARY
        return route

    def test_initial_isolation_scope_conflict_keeps_normal_file_route_usable_without_native_launch(self):
        trusted_source=fixture.REPO/'agent_backend/nanobot/project_runtime.py'
        client,model,proof=self.start_project(action=self.fallback_route(),file_grants=[{
            'path':str(trusted_source),'access':'read','directory':False}])
        result=client.terminal(timeout=35)
        self.assertEqual('completed',result.get('status'),result)
        self.assertEqual(b'46',(self.workspace/'result.txt').read_bytes())
        self.assertEqual([],self.finished_proof(client,proof)['native'])
        self.assertEqual(1,len(model.judge_calls));self.assertTrue(result['acceptance']['passed'])

    def test_confirmed_failed_payload_with_no_publication_allows_an_independently_verified_file_route(self):
        client,model,proof=self.start_project(mode='payload_failed',action=self.fallback_route())
        result=client.terminal(timeout=35)
        self.assertEqual('completed',result.get('status'),result)
        self.assertEqual(b'46',(self.workspace/'result.txt').read_bytes())
        state=self.finished_proof(client,proof)
        self.assertEqual(2,len(state['native']));self.assertFalse(state['runtime_unknown'])
        self.assertTrue(result['acceptance']['passed']);self.assertEqual(1,len(model.judge_calls))


if __name__=='__main__':unittest.main()
