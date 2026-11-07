"""Pure artifact/oracle mutants. Candidate Python is never host-executed."""
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import warnings
import zipfile

from tools import qa_project_model as runner
from tools import qa_project_oracles as oracle


def package(source=oracle.FIXED_SOURCE.encode(),results=None,extra=(),*,duplicate=False,symlink=False):
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,"w",compression=zipfile.ZIP_DEFLATED) as z:
        if symlink:
            info=zipfile.ZipInfo("solution.py");info.create_system=3;info.external_attr=(stat.S_IFLNK|0o777)<<16
            z.writestr(info,source)
        else:z.writestr("solution.py",source)
        z.writestr("results.json",results if results is not None else b'[]')
        for name,raw in extra:z.writestr(name,raw)
        if duplicate:
            with warnings.catch_warnings():warnings.simplefilter("ignore",UserWarning);z.writestr("solution.py",source)
    return stream.getvalue()


class ProjectOracleTests(unittest.TestCase):
    def row(self,category,status,amount):return {"category":category,"status":status,"amount":amount}

    def test_reference_exact_money_refund_pending_zero_and_input_immutability(self):
        rows=[self.row("🐳","paid","0.10"),self.row("🐳","paid","0.20"),self.row("🐳","refund","0.05"),
            self.row("pending","pending","99.99"),self.row("negative","refund","9.25"),
            self.row("zero","paid","2.00"),self.row("zero","refund","2.00")]
        before=deepcopy(rows)
        self.assertEqual([{"category":"negative","total":"-9.25"},{"category":"zero","total":"0.00"},
            {"category":"🐳","total":"0.25"}],oracle.reference(rows))
        self.assertEqual(before,rows);self.assertEqual([],oracle.reference([]))

    def test_reference_over_53_bits_and_full_30_integer_digits_without_default_context_rounding(self):
        rows=[self.row("big","paid","9007199254740993.01"),self.row("big","refund","0.02"),
            self.row("huge","paid","999999999999999999999999999999.99"),self.row("huge","refund","0.01")]
        self.assertEqual([{"category":"big","total":"9007199254740992.99"},
            {"category":"huge","total":"999999999999999999999999999999.98"}],oracle.reference(rows))

    def test_unicode_categories_remain_distinct_and_sorted_without_casefold_or_normalization(self):
        rows=[self.row(c,"paid","1.00") for c in ["鲸鱼🌊","é","e\u0301","A","a"]]
        self.assertEqual(["A","a","e\u0301","é","鲸鱼🌊"],[r["category"] for r in oracle.reference(rows)])

    def test_zip_exact_current_source_and_strict_expected_results(self):
        source=b'raise RuntimeError("THIS CANDIDATE MUST NEVER EXECUTE ON HOST")\n'
        expected=[{"category":"🌊","total":"0.30"}]
        raw=package(source,oracle.encode(expected))
        self.assertTrue(oracle.package_oracle(raw,source,expected)["passed"])
        self.assertFalse(oracle.package_oracle(raw,b"a different saved source",expected)["passed"])
        for data in (b'[{"category":"x","category":"y","total":"0.30"}]',
                     b'[{"category":"x","total":0.30}]',b'[{"category":"x","total":"3e-1"}]',
                     b'[{"category":"x","total":"-0.00"}]',b'NaN',b'[]'):
            with self.subTest(data=data):self.assertFalse(oracle.package_oracle(package(source,data),source,expected)["passed"])

    def test_zip_duplicate_traversal_extra_symlink_corruption_and_oversize_fail_closed(self):
        source=oracle.FIXED_SOURCE.encode()
        bad=[package(source,duplicate=True),package(source,extra=[("../escape.py",b"pass")]),
            package(source,extra=[("README.txt",b"extra")]),package(source,symlink=True),b"not a zip",
            package(source,b" "*262145)]
        for raw in bad:
            with self.subTest(size=len(raw)):self.assertFalse(oracle.package_oracle(raw,source,[])["passed"])

    def test_hidden_nonce_source_exact_function_results_and_readonly_input_are_all_required(self):
        cases=oracle.hidden_cases(13751)
        value={"nonce":"host_nonce","source_sha256":"a"*64,"compiled":True,"inputs_unchanged":True,
            "outputs":[oracle.reference(rows) for rows in cases]}
        self.assertTrue(oracle.hidden_oracle(value,cases,"a"*64,"host_nonce")["passed"])
        for mutation in ({"nonce":"stale"},{"source_sha256":"b"*64},{"compiled":False},{"inputs_unchanged":False},
                         {"outputs":[[] for _ in cases]}):
            with self.subTest(mutation=list(mutation)):
                self.assertFalse(oracle.hidden_oracle({**value,**mutation},cases,"a"*64,"host_nonce")["passed"])

    def test_model_summary_or_stale_or_unpaired_tool_receipt_is_not_native_publication_evidence(self):
        inputs={"solution.py":"1"*64,"SPEC.md":"2"*64,"input.csv":"3"*64};digest="4"*64
        args={"code":"untrusted model program","inputs":[{"path":p,"expected_sha256":h} for p,h in inputs.items()],
            "output":{"path":"build/fixed.zip","target":"fixed.zip"}}
        receipt={"schema":"aura.project_artifact.v1","written":"fixed.zip","sha256":digest,"bytes":123,
            "snapshot_sha256":"5"*64,"task_verified":False,"artifact":{"path":"fixed.zip","sha256":digest,"bytes":123}}
        assistant={"role":"assistant","tool_calls":[{"id":"current_call","function":{"name":"compute_project_artifact","arguments":json.dumps(args)}}]}
        tool={"role":"tool","name":"compute_project_artifact","tool_call_id":"current_call","content":json.dumps(receipt)}
        self.assertEqual(1,len(oracle.publication_witnesses([assistant,tool],inputs,digest,123)))
        for history in ([tool],[{"role":"assistant","content":json.dumps(receipt)}],
                        [assistant,{**tool,"tool_call_id":"another_call"}],[assistant,assistant,tool]):
            self.assertEqual([],oracle.publication_witnesses(history,inputs,digest,123))
        self.assertEqual([],oracle.publication_witnesses([assistant,tool],{**inputs,"solution.py":"6"*64},digest,123))

    def test_default_offline_cli_does_not_read_credentials_create_workspace_or_execute(self):
        with (patch.object(sys,"argv",["qa_project_model.py"]),patch.object(runner,"prepare",side_effect=AssertionError),
             patch.object(runner,"getter_configured",side_effect=AssertionError),patch.object(runner,"fixed_selftest",side_effect=AssertionError)):
            with patch("sys.stdout",new_callable=io.StringIO) as output:self.assertEqual(0,runner.main())
            self.assertEqual({"status":"not_run","model_called":False,"credentials_read":False,"native_execution":False},json.loads(output.getvalue()))

    def test_missing_or_disabled_execution_policy_is_checked_before_vault_or_workspace(self):
        for config in (None,{"enabled":False}):
            with patch("desktop_pet.agents.execution_runtime.capture_execution_runtime",return_value=SimpleNamespace(configuration=config)),\
                 patch.object(runner,"getter_configured",side_effect=AssertionError("vault touched")),\
                 patch.object(runner,"prepare",side_effect=AssertionError("workspace created")):
                value=runner.configured_case(180)
            self.assertEqual("unavailable",value["status"]);self.assertFalse(value["credentials_read"] or value["workspace_created"])

    def test_retained_unknown_owner_blocks_all_new_model_or_native_work_in_process(self):
        with patch.object(runner,"_RETAINED",[object()]),patch.object(runner,"getter_configured",side_effect=AssertionError),\
             patch.object(runner,"prepare",side_effect=AssertionError):
            self.assertEqual("PROJECT_QA_CLEANUP_UNCONFIRMED",runner.hidden_check(b"pass",[],known_fixed_qa=True)["code"])
            self.assertEqual("PROJECT_QA_CLEANUP_UNCONFIRMED",runner.fixed_selftest()["code"])
            self.assertEqual("PROJECT_QA_CLEANUP_UNCONFIRMED",runner.configured_case(180)["code"])

    def test_both_version_and_workload_require_real_resume_and_verified_native_closure(self):
        base={"code":"MXC_EXITED","tree_reaped":True,"resources_closed":True,"limits_verified":True,
            "cleanup_lease_absent":True,"resumed":True}
        events=[{**base,"phase":"runtime_version"},{**base,"phase":"workload"}]
        self.assertTrue(runner.native_evidence_ok(events))
        for key in ("tree_reaped","resources_closed","limits_verified","cleanup_lease_absent","resumed"):
            for index in (0,1):
                changed=deepcopy(events);changed[index][key]=False
                self.assertFalse(runner.native_evidence_ok(changed))
        self.assertFalse(runner.native_evidence_ok(events[:1]))
        self.assertFalse(runner.native_evidence_ok([events[0],{**events[1],"phase":"unknown"}]))
        self.assertFalse(runner.native_evidence_ok(["untrusted receipt text"]))
        self.assertFalse(runner.native_evidence_ok([{**events[0],"phase":[]}]))

    def test_source_or_zip_change_after_hidden_execution_invalidates_final_artifact_acceptance(self):
        with tempfile.TemporaryDirectory(prefix="project-post-hidden-review-") as d:
            root=Path(d);rows=oracle.visible_rows("1"*32);source=oracle.FIXED_SOURCE.encode()
            data={"solution.py":source,"SPEC.md":b"OWNED_SPEC","input.csv":oracle.csv_bytes(rows)}
            for name,raw in data.items():(root/name).write_bytes(raw)
            zipped=package(source,oracle.encode(oracle.reference(rows)));(root/"fixed.zip").write_bytes(zipped)
            hashes={name:oracle.sha(raw) for name,raw in data.items()};baseline={**hashes,"solution.py":oracle.sha(oracle.SEED_SOURCE.encode())}
            args={"inputs":[{"path":name,"expected_sha256":digest} for name,digest in hashes.items()],"output":{"target":"fixed.zip","path":"fixed.zip"}}
            receipt={"schema":"aura.project_artifact.v1","written":"fixed.zip","sha256":oracle.sha(zipped),"bytes":len(zipped),
                "snapshot_sha256":"a"*64,"task_verified":False,"artifact":{"path":"fixed.zip","sha256":oracle.sha(zipped),"bytes":len(zipped)}}
            history=[{"role":"assistant","tool_calls":[{"id":"unit_control","function":{"name":"compute_project_artifact","arguments":json.dumps(args)}}]},
                {"role":"tool","name":"compute_project_artifact","tool_call_id":"unit_control","content":json.dumps(receipt)}]
            hidden={"candidate_source_sha256":oracle.sha(source)}
            checked,_,current=runner.final_artifacts(root,rows,baseline,history,hidden)
            self.assertTrue(checked["passed"] and current,"Unit byte/receipt control only; no native proof or candidate execution is claimed")
            (root/"solution.py").write_bytes(source+b"\n# New untested source version\n")
            checked,_,current=runner.final_artifacts(root,rows,baseline,history,hidden);self.assertFalse(checked["passed"] or current)
            (root/"solution.py").write_bytes(source);(root/"fixed.zip").write_bytes(package(source,b"[]"))
            checked,witnesses,_=runner.final_artifacts(root,rows,baseline,history,hidden)
            self.assertFalse(checked["passed"]);self.assertEqual([],witnesses)

    @unittest.skipUnless(os.name=="nt" and (runner.ROOT/"agent_backend/nanobot/.venv/Scripts/python.exe").is_file(),"actual Windows SDK required")
    def test_metadata_only_actual_sdk_wrapper_eof_never_loads_task_or_model(self):
        from desktop_pet.agents.process_lifecycle import WorkerLifecycle
        from desktop_pet.agents.service import _ProcessTree
        parent=runner.ROOT/"agent_workspace";parent.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="project-wrapper-smoke-",dir=parent) as d:
            proof=Path(d)/"native.json";env=dict(os.environ);env["AURA_QA_PROJECT_PROOF"]=str(proof)
            process=subprocess.Popen([str(runner.ROOT/"agent_backend/nanobot/.venv/Scripts/python.exe"),"-I","-B",
                str(runner.ROOT/"tools/qa_project_worker.py")],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                env=env,creationflags=0x08000000|0x00000200)
            lifecycle=WorkerLifecycle();handle=lifecycle.register(process,"project-wrapper-smoke",None)
            lifecycle.attach_tree(handle,_ProcessTree(process));lifecycle.mark_dispatched(handle)
            try:
                stdout,_=process.communicate(b"",timeout=10)
                self.assertEqual(0,process.returncode)
                frames=[json.loads(line) for line in stdout.splitlines()]
                self.assertTrue(any(frame.get("type")=="ready" for frame in frames))
                self.assertEqual({"schema":"aura.project_native_qa.v1","native":[]},json.loads(proof.read_bytes()))
            finally:
                receipt=lifecycle.settle(handle,timeout_seconds=3)
                self.assertTrue(lifecycle.validates(handle,receipt))


if __name__=="__main__":unittest.main()
