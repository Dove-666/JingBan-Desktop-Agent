"""Opt-in standalone SDK planning over two exact-owned editor tools only.

Default execution performs no UI, credential IO, filesystem mutation or model
request. This is a controlled QA subset, not production AgentService routing.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from tools.qa_owned_editor import OwnedEditor,EditorQAError,roundtrip_oracle,_read_owned_document
from agent_backend.nanobot.desktop_controls import DesktopControls,DesktopControlError,ERROR_CODES
from agent_backend.nanobot.redaction import redact_text

TOOLS=frozenset({"owned_editor_observe","owned_editor_act"})
NAMES=frozenset({"Document text","Document path","Document status","Save document","Close document","Reopen document"})
SDK_PYTHON=ROOT/"agent_backend/nanobot/.venv/Scripts/python.exe"
_SDK_OWNERS=set()
_RETAINED=[]


def unique_members(pairs):
    value={}
    for key,item in pairs:
        if key in value:raise ValueError("EDITOR_SDK_PROTOCOL_INVALID")
        value[key]=item
    return value


def public_observation(snapshot,secrets=()):
    """No HWND/PID/starttime/nonce/native RuntimeID enters model context."""
    rows=[]
    for row in snapshot["controls"]:
        if row.get("name") not in NAMES:continue
        state={key:item for key,item in row.get("state",{}).items() if key in {"enabled","value"} and type(item) in {str,bool}}
        rows.append({"ref":row["ref"],"name":row["name"],"actions":[a for a in row["actions"] if a in {"set_value","invoke"}],
            "state":{key:redact_text(item,secrets) if isinstance(item,str) else item for key,item in state.items()}})
    return {"status":"observed","scope":"single_owned_synthetic_editor","controls":rows,"previous_refs_expired":True,
        "task_verified":False}


class EditorBroker:
    """Real native actions plus a host-only, ordered, independent goal oracle."""
    def __init__(self,editor,marker,*,secrets=()):
        self.editor,self.marker,self.secrets=editor,marker,tuple(s for s in secrets if s)
        self.controls=DesktopControls(editor.cancel,backend=editor,max_age_seconds=120)
        self.saved_sha=self.saved_runtime=None
        self.closed=False;self.latest=None;self.events=[];self.tool_count=0
        self.started=time.monotonic()

    def record(self,stage,**facts):
        self.events.append({"sequence":len(self.events)+1,"stage":stage,"elapsed_ms":round((time.monotonic()-self.started)*1000,3),**facts})

    def collect(self,snapshot,action_name=None):
        self.latest=snapshot
        fields=[row for row in snapshot["controls"] if row["name"]=="Document text"]
        statuses=[row for row in snapshot["controls"] if row["name"]=="Document status"]
        status=statuses[0]["state"].get("value") if len(statuses)==1 else None
        field=fields[0] if len(fields)==1 else None
        if action_name=="Save document":
            self.closed=False;self.saved_sha=self.saved_runtime=None
            try:raw=_read_owned_document(self.editor.workspace)
            except OSError:raw=None
            if status=="Saved" and field and raw==self.marker.encode("utf-8"):
                self.saved_sha=hashlib.sha256(raw).hexdigest()
                self.saved_runtime=self.controls._refs[field["ref"]]["target"]["runtime_id"]
            self.record("save_independently_read_back",current_exact_file=self.saved_sha is not None,file_sha256=self.saved_sha)
        if action_name=="Close document":
            self.closed=bool(self.saved_sha is not None and not fields and status=="Closed")
            self.record("document_closed_observed",after_current_save=self.closed)
        if action_name=="Reopen document":
            runtime=self.controls._refs[field["ref"]]["target"]["runtime_id"] if field else None
            self.record("document_reopened_observed",after_saved_document_closed=self.closed,
                new_native_control=bool(runtime and self.saved_runtime and runtime!=self.saved_runtime),
                exact_current_value=bool(field and field["state"].get("value")==self.marker and status=="Reopened"))

    async def snapshot(self,action_name=None):
        self.editor.guard()
        value=json.loads(await self.controls.observe())
        self.editor.guard();self.collect(value,action_name)
        return public_observation(value,self.secrets)

    async def dispatch(self,name,arguments):
        self.editor.guard()
        self.tool_count+=1
        if name not in TOOLS or type(arguments) is not dict:return {"status":"error","code":"EDITOR_TOOL_DENIED","task_verified":False}
        if name=="owned_editor_observe":
            if arguments:return {"status":"error","code":"EDITOR_ARGUMENT_INVALID","task_verified":False}
            return {"observation":await self.snapshot()}
        if (set(arguments)-{"ref","action","value"} or not {"ref","action"}<=set(arguments)
                or type(arguments["ref"]) is not str or type(arguments["action"]) is not str
                or arguments["action"] not in {"set_value","invoke"} or type(arguments.get("value","")) is not str
                or len(arguments.get("value",""))>1024):
            return {"status":"error","code":"EDITOR_ARGUMENT_INVALID","task_verified":False}
        try:
            item=self.controls._reference(arguments["ref"],mutation=True)
            target=item["target"]["name"]
            if target not in NAMES:return {"status":"error","code":"EDITOR_TOOL_DENIED","task_verified":False}
            result=json.loads(await self.controls.act(arguments["ref"],arguments["action"],redact_text(arguments.get("value",""),self.secrets)))
            self.record("native_action_returned",target=target,action=arguments["action"],dispatched=result.get("dispatched") is True,
                single_property_readback=result.get("postcondition",{}).get("passed") is True)
            return {"status":result["status"],"dispatched":result.get("dispatched") is True,"task_verified":False,
                "observation":await self.snapshot(target if arguments["action"]=="invoke" else None)}
        except DesktopControlError as error:
            self.record("native_action_rejected",code=error.code)
            if getattr(error,"effect_state",None)=="uncertain" or self.editor.cancel.is_set():
                self.editor.stop("EDITOR_NATIVE_EFFECT_UNCERTAIN");raise EditorQAError("EDITOR_NATIVE_EFFECT_UNCERTAIN") from None
            return {"status":"error","code":error.code,"task_verified":False}

    async def final_oracle(self):
        await self.snapshot()
        fields=[row for row in self.latest["controls"] if row["name"]=="Document text"]
        statuses=[row for row in self.latest["controls"] if row["name"]=="Document status"]
        field=fields[0] if len(fields)==1 else None
        return roundtrip_oracle(self.editor.workspace,self.marker,first_runtime=self.saved_runtime,
            reopened_runtime=self.controls._refs[field["ref"]]["target"]["runtime_id"] if field else None,
            closed_without_editor=self.closed,current_value=field["state"].get("value") if field else None,
            status=statuses[0]["state"].get("value") if len(statuses)==1 else None,saved_sha=self.saved_sha,
            current_identity_matches=self.latest["window"]["process_id"]==self.editor.process.pid,cancelled=self.editor.cancel.is_set())


class SdkEditorRun:
    def __init__(self,broker,config,state,*,deadline_seconds=120,fault="none",on_event=None):
        if _SDK_OWNERS:raise EditorQAError("EDITOR_SDK_CLEANUP_UNCONFIRMED")
        if (type(config) is not dict or set(config)!={"base_url","model","api_key"}
                or any(type(v) is not str or not v for v in config.values())
                or type(deadline_seconds) is not int or not 5<=deadline_seconds<=180
                or fault not in {"none","eof_after_first_tool"}):raise EditorQAError("EDITOR_SDK_PROTOCOL_INVALID")
        self.broker,self.config,self.state=broker,config,Path(state)
        self.deadline_seconds,self.fault,self.on_event=deadline_seconds,fault,on_event
        self.token=uuid4().hex;self.process=self.handle=self.reader=self.watchdog=None
        self.frames=queue.Queue();self.finished=threading.Event();self.lifecycle=None
        self.model_requests=self.model_responses=0;self.error_code=None;self.launch_unknown=False
        self.active_io=0;self.io_lock=threading.Lock();self.seen=set()

    def _send(self,value):
        self.broker.editor.guard()
        raw=json.dumps(value,ensure_ascii=False,allow_nan=False).encode("utf-8")+b"\n"
        if len(raw)>131072:raise EditorQAError("EDITOR_SDK_PROTOCOL_INVALID")
        with self.io_lock:self.active_io+=1
        try:
            offset=0
            while offset<len(raw):
                self.broker.editor.guard()
                written=self.process.stdin.write(raw[offset:])
                if type(written) is not int or written<=0:raise EditorQAError("EDITOR_SDK_PROTOCOL_INVALID")
                offset+=written
            self.process.stdin.flush()
        finally:
            with self.io_lock:self.active_io-=1

    def start(self):
        from desktop_pet.agents.process_lifecycle import WorkerLifecycle
        from desktop_pet.agents.service import _ProcessTree
        self.lifecycle=WorkerLifecycle();_SDK_OWNERS.add(self)
        try:
            self.process=subprocess.Popen([str(SDK_PYTHON),"-I","-B","-u",str(ROOT/"tools/qa_owned_editor_sdk_worker.py")],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,bufsize=0,
                creationflags=0x08000000|0x00000200,env={k:v for k,v in os.environ.items()
                    if not re.search("API.?KEY|TOKEN|SECRET|PASSWORD|OPENAI|ANTHROPIC|NANOBOT",k,re.I)})
        except Exception:
            self.launch_unknown=True;raise EditorQAError("EDITOR_SDK_LAUNCH_UNCONFIRMED") from None
        self.handle=self.lifecycle.register(self.process,"owned-editor-sdk-"+self.token,None)
        self.lifecycle.attach_tree(self.handle,_ProcessTree(self.process))
        def read():
            try:
                while True:
                    raw=self.process.stdout.readline(131073)
                    if not raw:break
                    if not raw.endswith(b"\n") or len(raw)>131072:raise ValueError()
                    value=json.loads(raw,object_pairs_hook=unique_members)
                    if type(value) is not dict:raise ValueError()
                    self.frames.put(value)
            except Exception:self.frames.put({"type":"protocol_error"})
            finally:self.frames.put({"type":"eof"})
        self.reader=threading.Thread(target=read,daemon=True,name="owned-editor-sdk-output");self.reader.start()
        deadline=time.monotonic()+self.deadline_seconds
        def watch():
            while not self.finished.wait(.025):
                if self.broker.editor.cancel.is_set() or time.monotonic()>=deadline:
                    if not self.broker.editor.cancel.is_set():self.broker.editor.stop("EDITOR_QA_DEADLINE")
                    self.lifecycle.request_stop(self.handle);return
        self.watchdog=threading.Thread(target=watch,daemon=True,name="owned-editor-sdk-stop");self.watchdog.start()
        value=self.frames.get(timeout=min(15,self.deadline_seconds))
        if value!={"type":"ready","protocol":1,"role":"owned_editor_planner"}:raise EditorQAError("EDITOR_SDK_PROTOCOL_INVALID")
        self.lifecycle.mark_dispatched(self.handle)
        self._send({"type":"run","token":self.token,"marker":self.broker.marker,"config":self.config,
            "state_dir":str(self.state),"fault":self.fault})

    async def run(self):
        self.start()
        while True:
            self.broker.editor.guard()
            try:value=self.frames.get(timeout=.05)
            except queue.Empty:continue
            kind=value.get("type")
            if kind=="eof":raise EditorQAError("EDITOR_SDK_EOF")
            if kind=="protocol_error" or value.get("token")!=self.token:raise EditorQAError("EDITOR_SDK_PROTOCOL_INVALID")
            if kind in {"model_request","model_response"} and set(value)=={"type","token"}:
                if kind=="model_request":self.model_requests+=1
                else:self.model_responses+=1
                if self.on_event:self.on_event(kind,self.model_requests,self.broker.editor)
            elif kind=="tool":
                if (set(value)!={"type","token","request_id","name","arguments"} or type(value["request_id"]) is not str
                        or not re.fullmatch("[a-f0-9]{32}",value["request_id"]) or value["request_id"] in self.seen
                        or value["name"] not in TOOLS):raise EditorQAError("EDITOR_SDK_PROTOCOL_INVALID")
                self.seen.add(value["request_id"])
                result=await self.broker.dispatch(value["name"],value["arguments"])
                self._send({"type":"tool_reply","token":self.token,"request_id":value["request_id"],"value":result})
            elif kind=="done" and value=={"type":"done","token":self.token,"final_text_present":True}:return True
            elif kind=="error" and value.get("code") in {"EDITOR_MODEL_FAILED","EDITOR_SDK_INVALID"}:
                raise EditorQAError(value["code"])
            else:raise EditorQAError("EDITOR_SDK_PROTOCOL_INVALID")

    def close(self):
        self.finished.set();closed=not self.launch_unknown
        if self.watchdog is not None:self.watchdog.join(timeout=2);closed&=not self.watchdog.is_alive()
        if self.process is not None and self.handle is None:
            self.handle=self.lifecycle.register(self.process,"owned-editor-sdk-cleanup-"+self.token,None)
        if self.handle is not None:
            receipt=self.lifecycle.settle(self.handle,timeout_seconds=3)
            closed&=receipt is not None and self.lifecycle.validates(self.handle,receipt)
        if self.reader is not None:self.reader.join(timeout=2);closed&=not self.reader.is_alive()
        with self.io_lock:closed&=self.active_io==0
        if closed:
            if self.process is not None:
                for pipe in (self.process.stdin,self.process.stdout):
                    if pipe is not None:pipe.close()
            _SDK_OWNERS.discard(self)
        return bool(closed)


def run_case(config,*,deadline_seconds=120,fault="none",on_event=None,marker=None):
    if _SDK_OWNERS:raise EditorQAError("EDITOR_SDK_CLEANUP_UNCONFIRMED")
    if (type(config) is not dict or set(config)!={"base_url","model","api_key"}
            or any(type(v) is not str or not v for v in config.values())
            or type(deadline_seconds) is not int or not 5<=deadline_seconds<=180
            or fault not in {"none","eof_after_first_tool"}
            or marker is not None and (type(marker) is not str or not 1<=len(marker)<=200 or "\0" in marker)):
        raise EditorQAError("EDITOR_SDK_PROTOCOL_INVALID")
    parent=ROOT/"agent_workspace";parent.mkdir(exist_ok=True)
    workspace=tempfile.TemporaryDirectory(prefix="owned-editor-model-",dir=parent)
    state=tempfile.TemporaryDirectory(prefix="owned-editor-sdk-state-",dir=parent)
    editor=broker=run=None;started=time.monotonic();done=False;code=None;oracle={"passed":False,"checks":{}}
    try:
        editor=OwnedEditor(workspace.name,watchdog_ms=min(180000,(deadline_seconds+5)*1000));editor.start()
        goal=redact_text(marker or "AURA 模型编辑 ✅ "+uuid4().hex[:12],(config["api_key"],))
        broker=EditorBroker(editor,goal,secrets=(config["api_key"],))
        run=SdkEditorRun(broker,config,state.name,deadline_seconds=deadline_seconds,fault=fault,on_event=on_event)
        try:done=asyncio.run(run.run())
        except Exception as error:
            raw=getattr(error,"code",None)
            code=raw if isinstance(raw,str) and re.fullmatch("EDITOR_[A-Z_]{1,70}",raw) else "EDITOR_SDK_FAILED"
        if not editor.cancel.is_set():
            try:oracle=asyncio.run(broker.final_oracle())
            except Exception:code=code or "EDITOR_FINAL_OBSERVATION_FAILED"
    except Exception as error:
        raw=getattr(error,"code",None)
        code=raw if isinstance(raw,str) and re.fullmatch("EDITOR_[A-Z_]{1,70}",raw) else "EDITOR_QA_FAILED"
    finally:
        try:sdk_closed=run.close() if run is not None else True
        except Exception:sdk_closed=False
        try:editor_closed=editor.close() if editor is not None else True
        except Exception:editor_closed=False
        no_state_files=not any(p.is_file() for p in Path(state.name).rglob("*"))
        closed=sdk_closed and editor_closed
        if closed:workspace.cleanup();state.cleanup()
        else:
            workspace._finalizer.detach();state._finalizer.detach();_RETAINED.append((editor,run,workspace,state))
    if not sdk_closed:code=code or "EDITOR_SDK_CLEANUP_UNCONFIRMED"
    elif not editor_closed:code=code or "EDITOR_FIXTURE_CLEANUP_UNCONFIRMED"
    elif not no_state_files:code=code or "EDITOR_SDK_STATE_NOT_EMPTY"
    elif done and not oracle["passed"]:code=code or "EDITOR_GOAL_NOT_VERIFIED"
    passed=done and oracle["passed"] and closed and no_state_files
    return {"schema":"aura.owned_editor_standalone_sdk.v1","created_at":datetime.now(timezone.utc).isoformat(),
        "status":"passed" if passed else "failed","agent_status":"completed" if passed else "needs_user", "error_code":code,
        "model_final_present":done,"independent_oracle":oracle,"sdk_exact_tree_closed":sdk_closed,
        "fixture_and_helpers_exact_closed":editor_closed,"sdk_state_files_absent":no_state_files,
        "model_request_count":run.model_requests if run else 0,"model_response_count":run.model_responses if run else 0,
        "tool_count":broker.tool_count if broker else 0,"host_observed_events":broker.events if broker else [],
        "duration_seconds":round(time.monotonic()-started,3),"native_operations":editor.operations if editor else [],
        "focus_result":getattr(editor,"focus_result","unavailable"),"provider_response_or_reasoning_saved":False,
        "user_applications_operated":False,"screenshot_files_written":0,"clipboard_used":False,
        "registered_tools":sorted(TOOLS),"scope":"standalone SDK planner over one owned editor document; not production AgentService, phone, or general Windows skill"}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native",action="store_true")
    parser.add_argument("--provider",choices=("offline","configured"),default="offline")
    parser.add_argument("--timeout",type=int,default=120)
    parser.add_argument("--output",type=Path,default=ROOT/"artifacts/owned-editor-model/configured.json")
    args=parser.parse_args()
    if not args.native or args.provider=="offline":print(json.dumps({"status":"not_run","native_input":False,"model_called":False}));return 0
    if not 5<=args.timeout<=180:parser.error("QA deadline must be 5..180 seconds")
    output=args.output.resolve()
    if not output.is_relative_to((ROOT/"artifacts").resolve()):parser.error("Output must be inside artifacts")
    from tools.run_agent_benchmarks import ConfiguredExecutor,runtime_fingerprint
    configured=ConfiguredExecutor(args.timeout)
    own_paths=[Path(__file__),ROOT/"tools/qa_owned_editor_sdk_worker.py",ROOT/"tools/qa_owned_editor.py",
        ROOT/"tools/qa_owned_editor_fixture.py",ROOT/"tools/qa_owned_editor_uia.ps1"]
    def own_hashes():return {str(p.relative_to(ROOT)).replace('\\','/'):hashlib.sha256(p.read_bytes()).hexdigest() for p in own_paths}
    before,extra_before=runtime_fingerprint(),own_hashes()
    report=run_case({"base_url":configured.api.base_url,"model":configured.api.model,"api_key":configured.key},deadline_seconds=args.timeout)
    after,extra_after=runtime_fingerprint(),own_hashes()
    stable=before==after and extra_before==extra_after
    report.update(real_model_attempted=True,model=configured.model,settings_unchanged=configured.unchanged(),
        source_snapshot={"before":before,"after":after,"unchanged":before==after},
        qa_source_snapshot={"before":extra_before,"after":extra_after,"unchanged":extra_before==extra_after})
    if not stable or not report["settings_unchanged"]:
        report["execution_status_before_fence"]=report["status"];report.update(status="failed",agent_status="needs_user")
    output.parent.mkdir(parents=True,exist_ok=True);output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({k:report[k] for k in ("status","agent_status","model","duration_seconds","model_request_count","tool_count","error_code")},ensure_ascii=True))
    return 0 if report["status"]=="passed" else 1


if __name__=="__main__":raise SystemExit(main())
