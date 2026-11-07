"""Broker protocol/close failures with complete in-memory Win32 substitutes."""
import ctypes as C
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_backend.nanobot import mxc_entry as entry, mxc_execution as execution
from tests.test_mxc_backend_review import Function


class FakeBrokerKernel:
    def __init__(self):
        self.events=[];self.policy=True;self.contained=True;self.image_matches=True
        self.close_failure=None;self.create_exception=False;self.stopped=False;self.resumed=False
        self.executable=None;self.next_handle=100
        functions={
            'GetCurrentProcess':lambda:-1,'GetStdHandle':lambda standard:standard,
            'DuplicateHandle':self.duplicate,'CloseHandle':self.close,
            'InitializeProcThreadAttributeList':self.initialize,'UpdateProcThreadAttribute':self.update,
            'DeleteProcThreadAttributeList':lambda *_:self.events.append('attributes-closed'),
            'CreateProcessW':self.create,'GetProcessMitigationPolicy':self.query_policy,
            'IsProcessInJob':self.in_job,'QueryFullProcessImageNameW':self.image,
            'ResumeThread':self.resume,'WaitForSingleObject':self.wait,
            'GetExitCodeProcess':self.exit_code,'TerminateProcess':self.terminate,
        }
        for name,function in functions.items():setattr(self,name,Function(function))

    def duplicate(self,current,source,target,result,access,inherit,options):
        self.next_handle+=1;result._obj.value=self.next_handle
        self.events.append(('duplicate',source,result._obj.value,inherit,options));return 1
    def close(self,handle):
        self.events.append(('close',handle));return handle!=self.close_failure
    def initialize(self,attributes,count,flags,size):
        if attributes is None:size._obj.value=128;return 0
        self.events.append(('initialize',count));return 1
    def update(self,attributes,flags,kind,value,size,*_):
        self.events.append(('attribute',kind,tuple(value) if kind==0x20002 else value._obj.value));return 1
    def create(self,exe,command,*arguments):
        self.executable=exe;info=arguments[-1]._obj
        info.process=201;info.thread=202
        self.events.append(('create',exe,command.value,arguments[2],arguments[3]))
        if self.create_exception:raise RuntimeError('Controlled post-creation exception')
        return 1
    def query_policy(self,process,kind,result,size):
        self.events.append('policy-query');result._obj.value=1 if self.policy else 0;return 1
    def in_job(self,process,job,result):
        self.events.append('parent-job' if process==-1 else 'child-job')
        result._obj.value=1 if process==-1 or self.contained else 0;return 1
    def image(self,process,flags,buffer,length):
        self.events.append('image-query');buffer.value=self.executable if self.image_matches else 'C:\\wrong\\python.exe';return 1
    def resume(self,thread):self.events.append('resume');self.resumed=True;return 1
    def wait(self,process,milliseconds):return 0 if self.resumed or self.stopped else 258
    def exit_code(self,process,result):result._obj.value=0;return 1
    def terminate(self,*_):self.events.append('terminate');self.stopped=True;return 1


class BrokerProtocolTests(unittest.TestCase):
    def broker(self,kernel):
        with patch.object(C,'WinDLL',return_value=kernel):return entry.WindowsBroker()

    def run_broker(self,kernel):
        return self.broker(kernel).run(Path('C:/owned/python.exe'),Path('E:/owned/input/worker.py'))

    def test_creation_policy_image_and_job_are_verified_before_resume_with_only_stdio(self):
        kernel=FakeBrokerKernel();self.assertEqual(0,self.run_broker(kernel))
        for check in ('policy-query','child-job','image-query'):
            self.assertLess(kernel.events.index(check),kernel.events.index('resume'))
        attributes=[e for e in kernel.events if isinstance(e,tuple) and e[0]=='attribute']
        self.assertEqual([('attribute',0x20002,(101,102,103)),('attribute',0x20007,1<<28)],attributes)
        create=next(e for e in kernel.events if isinstance(e,tuple) and e[0]=='create')
        self.assertIn('-I -S -B',create[2]);self.assertTrue(create[4]&4)
        self.assertFalse(create[4]&0x01000000,'No breakaway from owned Job')
        self.assertEqual({101,102,103,201,202},{e[1] for e in kernel.events if isinstance(e,tuple) and e[0]=='close'})

    def test_policy_off_membership_failure_or_wrong_image_never_resumes(self):
        for property in ('policy','contained','image_matches'):
            with self.subTest(property=property):
                kernel=FakeBrokerKernel();setattr(kernel,property,False)
                self.assertEqual(entry.FAILED,self.run_broker(kernel))
                self.assertNotIn('resume',kernel.events)
                self.assertIn('terminate',kernel.events)
                self.assertIn(('close',201),kernel.events)

    def test_exception_after_actual_creation_retains_returned_handles_and_stops_child(self):
        kernel=FakeBrokerKernel();kernel.create_exception=True
        self.assertEqual(entry.FAILED,self.run_broker(kernel));self.assertNotIn('resume',kernel.events)
        self.assertIn('terminate',kernel.events)
        self.assertIn(('close',201),kernel.events);self.assertIn(('close',202),kernel.events)

    def test_close_failure_cannot_return_success_and_does_not_skip_other_closes(self):
        for handle in (101,201,202):
            with self.subTest(handle=handle):
                kernel=FakeBrokerKernel();kernel.close_failure=handle
                self.assertEqual(entry.UNCLOSED,self.run_broker(kernel))
                self.assertEqual(5,len([e for e in kernel.events if isinstance(e,tuple) and e[0]=='close']))

    def test_broker_main_never_reads_the_model_request_and_rejects_config_extension(self):
        with tempfile.TemporaryDirectory(prefix='mxc-broker-unit-') as d:
            root=Path(d);(root/'worker.py').write_text('Not executed by this test',encoding='utf-8')
            (root/'request.json').write_text('Invalid JSON model request; broker must not parse',encoding='utf-8')
            exe=Path(sys.executable).resolve()
            config={'version':1,'desktop_boundary':entry.BOUNDARY,'python_executable':str(exe),
                'python_sha256':hashlib.sha256(exe.read_bytes()).hexdigest()}
            target=root/'broker.json';target.write_text(json.dumps(config),encoding='utf-8')
            with patch.object(entry,'__file__',str(root/'entry.py')),patch.object(entry.WindowsBroker,'run',return_value=0) as run:
                self.assertEqual(0,entry.main());run.assert_called_once_with(exe,root/'worker.py')
                config['code']='MODEL_CODE';target.write_text(json.dumps(config),encoding='utf-8');run.reset_mock()
                self.assertEqual(entry.FAILED,entry.main());run.assert_not_called()

    def test_boundary_is_fixed_and_cannot_be_switched_off(self):
        from dataclasses import asdict,replace
        policy=execution.MxcPolicy()
        self.assertEqual(entry.BOUNDARY,asdict(policy)['desktop_boundary'])
        for value in ('off','',None,False):
            with self.subTest(value=value),self.assertRaises(ValueError):replace(policy,desktop_boundary=value).validate()


if __name__=='__main__':unittest.main()
