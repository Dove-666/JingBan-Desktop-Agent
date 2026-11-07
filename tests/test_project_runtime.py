"""Host runtime/control tests. Fake native transports never run supplied code."""
import asyncio
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from agent_backend.nanobot import project_runtime as runtime
from agent_backend.nanobot import mxc_execution as mxc, mxc_native as native
from agent_backend.nanobot.safe_files import WorkspaceFiles
from desktop_pet.agents import execution_runtime as host


class ProjectRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(prefix='aura-project-runtime-test-')
        self.root=Path(self.temporary.name);self.workspace=self.root/'workspace';self.workspace.mkdir()
        self.files=WorkspaceFiles(self.workspace);(self.workspace/'source.txt').write_bytes(b'17 29')
        self.grant=self.root/'approved-extra.txt';self.grant.write_bytes(b'owned unrelated source')
        self.revision,self.call,self.authorized='revision-1','c'*64,True
        self.cancel,self.guard=threading.Event(),threading.RLock()
        self.authority={'version':1,'conversation_id':str(uuid4()),'generation':str(uuid4()),
            'request_id':str(uuid4()),'settings_scope':'f'*64}
        config=host._fixed_configuration({'engine':'mxc','enabled':True})
        self.capture_factory=host.capture_execution_runtime
        with patch.object(host,'load_execution_runtime',return_value=config):self.snapshot=host.capture_execution_runtime()
        self.configuration,self.identity=self.snapshot.configuration,self.snapshot.identity
        self.patches=[patch.object(host,'capture_execution_runtime',side_effect=lambda:self.snapshot),
            patch.object(mxc.MxcPythonRunner,'_pins',return_value=()),patch.object(native,'run',side_effect=self.native)]
        for item in self.patches:item.start()
        self.runtimes,self.calls=[],[]
        self.block,self.entered=threading.Event(),threading.Event()
        self.block.set();self.thread_done=threading.Event()
        self.after_version=None

    async def asyncTearDown(self):
        self.block.set()
        for value in self.runtimes:await value.aclose()
        for item in reversed(self.patches):item.stop()
        self.temporary.cleanup()

    def make(self):
        value=runtime.ProjectRuntime(files=self.files,authority=self.authority,configuration=self.configuration,
            configuration_identity=self.identity,current_goal_revision=lambda:self.revision,
            authorization_current=lambda:self.authorized,parent_cancellation=self.cancel,
            call_identity=lambda:self.call,authority_guard=lambda:self.guard)
        self.runtimes.append(value);return value

    def arguments(self):
        return {'code':'pass','inputs':[{'path':'source.txt','expected_sha256':hashlib.sha256(b'17 29').hexdigest()}],
            'output':{'path':'result.txt','target':'result.txt'}}

    def native(self,argv,**kwargs):
        self.calls.append(tuple(argv));kwargs['launch_attempt_callback']()
        with kwargs['authority_guard']():kwargs['before_resume']()
        if argv[1:]==['--version']:
            if self.after_version:self.after_version()
            return native.NativeResult('MXC_EXITED',0,mxc.CODEX_VERSION.encode(),limits_verified=True)
        path=kwargs['cwd'].parent/'task/result.txt';path.write_bytes(b'46')
        self.entered.set()
        while not self.block.is_set() and not kwargs['cancel_event'].is_set():time.sleep(.005)
        self.thread_done.set()
        return native.NativeResult('MXC_CANCELLED' if kwargs['cancel_event'].is_set() else 'MXC_EXITED',
            0,b'{}',limits_verified=True)

    async def test_published_receipt_is_typed_actual_metadata_and_duplicate_never_reexecutes(self):
        value=self.make();result=await value.compute_project_artifact(**self.arguments())
        self.assertIs(type(result),runtime.ProjectArtifactResult)
        self.assertEqual('result.txt',runtime.published_artifact(result)['written'])
        self.assertIsNone(runtime.published_artifact(str(result)))
        self.assertIsNone(runtime.published_artifact(json.loads(result)))
        self.assertEqual(b'46',(self.workspace/'result.txt').read_bytes())
        self.assertEqual(2,len(self.calls))
        entry=next(iter(value._entries.values()))
        self.assertEqual(2,len(entry.attempts));self.assertIsNot(entry.attempts[0],entry.attempts[1])
        self.assertTrue(all(value._attempts._get(item).finished and value._attempts._get(item).launch_attempted
                            for item in entry.attempts))
        self.assertIsNone(entry.private);self.assertIsNone(entry.plan)
        self.assertIs(result,await value.compute_project_artifact(**self.arguments()))
        self.assertEqual(2,len(self.calls))
        with self.assertRaisesRegex(runtime.ProjectRuntimeError,'PROJECT_CALL_IDENTITY_CONFLICT'):
            await value.compute_project_artifact(**{**self.arguments(),'code':'print(1)'})

    async def test_model_cannot_select_configuration_scope_runtime_or_authority(self):
        for altered in ({**self.configuration,'python':'C:/untrusted'},
                        {**self.configuration,'policy':{**self.configuration['policy'],'process_limit':1000}},
                        {**self.configuration,'enabled':1}):
            with self.assertRaisesRegex(runtime.ProjectRuntimeError,'PROJECT_HOST_CONFIGURATION_INVALID'):
                runtime.checked_configuration(altered,self.identity)
        for field in ('conversation_id','generation','request_id'):
            with self.assertRaises(runtime.ProjectRuntimeError):
                runtime.checked_authority({**self.authority,field:'0'*32},
                    conversation_id=self.authority['conversation_id'],request_id=self.authority['request_id'])
        value=self.make()
        for invalid in ({**self.arguments(),'inputs':[{'path':'source.txt','expected_sha256':'a'*64,'scope':{}}]},
                        {**self.arguments(),'output':{'path':'x','target':'x','runtime':'python'}},
                        {**self.arguments(),'code':'x'*65537}):
            with self.assertRaisesRegex(runtime.ProjectRuntimeError,'PROJECT_ARGUMENTS_INVALID'):
                await value.compute_project_artifact(**invalid)
        self.assertEqual([],self.calls)

    async def test_goal_changed_after_version_never_dispatches_workload_or_publishes(self):
        value=self.make();self.after_version=lambda:setattr(self,'revision','revision-2')
        with self.assertRaises(runtime.ProjectRuntimeError):await value.compute_project_artifact(**self.arguments())
        self.assertEqual(2,len(self.calls),'Second native call is recorded before the fake callback denies it; no payload was dispatched')
        self.assertFalse((self.workspace/'result.txt').exists())
        self.assertFalse(value._has_unknown())
        self.assertIsNone(next(iter(value._entries.values())).private)

    async def test_real_grant_change_invalidates_original_scope_before_workload_resume(self):
        value=self.make()
        def grant():
            with self.guard:self.files.add_grant({'path':str(self.grant),'access':'read','directory':False})
        self.after_version=grant
        with self.assertRaises(runtime.ProjectRuntimeError):await value.compute_project_artifact(**self.arguments())
        self.assertFalse((self.workspace/'result.txt').exists())
        self.assertFalse(value._has_unknown())

    async def test_protected_source_configuration_and_runtime_grants_rejected_without_install(self):
        value=self.make()
        for path in (host._CONFIG.parent,mxc.PYTHON_ROOT,mxc.CODEX.parent,Path(runtime.__file__).parent):
            with self.subTest(kind=path.name),self.assertRaisesRegex(runtime.ProjectRuntimeError,'PROJECT_PRIVATE_SCOPE_CONFLICT'):
                value.assert_scopes(extra_grant={'path':str(path),'directory':True,'access':'read'})
        self.assertEqual([],self.files._read_roots)
        self.assertEqual([],self.files._write_roots)

    async def test_configuration_change_to_disabled_blocks_execution_without_native_call(self):
        value=self.make()
        with patch.object(host,'load_execution_runtime',return_value=host._fixed_configuration({'engine':'mxc','enabled':False})):
            self.snapshot=self.capture_factory()
        with self.assertRaisesRegex(runtime.ProjectRuntimeError,'PROJECT_PARENT_INACTIVE'):
            await value.compute_project_artifact(**self.arguments())
        self.assertEqual([],self.calls)

    async def test_repeated_async_stop_drains_thread_keeps_loop_live_and_does_not_publish(self):
        value=self.make();self.block.clear()
        task=asyncio.create_task(value.compute_project_artifact(**self.arguments()))
        ticks=0
        while not self.entered.is_set():
            await asyncio.sleep(.005);ticks+=1
            self.assertLess(ticks,500)
        task.cancel();await asyncio.sleep(.01);task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertTrue(self.thread_done.is_set())
        self.assertEqual(2,len(self.calls))
        self.assertFalse((self.workspace/'result.txt').exists())
        self.assertFalse(value._tasks)
        self.assertFalse(value._has_unknown())

    async def test_cancelling_async_wrapper_cannot_confirm_closure_while_owned_thread_is_still_live(self):
        value=self.make();self.block.clear()
        original=self.native
        def noncooperative(argv,**kwargs):
            if argv[1:]!=['--version']:
                self.entered.set()
                self.block.wait(3)  # Fixed trusted fixture; no process/model/IO.
            return original(argv,**kwargs)
        with patch.object(native,'run',side_effect=noncooperative):
            owner=asyncio.create_task(value.compute_project_artifact(**self.arguments()))
            while not self.entered.is_set():await asyncio.sleep(.005)
            delegates=[task for task in value._tasks if task is not owner]
            if delegates:
                for task in delegates:task.cancel()
            else:owner.cancel()
            close=None
            try:
                await asyncio.sleep(.03)
                close=asyncio.create_task(value.aclose())
                await asyncio.sleep(.03)
                self.assertFalse(close.done(),'Cancelled asyncio wrappers are not exact thread-closure evidence')
                self.assertFalse(self.thread_done.is_set())
                self.assertIn(value,runtime._OWNERS)
            finally:
                self.block.set()
                await asyncio.gather(owner,return_exceptions=True)
                if close is not None:await asyncio.gather(close,return_exceptions=True)


if __name__=='__main__':unittest.main()
