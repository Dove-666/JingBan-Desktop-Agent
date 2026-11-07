"""Actual host capture → SDK parent → fixed native MXC → file readback.

Owned inputs, temporary opt-in file and localhost scripted model only. The real
machine enable file and user settings are untouched; no model-generated code.
"""
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from desktop_pet.agents import execution_runtime as host
from desktop_pet.agents import service as service_module
from desktop_pet.agents import AgentService, AgentRequest
from desktop_pet.agents.contracts import AgentError
from desktop_pet.agents.staged_input import StagedRunInput
from desktop_pet.agents.checkpoints import CheckpointStore
from desktop_pet.agents.settings import AgentSettings, AGENT_WORKSPACE_ROOT, DEFAULT_PYTHON
from desktop_pet.settings_manager import ApiSettings
from tests.test_research_parent_worker import SeparateGoalModel


@unittest.skipUnless(os.name == "nt" and Path(DEFAULT_PYTHON).is_file(), "actual Windows SDK required")
class ProjectHostServiceTests(unittest.TestCase):
    def test_actual_host_snapshot_authority_and_native_publication_have_one_exact_owner(self):
        self.case()

    def test_policy_change_after_staging_blocks_final_lf_and_every_model_native_request(self):
        self.case(change_after_stage=True)

    def test_policy_change_after_checkpoint_commit_blocks_ack_and_next_dispatch(self):
        self.case(change_after_checkpoint=True)

    def case(self, *, change_after_stage=False, change_after_checkpoint=False):
        AGENT_WORKSPACE_ROOT.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="project-host-", dir=AGENT_WORKSPACE_ROOT) as temporary:
            root = Path(temporary); workspace = root/"workspace"; workspace.mkdir()
            private = root/"private"; private.mkdir()
            config_path = private/"runtime.json"
            config_path.write_text('{"engine":"mxc","enabled":true}', encoding="utf-8")
            (workspace/"source.txt").write_bytes(b"17 29")
            digest = hashlib.sha256(b"17 29").hexdigest()
            generation = str(uuid4()); proof = root/"proof.json"
            source_root = Path(__file__).resolve().parents[1]
            wrapper = root/"worker_entry.py"
            wrapper.write_text("import sys,asyncio,json\nfrom pathlib import Path\n"
                +"sys.path.insert(0,"+repr(str(source_root))+")\n"
                +"sys.path.insert(0,"+repr(str(source_root/"agent_backend/nanobot"))+")\n"
                +"from desktop_pet.agents import execution_runtime as host\n"
                +"host._ROOT=Path("+repr(str(private))+");host._CONFIG=Path("+repr(str(config_path))+")\n"
                +"import worker,mxc_native\nstate={'native':[]}\ncheck=worker.checked_request\n"
                +"def checked(raw):\n    value=check(raw)\n    authority=value.get('execution_authority',{})\n"
                +"    state['generation_matches']=authority.get('generation')=="+repr(generation)+"\n"
                +"    state['identity_matches']=value['config'].get('project_execution_identity')==host.capture_execution_runtime().identity\n"
                +"    state['has_authority']=set(authority)=={'version','conversation_id','generation','request_id','settings_scope'}\n    return value\n"
                +"worker.checked_request=checked\nactual=mxc_native.run\n"
                +"def observed(*args,**kwargs):\n    value=actual(*args,**kwargs)\n"
                +"    state['native'].append({'tree_zero':value.tree_reaped,'closed':value.resources_closed,'limits':value.limits_verified,'code':value.code})\n    return value\n"
                +"mxc_native.run=observed\ntry:asyncio.run(worker.main())\n"
                +"finally:Path("+repr(str(proof))+").write_text(json.dumps(state),encoding='utf-8')\n", encoding="utf-8")
            summary = "Created result.txt containing exactly 46 and read it back."
            code = "import pathlib\nnumbers=[int(n) for n in (pathlib.Path(INPUT_DIR)/'source.txt').read_text().split()]\n(pathlib.Path(OUTPUT_DIR)/'result.txt').write_text(str(sum(numbers)))\nprint('{}')"
            def action(body):
                names = [row.get("name") for row in body["messages"] if row.get("role") == "tool"]
                if "read_file" not in names: return "read_file", {"path":"source.txt"}
                if "compute_project_artifact" not in names:
                    return "compute_project_artifact", {"code":code, "inputs":[{"path":"source.txt","expected_sha256":digest}],
                        "output":{"path":"result.txt","target":"result.txt"}}
                if names.count("read_file") < 2: return "read_file", {"path":"result.txt"}
                if "verify_artifact" not in names: return "verify_artifact", {"path":"result.txt","kind":"text"}
                if "finish_task" not in names: return "finish_task", {"status":"completed","summary":summary}
                return summary
            model = SeparateGoalModel([action]*30, judge_mode="optimistic")
            service = AgentService(checkpoint_path=root/"checkpoint.sqlite3" if change_after_checkpoint else None); handles = []
            try:
                with patch.object(host,"_ROOT",private), patch.object(host,"_CONFIG",config_path), patch.object(service_module,"_WORKER_PATH",wrapper):
                    snapshot = host.capture_execution_runtime()
                    committed_barriers = []
                    save = CheckpointStore.save
                    def commit_then_revoke(store, *args, **kwargs):
                        value = save(store, *args, **kwargs)
                        if kwargs.get("barrier") is not None:
                            committed_barriers.append(kwargs["barrier"])
                            if len(committed_barriers) == 1:
                                config_path.write_text('{"engine":"mxc","enabled":false}', encoding="utf-8")
                        return value
                    def invoke():
                        return service.execute(replace(AgentSettings(), enabled=True, tools_enabled=True, computer_enabled=False,
                            workspace=str(workspace), timeout_seconds=40), "SYNTHETIC_PROJECT_HOST_KEY", AgentRequest(
                            "Create result.txt containing exactly 46 from source.txt. Read back and verify the actual file before completion.", "agent"),
                            {"api":ApiSettings(model.base_url,"owned-project-host",5),"messages":[],"conversation_id":str(uuid4()),
                             "conversation_generation":generation,"execution_runtime_snapshot":snapshot}, worker_lifecycle_callback=handles.append)
                    if change_after_stage:
                        stage = StagedRunInput.stage
                        def revoke_after_stage(writer, raw):
                            stage(writer, raw)
                            config_path.write_text('{"engine":"mxc","enabled":false}', encoding="utf-8")
                        with patch.object(StagedRunInput,"stage",revoke_after_stage):
                            with self.assertRaises(AgentError) as failure: invoke()
                        self.assertEqual("execution_runtime_changed", failure.exception.code)
                        self.assertTrue(handles and service.validate_worker_closure(handles[0],service.worker_closure(handles[0])))
                        self.assertFalse((workspace/"result.txt").exists())
                        self.assertEqual([], model.planner_calls)
                        self.assertEqual([], model.judge_calls)
                        return
                    if change_after_checkpoint:
                        with patch.object(CheckpointStore,"save",commit_then_revoke):
                            with self.assertRaises(AgentError) as failure: invoke()
                        self.assertEqual("execution_runtime_changed", failure.exception.code)
                        self.assertTrue(committed_barriers)
                        self.assertTrue(handles and service.validate_worker_closure(handles[0],service.worker_closure(handles[0])))
                        self.assertFalse((workspace/"result.txt").exists())
                        self.assertLessEqual(len(model.planner_calls), 1)
                        self.assertEqual([], model.judge_calls)
                        return
                    result = invoke()
                self.assertEqual("completed", result.status)
                self.assertEqual(b"46", (workspace/"result.txt").read_bytes())
                self.assertEqual(b"17 29", (workspace/"source.txt").read_bytes())
                self.assertTrue(handles and service.validate_worker_closure(handles[0],service.worker_closure(handles[0])))
                actual = json.loads(proof.read_text(encoding="utf-8"))
                self.assertTrue(actual["generation_matches"] and actual["identity_matches"] and actual["has_authority"])
                self.assertEqual(2, len(actual["native"]))
                self.assertTrue(all(row["tree_zero"] and row["closed"] and row["limits"] and row["code"]=="MXC_EXITED" for row in actual["native"]))
            finally:
                service.shutdown(); model.close()
                self.assertEqual([], model.errors)


if __name__ == "__main__": unittest.main()
