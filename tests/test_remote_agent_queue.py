"""Authenticated RPC -> durable queue -> actual SDK/loopback execution.

Owned temporary data and synthetic localhost model replies only. These test the
host control chain; they are not added to real-model capability success rates.
"""
from dataclasses import replace
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

from agent_backend.nanobot.tests.test_goal_contract_worker import SeparateGoalModel
from agent_backend.nanobot.tests.test_worker import PYTHON, REPO
from desktop_pet.agents.coordinator import operation_identity
from desktop_pet.settings_manager import ApiSettings
from tests import test_chat_images as fixture
from tests.test_remote_controller import PEER, spin_until

A='Create a.txt containing exactly "A".'
B='Create b.txt containing exactly "B". QUEUED_FUTURE_B_SENTINEL_3291.'
AREPLY='Created a.txt with actual content A.'
BREPLY='Created b.txt after the earlier task completed.'


@unittest.skipUnless(PYTHON.is_file(),"isolated SDK unavailable")
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

    def tearDown(self):
        fixture.ChatImageTests.tearDown(self)
        for model in self.models:
            model.close()
            self.assertEqual(model.errors,[])
        self.owned.cleanup()

    def configure(self,steps):
        model=SeparateGoalModel(steps,judge_mode='optimistic')
        self.models.append(model)
        self.settings.save_api_settings(ApiSettings(model.base_url,'offline-owned-queue',10))
        self.settings.save_api_key('synthetic-owned-queue-key')
        self.settings.save_agent_settings(replace(self.settings.load_agent_settings(),enabled=True,
            computer_enabled=False,python_executable=str(PYTHON),workspace=str(self.workspace),timeout_seconds=25,max_steps=0))
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
            return ('write_file',{'path':'a.txt','content':'A'})
        def first_b(body):
            self.assertIn(AREPLY,json.dumps(body))
            self.assertTrue((self.workspace/'a.txt').is_file())
            return ('write_file',{'path':'b.txt','content':'B'})
        model=self.configure([first_a,('read_file',{'path':'a.txt'}),
            ('finish_task',{'status':'completed','summary':AREPLY}),'Unchecked rewrite A',
            first_b,('read_file',{'path':'b.txt'}),
            ('finish_task',{'status':'completed','summary':BREPLY}),'Unchecked rewrite B'])
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
        self.assertEqual(len(model.planner_calls),8)
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
            return ('write_file',{'path':'a.txt','content':'A'})
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
        model=self.configure([('write_file',{'path':'a.txt','content':'A'}),('read_file',{'path':'a.txt'}),
            ('finish_task',{'status':'completed','summary':AREPLY}),'Unchecked final'])
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
                self.assertEqual(len(model.planner_calls),4)
                self.assertEqual(self.chat.store.load_messages(self.cid)[-1]['content'],AREPLY)
            finally:release.set()

    def test_partial_goal_is_saved_but_queue_never_marks_it_completed(self):
        partial='未完成：仅创建并回读了 a.txt，missing.txt 尚未创建。'
        def report_incomplete(body):
            reports=[json.loads(message['content']) for message in body['messages']
                     if message.get('role')=='tool' and '"accepted"' in message.get('content','')]
            self.assertTrue(reports)
            self.assertIs(reports[-1]['accepted'],False)
            self.assertIn('GOAL_REQUIREMENT_FAILED',reports[-1]['issues'])
            self.assertIn('HOST OUTCOME CHECK',json.dumps(body))
            return ('finish_task',{'status':'needs_user','summary':partial})
        # A rejected completion and a terminal draft may legitimately trigger
        # repair planning. Script the truthful incomplete exit as well, while
        # retaining the fixture's assertion against any unplanned request.
        model=self.configure([('write_file',{'path':'a.txt','content':'A'}),('read_file',{'path':'a.txt'}),
            ('finish_task',{'status':'completed','summary':'I claim every file exists.'}),
            'Only a.txt was created.',report_incomplete,partial])
        ack=self.send(A+' Also create missing.txt containing exactly "M".')
        spin_until(lambda:self.job(ack).state=='interrupted',timeout=15)
        self.idle()
        job=self.job(ack)
        self.assertIsNotNone(job.worker_closed_at)
        self.assertIsNone(job.reply_id)
        self.assertFalse(self.chat._agent_queue.queue.has_unclosed_worker())
        self.assertEqual((self.workspace/'a.txt').read_text(),'A')
        self.assertFalse((self.workspace/'missing.txt').exists())
        self.assertIn('未',self.chat.store.load_messages(self.cid)[-1]['content'])
        self.assertEqual(len(model.planner_calls),6)

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
        model=self.configure([('write_file',{'path':'a.txt','content':'A'}),('read_file',{'path':'a.txt'}),
            ('finish_task',{'status':'completed','summary':AREPLY}),'Unchecked final'])
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
        self.assertEqual(len(model.planner_calls),4)

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
