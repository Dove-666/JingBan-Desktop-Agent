"""Authenticated RPC -> durable queue -> actual SDK/loopback execution.

Owned temporary data and synthetic localhost model replies only. These test the
host control chain; they are not added to real-model capability success rates.
"""
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QApplication

from desktop_pet.agents.coordinator import operation_identity
from desktop_pet.agents.service import dsh_installation, node_executable, _DSH_VERSION
from desktop_pet.settings_manager import ApiSettings
from tests import test_chat_images as fixture
from tests.test_remote_controller import PEER, spin_until

A='Create a.txt containing exactly "A".'
B='Create b.txt containing exactly "B". QUEUED_FUTURE_B_SENTINEL_3291.'
AREPLY='Created a.txt with actual content A.'
BREPLY='Created b.txt after the earlier task completed.'
REPO=Path(__file__).resolve().parents[1]


class DshQueueModel:
    """Script only the advertised DSH tool protocol over a local HTTP stream."""
    def __init__(self,steps):
        self.steps=list(steps)
        self.planner_calls,self.errors=[],[]
        owner=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*_):pass
            def do_POST(self):
                try:
                    if self.path!='/v1/chat/completions':raise AssertionError('Unexpected fixture route')
                    if self.headers.get('Authorization')!='Bearer synthetic-owned-queue-key':
                        raise AssertionError('Unexpected fixture credential')
                    body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    index=len(owner.planner_calls)
                    owner.planner_calls.append(body)
                    if index>=len(owner.steps):raise AssertionError('Unplanned model request')
                    step=owner.steps[index]
                    step=step(body) if callable(step) else step
                    message={'role':'assistant','content':None if isinstance(step,tuple) else str(step)}
                    if isinstance(step,tuple):
                        name,arguments=step
                        names={tool['function']['name'] for tool in body.get('tools',[])}
                        if name not in names:raise AssertionError(f'Tool not advertised by DSH: {name}')
                        message['tool_calls']=[{'id':f'queue-{index}','type':'function','function':{
                            'name':name,'arguments':json.dumps(arguments)}}]
                    reason='tool_calls' if isinstance(step,tuple) else 'stop'
                    if body.get('stream'):
                        delta=dict(message)
                        if 'tool_calls' in delta:delta['tool_calls']=[{'index':0,**delta['tool_calls'][0]}]
                        chunks=[{'id':'queue-fixture','object':'chat.completion.chunk','created':1,
                            'model':'offline-owned-queue','choices':[{'index':0,'delta':delta,'finish_reason':None}]},
                            {'id':'queue-fixture','object':'chat.completion.chunk','created':1,
                            'model':'offline-owned-queue','choices':[{'index':0,'delta':{},'finish_reason':reason}]}]
                        payload=(''.join('data: '+json.dumps(chunk)+'\n\n' for chunk in chunks)+'data: [DONE]\n\n').encode()
                        content_type='text/event-stream'
                    else:
                        payload=json.dumps({'id':'queue-fixture','object':'chat.completion','created':1,
                            'model':'offline-owned-queue','choices':[{'index':0,'message':message,'finish_reason':reason}]}).encode()
                        content_type='application/json'
                    self.send_response(200)
                except Exception as exc:
                    owner.errors.append(type(exc).__name__+': '+str(exc))
                    self.send_response(500)
                    payload=b'{"error":{"message":"Queue fixture assertion failed"}}'
                    content_type='application/json'
                self.send_header('Content-Type',content_type)
                self.send_header('Content-Length',str(len(payload)))
                self.send_header('Connection','close')
                try:
                    self.end_headers()
                    self.wfile.write(payload)
                except ConnectionError:pass
                self.close_connection=True
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.server.daemon_threads=True
        self.server.block_on_close=False
        self.thread=threading.Thread(target=self.server.serve_forever,kwargs={'poll_interval':.02},daemon=True)
        self.thread.start()
        self.base_url=f'http://127.0.0.1:{self.server.server_port}/v1'

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


@unittest.skipUnless(node_executable() is not None and all(value==_DSH_VERSION for value in dsh_installation().values()),
                     'Pinned official DSH runtime is not installed')
class RemoteAgentQueueTests(unittest.TestCase):
    setUpClass=classmethod(fixture.ChatImageTests.setUpClass.__func__)
    rpc=fixture.ChatImageTests.rpc

    def setUp(self):
        fixture.ChatImageTests.setUp(self)
        root=REPO/'agent_workspace'
        root.mkdir(exist_ok=True)
        self.owned=tempfile.TemporaryDirectory(prefix='remote-queue-sdk-',dir=root)
        self.workspace=Path(self.owned.name)
        self.models=[]
        self.chat._metadata_factory=None
        workspace_scope=patch('desktop_pet.agents.settings.agent_workspace_root',lambda:self.workspace)
        workspace_scope.start()
        self.addCleanup(workspace_scope.stop)

    def tearDown(self):
        fixture.ChatImageTests.tearDown(self)
        for model in self.models:
            model.close()
            self.assertEqual(model.errors,[])
        self.owned.cleanup()

    def configure(self,steps):
        model=DshQueueModel(steps)
        self.models.append(model)
        self.settings.save_api_settings(ApiSettings(model.base_url,'offline-owned-queue',10))
        self.settings.save_api_key('synthetic-owned-queue-key')
        self.settings.save_agent_settings(replace(self.settings.load_agent_settings(),enabled=True,
            computer_enabled=False,approval_mode='full',workspace=str(self.workspace),timeout_seconds=25,max_steps=0))
        self.chat.refresh_agent_settings()
        return model

    def send(self,text,request_id=None):
        response=self.rpc('agent.send',{'conversation_id':self.cid,'text':text},request_id=request_id or str(uuid4()))
        self.assertTrue(response['ok'],response)
        self.assertTrue(response['result']['accepted'])
        return response['result']

    def job(self,ack): return self.chat._agent_queue.queue.get(ack['job_id'])

    def idle(self):
        spin_until(lambda:not self.chat.busy and not QThreadPool.globalInstance().activeThreadCount(),timeout=15)

    def test_busy_ack_is_durable_and_a_b_run_serially_without_future_task_context(self):
        def first_a(body):
            self.assertNotIn('QUEUED_FUTURE_B_SENTINEL_3291',json.dumps(body))
            return ('write',{'file_path':'a.txt','content':'A'})
        def first_b(body):
            self.assertIn(AREPLY,json.dumps(body))
            self.assertTrue((self.workspace/'a.txt').is_file())
            return ('write',{'file_path':'b.txt','content':'B'})
        model=self.configure([first_a,('read',{'file_path':'a.txt'}),AREPLY,
            first_b,('read',{'file_path':'b.txt'}),BREPLY])
        self.chat._set_busy(True)
        first,second=self.send(A),self.send(B)
        self.assertEqual((self.job(first).state,self.job(second).state),('queued','queued'))
        self.assertEqual(model.planner_calls,[])
        for ack,text in ((first,A),(second,B)):
            row=self.chat.store.read_message(self.cid,ack['message_id'])
            self.assertEqual(row['content'],text)
            self.assertEqual(row['metadata']['request_status'],'queued')
            self.assertNotIn('agent_execution_started',row['metadata'])
        self.chat._set_busy(False)
        spin_until(lambda:self.job(first).state=='completed' and self.job(second).state=='completed',timeout=25)
        self.idle()
        self.assertEqual(len(model.planner_calls),6)
        self.assertEqual((self.workspace/'a.txt').read_text(),'A')
        self.assertEqual((self.workspace/'b.txt').read_text(),'B')
        replies=[row['content'] for row in self.chat.store.load_messages(self.cid) if row['role']=='assistant']
        self.assertEqual(replies,[AREPLY,BREPLY])
        self.assertTrue(self.job(first).worker_closed_at is not None)
        self.assertTrue(self.job(second).worker_closed_at is not None)

    def test_queued_cancel_preserves_original_text_and_never_starts_worker(self):
        model=self.configure([])
        self.chat._set_busy(True)
        ack=self.send(A)
        response=self.rpc('agent.cancel',{'conversation_id':self.cid})
        self.assertTrue(response['result']['cancelled'])
        self.assertEqual(self.job(ack).state,'cancelled')
        row=self.chat.store.read_message(self.cid,ack['message_id'])
        self.assertEqual(row['content'],A)
        self.assertEqual(row['metadata']['request_status'],'cancelled')
        self.chat._set_busy(False)
        QApplication.processEvents()
        self.assertEqual(model.planner_calls,[])
        self.assertEqual(self.chat._agent_service._processes,{})

    def test_revoke_and_same_peer_repair_cannot_revive_queued_authority(self):
        model=self.configure([])
        self.chat._set_busy(True)
        request=str(uuid4())
        ack=self.send(A,request)
        self.assertTrue(self.remote.revoke_device(PEER))
        self.remote.preferences.add_device(PEER,'paired again','same timestamp')
        self.chat._set_busy(False)
        spin_until(lambda:self.job(ack).state=='blocked',timeout=5)
        self.assertEqual(model.planner_calls,[])
        self.assertFalse((self.workspace/'a.txt').exists())
        self.assertNotIn('agent_execution_started',self.chat.store.read_message(self.cid,ack['message_id'])['metadata'])

    def test_active_stop_also_cancels_waiting_tasks_and_late_planner_cannot_execute(self):
        entered,release=threading.Event(),threading.Event()
        def pending(_):
            entered.set()
            if not release.wait(10): raise AssertionError('owned planner was never released')
            return ('write',{'file_path':'a.txt','content':'A'})
        model=self.configure([pending])
        first=self.send(A)
        try:
            spin_until(entered.is_set,timeout=10)
            second=self.send(B)
            response=self.rpc('agent.cancel',{'conversation_id':self.cid})
            self.assertTrue(response['result']['active_cancelled'])
            self.assertEqual(response['result']['queued_cancelled_count'],1)
            release.set()
            spin_until(lambda:self.job(first).state=='cancelled',timeout=10)
            self.idle()
            self.assertEqual(self.job(second).state,'cancelled')
            self.assertIsNotNone(self.job(first).worker_closed_at)
            self.assertEqual(len(model.planner_calls),1)
            self.assertFalse((self.workspace/'a.txt').exists())
            rows=self.chat.store.load_messages(self.cid)
            self.assertEqual([row['content'] for row in rows],[A,B])
        finally:release.set()

    def test_too_late_stop_does_not_claim_active_commit_stopped_but_cancels_queue(self):
        entered,release=threading.Event(),threading.Event()
        model=self.configure([('write',{'file_path':'a.txt','content':'A'}),('read',{'file_path':'a.txt'}),AREPLY])
        original=self.chat.store.complete_request
        def committing(*args,**kwargs):
            entered.set()
            if not release.wait(10):raise AssertionError('owned reply COMMIT was never released')
            return original(*args,**kwargs)
        with patch.object(self.chat.store,'complete_request',side_effect=committing):
            first=self.send(A)
            try:
                spin_until(entered.is_set,timeout=15)
                second=self.send(B)
                result=self.rpc('agent.cancel',{'conversation_id':self.cid})['result']
                self.assertFalse(result['active_cancelled'])
                self.assertEqual(result['queued_cancelled_count'],1)
                release.set()
                spin_until(lambda:self.job(first).state=='completed',timeout=10)
                self.idle()
                self.assertEqual(self.job(second).state,'cancelled')
                self.assertEqual(len(model.planner_calls),3)
                self.assertEqual(self.chat.store.load_messages(self.cid)[-1]['content'],AREPLY)
            finally:release.set()

    def test_cancel_after_partial_work_preserves_files_without_completed_queue_or_reply(self):
        # DSH owns completion; the former nanobot finish_task receipt is retired.
        # Exercise a real partial execution followed by explicit cancellation.
        entered,release=threading.Event(),threading.Event()
        def pending_after_read(body):
            self.assertEqual((self.workspace/'a.txt').read_text(),'A')
            self.assertEqual(body['messages'][-1]['role'],'tool')
            self.assertIn('A',body['messages'][-1]['content'])
            entered.set()
            if not release.wait(10):raise AssertionError('owned partial planner was never released')
            return ('write',{'file_path':'missing.txt','content':'M'})
        model=self.configure([('write',{'file_path':'a.txt','content':'A'}),
            ('read',{'file_path':'a.txt'}),pending_after_read])
        task=A+' Also create missing.txt containing exactly "M".'
        ack=self.send(task)
        try:
            spin_until(entered.is_set,timeout=15)
            self.assertEqual(self.job(ack).state,'running')
            result=self.rpc('agent.cancel',{'conversation_id':self.cid})['result']
            self.assertTrue(result['active_cancelled'])
            release.set()
            spin_until(lambda:self.job(ack).state=='cancelled',timeout=10)
            self.idle()
            job=self.job(ack)
            self.assertIsNotNone(job.worker_closed_at)
            self.assertIsNone(job.reply_id)
            self.assertFalse(self.chat._agent_queue.queue.has_unclosed_worker())
            self.assertEqual((self.workspace/'a.txt').read_text(),'A')
            self.assertFalse((self.workspace/'missing.txt').exists())
            self.assertEqual([row['content'] for row in self.chat.store.load_messages(self.cid)],[task])
            self.assertEqual(len(model.planner_calls),3)
        finally:release.set()

    def test_clear_revokes_old_generation_and_new_task_does_not_resurrect_old_ids(self):
        model=self.configure([])
        old=self.send(A)
        old_generation=self.job(old).generation
        self.chat.clear()  # Queue ACK happened, dispatch wake has not run yet.
        spin_until(lambda:not self.chat.history_loading and not QThreadPool.globalInstance().activeThreadCount(),timeout=8)
        self.assertEqual(self.job(old).state,'cancelled')
        self.assertIsNone(self.chat.store.read_message(self.cid,old['message_id']))
        self.chat._set_busy(True)
        new=self.send(B)
        self.assertNotEqual(self.job(new).generation,old_generation)
        self.assertEqual(model.planner_calls,[])
        self.assertNotEqual(new['message_id'],old['message_id'])

    def test_new_phone_cid_can_enroll_and_execute_without_switching_desktop_selection(self):
        model=self.configure([('write',{'file_path':'a.txt','content':'A'}),('read',{'file_path':'a.txt'}),AREPLY])
        selected=self.chat.current_conversation_id
        phone_cid=str(uuid4())
        self.assertNotEqual(phone_cid,selected)
        self.assertIsNone(self.chat.store.get_conversation(phone_cid))
        response=self.rpc('agent.send',{'conversation_id':phone_cid,'text':A})
        self.assertTrue(response['ok'],response)
        ack=response['result']
        spin_until(lambda:self.job(ack).state=='completed',timeout=15)
        self.idle()
        self.assertEqual(self.chat.current_conversation_id,selected)
        self.assertEqual(self.chat.store.load_messages(phone_cid)[-1]['content'],AREPLY)
        self.assertEqual((self.workspace/'a.txt').read_text(),'A')
        self.assertEqual(len(model.planner_calls),3)

    def test_deleted_phone_cid_cannot_be_recreated_without_in_memory_deleted_set(self):
        model=self.configure([])
        deleted=str(uuid4())
        self.chat.store.append_message(deleted,'user','Synthetic prior user text','user')
        self.chat.store.delete_conversation(deleted)
        self.assertNotIn(deleted,self.chat._deleted_conversations)
        response=self.rpc('agent.send',{'conversation_id':deleted,'text':A})
        self.assertFalse(response['ok'])
        self.assertIsNone(self.chat.store.get_conversation(deleted))
        self.assertEqual(self.chat._agent_queue.queue.list_jobs(conversation_id=deleted),())
        self.assertEqual(model.planner_calls,[])

    def test_startup_unknown_barrier_blocks_desktop_agent_but_allows_ordinary_api_chat(self):
        model=self.configure([json.dumps({'reply':'普通聊天仍可用','emotion':'neutral'},ensure_ascii=False)])
        self.chat._set_busy(True)
        ack=self.send(A)
        coordinator=self.chat._agent_queue
        claim=coordinator.queue.claim()
        self.assertTrue(coordinator.queue.begin_dispatch(claim.lease,revalidate=coordinator._scope).accepted)
        coordinator.queue.recover_after_restart()
        self.assertEqual(self.job(ack).state,'unknown')
        self.chat._set_busy(False)
        self.assertTrue(self.chat.send('Read system information.',agent_mode=True))
        self.idle()
        self.assertEqual(model.planner_calls,[])
        self.assertEqual(self.chat._agent_service._processes,{})
        self.assertTrue(self.chat.send('只是普通问候，请回答你好。'))
        self.idle()
        self.assertEqual(len(model.planner_calls),1)
        self.assertEqual(self.chat.store.load_messages(self.cid)[-1]['content'],'普通聊天仍可用')
        self.assertEqual(self.job(ack).state,'unknown')


if __name__=='__main__':unittest.main()
