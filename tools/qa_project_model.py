"""Owned small-project repair QA. Default performs no task/credential IO.

Configured execution requires the existing host opt-in and is not enabled by
this script. Candidate Python is compiled/executed ONLY inside actual MXC.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict,replace
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import threading
import time
from unittest.mock import patch
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from tools.qa_project_oracles import (SEED_SOURCE,FIXED_SOURCE,SPEC,sha,encode,strict_json,reference,
    visible_rows,csv_bytes,hidden_cases,hidden_oracle,package_oracle,publication_witnesses)

_RETAINED=[]

# This exact trusted build payload is used ONLY by fixed-selftest. It is never
# substituted for the configured model's code or counted as model planning.
FIXED_BUILD=r'''
import csv,io,json,pathlib,zipfile
root=pathlib.Path(INPUT_DIR)
source=(root/'solution.py').read_bytes()
namespace={'__name__':'owned_fixed_candidate'}
exec(compile(source,'<fixed-candidate>','exec'),namespace)
rows=list(csv.DictReader(io.StringIO((root/'input.csv').read_text(encoding='utf-8'))))
results=namespace['group_totals'](rows)
target=pathlib.Path(OUTPUT_DIR)/'build/fixed.zip';target.parent.mkdir()
with zipfile.ZipFile(target,'w',compression=zipfile.ZIP_DEFLATED) as package:
    package.writestr('solution.py',source)
    package.writestr('results.json',json.dumps(results,ensure_ascii=False,separators=(',',':')).encode('utf-8'))
print(json.dumps({'compiled':True,'rows':len(rows),'artifact_count':1}))
'''

# Candidate and hidden inputs are staged read-only. Expected results and the
# trusted host reference algorithm are deliberately absent from this payload.
HIDDEN_CHECK=r'''
import copy,hashlib,json,pathlib
source=(pathlib.Path(INPUT_DIR)/'candidate.py').read_bytes()
namespace={'__name__':'owned_hidden_candidate'}
exec(compile(source,'<current-saved-candidate>','exec'),namespace)
function=namespace.get('group_totals')
assert callable(function)
outputs=[];unchanged=True
for case in input['cases']:
    rows=copy.deepcopy(case);before=copy.deepcopy(rows)
    outputs.append(function(rows))
    unchanged=unchanged and rows==before
print(json.dumps({'nonce':input['nonce'],'source_sha256':hashlib.sha256(source).hexdigest(),
    'compiled':True,'inputs_unchanged':unchanged,'outputs':outputs},ensure_ascii=False,allow_nan=False))
'''


def safe_code(error,fallback):
    import re
    value=getattr(error,"code",None)
    return value if isinstance(value,str) and re.fullmatch("[A-Z_a-z0-9]{1,90}",value) else fallback


def source_fingerprint():
    from tools.run_agent_benchmarks import runtime_fingerprint
    value=runtime_fingerprint()
    value["qa_files"]={p:sha((ROOT/p).read_bytes()) for p in (
        "tools/qa_project_model.py","tools/qa_project_oracles.py","tools/qa_project_worker.py",
        "tools/qa_editor_config_readonly.py")}
    return value


def prepare():
    parent=ROOT/"agent_workspace";parent.mkdir(exist_ok=True)
    nonce=uuid4().hex;workspace=Path(tempfile.mkdtemp(prefix="project-model-",dir=parent))
    rows=visible_rows(nonce)
    data={"solution.py":SEED_SOURCE.encode("utf-8"),"SPEC.md":SPEC.format(nonce=nonce).encode("utf-8"),"input.csv":csv_bytes(rows)}
    for name,raw in data.items():(workspace/name).write_bytes(raw)
    return workspace,rows,{name:sha(raw) for name,raw in data.items()}


def workspace_bytes(workspace):
    from agent_backend.nanobot.safe_files import WorkspaceFiles
    files=WorkspaceFiles(workspace);values={}
    for name in ("solution.py","SPEC.md","input.csv","fixed.zip"):
        try:values[name]=files.read_bytes(name,maximum=4*1024*1024)[1]
        except Exception:values[name]=None
    names={p.name for p in workspace.iterdir()}
    return values,names


def artifact_oracle(workspace,rows,baseline,*,witnesses=()):
    values,names=workspace_bytes(workspace)
    source=values["solution.py"];package=values["fixed.zip"]
    checked=package_oracle(package,source,reference(rows))
    checks={"ACTUAL_SAVED_SOURCE_REPAIRED":type(source) is bytes and 0<len(source)<=262144 and sha(source)!=baseline["solution.py"],
        "SPEC_AND_INPUT_UNCHANGED":all(type(values[n]) is bytes and sha(values[n])==baseline[n] for n in ("SPEC.md","input.csv")),
        "ONLY_DECLARED_WORKSPACE_FILES":names=={"solution.py","SPEC.md","input.csv","fixed.zip"},
        **checked["checks"],"ACTUAL_VERSIONED_MXC_ARTIFACT_PUBLICATION":bool(witnesses)}
    return {"passed":all(checks.values()),"checks":checks,"metadata":checked["metadata"]},values


def final_artifacts(workspace,rows,baseline,history,hidden):
    """Recheck current versions after the independent native execution."""
    values,_=workspace_bytes(workspace)
    hashes={n:sha(values[n]) for n in ("solution.py","SPEC.md","input.csv") if type(values[n]) is bytes}
    witnesses=publication_witnesses(history,hashes,sha(values["fixed.zip"]) if values["fixed.zip"] else "",len(values["fixed.zip"] or b""))
    checked,values=artifact_oracle(workspace,rows,baseline,witnesses=witnesses)
    current=(type(values["solution.py"]) is bytes and sha(values["solution.py"])==hidden.get("candidate_source_sha256"))
    return checked,witnesses,current


def native_projection(result,argv=()):
    phase="runtime_version" if list(argv)[1:]==["--version"] else "workload" if "sandbox" in argv and "--include-managed-config" in argv else "unknown"
    return {"code":result.code,"tree_reaped":result.tree_reaped is True,"resources_closed":result.resources_closed is True,
        "limits_verified":result.limits_verified is True,"cleanup_lease_absent":getattr(result,"_cleanup_lease",None) is None,
        "resumed":result.resumed is True,"phase":phase}


def native_evidence_ok(events):
    return (type(events) is list and all(type(row) is dict and type(row.get("phase")) is str for row in events)
        and {row.get("phase") for row in events}=={"runtime_version","workload"}
        and all(row.get("code")=="MXC_EXITED" and all(row.get(key) is True for key in (
            "tree_reaped","resources_closed","limits_verified","cleanup_lease_absent","resumed")) for row in events))


def hidden_check(source,cases,*,current=lambda:True,known_fixed_qa=False):
    """Never host-executes source. Explicit scope, native pins, one MXC call."""
    if _RETAINED:return {"passed":False,"code":"PROJECT_QA_CLEANUP_UNCONFIRMED","closed":False}
    from agent_backend.nanobot import mxc_execution as mxc,mxc_native
    from agent_backend.nanobot.safe_files import WorkspaceFiles
    from agent_backend.nanobot.container_execution import HostExecutionScope,AuthorizedInput
    if type(source) is not bytes or not 1<=len(source)<=262144:raise ValueError("PROJECT_QA_SOURCE_INVALID")
    if mxc_native.has_pending_cleanup():return {"passed":False,"code":"MXC_CLEANUP_PENDING","closed":False}
    temporary=tempfile.TemporaryDirectory(prefix="project-hidden-",dir=ROOT/"agent_workspace")
    root=Path(temporary.name);workspace=root/"workspace";workspace.mkdir();scratch=root/"scratch";scratch.mkdir()
    (workspace/"candidate.py").write_bytes(source)
    nonce=uuid4().hex;lock=threading.RLock();active=True
    @contextmanager
    def guard():
        with lock:yield
    scope=HostExecutionScope(str(uuid4()),str(uuid4()),str(uuid4()),WorkspaceFiles(workspace),
        grant_revision=sha(source),is_current=lambda:active and current())
    runner=mxc.MxcPythonRunner(live_enabled=True,scratch_parent=scratch)
    history=[];actual=runner._native_run
    def observe(*args,**kwargs):
        value=actual(*args,**kwargs);history.append(native_projection(value,args[0]));return value
    runner._native_run=observe
    value=None;code="PROJECT_QA_HIDDEN_FAILED";closed=False
    try:
        result=runner.execute(HIDDEN_CHECK,{"cases":cases,"nonce":nonce},scope=scope,
            authorized_inputs=[AuthorizedInput("candidate.py",sha(source),"candidate.py")],authority_guard=guard)
        code=result.code
        checked=hidden_oracle(result.value,cases,sha(source),nonce)
        closed=(result.tree_reaped is True and result.resources_closed is True and result.scratch_removed is True
            and not runner._pending and not mxc_native.has_pending_cleanup() and not list(scratch.iterdir()))
        success=result.code=="MXC_OK" and result.limits_verified is True and checked["passed"] and closed and native_evidence_ok(history)
        value={"passed":success,"code":code,"closed":closed,"independent_oracle":checked,
            "native_invocations":history,"candidate_source_sha256":sha(source),"hidden_inputs_sha256":sha(encode(cases)),
            "known_fixed_qa":known_fixed_qa,"candidate_executed_on_host":False}
    except Exception as error:value={"passed":False,"code":safe_code(error,code),"closed":False,"native_invocations":history,
        "candidate_executed_on_host":False}
    finally:
        if closed:
            with lock:active=False
            temporary.cleanup()
        else:
            temporary._finalizer.detach();_RETAINED.append((temporary,runner,scope))
    return value


def fixed_selftest():
    if _RETAINED:return {"status":"unavailable","code":"PROJECT_QA_CLEANUP_UNCONFIRMED","model_called":False}
    from agent_backend.nanobot import mxc_execution as mxc,mxc_native
    from agent_backend.nanobot.safe_files import WorkspaceFiles
    from agent_backend.nanobot.container_execution import HostExecutionScope
    from agent_backend.nanobot.project_snapshot import ProjectSnapshot,ProjectInput,ProjectOutput
    from desktop_pet.agents.execution_runtime import capture_execution_runtime
    before=source_fingerprint();policy_before=capture_execution_runtime().identity;started=time.monotonic()
    workspace,rows,baseline=prepare();(workspace/"solution.py").write_bytes(FIXED_SOURCE.encode("utf-8"))
    temporary=tempfile.TemporaryDirectory(prefix="project-fixed-native-",dir=ROOT/"agent_workspace")
    scratch=Path(temporary.name);lock=threading.RLock();files=WorkspaceFiles(workspace)
    @contextmanager
    def guard():
        with lock:yield
    active=True
    scope=HostExecutionScope(str(uuid4()),str(uuid4()),str(uuid4()),files,grant_revision="a"*64,is_current=lambda:active)
    runner=mxc.MxcPythonRunner(live_enabled=True,scratch_parent=scratch);native=[];actual=runner._native_run
    def observe(*args,**kwargs):
        value=actual(*args,**kwargs);native.append(native_projection(value,args[0]));return value
    runner._native_run=observe
    inputs=[ProjectInput(name,sha(files.read_bytes(name)[1])) for name in ("solution.py","SPEC.md","input.csv")]
    goal="b"*64;plan=None;published=None;code=None;build_closed=False;hidden={"passed":False}
    try:
        plan=ProjectSnapshot.prepare(scope,inputs,ProjectOutput("build/fixed.zip","fixed.zip"),goal_revision=goal,
            goal_is_current=lambda g:active and g==goal,authority_guard=guard)
        result=plan.execute(runner,FIXED_BUILD)
        code=result.code
        build_closed=(result.tree_reaped and result.resources_closed and result.scratch_removed
            and not runner._pending and not mxc_native.has_pending_cleanup() and not list(scratch.iterdir()))
        if build_closed and result.code=="MXC_OK":published=plan.publish()
        witnesses=[{"snapshot_sha256":published["snapshot_sha256"],"artifact_sha256":published["artifact"]["sha256"]}] if published else []
        artifacts,values=artifact_oracle(workspace,rows,baseline,witnesses=witnesses)
        if artifacts["passed"]:
            hidden=hidden_check(values["solution.py"],hidden_cases(secrets.randbits(128)),known_fixed_qa=True)
    except Exception as error:
        code=safe_code(error,"PROJECT_QA_FIXED_SELFTEST_FAILED");artifacts={"passed":False,"checks":{}}
    finally:
        if build_closed:
            with lock:active=False
            temporary.cleanup()
        else:
            temporary._finalizer.detach();_RETAINED.append((temporary,runner,scope,plan))
    after=source_fingerprint();policy_after=capture_execution_runtime().identity
    passed=artifacts["passed"] and hidden["passed"] and build_closed and native_evidence_ok(native) and before==after and policy_before==policy_after
    return {"schema":"aura.project_model_qa.v1","mode":"fixed_trusted_native_selftest","status":"passed" if passed else "failed",
        "model_called":False,"model_generated_code":False,"code":code,"duration_seconds":round(time.monotonic()-started,3),
        "artifact_oracle":artifacts,"hidden_check":hidden,"build_native":native,"build_exact_cleanup_confirmed":build_closed,
        "owned_workspace":str(workspace),"source_before":before,"source_after":after,"source_unchanged":before==after,
        "production_optin_identity_before":policy_before,"production_optin_identity_after":policy_after,
        "production_optin_unchanged":policy_before==policy_after,"production_enable_file_written":False,
        "candidate_executed_on_host":False,"scope":"known fixed stdlib repair/package and independent MXC function checks, not real-model capability"}


def getter_configured(timeout):
    from desktop_pet import settings_manager as module
    from tools.qa_editor_config_readonly import GetterSettings,GetterVault,Audit
    from tools.run_agent_benchmarks import ConfiguredExecutor
    audit=Audit();original=module.SettingsManager
    class GetterManager(original):
        def __init__(self,settings=None,credential_store=None):
            if settings is None:raise RuntimeError("PROJECT_QA_GETTER_STORE_REQUIRED")
            credentials=credential_store or module.CredentialStore()
            if credentials._backend is not None:credentials._backend=GetterVault(credentials._backend,audit)
            super().__init__(GetterSettings(settings,audit),credential_store=credentials)
    with patch.object(module,"SettingsManager",GetterManager):value=ConfiguredExecutor(timeout)
    return value,audit


def configured_case(timeout):
    # Important: unavailable policy is checked BEFORE credentials/workspace IO.
    if _RETAINED:return {"status":"unavailable","code":"PROJECT_QA_CLEANUP_UNCONFIRMED","model_called":False,
        "credentials_read":False,"workspace_created":False}
    from desktop_pet.agents.execution_runtime import capture_execution_runtime
    snapshot=capture_execution_runtime()
    if not snapshot.configuration or snapshot.configuration.get("enabled") is not True:
        return {"schema":"aura.project_model_qa.v1","status":"unavailable","code":"PROJECT_EXECUTION_DISABLED",
            "model_called":False,"credentials_read":False,"workspace_created":False,"production_enable_file_written":False}
    configured,audit=getter_configured(timeout)
    from desktop_pet.agents import AgentService,AgentRequest
    from desktop_pet.agents import service as service_module
    from desktop_pet.agents.service import _ERROR_MESSAGES
    from agent_backend.nanobot.acceptance_report import checked_assessment
    before=source_fingerprint();workspace,rows,baseline=prepare();started=time.monotonic()
    private=tempfile.TemporaryDirectory(prefix="project-model-proof-",dir=ROOT/"agent_workspace")
    proof=Path(private.name)/"native.json"
    service=AgentService();handles=[];result=None;code=None
    task=("修复当前小项目实际solution.py中的group_totals函数。请先完整阅读SPEC.md、input.csv和当前源码，"
        "严格遵守SPEC的金额、退款、pending与Unicode排序要求。先用write_file/edit_file保存真正修复的源码，"
        "然后使用compute_project_artifact以三个当前SHA只读快照编译运行测试并发布fixed.zip。压缩包只能含当前"
        "solution.py与input.csv对应的results.json。保持SPEC/input字节不变，工作区仅这四个文件；不要网络、"
        "终端、软件、额外文件或包。以实际产物读回验收，不要只声称修好。")
    context={"api":configured.api,"messages":[],"conversation_id":str(uuid4()),"conversation_generation":str(uuid4()),
        "execution_runtime_snapshot":snapshot}
    # The metadata-only wrapper keeps the real worker and policy checks intact.
    # Its private proof path is host-provided through a fixed bootstrap entry.
    wrapper=Path(private.name)/"entry.py"
    wrapper.write_text("import os,runpy\nos.environ['AURA_QA_PROJECT_PROOF']="+repr(str(proof))+
        "\nrunpy.run_path("+repr(str(ROOT/"tools/qa_project_worker.py"))+",run_name='__main__')\n",encoding="utf-8")
    try:
        with patch.object(service_module,"_WORKER_PATH",wrapper):
            try:
                result=service.execute(replace(configured.settings,enabled=True,tools_enabled=True,computer_enabled=False,
                    workspace=str(workspace),max_steps=0,timeout_seconds=timeout),configured.key,AgentRequest(task,"tools"),
                    context,worker_lifecycle_callback=handles.append)
            except Exception as error:
                candidate=getattr(error,"code",None);code=candidate if candidate in _ERROR_MESSAGES else "PROJECT_QA_AGENT_FAILED"
        closed=bool(handles and service.validate_worker_closure(handles[0],service.worker_closure(handles[0])))
        history=service._sessions.get(context["conversation_id"],[])
        values,names=workspace_bytes(workspace)
        hashes={n:sha(values[n]) for n in ("solution.py","SPEC.md","input.csv") if type(values[n]) is bytes}
        witnesses=publication_witnesses(history,hashes,sha(values["fixed.zip"]) if values["fixed.zip"] else "",len(values["fixed.zip"] or b""))
        artifacts,values=artifact_oracle(workspace,rows,baseline,witnesses=witnesses)
        native=strict_json(proof.read_bytes()) if closed and proof.is_file() else {"native":[],"closed_proof_available":False}
        native_ok=native_evidence_ok(native.get("native"))
        policy_current=lambda:capture_execution_runtime().identity==snapshot.identity
        hidden={"passed":False,"code":"PROJECT_QA_HIDDEN_NOT_RUN"}
        if closed and native_ok and artifacts["passed"] and policy_current():
            original_source=sha(values["solution.py"])
            def current():
                from agent_backend.nanobot.safe_files import WorkspaceFiles
                try:
                    files=WorkspaceFiles(workspace)
                    hashes={n:files.read_bytes(n,maximum=262144)[2] for n in ("solution.py","SPEC.md","input.csv")}
                    return policy_current() and hashes["solution.py"]==original_source and all(hashes[n]==baseline[n] for n in ("SPEC.md","input.csv"))
                except Exception:return False
            hidden=hidden_check(values["solution.py"],hidden_cases(secrets.randbits(128)),current=current)
        # Hidden execution may take a full native budget. Re-read the actual
        # files and re-pair publication against the CURRENT saved versions.
        artifacts,witnesses,same_hidden_source=final_artifacts(workspace,rows,baseline,history,hidden)
        after=source_fingerprint();ini_stable=configured.unchanged();policy_stable=policy_current()
        functional=bool(result and result.status=="completed" and artifacts["passed"] and hidden["passed"] and same_hidden_source and closed and native_ok)
        passed=functional and before==after and ini_stable and policy_stable and not audit.denied
        return {"schema":"aura.project_model_qa.v1","mode":"configured","created_at":datetime.now(timezone.utc).isoformat(),
            "status":"passed" if passed else "failed","functional_pass":functional,"agent_status":result.status if result else None,
            "code":code,"model":configured.model,"model_execution_requested":True,
            "model_called":True if any(row.get("role")=="assistant" for row in history) else None,
            "duration_seconds":round(time.monotonic()-started,3),"hidden_source_still_current":same_hidden_source,
            "artifact_oracle":artifacts,"hidden_check":hidden,"actual_publication_witnesses":witnesses,
            "build_native":native,"worker_exact_closed":closed,"final_acceptance":checked_assessment(result.acceptance) if result else None,
            "owned_workspace":str(workspace),"source_before":before,"source_after":after,"source_unchanged":before==after,
            "whole_ini_bytes_unchanged":ini_stable,"captured_api_public_scope_sha256":sha(encode(asdict(configured.api))),
            "captured_execution_policy_identity":snapshot.identity,"execution_policy_still_current":policy_stable,
            "configuration_write_attempts":audit.denied,"production_enable_file_written":False,"candidate_executed_on_host":False,
            "raw_model_data_saved":False,"scope":"one owned offline stdlib Python repair/package; not general repository, terminal, phone or Claude parity"}
    finally:
        service.shutdown()
        if handles and service.validate_worker_closure(handles[0],service.worker_closure(handles[0])):private.cleanup()
        else:private._finalizer.detach();_RETAINED.append((private,service))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider",choices=("offline","fixed-selftest","configured"),default="offline")
    parser.add_argument("--allow-fixed-native",action="store_true")
    parser.add_argument("--timeout",type=int,default=180)
    parser.add_argument("--output",type=Path,default=ROOT/"artifacts/project-model-qa/verification.json")
    args=parser.parse_args()
    if args.provider=="offline":print(json.dumps({"status":"not_run","model_called":False,"credentials_read":False,"native_execution":False}));return 0
    if args.provider=="fixed-selftest" and not args.allow_fixed_native:parser.error("fixed native QA requires explicit --allow-fixed-native")
    if not 30<=args.timeout<=900:parser.error("timeout must be 30..900")
    output=args.output.resolve()
    if not output.is_relative_to((ROOT/"artifacts").resolve()) or output.exists():parser.error("use a new output path inside artifacts")
    try:report=fixed_selftest() if args.provider=="fixed-selftest" else configured_case(args.timeout)
    except Exception as error:report={"status":"failed","code":safe_code(error,"PROJECT_QA_RUNNER_FAILED"),"candidate_executed_on_host":False}
    output.parent.mkdir(parents=True,exist_ok=True);output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({k:report.get(k) for k in ("status","mode","functional_pass","code","duration_seconds","model_called")},ensure_ascii=True))
    return 0 if report["status"]=="passed" else 1


if __name__=="__main__":raise SystemExit(main())
