"""Owned local document transfer and queued official-DSH read acceptance."""
import base64
from dataclasses import replace
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from PySide6.QtCore import QThreadPool

from desktop_pet.agents.service import AgentService, dsh_installation, node_executable, _DSH_VERSION
from desktop_pet.agents.contracts import AgentRequest, AgentValidationError
from desktop_pet.agents.settings import AgentSettings
from desktop_pet.remote.files import FileInbox, MAX_FILE_BYTES, MAX_CHUNK_BYTES
from desktop_pet.remote.errors import RemoteError
from desktop_pet.conversations.store import ConversationStore, REMOTE_SYNC_SCHEMA_VERSION
from desktop_pet.remote.sync import RemoteSyncStore, sanitize_message
from desktop_pet.settings_manager import ApiSettings
from tests import test_chat_images as fixture
from tests.test_remote_controller import PEER, OTHER_PEER, spin_until


def upload_params(cid, content, offset=0, chunk=None, **changes):
    params = {"conversation_id": cid, "filename": "资料.txt", "mime_type": "text/plain",
              "size": len(content), "sha256": hashlib.sha256(content).hexdigest(), "offset": offset,
              "data_base64": base64.b64encode(content[offset:offset+MAX_CHUNK_BYTES] if chunk is None else chunk).decode()}
    return dict(params, **changes)


class FileInboxTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.inbox = FileInbox(self.root / "inbox")
        self.cid = str(uuid4())

    def tearDown(self): self.directory.cleanup()

    def test_chunks_replay_restart_and_exact_materialized_bytes(self):
        data = bytes(range(256)) * 1600
        first = self.inbox.upload(PEER, upload_params(self.cid, data))
        self.assertFalse(first["complete"])
        self.assertEqual(first["offset"], MAX_CHUNK_BYTES)
        self.assertEqual(self.inbox.upload(PEER, upload_params(self.cid, data)), first)
        self.inbox = FileInbox(self.inbox.directory)
        last = self.inbox.upload(PEER, upload_params(self.cid, data, MAX_CHUNK_BYTES))
        self.assertTrue(last["complete"])
        self.assertEqual(self.inbox.upload(PEER, upload_params(self.cid, data)), last)
        item = self.inbox.materialize(PEER, self.cid, last["id"], self.root / "workspace")
        self.assertEqual(Path(item["path"]).read_bytes(), data)
        self.assertEqual(self.inbox._path(PEER, self.cid, last["id"]).read_bytes(), data)
        self.assertEqual(item["size"], len(data))

    def test_partial_and_complete_are_peer_and_conversation_scoped(self):
        data = b"first-second"
        first = self.inbox.upload(PEER, upload_params(self.cid, data, chunk=b"first-"))
        for peer, cid in ((OTHER_PEER,self.cid),(PEER,str(uuid4()))):
            with self.assertRaises(RemoteError): self.inbox.metadata(peer,cid,first["id"])
            with self.assertRaises(RemoteError): self.inbox.upload(peer,upload_params(cid,data,6))
        with self.assertRaises(RemoteError): self.inbox.metadata(PEER,self.cid,first["id"])
        self.inbox.upload(PEER,upload_params(self.cid,data,6))
        with self.assertRaises(RemoteError): self.inbox.metadata(OTHER_PEER,self.cid,first["id"])

    def test_conflicting_chunk_metadata_gap_and_bad_digest_are_rejected(self):
        data = b"abcdef"
        params = upload_params(self.cid,data,chunk=b"abc")
        self.inbox.upload(PEER,params)
        for invalid in (dict(params,data_base64=base64.b64encode(b"xyz").decode()),
                        dict(params,filename="different.txt"),upload_params(self.cid,data,4),
                        upload_params(self.cid,data,3,chunk=b"xxx")):
            with self.subTest(params=invalid), self.assertRaises(RemoteError): self.inbox.upload(PEER,invalid)
        self.assertTrue(self.inbox.upload(PEER,upload_params(self.cid,data,3))["complete"])

    def test_empty_binary_filename_sanitizing_and_input_limits(self):
        result = self.inbox.upload(PEER, upload_params(self.cid,b"",filename="../../document.zip",mime_type="application/zip"))
        self.assertEqual(result["filename"],"document.zip")
        self.assertTrue(result["complete"])
        self.assertEqual(Path(self.inbox.materialize(PEER,self.cid,result["id"],self.root/"work")["path"]).read_bytes(),b"")
        for changes in ({"size":MAX_FILE_BYTES+1},{"offset":-1},{"data_base64":"?"},{"filename":"CON.txt"},
                        {"filename":"bad\nname"},{"mime_type":"text/plain\r\nx-inject: true"},
                        {"data_base64":base64.b64encode(b"a"*(MAX_CHUNK_BYTES+1)).decode()},
                        {"path":"C:/private.txt"}):
            with self.subTest(changes=list(changes)), self.assertRaises(RemoteError):
                self.inbox.upload(PEER,upload_params(self.cid,b"data",**changes))

    def test_quota_rejects_before_creating_another_partial(self):
        with patch("desktop_pet.remote.files.MAX_INBOX_BYTES",6):
            self.inbox.upload(PEER,upload_params(self.cid,b"abcdef",chunk=b"a"))
            with self.assertRaises(RemoteError): self.inbox.upload(OTHER_PEER,upload_params(self.cid,b"x"))

    def test_deleted_data_reclaims_quota_and_can_restart_upload(self):
        params = upload_params(self.cid,b"abcdef")
        with patch("desktop_pet.remote.files.MAX_INBOX_BYTES",6):
            self.inbox.upload(PEER,params)
            self.inbox._path(PEER,self.cid,params["sha256"]).unlink()
            self.assertTrue(self.inbox.upload(PEER,params)["complete"])
            self.inbox._path(PEER,self.cid,params["sha256"]).unlink()
            self.assertTrue(self.inbox.upload(OTHER_PEER,upload_params(self.cid,b"xyz"))["complete"])

    def test_tampered_input_and_symlink_escape_are_never_materialized(self):
        result = self.inbox.upload(PEER,upload_params(self.cid,b"abc"))
        path = self.inbox._path(PEER,self.cid,result["id"])
        path.write_bytes(b"xyz")
        with self.assertRaises(RemoteError): self.inbox.materialize(PEER,self.cid,result["id"],self.root/"work")
        path.write_bytes(b"abc")
        work = self.root/"work"
        work.mkdir()
        outside = self.root/"outside"
        outside.mkdir()
        try: (work/".aura-attachments").symlink_to(outside,target_is_directory=True)
        except OSError: self.skipTest("native symlink privilege unavailable")
        with self.assertRaises(RemoteError): self.inbox.materialize(PEER,self.cid,result["id"],work)
        self.assertEqual(list(outside.iterdir()),[])

    def test_service_accepts_only_intact_host_materialized_attachment(self):
        result = self.inbox.upload(PEER,upload_params(self.cid,b"abc"))
        workspace = self.root/"work"
        item = self.inbox.materialize(PEER,self.cid,result["id"],workspace)
        context = {"api":{"base_url":"http://127.0.0.1:9999/v1","model":"fixture"},"current_file":item}
        settings = AgentSettings(workspace=str(workspace))
        request = AgentRequest("Read file.","agent")
        self.assertEqual(AgentService._context(context,"synthetic-key",request,settings)[1]["current_file"],item)
        for change in ({"path":str(self.root/"outside.txt")},{"name":"../escape"},{"sha256":"a"*64}):
            with self.subTest(change=change), self.assertRaises(AgentValidationError):
                AgentService._context(dict(context,current_file=dict(item,**change)),"synthetic-key",request,settings)

    def test_installed_v13_projection_migrates_and_public_names_have_no_paths(self):
        metadata = {"file_id":"a"*64,"file_name":"report.pdf","file_size":12,"file_mime":"application/pdf"}
        path = self.root/"history.sqlite3"
        with patch("desktop_pet.conversations.store.REMOTE_SYNC_SCHEMA_VERSION",13):
            store = ConversationStore(path)
            message = store.append_message(self.cid,"user","Read this","user",metadata=metadata)
        with store._connection() as db:
            before = [tuple(row) for row in db.execute("SELECT * FROM messages")]
        upgraded = ConversationStore(path)
        upgraded.initialize()
        public = next(item["data"] for item in RemoteSyncStore(upgraded).snapshot()["items"] if item["id"]==message["id"])
        self.assertEqual(public["file_name"],"report.pdf")
        self.assertEqual(sanitize_message(message)["file_name"],"report.pdf")
        self.assertIsNone(sanitize_message(dict(message,role="assistant"))["file_name"])
        self.assertIsNone(sanitize_message(dict(message,metadata=dict(metadata,file_name="C:/private.txt")))["file_name"])
        with upgraded._connection() as db:
            self.assertEqual([tuple(row) for row in db.execute("SELECT * FROM messages")],before)
            self.assertEqual(db.execute("SELECT version FROM remote_sync_schema").fetchone()[0],REMOTE_SYNC_SCHEMA_VERSION)


class RemoteFileQueueTests(unittest.TestCase):
    setUpClass = classmethod(fixture.ChatImageTests.setUpClass.__func__)
    setUp = fixture.ChatImageTests.setUp
    tearDown = fixture.ChatImageTests.tearDown
    rpc = fixture.ChatImageTests.rpc

    def configure(self, base_url="http://127.0.0.1:9999/v1"):
        self.workspace = self.path/"workspace"
        self.workspace.mkdir(exist_ok=True)
        workspace_scope = patch("desktop_pet.agents.settings.agent_workspace_root",lambda:self.workspace)
        workspace_scope.start()
        self.addCleanup(workspace_scope.stop)
        self.settings.save_api_settings(ApiSettings(base_url,"fixture-model",10))
        self.settings.save_api_key("synthetic-file-test-key")
        self.settings.save_agent_settings(replace(self.settings.load_agent_settings(),enabled=True,
            computer_enabled=False,approval_mode="full",workspace=str(self.workspace),timeout_seconds=60,max_steps=0))
        self.chat.refresh_agent_settings()

    def test_rpc_capability_admission_metadata_sync_and_duplicate_conflicts(self):
        self.configure()
        self.chat._set_busy(True)
        self.assertIn("file.upload",self.rpc("status")["result"]["capabilities"])
        result = self.rpc("file.upload",upload_params(self.cid,b"owned file"))
        self.assertTrue(result["ok"],result)
        file_id = result["result"]["id"]
        request_id = str(uuid4())
        params = {"conversation_id":self.cid,"text":"读取附件","file_id":file_id}
        ack = self.rpc("agent.send",params,request_id)
        self.assertTrue(ack["ok"],ack)
        self.assertEqual(self.rpc("agent.send",params,request_id)["result"]["message_id"],ack["result"]["message_id"])
        self.assertFalse(self.rpc("agent.send",dict(params,file_id="f"*64),request_id)["ok"])
        row = self.chat.store.read_message(self.cid,ack["result"]["message_id"])
        self.assertEqual(row["metadata"]["file_id"],file_id)
        self.assertEqual(row["metadata"]["file_name"],"资料.txt")
        snapshot = self.remote.sync.snapshot(limit=100)
        public = next(item["data"] for item in snapshot["items"] if item["id"]==row["id"])
        self.assertEqual((public["file_name"],public["file_size"],public["file_id"]),("资料.txt",10,file_id))
        changes = self.remote.sync.pull()["changes"]
        self.assertEqual(next(item["data"]["file_name"] for item in changes if item["id"]==row["id"]),"资料.txt")
        serialized = json.dumps({"snapshot":snapshot,"pull":changes})
        for private in (str(self.remote.files.directory),str(self.workspace),"synthetic-file-test-key"):
            self.assertNotIn(private,serialized)
        next_ack = self.rpc("agent.send",{"conversation_id":self.cid,"text":"下一条纯文字"})
        self.assertTrue(next_ack["ok"],next_ack)
        self.assertNotIn("file_id",self.chat.store.read_message(self.cid,next_ack["result"]["message_id"])["metadata"])
        self.assertFalse(self.rpc("agent.send",dict(params,conversation_id=str(uuid4())))["ok"])
        self.assertTrue(self.remote.revoke_device(PEER))
        self.assertFalse(self.rpc("file.upload",upload_params(self.cid,b"next"))["ok"])

    def test_completed_artifact_owners_use_assistant_metadata_or_legacy_job_not_last_reply(self):
        refs = ["artifact_"+c*64 for c in "abcde"]
        user = self.chat.store.append_message(self.cid,"user","Make a file","user")
        first = self.chat.store.complete_request(self.cid,user["id"],"Done",artifact_refs=[refs[0]])
        legacy = self.chat.store.append_message(self.cid,"assistant","Old file","assistant",metadata={"artifacts":[{"reference":refs[1]}]})
        queued = self.chat.store.append_message(self.cid,"assistant","Queue file","assistant")
        newest = self.chat.store.append_message(self.cid,"assistant","Unrelated later reply","assistant")
        jobs = [SimpleNamespace(state="completed",reply_id=queued["id"],artifact_refs=(refs[2],)),
                SimpleNamespace(state="interrupted",reply_id=newest["id"],artifact_refs=(refs[3],))]
        with patch.object(self.chat._agent_service,"artifacts",return_value=[{"reference":ref,"name":"same.txt"} for ref in refs]), \
                patch.object(self.chat._agent_queue.queue,"list_jobs",return_value=jobs):
            values = self.chat.agent_artifacts_for(self.cid)
        self.assertEqual([item["message_id"] for item in values],[first["id"],legacy["id"],queued["id"],None,None])

    def test_real_official_dsh_reads_uploaded_file_through_queue_without_reattaching_next_turn(self):
        if node_executable() is None or any(value != _DSH_VERSION for value in dsh_installation().values()):
            self.skipTest("Pinned official DSH runtime not installed")
        data = "这是手机上传的本地测试文档。SENTINEL_REMOTE_FILE_721\n第二行原文。\n".encode()
        requests, errors, contexts = [], [], []
        owner = self
        digest = hashlib.sha256(data).hexdigest()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_POST(self):
                try:
                    body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    requests.append(body)
                    count = len(requests)
                    target = owner.workspace/".aura-attachments"/owner.cid/digest/"资料.txt"
                    if count == 1:
                        owner.assertEqual(target.read_bytes(),data)
                        user_contents = []
                        for message in body["messages"]:
                            if message["role"] != "user": continue
                            content = message["content"]
                            if isinstance(content,str): user_contents.append(content)
                            elif isinstance(content,list): user_contents.extend(item.get("text","") for item in content if isinstance(item,dict))
                        owner.assertTrue(json.dumps(str(target),ensure_ascii=False)[1:-1] in "\n".join(user_contents),
                                         "Uploaded path missing from current user prompt")
                        owner.assertIn("read",{tool["function"]["name"] for tool in body["tools"]})
                    elif count == 2:
                        results = [m for m in body["messages"] if m["role"]=="tool"]
                        owner.assertIn("SENTINEL_REMOTE_FILE_721",json.dumps(results,ensure_ascii=False))
                    elif count not in (3,4): raise AssertionError("Unexpected model call")
                    self.send_response(200)
                    self.send_header("Content-Type","text/event-stream")
                    self.send_header("Connection","close")
                    self.end_headers()
                    def emit(delta,finish=None):
                        frame = {"id":"chatcmpl-owned-file","object":"chat.completion.chunk","created":1,
                            "model":"fixture-model","choices":[{"index":0,"delta":delta,"finish_reason":finish}]}
                        self.wfile.write(("data: "+json.dumps(frame)+"\n\n").encode())
                    emit({"role":"assistant"})
                    if count in (1,2):
                        tool = "read" if count==1 else "present"
                        arguments = {"file_path":str(target)} if count==1 else {"files":[{"path":str(target)}]}
                        emit({"tool_calls":[{"index":0,"id":f"file-call-{count}","type":"function","function":{
                            "name":tool,"arguments":json.dumps(arguments)}}]})
                    else: emit({"content":"已读取：SENTINEL_REMOTE_FILE_721，原文件已交付。" if count==3 else "好的。"})
                    emit({},"tool_calls" if count in (1,2) else "stop")
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                    self.close_connection=True
                except Exception as exc:
                    errors.append(repr(exc))
                    self.send_error(500)
        server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
        server.daemon_threads=True
        worker=threading.Thread(target=server.serve_forever,daemon=True)
        worker.start()
        try:
            self.configure(f"http://127.0.0.1:{server.server_port}/v1")
            original = self.chat._agent_service.execute
            def captured(settings,key,request,context,*args,**kwargs):
                contexts.append(context.copy())
                return original(settings,key,request,context,*args,**kwargs)
            with patch("desktop_pet.agents.settings.agent_workspace_root",lambda:self.workspace), \
                    patch.object(self.chat._agent_service,"execute",side_effect=captured):
                upload = self.rpc("file.upload",upload_params(self.cid,data))
                self.assertTrue(upload["ok"],upload)
                ack = self.rpc("agent.send",{"conversation_id":self.cid,"text":"读取附件并告诉我其中的测试标识，再把原文件交付给我，不要修改文件。","file_id":digest})
                self.assertTrue(ack["ok"],ack)
                first = ack["result"]["job_id"]
                spin_until(lambda:self.chat._agent_queue.queue.get(first).state in {"completed","interrupted","blocked"},timeout=40)
                self.assertEqual(self.chat._agent_queue.queue.get(first).state,"completed",(errors,self.chat.error))
                spin_until(lambda:not self.chat.busy and not QThreadPool.globalInstance().activeThreadCount())
                job = self.chat._agent_queue.queue.get(first)
                self.assertEqual(len(job.artifact_refs),1)
                reply = self.chat.store.read_message(self.cid,job.reply_id)
                self.assertEqual(reply["metadata"]["artifact_refs"],list(job.artifact_refs))
                artifacts = self.chat.agent_artifacts_for(self.cid)
                self.assertEqual(artifacts[0]["message_id"],job.reply_id)
                again = self.rpc("agent.send",{"conversation_id":self.cid,"text":"回复好的即可。"})
                self.assertTrue(again["ok"],again)
                second = again["result"]["job_id"]
                spin_until(lambda:self.chat._agent_queue.queue.get(second).state in {"completed","interrupted","blocked"},timeout=40)
                self.assertEqual(self.chat._agent_queue.queue.get(second).state,"completed",(errors,self.chat.error))
                spin_until(lambda:not self.chat.busy and not QThreadPool.globalInstance().activeThreadCount())
            self.assertFalse(errors,errors)
            self.assertEqual(len(requests),4)
            self.assertEqual(len(contexts),2)
            self.assertEqual(Path(contexts[0]["current_file"]["path"]).read_bytes(),data)
            self.assertNotIn("current_file",contexts[1])
            self.assertEqual(self.remote.files._path(PEER,self.cid,digest).read_bytes(),data)
            self.assertEqual(self.chat.store.load_messages(self.cid)[-1]["content"],"好的。")
            self.assertEqual(self.chat.agent_artifacts_for(self.cid)[0]["message_id"],job.reply_id)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(5)


if __name__ == "__main__": unittest.main()
