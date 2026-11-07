"""Opt-in fixed native code: Host -> SDK -> ZIP verification -> immutable delivery.

The localhost planner is scripted, the Python payload is a trusted literal, and
all enable/config/checkpoint files are owned temporary fixtures. No cloud call,
user configuration, model-generated code, archive extraction or user app input.
"""
from dataclasses import replace
from contextlib import contextmanager
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4
import zipfile

from desktop_pet.agents import AgentRequest, AgentService
from desktop_pet.agents import execution_runtime as host
from desktop_pet.agents import service as service_module
from desktop_pet.agents.settings import AgentSettings, AGENT_WORKSPACE_ROOT, DEFAULT_PYTHON
from desktop_pet.settings_manager import ApiSettings
from tests.test_research_parent_worker import SeparateGoalModel


_RETAINED = []


@contextmanager
def owned_directory():
    owner = tempfile.TemporaryDirectory(prefix='project-zip-delivery-', dir=AGENT_WORKSPACE_ROOT)
    root = Path(owner.name).resolve(); state = {'execution_entered':False}
    try:
        yield root,state
    finally:
        try:
            handles,service = state.get('handles',[]),state.get('service')
            closed = not state['execution_entered'] or bool(handles and service
                and service.validate_worker_closure(handles[0],service.worker_closure(handles[0])))
            contained = root.parent == AGENT_WORKSPACE_ROOT.resolve() and Path(owner.name).resolve() == root
        except BaseException:
            closed = contained = False
        if closed and contained:
            owner.cleanup()
        else:
            # An assertion/error is not process closure. Retain the exact
            # fixture/worker owners; never recurse into an uncertain live run.
            owner._finalizer.detach()
            _RETAINED.append((owner,state))


FIXED_ZIP_CODE = """import json,pathlib,zipfile
source=(pathlib.Path(INPUT_DIR)/'source.txt').read_bytes()
total=sum(int(value) for value in source.decode('utf-8').split())
with zipfile.ZipFile(pathlib.Path(OUTPUT_DIR)/'delivered.zip','w',compression=zipfile.ZIP_DEFLATED) as archive:
    archive.writestr('source.txt',source)
    archive.writestr('result.json',json.dumps({'total':total},separators=(',',':')))
print('{}')
"""
FIXED_CORRUPT_CODE = """import pathlib
(pathlib.Path(OUTPUT_DIR)/'delivered.zip').write_bytes(b'INVALID_ZIP_FIXED_QA')
print('{}')
"""


@unittest.skipUnless(os.name == 'nt' and Path(DEFAULT_PYTHON).is_file()
    and os.environ.get('AURA_RUN_PROJECT_MXC_INTEGRATION') == '1', 'explicit fixed native project QA required')
class ProjectZipDeliveryTests(unittest.TestCase):
    def test_valid_native_zip_is_verified_and_delivered_as_an_immutable_conversation_copy(self):
        self.case(corrupt=False)

    def test_native_zero_exit_and_publication_do_not_verify_a_corrupt_zip(self):
        self.case(corrupt=True)

    def case(self, *, corrupt):
        AGENT_WORKSPACE_ROOT.mkdir(exist_ok=True)
        with owned_directory() as (root,owners):
            workspace = root/'workspace'; workspace.mkdir()
            private = root/'private'; private.mkdir()
            configuration = private/'runtime.json'
            configuration.write_text('{"engine":"mxc","enabled":true}', encoding='utf-8')
            (workspace/'source.txt').write_bytes(b'17 29')
            digest = hashlib.sha256(b'17 29').hexdigest()
            proof = private/'native-proof.json'; wrapper = private/'entry.py'
            repo = Path(__file__).resolve().parents[1]
            wrapper.write_text(
                'import sys,asyncio,json\nfrom pathlib import Path\n'
                +'sys.path.insert(0,'+repr(str(repo))+')\n'
                +'sys.path.insert(0,'+repr(str(repo/'agent_backend/nanobot'))+')\n'
                +'from desktop_pet.agents import execution_runtime as host\n'
                +'host._ROOT=Path('+repr(str(private))+');host._CONFIG=Path('+repr(str(configuration))+')\n'
                +'import worker,mxc_native\nactual=mxc_native.run\nevents=[]\n'
                +'def observed(*args,**kwargs):\n    value=actual(*args,**kwargs)\n'
                +"    events.append({'code':value.code,'resumed':value.resumed,'tree':value.tree_reaped,'resources':value.resources_closed,'limits':value.limits_verified})\n    return value\n"
                +'mxc_native.run=observed\ntry:asyncio.run(worker.main())\n'
                +'finally:Path('+repr(str(proof))+').write_text(json.dumps(events),encoding="utf-8")\n', encoding='utf-8')
            summary = 'Created delivered.zip from source.txt and independently verified the current ZIP.'
            def action(body):
                names = [row.get('name') for row in body['messages'] if row.get('role') == 'tool']
                if 'read_file' not in names: return 'read_file', {'path':'source.txt'}
                if 'compute_project_artifact' not in names:
                    return 'compute_project_artifact', {'code':FIXED_CORRUPT_CODE if corrupt else FIXED_ZIP_CODE,
                        'inputs':[{'path':'source.txt','expected_sha256':digest}],
                        'output':{'path':'delivered.zip','target':'delivered.zip'}}
                if 'verify_artifact' not in names: return 'verify_artifact', {'path':'delivered.zip','kind':'zip'}
                if 'finish_task' not in names: return 'finish_task', {'status':'completed','summary':summary}
                return summary
            model = SeparateGoalModel([action]*30, judge_mode='optimistic')
            service = AgentService(checkpoint_path=private/'checkpoints.sqlite3')
            handles = []; cid = str(uuid4())
            owners.update(service=service,handles=handles,model=model,proof=proof)
            try:
                with patch.object(host,'_ROOT',private), patch.object(host,'_CONFIG',configuration),\
                     patch.object(service_module,'_WORKER_PATH',wrapper):
                    snapshot = host.capture_execution_runtime()
                    owners['execution_entered'] = True
                    result = service.execute(replace(AgentSettings(), enabled=True, tools_enabled=True,
                        computer_enabled=False, workspace=str(workspace), timeout_seconds=40),
                        'FIXED_SYNTHETIC_ZIP_KEY', AgentRequest(
                            'Create delivered.zip containing source.txt and result.json with the exact sum 46. Verify the actual ZIP before completion.', 'agent'),
                        {'api':ApiSettings(model.base_url,'fixed-project-zip',5), 'messages':[],
                         'conversation_id':cid,'conversation_generation':str(uuid4()),'execution_runtime_snapshot':snapshot},
                        worker_lifecycle_callback=handles.append)
                self.assertTrue(handles and service.validate_worker_closure(handles[0],service.worker_closure(handles[0])))
                events = json.loads(proof.read_text(encoding='utf-8'))
                self.assertEqual(2,len(events))
                self.assertTrue(all(row['code']=='MXC_EXITED' and all(row[key] is True
                    for key in ('resumed','tree','resources','limits')) for row in events))
                self.assertEqual(b'17 29',(workspace/'source.txt').read_bytes())
                if corrupt:
                    self.assertEqual(b'INVALID_ZIP_FIXED_QA',(workspace/'delivered.zip').read_bytes())
                    self.assertEqual('needs_user',result.status)
                    self.assertEqual((),result.artifacts)
                    self.assertEqual([],service.artifacts(cid))
                    return
                self.assertEqual('completed',result.status, json.dumps(result.acceptance, ensure_ascii=True))
                self.assertEqual(1,len(result.artifacts))
                metadata = result.artifacts[0]
                self.assertEqual('application/zip',metadata['mime'])
                self.assertEqual('zip_structure_crc_only',metadata['verification_scope'])
                original = (workspace/'delivered.zip').read_bytes()
                with zipfile.ZipFile(io.BytesIO(original)) as archive:
                    self.assertEqual({'source.txt','result.json'},set(archive.namelist()))
                    self.assertEqual(b'17 29',archive.read('source.txt'))
                    self.assertEqual({'total':46},json.loads(archive.read('result.json')))
                # A later live-workspace change must not rewrite the delivered copy.
                (workspace/'delivered.zip').write_bytes(b'CHANGED_AFTER_DELIVERY')
                recovered = bytearray()
                for _ in range((metadata['bytes']+22)//23):
                    page = service.artifact_page(cid,metadata['reference'],offset=len(recovered),limit=23)
                    self.assertEqual(len(recovered),page['offset'])
                    self.assertEqual(metadata['bytes'],page['total_bytes'])
                    self.assertTrue(all(page[key] == metadata[key] for key in
                        ('reference','name','mime','bytes','sha256','verification_scope')))
                    block = base64.b64decode(page['data_base64'],validate=True)
                    self.assertTrue(0 < len(block) <= 23, 'Paging must make bounded forward progress')
                    recovered.extend(block)
                    self.assertEqual(len(recovered) if len(recovered) < metadata['bytes'] else None,page['next_offset'])
                self.assertEqual(metadata['bytes'],len(recovered))
                self.assertEqual(original,bytes(recovered))
                self.assertEqual(metadata['sha256'],hashlib.sha256(recovered).hexdigest())
                with self.assertRaises(ValueError): service.artifact_page(str(uuid4()),metadata['reference'])
            finally:
                try:
                    service.shutdown()
                finally:
                    model.close()
                self.assertEqual([],model.errors)


if __name__ == '__main__': unittest.main()
