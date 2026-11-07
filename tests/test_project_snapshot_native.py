"""Opt-in fixed trusted file-computation/project payloads through actual MXC.

No model-generated code, provider, package install, network or production tool
registration. Cases record exact native results separately from host CAS.
"""
import io
import json
import os
from pathlib import Path
import threading
import time
import unittest
import zipfile

from agent_backend.nanobot import mxc_execution as mxc, mxc_native as native
from agent_backend.nanobot import project_snapshot as project
from tests import test_project_snapshot as fixture
digest = fixture.digest


AGGREGATE = r'''
import json,pathlib,unicodedata
from decimal import Decimal
root=pathlib.Path(INPUT_DIR)
rows=json.loads((root/'data/orders.json').read_text(encoding='utf-8'))
totals={}
for row in rows:
    label=unicodedata.normalize('NFC',row['label'].strip()).casefold()
    totals[label]=totals.get(label,Decimal(0))+Decimal(row['unit'])*row['quantity']
try:
    (root/'data/orders.json').write_text('FORBIDDEN',encoding='utf-8')
    denied=False
except PermissionError:denied=True
assert denied
result=[{'label':label,'total':format(total.quantize(Decimal('.01')),'f')} for label,total in sorted(totals.items())]
out=pathlib.Path(OUTPUT_DIR)/'build/result.json';out.parent.mkdir()
out.write_text(json.dumps(result,ensure_ascii=False,separators=(',',':')),encoding='utf-8')
print(json.dumps({'groups':len(result),'input_write_denied':denied}))
'''


PROJECT = r'''
import json,pathlib,zipfile
root=pathlib.Path(INPUT_DIR)
source=(root/'src/labels.py').read_text(encoding='utf-8')
assert source.count('label.strip().lower()')==1
source='import unicodedata\n'+source.replace('label.strip().lower()',"unicodedata.normalize('NFC',label.strip()).casefold()")
namespace={};exec(compile(source,'<owned-fixed-project>','exec'),namespace)
cases=json.loads((root/'data/cases.json').read_text(encoding='utf-8'))
for case in cases:assert namespace['unique_labels'](case['input'])==case['expected']
out=pathlib.Path(OUTPUT_DIR)/'build/package.zip';out.parent.mkdir()
with zipfile.ZipFile(out,'w',compression=zipfile.ZIP_DEFLATED) as package:
    package.writestr('src/labels.py',source.encode('utf-8'))
    package.writestr('data/cases.json',(root/'data/cases.json').read_bytes())
    package.writestr('README.txt',(root/'README.txt').read_bytes())
print(json.dumps({'fixed_cases':len(cases),'artifact_count':1}))
'''


@unittest.skipUnless(os.name == 'nt' and os.environ.get('AURA_RUN_PROJECT_MXC_INTEGRATION') == '1',
    'explicit fixed-payload Windows MXC project opt-in')
class ProjectSnapshotNativeTests(unittest.TestCase):
    source = fixture.ProjectSnapshotTests.source
    guard = fixture.ProjectSnapshotTests.guard
    plan = fixture.ProjectSnapshotTests.plan
    transport = fixture.ProjectSnapshotTests.transport
    proofs = {}

    def setUp(self):
        fixture.ProjectSnapshotTests.setUp(self)
        self.assertFalse(native.has_pending_cleanup(), 'Unknown native resources must block this fixture')
        self.runner = mxc.MxcPythonRunner(live_enabled=True, scratch_parent=self.scratch)
        self.history = []
        actual = self.runner._native_run
        def recorded(argv, **kwargs):
            result = actual(argv, **kwargs)
            self.history.append(result)
            return result
        self.runner._native_run = recorded

    def prove_cleanup(self):
        self.assertTrue(self.history)
        self.assertTrue(all(result.tree_reaped is True and result.resources_closed is True
                            and result._cleanup_lease is None for result in self.history))
        self.assertEqual([], self.runner._pending)
        self.assertFalse(native.has_pending_cleanup())
        self.assertEqual([], list(self.scratch.iterdir()))
        self.proofs[self._testMethodName] = {
            'native_invocations':len(self.history),
            'fixed_codes':[result.code for result in self.history],
            'resumed':[result.resumed for result in self.history],
            'limits_verified':all(result.limits_verified is True for result in self.history),
            'tree_zero':True,'resources_closed':True,'scratch_removed':True,'task_verified':False}

    def test_actual_decimal_unicode_snapshot_computation_ro_inputs_and_atomic_publication(self):
        raw=json.dumps([{'label':' 鲸鱼🌊 ','unit':'0.10','quantity':3},
                        {'label':'鲸鱼🌊','unit':'0.20','quantity':2},
                        {'label':'Straße','unit':'2.35','quantity':2},
                        {'label':'STRASSE','unit':'0.30','quantity':1}],ensure_ascii=False).encode()
        row=self.source('data/orders.json',raw)
        plan=self.plan([row]);result=plan.execute(self.runner,AGGREGATE)
        self.assertEqual('MXC_OK',result.code)
        self.assertEqual({'groups':2,'input_write_denied':True},result.value)
        self.assertIs(type(result._capture_receipt),mxc.CaptureReceipt)
        self.prove_cleanup()
        self.assertFalse((self.workspace/'result.json').exists())
        published=plan.publish()
        data=(self.workspace/'result.json').read_bytes()
        self.assertEqual([{'label':'strasse','total':'5.00'},{'label':'鲸鱼🌊','total':'0.70'}],json.loads(data))
        self.assertEqual(digest(data),published['artifact']['sha256'])
        self.assertFalse(published['task_verified'])
        self.assertEqual(raw,(self.workspace/'data/orders.json').read_bytes())
        self.assertEqual(2,len(self.history))

    def test_actual_small_project_fix_test_compile_and_single_zip_preserves_sources(self):
        seed=b'def unique_labels(labels):\n    seen=[]\n    for label in labels:\n        key=label.strip().lower()\n        if key not in seen: seen.append(key)\n    return seen\n'
        cases=json.dumps([{'input':[' Straße ','STRASSE','鲸鱼🌊','鲸鱼🌊'],
                           'expected':['strasse','鲸鱼🌊']},
                          {'input':['E\u0301','É','乙'],'expected':['é','乙']}],ensure_ascii=False).encode()
        readme=b'Owned source fixture. Do not alter the live project.'
        rows=[self.source('src/labels.py',seed),self.source('data/cases.json',cases),self.source('README.txt',readme)]
        plan=self.plan(rows,project.ProjectOutput('build/package.zip','dist/repaired-project.zip'))
        result=plan.execute(self.runner,PROJECT)
        self.assertEqual('MXC_OK',result.code)
        self.assertEqual({'fixed_cases':2,'artifact_count':1},result.value)
        self.prove_cleanup();published=plan.publish()
        artifact=(self.workspace/'dist/repaired-project.zip').read_bytes()
        with zipfile.ZipFile(io.BytesIO(artifact)) as package:
            self.assertEqual(['src/labels.py','data/cases.json','README.txt'],package.namelist())
            self.assertIn(b"unicodedata.normalize('NFC',label.strip()).casefold()",package.read('src/labels.py'))
            self.assertEqual(cases,package.read('data/cases.json'))
            self.assertEqual(readme,package.read('README.txt'))
        # Do not execute the produced artifact on the host; fixed runtime case
        # assertions ran only inside the actual MXC job.
        for row,expected in zip(rows,(seed,cases,readme)):
            self.assertEqual(expected,(self.workspace/row.path).read_bytes())
        self.assertEqual(digest(artifact),published['artifact']['sha256'])
        self.assertFalse(published['task_verified'])
        self.assertEqual(2,len(self.history))

    def test_actual_changed_live_source_after_stage_never_resumes_even_version_root(self):
        plan=self.plan();stage=plan._stage_inputs
        def changed(*args):
            stage(*args)
            (self.workspace/'src/input.json').write_bytes(b'changed live source')
        plan._stage_inputs=changed
        result=plan.execute(self.runner,"import json; print(json.dumps({'unexpected':True}))")
        self.assertEqual('MXC_SCOPE_REVOKED',result.code)
        self.assertEqual(1,len(self.history))
        self.assertFalse(self.history[0].resumed)
        self.assertIsNone(result._capture_receipt)
        with self.assertRaisesRegex(project.ProjectSnapshotError,'PROJECT_CAPTURE_REQUIRED'):plan.publish()
        self.assertFalse((self.workspace/'result.json').exists())
        self.prove_cleanup()

    def test_actual_stop_after_output_creation_reaps_tree_drops_artifact_and_never_replays(self):
        plan=self.plan();cancel=threading.Event();observed=[];finished=threading.Event()
        def stop_when_ready():
            deadline=time.monotonic()+12
            while not finished.is_set() and time.monotonic()<deadline:
                if list(self.scratch.glob('aura-mxc-*/task/build/result.json')):
                    with self.lock:
                        observed.append(time.monotonic());cancel.set()
                    return
                time.sleep(.01)
        stopper=threading.Thread(target=stop_when_ready,daemon=True);stopper.start()
        code="import pathlib,time\np=pathlib.Path(OUTPUT_DIR)/'build/result.json';p.parent.mkdir();p.write_bytes(b'UNPUBLISHED_FIXED')\ntime.sleep(10)\nprint('{}')"
        try:
            with self.assertRaisesRegex(project.ProjectSnapshotError,'PROJECT_CANCELLED'):
                plan.execute(self.runner,code,cancel_event=cancel)
        finally:finished.set();stopper.join(2)
        self.assertFalse(stopper.is_alive());self.assertEqual(1,len(observed))
        self.assertLess(time.monotonic()-observed[0],2)
        self.assertEqual(2,len(self.history))
        self.assertEqual('MXC_CANCELLED',self.history[-1].code)
        self.assertIsNone(plan._receipt);self.assertIsNone(plan._captured)
        with self.assertRaisesRegex(project.ProjectSnapshotError,'PROJECT_CAPTURE_REQUIRED'):plan.publish()
        self.assertFalse((self.workspace/'result.json').exists())
        self.prove_cleanup()


if __name__=='__main__':unittest.main()
