"""Owned real-controller hidden panel measurement; no API or visible UI.

Compare current legacy full snapshots and negotiated stream patches in the same
frozen source/fixture. Historical before reports remain independent artifacts.
"""
from __future__ import annotations
import argparse
import gc
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from tools.qa_hidden_chat_profile import benchmark

SOURCES=('desktop_pet/ui/assistant_panel.py','frontend/chat-ui/src/bridge.ts',
         'desktop_pet/chat_controller.py','desktop_pet/ui/chat_panel.py',
         'desktop_pet/ui/draft_state.py','desktop_pet/draft_owner.py',
         'desktop_pet/conversations/store.py','tools/qa_hidden_chat_profile.py',
         'tools/qa_hidden_stream_patch.py')
def hashes():return {p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in SOURCES}


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
    if type(iterations) is not int or not 5<=iterations<=200:raise ValueError('ITERATIONS_INVALID')
    output=Path(output).resolve()
    if output.exists() or not output.is_relative_to((ROOT/'artifacts').resolve()):raise ValueError('NEW_OWNED_OUTPUT_REQUIRED')
    output.mkdir(parents=True)
    app=QApplication.instance() or QApplication([]);app.setQuitOnLastWindowClosed(False)
    invisible=lambda:not any(widget.isVisible() for widget in app.topLevelWidgets())
    if not invisible():raise RuntimeError('HIDDEN_PROFILE_REQUIRES_NO_VISIBLE_WINDOWS')
    def wait(predicate):
        deadline=time.monotonic()+10
        while not predicate() and time.monotonic()<deadline:QTest.qWait(5);time.sleep(.002)
        if not predicate():raise RuntimeError('OWNED_CLEANUP_UNCONFIRMED')
    before=hashes();cases=[];parent=(ROOT/'agent_workspace').resolve(strict=True)
    for count in (200,800,2000):
        for chars in (200,1000):
            owner=tempfile.TemporaryDirectory(prefix='hidden-stream-patch-',dir=parent)
            runtime=Path(owner.name).resolve(strict=True);assert runtime.parent==parent
            settings=isolated_settings(runtime/'settings.ini',MemoryCredentialBackend())
            settings.save_api_settings(ApiSettings('http://127.0.0.1:1/v1','owned-profile'))
            settings.save_api_key('FAKE_OWNED_HIDDEN_PROFILE_KEY')
            store=ConversationStore(runtime/'history.sqlite3')
            chat=ChatController(settings,ApiClient(),store=store,metadata_client_factory=lambda:None)
            panel=AssistantChatPanel(chat)
            try:
                wait(lambda:not chat.history_loading and chat._local_draft_owner._loaded.is_set()
                     and QThreadPool.globalInstance().activeThreadCount()==0)
                cid=chat.current_conversation_id;rows=[]
                for index in range(count):
                    role='user' if index%2==0 else 'assistant'
                    content=(f'合成记录{index}：'+'海流与鲸鱼的读书笔记。'*100)[:chars]
                    metadata={'request_status':'complete'}
                    if role=='assistant':metadata['reply_to']=f'owned-{index-1:05d}'
                    rows.append((f'owned-{index:05d}',cid,role,content,
                        f'2026-11-05T12:{index//60:02d}:{index%60:02d}+00:00',role,json.dumps(metadata)))
                with store._connection() as connection:
                    store._ensure_conversation(connection,cid,'合成性能对话','2026-11-05T12:00:00+00:00')
                    connection.executemany('INSERT INTO messages(id,conversation_id,role,content,created_at,source,metadata) VALUES(?,?,?,?,?,?,?)',rows)
                    store._touch(connection,cid,'2026-11-05T12:59:00+00:00')
                chat._messages=store.load_messages(cid);chat._conversations=store.list_conversations()
                chat._current_title='合成长对话';chat._busy=True;chat._active_request_conversation=cid
                chat._streaming_reply={'requestId':'owned-stream','conversationId':cid,'state':'streaming','text':'海流正在变化'}
                app.processEvents()
                for timer in (panel._web_publish_timer,panel._web_stream_timer,panel._web_start_timer,panel._web_timeout):timer.stop()
                fulls=[];patches=[]
                panel.bridge.stateChanged.connect(fulls.append);panel.bridge.streamingChanged.connect(patches.append)
                tree=chat._messages;tree_hash=hashlib.sha256(json.dumps(tree,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
                tick=0
                def delta():
                    nonlocal tick
                    tick+=1;chat._streaming_reply['text']='海流正在变化：'+str(tick)
                    chat.streaming_reply_changed.emit()
                initial=json.loads(panel.bridge.getState())
                panel.bridge.dispatch('ready',json.dumps({'draftDocument':initial['draftDocument']}))
                panel._web_publish_timer.stop();panel._publish_web_state();fulls.clear()
                def legacy():
                    delta();panel._web_publish_timer.stop();panel._publish_web_state()
                full_metrics=benchmark(legacy,iterations);full_payload=fulls[-1];legacy_emits=len(fulls)
                panel.bridge.dispatch('ready',json.dumps({'draftDocument':initial['draftDocument'],'streamProtocol':1}))
                panel._web_publish_timer.stop();panel._publish_web_state();fulls.clear()
                def streamed():
                    delta();panel._web_stream_timer.stop();panel._publish_web_stream()
                patch_metrics=benchmark(streamed,iterations);patch_payload=patches[-1];patch_emits=len(patches)
                history_copies=0;store_calls=0
                original_copy=controller_module.deepcopy;original_connection=store._connection
                def counted_copy(value,*args,**kwargs):
                    nonlocal history_copies
                    if value is chat._messages:history_copies+=1
                    return original_copy(value,*args,**kwargs)
                def counted_connection(*args,**kwargs):
                    nonlocal store_calls
                    store_calls+=1;return original_connection(*args,**kwargs)
                with patch.object(controller_module,'deepcopy',side_effect=counted_copy),patch.object(store,'_connection',side_effect=counted_connection),patch.object(panel,'web_state',side_effect=AssertionError('STREAM_REBUILT_FULL_STATE')):
                    streamed()
                cases.append({'messages':count,'body_characters_each':chars,'legacy_full_publish':full_metrics,
                    'negotiated_stream_patch':patch_metrics,'legacy_full_utf8_bytes':len(full_payload.encode()),
                    'patch_utf8_bytes':len(patch_payload.encode()),'legacy_signal_emits_including_4_warmups':legacy_emits,
                    'patch_signal_emits_including_4_warmups':patch_emits,'full_signal_emits_during_patch_phase':len(fulls),
                    'history_deepcopies_per_patch':history_copies,'store_connections_per_patch':store_calls,
                    'patch_fields':sorted(json.loads(patch_payload)),
                    'source_history_identity_unchanged':chat._messages is tree,
                    'source_history_bytes_unchanged':tree_hash==hashlib.sha256(json.dumps(chat._messages,ensure_ascii=False,sort_keys=True).encode()).hexdigest(),
                    'hidden':not panel.isVisible(),'webengine_not_initialized':panel.web_view is None})
            finally:
                chat._busy=False;chat._active_request_conversation=None;chat._streaming_reply=None
                for timer in (panel._web_publish_timer,panel._web_stream_timer,panel._web_start_timer,panel._web_timeout):timer.stop()
                panel.close();chat.shutdown()
                wait(lambda:QThreadPool.globalInstance().activeThreadCount()==0 and chat._local_draft_owner._stopped.is_set())
                panel.deleteLater();chat.deleteLater();app.sendPostedEvents(None,QEvent.Type.DeferredDelete);app.processEvents()
                assert runtime.resolve(strict=True).parent==parent and runtime.name.startswith('hidden-stream-patch-')
                owner.cleanup();gc.collect()
    after=hashes()
    historical=ROOT/'artifacts/user-testing-20261105/hidden-chat-profile-valid-stream-v2/verification.json'
    report={'schema':'aura.hidden_stream_patch_profile.v1','actual_model_api_calls':0,
            'source_before':before,'source_after':after,'source_unchanged':before==after,'cases':cases,
            'historical_valid_before':{'path':str(historical.relative_to(ROOT)),'sha256':hashlib.sha256(historical.read_bytes()).hexdigest()},
            'scope':'Actual current ChatController with owned SQLite/INI/memory credentials and hidden actual panel/Qt signals. A valid stream preview is seeded; no provider request.',
            'limits':['No WebEngine/WebChannel IPC, React parsing/layout/render or physical-device performance claim.','No visible SSE before was acquired, therefore no native before/after speedup claim.','Legacy-vs-patch metrics use the same frozen current backend and fixture; historical before remains a separate run.','CPU averages are batched process time; wall timing shares host scheduling.'],
            'all_owned_workers_closed':True,'all_panels_hidden':invisible()}
    report['passed']=before==after and invisible() and all(row['history_deepcopies_per_patch']==0 and row['store_connections_per_patch']==0 and row['full_signal_emits_during_patch_phase']==0 and row['hidden'] and row['webengine_not_initialized'] and row['source_history_identity_unchanged'] and row['source_history_bytes_unchanged'] for row in cases)
    path=output/'verification.json'
    with path.open('x',encoding='utf-8') as file:json.dump(report,file,ensure_ascii=False,indent=2);file.write('\n')
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True);parser.add_argument('--iterations',type=int,default=30)
    args=parser.parse_args();result=run(args.output,args.iterations)
    print(json.dumps({'report':str(Path(args.output)/'verification.json'),'passed':result['passed'],'actual_model_api_calls':0}))
