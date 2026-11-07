"""Standalone SDK/native architecture tests, never cloud-model success data.

Visible tests require AURA_OWNED_EDITOR_NATIVE_TEST=1. Pure/default tests have
no UI or credential access. The HTTP fixture stores synthetic requests only
in memory, verifies the actual two-tool registry and never logs raw bodies.
"""
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

from tools.qa_owned_editor_model import ROOT,SDK_PYTHON,TOOLS,public_observation,unique_members,run_case

HIDDEN="SYNTHETIC_REASONING_NOT_TO_BE_ARCHIVED"
KEY="SYNTHETIC_EDITOR_PRIVATE_API_KEY"


class LocalEditorModel:
    def __init__(self,case="positive"):
        self.case=case;self.calls=[];self.errors=[];self.first_ref=None
        self.blocked=threading.Event();self.release=threading.Event()
        owner=self
        class Handler(BaseHTTPRequestHandler):
            protocol_version="HTTP/1.1"
            def log_message(self,*_):pass
            def do_POST(self):
                self.close_connection=True
                try:
                    length=int(self.headers.get("Content-Length","0"))
                    if not 0<length<=256000:raise ValueError("request_bound")
                    body=json.loads(self.rfile.read(length));owner.calls.append(body)
                    if {tool["function"]["name"] for tool in body["tools"]}!=TOOLS:raise ValueError("registry_expanded")
                    wire=json.dumps(body,ensure_ascii=False)
                    if KEY in wire or "process_started" in wire or "runtime_id" in wire or "AURA Owned Editor " in wire:
                        raise ValueError("private_identity_or_key_leaked")
                    if owner.case=="stop" and len(owner.calls)==2:
                        owner.blocked.set();owner.release.wait(10)
                    step=owner.step(body)
                    message={"role":"assistant","content":None,"reasoning_content":HIDDEN}
                    reason="stop"
                    if isinstance(step,tuple):
                        name,args=step;reason="tool_calls"
                        message["tool_calls"]=[{"id":"editor-call-"+str(len(owner.calls)),"type":"function","function":{
                            "name":name,"arguments":json.dumps(args,ensure_ascii=False)}}]
                    else:message["content"]=step
                    status=200
                    if body.get("stream"):
                        chunks=[{"id":"owned-editor-fixture","object":"chat.completion.chunk","created":1,"model":"synthetic",
                            "choices":[{"index":0,"delta":message,"finish_reason":None}]},
                            {"id":"owned-editor-fixture","object":"chat.completion.chunk","created":1,"model":"synthetic",
                            "choices":[{"index":0,"delta":{},"finish_reason":reason}]}]
                        payload=("".join("data: "+json.dumps(c)+"\n\n" for c in chunks)+"data: [DONE]\n\n").encode()
                        content_type="text/event-stream"
                    else:
                        payload=json.dumps({"id":"owned-editor-fixture","object":"chat.completion","created":1,"model":"synthetic",
                            "choices":[{"index":0,"message":message,"finish_reason":reason}]}).encode()
                        content_type="application/json"
                except Exception as error:
                    owner.errors.append(type(error).__name__)
                    status=500;payload=b'{"error":{"message":"synthetic editor fixture failed"}}';content_type="application/json"
                try:
                    self.send_response(status);self.send_header("Content-Type",content_type)
                    self.send_header("Connection","close")
                    self.send_header("Content-Length",str(len(payload)));self.end_headers();self.wfile.write(payload)
                except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):pass
        self.server=ThreadingHTTPServer(("127.0.0.1",0),Handler);self.server.daemon_threads=True
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.config={"base_url":f"http://127.0.0.1:{self.server.server_port}/v1","model":"synthetic-owned-editor","api_key":KEY}

    @staticmethod
    def rows(body):return [row for row in body["messages"] if row.get("role")=="tool"]

    def step(self,body):
        tools=self.rows(body)
        if not tools:return "owned_editor_observe",{}
        # The SDK's unregistered-tool error is deliberately plain text. It is
        # a valid refusal, not one of our typed host observation payloads.
        if self.case=="unknown_tool":
            if len(tools)==1:return "write_file",{"path":"report.txt","content":"UNAUTHORIZED"}
            return "No write tool is available."
        last=json.loads(tools[-1]["content"])
        if self.case=="wrong_order":
            if len(tools)==1:
                button=next(row for row in last["observation"]["controls"] if row["name"]=="Close document")
                return "owned_editor_act",{"ref":button["ref"],"action":"invoke"}
            return "Finished (deliberately incorrect fixture claim)."
        if self.case=="stale":
            if len(tools)==1:
                self.first_ref=next(row for row in last["observation"]["controls"] if row["name"]=="Document text")["ref"]
                return "owned_editor_observe",{}
            if len(tools)==2:return "owned_editor_act",{"ref":self.first_ref,"action":"set_value","value":"STALE_MUST_NOT_BE_TYPED"}
            if last.get("code")!="DESKTOP_STALE_REF":raise AssertionError("stale_not_refused")
            return "The stale reference was refused."
        if self.case=="eof":return "Unexpected second call."
        if self.case=="stop":return "Late response must not perform an action."
        observation=last.get("observation")
        if observation is None:raise AssertionError("unexpected_native_failure")
        controls=observation["controls"]
        status=next(row for row in controls if row["name"]=="Document status")["state"]["value"]
        field=next((row for row in controls if row["name"]=="Document text"),None)
        marker=json.loads(body["messages"][1]["content"])["marker"]
        if status=="Reopened":return "The owned document was reopened and read back."
        if status in {"New","Modified"} and field and field["state"]["value"]!=marker:
            return "owned_editor_act",{"ref":field["ref"],"action":"set_value","value":marker}
        target="Save document" if status in {"New","Modified"} else "Close document" if status=="Saved" else "Reopen document" if status=="Closed" else None
        if target is None:raise AssertionError("unexpected_native_state")
        return "owned_editor_act",{"ref":next(row for row in controls if row["name"]==target)["ref"],"action":"invoke"}

    def close(self):
        self.release.set();self.server.shutdown();self.server.server_close();self.thread.join(timeout=2)


class OwnedEditorModelPureTests(unittest.TestCase):
    def test_projection_never_exposes_private_native_identity_or_other_controls(self):
        snapshot={"window":{"title":"AURA Owned Editor private-nonce","process_id":12345},"controls":[
            {"name":"Document text","ref":"opaque-ref","runtime_id":"private-runtime","actions":["set_value","scroll"],"state":{
                "value":"known-secret-key","enabled":True,"private":"never"}},
            {"name":"AURA Owned Editor private-nonce","ref":"root","actions":[],"state":{}}]}
        value=public_observation(snapshot,("known-secret-key",))
        self.assertEqual(1,len(value["controls"]))
        self.assertEqual(["set_value"],value["controls"][0]["actions"])
        wire=json.dumps(value)
        for forbidden in ("private-nonce","12345","private-runtime","known-secret-key","never"):self.assertNotIn(forbidden,wire)
        self.assertFalse(value["task_verified"])

    def test_duplicate_ipc_members_cannot_replace_a_private_identity(self):
        with self.assertRaises(ValueError):json.loads('{"token":"first","token":"second"}',object_pairs_hook=unique_members)

    def test_default_cli_never_loads_credentials_or_starts_ui(self):
        with tempfile.TemporaryDirectory(prefix="editor-sdk-offline-") as d:
            path=Path(d)/"not-created.json"
            result=subprocess.run([sys.executable,str(ROOT/"tools/qa_owned_editor_model.py"),"--output",str(path)],
                capture_output=True,encoding="utf-8",timeout=10,**({"creationflags":0x08000000} if os.name=="nt" else {}))
            self.assertEqual(0,result.returncode)
            self.assertEqual({"status":"not_run","native_input":False,"model_called":False},json.loads(result.stdout))
            self.assertFalse(path.exists())


@unittest.skipUnless(os.name=="nt" and SDK_PYTHON.is_file() and os.environ.get("AURA_OWNED_EDITOR_NATIVE_TEST")=="1",
    "explicit owned native interactive QA opt-in required")
class OwnedEditorSdkNativeTests(unittest.TestCase):
    def exercise(self,case,**kwargs):
        provider=LocalEditorModel(case)
        try:
            result=run_case(provider.config,deadline_seconds=40,**kwargs)
            self.assertEqual([],provider.errors)
            self.assertTrue(result["sdk_exact_tree_closed"] and result["fixture_and_helpers_exact_closed"],result)
            self.assertTrue(result["sdk_state_files_absent"],result)
            wire=json.dumps(result)
            self.assertNotIn(HIDDEN,wire);self.assertNotIn(KEY,wire)
            self.assertEqual(sorted(TOOLS),result["registered_tools"])
            self.assertGreaterEqual(len(provider.calls),1)
            self.last=result
            return result,provider
        finally:provider.close()

    def test_actual_sdk_chooses_native_save_close_reopen_and_independent_bytes_pass(self):
        result,provider=self.exercise("positive")
        self.assertEqual("passed",result["status"],result)
        self.assertEqual(6,len(provider.calls))
        self.assertEqual(5,result["tool_count"])
        self.assertTrue(result["independent_oracle"]["passed"])

    def test_wrong_order_and_false_model_completion_cannot_satisfy_the_goal(self):
        result,_=self.exercise("wrong_order")
        self.assertEqual("needs_user",result["agent_status"])
        self.assertTrue(result["model_final_present"])
        self.assertFalse(result["independent_oracle"]["passed"])

    def test_actual_sdk_old_native_ref_after_refresh_is_rejected_without_file_write(self):
        result,_=self.exercise("stale")
        self.assertEqual("needs_user",result["agent_status"])
        self.assertTrue(any(row.get("code")=="DESKTOP_STALE_REF" for row in result["host_observed_events"]))
        self.assertFalse(result["independent_oracle"]["passed"])

    def test_stop_with_actual_http_request_in_flight_reaps_sdk_and_fixture_once(self):
        provider=LocalEditorModel("stop")
        def cancel(kind,count,editor):
            if kind=="model_request" and count==2:
                if not provider.blocked.wait(8):raise AssertionError("actual HTTP request did not arrive")
                editor.stop("EDITOR_CONTROLLED_CANCEL")
        try:
            result=run_case(provider.config,deadline_seconds=30,on_event=cancel)
            self.last=result
            self.assertTrue(provider.blocked.is_set())
            self.assertEqual("needs_user",result["agent_status"])
            self.assertTrue(result["sdk_exact_tree_closed"] and result["fixture_and_helpers_exact_closed"],result)
            self.assertEqual(1,result["tool_count"])
            self.assertFalse(any(row.get("operation")=="act" for row in result["native_operations"]))
            self.assertFalse(result["model_final_present"])
            self.assertEqual([],provider.errors)
        finally:provider.close()

    def test_actual_sdk_eof_after_observation_is_not_completion_but_exactly_closes(self):
        result,_=self.exercise("eof",fault="eof_after_first_tool")
        self.assertEqual("EDITOR_SDK_EOF",result["error_code"])
        self.assertEqual("needs_user",result["agent_status"])
        self.assertFalse(result["model_final_present"])
        self.assertEqual(1,result["tool_count"])

    def test_unknown_model_tool_never_expands_actual_registry_or_reaches_native_broker(self):
        result,_=self.exercise("unknown_tool")
        self.assertEqual("needs_user",result["agent_status"])
        self.assertEqual(1,result["tool_count"])
        self.assertFalse(result["independent_oracle"]["passed"])


if __name__=="__main__":unittest.main()
