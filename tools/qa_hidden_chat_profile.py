"""Hidden Qt snapshot profiling; owned data, no model/config/profile access.

Measures Python state building/serialization and a direct Qt signal receiver.
Does not initialize WebEngine, show windows, move a pointer, or benchmark React.
"""
from __future__ import annotations
import argparse
import cProfile
import gc
import hashlib
import json
import math
from pathlib import Path
import statistics
import sys
import tempfile
import time
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
SOURCES=("desktop_pet/chat_controller.py","desktop_pet/ui/assistant_panel.py",
         "desktop_pet/ui/chat_panel.py","desktop_pet/ui/draft_state.py",
         "desktop_pet/draft_owner.py","desktop_pet/conversations/store.py",
         "tools/qa_hidden_chat_profile.py")


def fingerprints():return {p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in SOURCES}


def benchmark(callback,iterations):
    for _ in range(4):callback()
    elapsed=[];cpu_start=time.process_time_ns()
    for _ in range(iterations):
        started=time.perf_counter_ns();callback();elapsed.append((time.perf_counter_ns()-started)/1e6)
    cpu=(time.process_time_ns()-cpu_start)/1e6
    ordered=sorted(elapsed)
    return {"iterations":iterations,"wall_median_ms":statistics.median(elapsed),
            "wall_p95_ms":ordered[math.ceil(.95*len(ordered))-1],
            "wall_min_ms":ordered[0],"wall_max_ms":ordered[-1],
            "cpu_ms_per_call":cpu/iterations,"cpu_ms_total":cpu}


def run(output,iterations=30):
    from PySide6.QtCore import QEvent,QThreadPool
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from desktop_pet.api_client import ApiClient
    from desktop_pet import chat_controller as controller_module
    from desktop_pet.chat_controller import ChatController
    from desktop_pet.conversations import ConversationStore
    from desktop_pet.settings_manager import ApiSettings
    from desktop_pet.ui.assistant_panel import AssistantChatPanel
    from tests.test_application import MemoryCredentialBackend,isolated_settings

    if type(iterations) is not int or not 5<=iterations<=200:raise ValueError("PROFILE_ITERATIONS_INVALID")
    output=Path(output).resolve()
    if output.exists() or not output.is_relative_to((ROOT/"artifacts").resolve()):raise ValueError("NEW_OWNED_PROFILE_REPORT_REQUIRED")
    output.mkdir(parents=True)
    app=QApplication.instance() or QApplication([]);app.setQuitOnLastWindowClosed(False)
    def invisible():return not any(widget.isVisible() for widget in app.topLevelWidgets())
    if not invisible():raise RuntimeError("HIDDEN_PROFILE_REQUIRES_NO_VISIBLE_WINDOWS")
    def wait(predicate):
        deadline=time.monotonic()+8
        while not predicate() and time.monotonic()<deadline:QTest.qWait(5);time.sleep(.002)
        if not predicate():raise RuntimeError("OWNED_PROFILE_CLEANUP_UNCONFIRMED")
    before=fingerprints();cases=[]
    parent=(ROOT/"agent_workspace").resolve(strict=True)
    for count in (200,800,2000):
        for chars in (200,1000):
            owner=tempfile.TemporaryDirectory(prefix="hidden-chat-profile-",dir=parent)
            runtime=Path(owner.name).resolve(strict=True);assert runtime.parent==parent
            settings=isolated_settings(runtime/"settings.ini",MemoryCredentialBackend())
            settings.save_api_settings(ApiSettings("http://127.0.0.1:1/v1","owned-profile"))
            settings.save_api_key("FAKE_OWNED_HIDDEN_PROFILE_KEY")
            store=ConversationStore(runtime/"history.sqlite3")
            chat=ChatController(settings,ApiClient(),store=store,metadata_client_factory=lambda:None)
            panel=AssistantChatPanel(chat)
            try:
                wait(lambda:not chat.history_loading and chat._local_draft_owner._loaded.is_set()
                     and QThreadPool.globalInstance().activeThreadCount()==0)
                cid=chat.current_conversation_id
                prepared=[]
                for index in range(count):
                    role="user" if index%2==0 else "assistant"
                    content=(f"合成记录{index}："+"海流与鲸鱼的读书笔记。"*100)[:chars]
                    assert len(content)==chars
                    metadata={"request_status":"complete"}
                    if role=="assistant":metadata["reply_to"]=f"owned-{index-1:05d}"
                    prepared.append((f"owned-{index:05d}",cid,role,content,
                        f"2026-11-05T12:{index//60:02d}:{index%60:02d}+00:00",role,json.dumps(metadata)))
                with store._connection() as connection:
                    store._ensure_conversation(connection,cid,"合成性能对话","2026-11-05T12:00:00+00:00")
                    connection.executemany("INSERT INTO messages(id,conversation_id,role,content,created_at,source,metadata) VALUES(?,?,?,?,?,?,?)",prepared)
                    store._touch(connection,cid,"2026-11-05T12:59:00+00:00")
                chat._messages=store.load_messages(cid)
                chat._conversations=store.list_conversations()
                chat._current_title="合成长对话"
                chat._busy=True;chat._active_request_conversation=cid
                chat._streaming_reply={"requestId":"owned-stream","conversationId":cid,"state":"streaming","text":"海流正在变化"}
                app.processEvents()
                panel._web_publish_timer.stop();panel._web_start_timer.stop();panel._web_timeout.stop()
                if not invisible() or panel.web_view is not None:raise RuntimeError("PROFILE_BECAME_VISIBLE_OR_STARTED_RENDERER")
                tree_before=hashlib.sha256(json.dumps(chat._messages,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
                tree_identity=id(chat._messages);tick=[0];sent={"count":0,"characters":0}
                def delta():
                    tick[0]+=1;chat._streaming_reply["text"]="海流正在变化："+str(tick[0])
                def state():delta();return panel.web_state()
                def receive(payload):sent["count"]+=1;sent["characters"]+=len(payload)
                panel.bridge.stateChanged.connect(receive)
                def publish():delta();panel._publish_web_state()
                state_metrics=benchmark(state,iterations)
                snapshot=panel.web_state()
                def encode():return json.dumps(snapshot,ensure_ascii=False)
                json_metrics=benchmark(encode,iterations)
                emitted_before=sent["count"]
                publish_metrics=benchmark(publish,iterations)
                emitted=sent["count"]-emitted_before
                payload=panel._last_web_state_json
                static_before=sent["count"]
                unchanged_metrics=benchmark(panel._publish_web_state,iterations)
                unchanged_emits=sent["count"]-static_before
                copies=[];original_copy=controller_module.deepcopy
                def observed_copy(value,*args,**kwargs):
                    cloned=original_copy(value,*args,**kwargs)
                    if value is chat._messages:
                        copies.append({"new_list":cloned is not value,"new_first_row":cloned[0] is not value[0],
                            "new_metadata":cloned[0]["metadata"] is not value[0]["metadata"],
                            "immutable_content_reused":cloned[0]["content"] is value[0]["content"]})
                    return cloned
                with patch.object(controller_module,"deepcopy",observed_copy),patch.object(store,"_connection",wraps=store._connection) as sql:
                    panel.web_state();conversation_connections=sql.call_count
                profile=cProfile.Profile();profile.runcall(publish);profile.create_stats()
                selected=[]
                for (file,line,name),(primitive,total,own,cumulative,callers) in profile.stats.items():
                    if name in {"deepcopy","_deepcopy_dict","_deepcopy_list","_deepcopy_atomic","web_state","getState","_publish_web_state","message_retry_state","_refresh_web_image_references","public_agent_metadata","safe_execution","iterencode"}:
                        selected.append({"function":name,"primitive_calls":primitive,"total_calls":total,
                                         "own_ms":own*1000,"cumulative_ms":cumulative*1000})
                profile.dump_stats(str(output/f"messages-{count}-chars-{chars}.prof"))
                after_hash=hashlib.sha256(json.dumps(chat._messages,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
                cases.append({"messages":count,"body_characters_each":chars,
                    "snapshot":state_metrics,"json_only":json_metrics,"stream_publish":publish_metrics,
                    "unchanged_publish":unchanged_metrics,"stream_signal_emits_including_4_warmups":emitted,
                    "unchanged_signal_emits":unchanged_emits,"payload_utf8_bytes":len(payload.encode()),
                    "payload_characters":len(payload),"messages_json_utf8_bytes":len(json.dumps(snapshot["messages"],ensure_ascii=False).encode()),
                    "history_deepcopies_per_web_state":len(copies),"history_copy_details":copies,
                    "conversation_store_connections_per_web_state":conversation_connections,
                    "cprofile_single_stream_publish":sorted(selected,key=lambda row:row["cumulative_ms"],reverse=True),
                    "source_tree_identity_unchanged":id(chat._messages)==tree_identity,
                    "source_tree_bytes_unchanged":after_hash==tree_before,
                    "panel_hidden":not panel.isVisible(),"webengine_not_initialized":panel.web_view is None})
            finally:
                chat._busy=False;chat._active_request_conversation=None;chat._streaming_reply=None
                panel.close();chat.shutdown()
                wait(lambda:QThreadPool.globalInstance().activeThreadCount()==0 and chat._local_draft_owner._stopped.is_set())
                panel.deleteLater();chat.deleteLater();app.sendPostedEvents(None,QEvent.Type.DeferredDelete);app.processEvents()
                assert runtime.resolve(strict=True).parent==parent and runtime.name.startswith("hidden-chat-profile-")
                owner.cleanup();gc.collect()
    after=fingerprints()
    report={"schema":"aura.hidden_chat_snapshot_profile.v1","python":sys.version.split()[0],
        "source_before":before,"source_after":after,"source_unchanged":before==after,"cases":cases,
        "actual_model_api_calls":0,"real_profile_or_credentials_read":False,"native_visible_windows":False,
        "all_owned_workers_closed":True,"cpu_clock_resolution_seconds":time.get_clock_info("process_time").resolution,
        "scope":"Real current ChatController with owned SQLite/INI/memory credential store, hidden actual AssistantChatPanel.web_state/ChatBridge.getState/_publish_web_state. Stream preview is seeded in memory, with no provider request.",
        "limits":["No WebEngine/WebChannel IPC, React JSON parsing, layout, rendering, network or physical-device performance claim.",
            "CPU averages are batched process time; wall timings may share CPU scheduling with other host work.",
            "Immutable message content strings are reused by deepcopy; mutable list/dict/metadata trees are recreated.",
            "message_retry_state is dictionary lookup, not a database query. Conversation-store connection counts measured separately.",
            "The artifact index is absent in this fixture; its optional existing-index query is outside this measurement."]}
    report["passed"]=before==after and invisible() and all(row["source_tree_identity_unchanged"] and row["source_tree_bytes_unchanged"] and row["panel_hidden"] and row["webengine_not_initialized"] and row["unchanged_signal_emits"]==0 for row in cases)
    (output/"verification.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    return report


if __name__=="__main__":
    parser=argparse.ArgumentParser();parser.add_argument("--output",required=True);parser.add_argument("--iterations",type=int,default=30)
    args=parser.parse_args();report=run(args.output,args.iterations)
    print(json.dumps({"report":str(Path(args.output)/"verification.json"),"passed":report["passed"],"cases":len(report["cases"]),"actual_model_api_calls":0}))
