"""Independent host policy/config/queue review; production files are read-only.

Only owned configuration/SQLite and scripted localhost fixtures are used. The
actual legacy-SDK check intentionally has no project/native-code permission.
"""
from dataclasses import replace
from contextlib import contextmanager
import io
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from desktop_pet import chat_controller as chat
from desktop_pet.agents import execution_runtime as runtime, service as service_module
from desktop_pet.agents.service import AgentService
from desktop_pet.agents.contracts import AgentError, AgentRequest
from desktop_pet.agents.settings import AgentSettings, AGENT_WORKSPACE_ROOT, DEFAULT_PYTHON
from desktop_pet.agents.staged_input import StagedRunInput
from desktop_pet.settings_manager import ApiSettings
from tests import test_host_queue_review as queue_fixture
from tests.test_research_parent_worker import SeparateGoalModel
from agent_backend.nanobot.tests.test_worker import Client
from tests import test_office_snapshot_independent_review as qt_fixture


class ProjectHostIndependentReviewTests(unittest.TestCase):
    make_coordinator = queue_fixture.HostQueueIndependentReviewTests.make_coordinator
    started = queue_fixture.HostQueueIndependentReviewTests.started
    admit = queue_fixture.HostQueueIndependentReviewTests.admit

    def setUp(self):
        queue_fixture.HostQueueIndependentReviewTests.setUp(self)
        self.private=self.root/'private-policy';self.config=self.private/'runtime.json'
        for name,value in (('_ROOT',self.private),('_CONFIG',self.config)):
            item=patch.object(runtime,name,value);item.start();self.addCleanup(item.stop)
        item=patch.object(chat,'capture_office_runtime',return_value=SimpleNamespace(identity=''))
        item.start();self.addCleanup(item.stop)
        self.cached=SimpleNamespace(_agent_queue_policy=dict(self.policy))
        self.coordinator.configuration=self.current

    def current(self):return chat.ChatController._current_agent_queue_policy(self.cached)

    def write(self,enabled=True,raw=None):
        self.private.mkdir(exist_ok=True)
        self.config.write_text(raw if raw is not None else json.dumps({'engine':'mxc','enabled':enabled}),encoding='utf-8')
        return runtime.capture_execution_runtime()

    def queued(self):
        ack=self.admit();self.coordinator.pump()
        context,request,_=self.starts[-1]
        self.store.set_message_status(self.cid,ack['message_id'],'pending')
        return ack,context,request

    def prepared(self):
        value=runtime.capture_execution_runtime()
        return {**self.policy,'project_execution_identity':value.identity},value

    def reserved(self):
        ack,context,request=self.queued();prepared,value=self.prepared()
        with self.coordinator.reserve_guard(request,context,prepared=prepared):
            self.store.reserve_agent_execution(self.cid,ack['message_id'])
        handle=object();self.coordinator.worker_bound(request,context,handle)
        return ack,context,request,handle,value

    def stage(self,frame):
        pipe=io.BytesIO()
        writer=StagedRunInput(pipe,stop_owned=lambda:None,cancellation_event=threading.Event())
        writer.start();writer.stage(json.dumps(frame).encode('ascii'))
        self.addCleanup(lambda:self.assertTrue(writer.close()))
        return pipe,writer

    def test_missing_to_enabled_between_preparation_and_reservation_never_reserves(self):
        ack,context,request=self.queued();prepared,captured=self.prepared()
        self.assertEqual('',captured.identity);self.write()
        with self.assertRaises(AgentError):
            with self.coordinator.reserve_guard(request,context,prepared=prepared):self.fail('stale prepared scope accepted')
        self.assertNotIn('agent_execution_started',self.store.read_message(self.cid,ack['message_id'])['metadata'])

    def test_removal_after_queued_frame_staging_cannot_commit_lf(self):
        self.write();_,context,request,handle,captured=self.reserved()
        frame={'type':'run','config':{'project_execution':captured.configuration,'project_execution_identity':captured.identity}}
        pipe,writer=self.stage(frame);before=pipe.getvalue();self.config.unlink()
        with self.assertRaises(AgentError):
            with self.coordinator.dispatch_guard(request,context,handle):writer.commit()
        self.assertEqual(before,pipe.getvalue());self.assertNotIn(b'\n',pipe.getvalue())
        self.assertTrue(captured.configuration['enabled'])

    def test_corrupt_policy_does_not_consult_qt_settings_or_preserve_old_authority(self):
        self.write();ack,context,request=self.queued();prepared,_=self.prepared()
        class NoGuiReads:
            def __getattr__(self,name):raise AssertionError('GUI provider read by background policy')
        self.cached._settings_manager=NoGuiReads()
        previous=dict(self.cached._agent_queue_policy)
        self.config.write_text('{"engine":"mxc","enabled":true,"secret":"SYNTHETIC_OWNED_ERROR"}',encoding='utf-8')
        self.assertIsNone(self.current())
        with self.assertRaises(AgentError) as raised:
            with self.coordinator.reserve_guard(request,context,prepared=prepared):self.fail('invalid config accepted')
        self.assertNotIn('SYNTHETIC_OWNED_ERROR',str(raised.exception))
        self.assertEqual(previous,self.cached._agent_queue_policy)
        self.assertNotIn('agent_execution_started',self.store.read_message(self.cid,ack['message_id'])['metadata'])

    def test_same_semantic_rewrite_keeps_immutable_prepared_frame_and_allows_one_lf(self):
        self.write();_,context,request,handle,captured=self.reserved()
        pipe,writer=self.stage({'type':'run','owned':'fixed'})
        self.config.write_text('{\n "enabled":true, "engine":"mxc"\n}',encoding='utf-8')
        self.assertEqual(captured.identity,self.current()['project_execution_identity'])
        with self.coordinator.dispatch_guard(request,context,handle):writer.commit()
        self.assertEqual(1,pipe.getvalue().count(b'\n'))
        self.assertNotIn('project_execution_identity',self.cached._agent_queue_policy)

    def test_cid_and_generation_replacement_cannot_borrow_an_existing_prepared_reservation(self):
        self.write();_,context,request,handle,captured=self.reserved()
        for key,value in (('conversation_id',str(uuid4())),('message_id',str(uuid4()))):
            changed={**request,key:value}
            with self.assertRaises(AgentError):
                with self.coordinator.dispatch_guard(changed,context,handle):self.fail('substituted request accepted')
        self.store.clear_messages(self.cid)
        with self.assertRaises(AgentError):
            with self.coordinator.dispatch_guard(request,context,handle):self.fail('deleted generation accepted')

    def test_raw_dict_uninitialized_or_property_object_does_not_mint_snapshot(self):
        reads=[]
        class Untrusted:
            @property
            def configuration(self):reads.append('configuration');raise AssertionError('SYNTHETIC_RAW_SECRET')
            @property
            def identity(self):reads.append('identity');raise AssertionError('SYNTHETIC_RAW_SECRET')
        forged=object.__new__(runtime.ExecutionRuntimeSnapshot)
        for value in (Untrusted(),forged,{'enabled':True}):
            with self.assertRaises(AgentError) as raised:
                AgentService._context({'api':self.policy['api'],'conversation_id':self.cid,'execution_runtime_snapshot':value},
                    self.policy['key'],AgentRequest('Owned host review','agent'),self.policy['agent'])
            self.assertEqual('execution_runtime_invalid',raised.exception.code)
        self.assertEqual([],reads)


@unittest.skipUnless(os.name=='nt' and Path(DEFAULT_PYTHON).is_file(),'actual Windows SDK required')
class ProjectLegacyHostIndependentTests(unittest.TestCase):
    def test_configured_machine_without_cid_still_runs_ordinary_sdk_files_for_both_enabled_states(self):
        AGENT_WORKSPACE_ROOT.mkdir(exist_ok=True)
        for enabled in (False,True):
            with self.subTest(enabled=enabled),tempfile.TemporaryDirectory(prefix='project-no-cid-review-',dir=AGENT_WORKSPACE_ROOT) as owned:
                root=Path(owned);workspace=root/'workspace';workspace.mkdir();private=root/'private';private.mkdir()
                config=private/'runtime.json';config.write_text(json.dumps({'engine':'mxc','enabled':enabled}),encoding='utf-8')
                source_root=Path(__file__).resolve().parents[1];wrapper=root/'worker.py'
                wrapper.write_text("import sys,asyncio\nfrom pathlib import Path\nsys.path.insert(0,"+repr(str(source_root))+")\n"
                    +"sys.path.insert(0,"+repr(str(source_root/'agent_backend/nanobot'))+")\n"
                    +"from desktop_pet.agents import execution_runtime as host\nhost._ROOT=Path("+repr(str(private))+");host._CONFIG=Path("+repr(str(config))+")\n"
                    +"import worker\nasyncio.run(worker.main())\n",encoding='utf-8')
                summary='Created legacy.txt containing HELLO and read it back.'
                def action(body):
                    self.assertNotIn('compute_project_artifact',[row['function']['name'] for row in body.get('tools',[])])
                    names=[row.get('name') for row in body['messages'] if row.get('role')=='tool']
                    if 'write_file' not in names:return 'write_file',{'path':'legacy.txt','content':'HELLO'}
                    if 'read_file' not in names:return 'read_file',{'path':'legacy.txt'}
                    if 'finish_task' not in names:return 'finish_task',{'status':'completed','summary':summary}
                    return summary
                model=SeparateGoalModel([action]*15,judge_mode='optimistic');service=AgentService();handles=[]
                try:
                    with patch.object(runtime,'_ROOT',private),patch.object(runtime,'_CONFIG',config),patch.object(service_module,'_WORKER_PATH',wrapper):
                        snapshot=runtime.capture_execution_runtime()
                        try:
                            result=service.execute(replace(AgentSettings(),enabled=True,tools_enabled=True,computer_enabled=False,
                                workspace=str(workspace),timeout_seconds=25),'SYNTHETIC_NO_CID_KEY',
                                AgentRequest('Create legacy.txt containing exactly HELLO and read it back.','agent'),
                                {'api':ApiSettings(model.base_url,'owned-legacy-review',5),'messages':[],
                                 'execution_runtime_snapshot':snapshot},worker_lifecycle_callback=handles.append)
                        except AgentError as error:
                            self.fail('Legacy identity-only host frame was rejected: '+error.code+
                                '; model_requests='+str(len(model.planner_calls)))
                    self.assertEqual('completed',result.status)
                    self.assertEqual(b'HELLO',(workspace/'legacy.txt').read_bytes())
                    self.assertTrue(handles and service.validate_worker_closure(handles[0],service.worker_closure(handles[0])))
                finally:service.shutdown();model.close()
                self.assertEqual([],model.errors)

    def test_identity_only_cannot_smuggle_cid_authority_or_invalid_hash_into_project_registration(self):
        AGENT_WORKSPACE_ROOT.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='project-wire-counter-review-',dir=AGENT_WORKSPACE_ROOT) as owned:
            workspace=Path(owned)
            model=SeparateGoalModel([],judge_mode='optimistic')
            try:
                cases=[({'project_execution_identity':'a'*64},{'conversation_id':str(uuid4())}),
                    ({'project_execution_identity':'a'*64},{'execution_authority':None}),
                    ({'project_execution_identity':'a'*64},{'execution_authority':{
                        'version':1,'conversation_id':str(uuid4()),'generation':str(uuid4()),
                        'request_id':str(uuid4()),'settings_scope':'b'*64}})]
                cases += [({'project_execution_identity':bad},{}) for bad in (False,'A'*64,'a'*63,'z'*64)]
                for config,extra in cases:
                    with self.subTest(kind=type(config['project_execution_identity']).__name__,fields=sorted(extra)):
                        client=Client()
                        try:
                            client.send({'type':'run','id':str(uuid4()),'kind':'agent','task':'Read only the owned empty workspace.',
                                'config':{'base_url':model.base_url,'model':'owned-countercase','api_key':'SYNTHETIC_COUNTERCASE_KEY',**config},
                                'options':{'workspace':str(workspace),'tools_enabled':True,'computer_enabled':False,
                                    'max_steps':0,'timeout_seconds':10},**extra})
                            result=client.terminal(timeout=15)
                            self.assertEqual('INVALID_REQUEST',result.get('code'),result)
                        finally:self.assertEqual('',client.close())
                self.assertEqual([],model.planner_calls);self.assertEqual([],model.judge_calls)
                self.assertEqual([],list(workspace.iterdir()))
            finally:model.close()
            self.assertEqual([],model.errors)


class ProjectPrivateGrantIndependentTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(prefix='project-grant-review-')
        self.root=Path(self.temporary.name);self.workspace=self.root/'workspace';self.workspace.mkdir()
        self.private=self.root/'missing-private-policy';self.path=self.private/'runtime.json'
        for name,value in (('_ROOT',self.private),('_CONFIG',self.path)):
            item=patch.object(runtime,name,value);item.start();self.addCleanup(item.stop)
        self.service=AgentService();self.pipe=io.BytesIO();self.thread=None
        self.cid,self.task=str(uuid4()),'owned-private-grant-task'
        self.service._active_control={'conversation_id':self.cid,'task_id':self.task,'scope':'c'*64,
            'workspace':str(self.workspace),'cancel':threading.Event(),'process':SimpleNamespace(stdin=self.pipe)}

    def tearDown(self):
        if self.thread is None or not self.thread.is_alive():self.service.shutdown()
        self.temporary.cleanup()

    def approve(self,path):
        self.service._interaction_broker.open({'id':'owned-private-grant','kind':'file_access','question':'Owned fixed scope review',
            'resource':{'path':str(path),'access':'read','directory':True}},conversation_id=self.cid,task_id=self.task)
        errors=[];answers=[]
        def answer():
            try:answers.append(self.service.answer_interaction(self.cid,'owned-private-grant',{'allowed':True}))
            except BaseException as error:errors.append(error)
        self.thread=threading.Thread(target=answer,daemon=True);self.thread.start();self.thread.join(1)
        self.assertFalse(self.thread.is_alive(),'Private policy guard must not recursively acquire the service plain Lock')
        if errors:raise errors[0]
        self.assertEqual([True],answers)
        return json.loads(self.pipe.getvalue())

    def test_missing_private_policy_without_guardian_denies_ancestor_grant_before_cache_and_delivery(self):
        self.assertIsNone(self.service._guardian_config)
        frame=self.approve(self.root)
        self.assertFalse(frame['answer']['allowed']);self.assertNotIn('grant',frame['answer'])
        self.assertEqual({},self.service._file_grants);self.assertFalse(self.private.exists())

    def test_allowed_unrelated_scope_still_installs_and_delivers_one_read_grant_without_lock_recursion(self):
        allowed=self.root/'allowed';allowed.mkdir()
        frame=self.approve(allowed)
        self.assertTrue(frame['answer']['allowed'])
        self.assertEqual('read',frame['answer']['grant']['access'])
        self.assertEqual(str(allowed.resolve()),frame['answer']['grant']['path'])
        self.assertEqual([frame['answer']['grant']],self.service._file_grants[(self.cid,'c'*64)])
        self.assertFalse(self.private.exists())


class ProjectControllerIndependentReviewTests(unittest.TestCase):
    """Real offscreen controller/SQLite; provider and worker remain local doubles."""
    spin=qt_fixture.OfficeControllerFrameIndependentReviewTests.spin
    controller=qt_fixture.OfficeControllerFrameIndependentReviewTests.controller
    tearDown=qt_fixture.OfficeControllerFrameIndependentReviewTests.tearDown

    @classmethod
    def setUpClass(cls):
        cls.app=qt_fixture.QApplication.instance() or qt_fixture.QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        qt_fixture.OfficeControllerFrameIndependentReviewTests.setUp(self)
        self.execution_private=self.root/'private-project';self.execution_private.mkdir()
        self.execution_path=self.execution_private/'runtime.json'
        self.execution_path.write_text('{"engine":"mxc","enabled":true}',encoding='utf-8')
        self.patches.enter_context(patch.object(runtime,'_ROOT',self.execution_private))
        self.patches.enter_context(patch.object(runtime,'_CONFIG',self.execution_path))

    def test_actual_controller_prepared_context_and_worker_config_use_the_same_opaque_snapshot(self):
        service=qt_fixture._RecordingHostService();controller=self.controller(service)
        original=controller._agent_queue.reserve_guard;prepared=[]
        @contextmanager
        def observed(*args,**kwargs):
            prepared.append(dict(kwargs['prepared']))
            with original(*args,**kwargs):yield
        with patch.object(controller._agent_queue,'reserve_guard',side_effect=observed):
            self.assertTrue(controller.send('Owned project host policy frame review',agent_mode=True))
            self.spin(lambda:not controller.busy)
        self.assertEqual(1,len(service.calls));self.assertEqual(1,len(prepared))
        captured=service.calls[0]['execution_runtime_snapshot']
        self.assertTrue(runtime.valid_execution_runtime_snapshot(captured))
        self.assertEqual(captured.identity,prepared[0]['project_execution_identity'])
        self.assertEqual(captured.identity,service.frames[0]['project_execution_identity'])
        self.assertEqual(captured.configuration,service.frames[0]['project_execution'])
        self.assertEqual(captured.identity,controller._current_agent_queue_policy()['project_execution_identity'])
        self.assertNotIn('execution_runtime_snapshot',json.dumps(controller.store.load_messages(controller.current_conversation_id)))
        self.assertEqual([],self.api.calls)

    def test_plain_plugin_context_cannot_receive_the_host_runtime_capability(self):
        service=qt_fixture.qt_fixture.FakeAgent();controller=self.controller(service)
        self.assertTrue(controller.send('Owned plugin frame review',agent_mode=True))
        self.spin(lambda:not controller.busy)
        self.assertEqual(1,len(service.calls))
        for field in ('execution_runtime_snapshot','project_execution','project_execution_identity'):
            self.assertNotIn(field,service.calls[0]['context'])
        self.assertEqual([],self.api.calls)

    def test_config_change_after_reservation_leaves_frame_immutable_but_blocks_dispatch(self):
        service=qt_fixture._RecordingHostService();service.check_dispatch=True
        controller=self.controller(service);original=controller._agent_queue.reserve_guard
        @contextmanager
        def changed(*args,**kwargs):
            with original(*args,**kwargs):yield
            self.execution_path.write_text('{"engine":"mxc","enabled":false}',encoding='utf-8')
        with patch.object(controller._agent_queue,'reserve_guard',side_effect=changed):
            self.assertTrue(controller.send('Owned stale frame review',agent_mode=True))
            self.spin(lambda:not controller.busy)
        self.assertEqual(1,len(service.calls));self.assertTrue(service.blocked)
        self.assertFalse(getattr(service,'fail_publication',False))
        self.assertTrue(service.frames[0]['project_execution']['enabled'])
        self.assertNotEqual(service.frames[0]['project_execution_identity'],controller._current_agent_queue_policy()['project_execution_identity'])
        self.assertEqual({},service._processes);self.assertEqual([],self.api.calls)


if __name__=='__main__':unittest.main()
