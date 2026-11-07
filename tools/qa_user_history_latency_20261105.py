"""Explicit owned archive/render/input timing; never accesses the user profile."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import statistics
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
PATHS=('desktop_pet/chat_controller.py','desktop_pet/ui/assistant_panel.py',
    'frontend/chat-ui/src/App.tsx','frontend/chat-ui/src/bridge.ts','desktop_pet/ui/web_chat/index.html')


def source():return {p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in PATHS}


def case(count,output):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from tests.test_startup_settings_ui import StartupSettingsUiTests
    from tests.test_application import spin_until
    from tests.test_assistant_ui import eval_js,wait_js
    StartupSettingsUiTests.setUpClass();f=StartupSettingsUiTests();f.setUp()
    class NeverNetwork:
        def complete_context(self,*args,**kwargs):raise AssertionError('No sends during typing audit')
        def shutdown(self):pass
    chat=f.session.chat;chat._client=NeverNetwork();chat._metadata_factory=None
    try:
        cid=chat.current_conversation_id
        paragraph='这是一条合成历史记录，用来检查阅读和输入是否流畅。' * 8
        for i in range(count):chat.store.append_message(cid,'user' if i%2==0 else 'assistant',f'第{i+1}条。'+paragraph,'user' if i%2==0 else 'assistant')
        begun=time.perf_counter();chat.open_conversation(cid)
        spin_until(lambda:not chat.history_loading,timeout=20)
        loaded=time.perf_counter();f.session.open_chat_window();window=f.session.chat_window
        window.resize(1000,740);window.setWindowTitle('AURA 长对话输入检查 · 合成数据')
        panel=window.chat_panel
        spin_until(lambda:panel._frontend_ready,timeout=30)
        wait_js(panel,f'document.querySelectorAll("[data-message-id]").length==={count}',timeout=40)
        ready=time.perf_counter()
        wait_js(panel,'!document.querySelector("#message-input").disabled')
        eval_js(panel,'(() => {document.querySelector("#message-input").focus();return true;})()')
        target=panel.web_view.focusProxy() or panel.web_view
        latencies=[];text=''
        for char in 'abcdefghij':
            started=time.perf_counter();QTest.keyClicks(target,char);text+=char
            wait_js(panel,'document.querySelector("#message-input").value==='+json.dumps(text),timeout=15)
            latencies.append(round((time.perf_counter()-started)*1000,3))
            QTest.qWait(40)
        geometry=eval_js(panel,'(() => {const v=document.querySelector(".thread-viewport");return {messages:document.querySelectorAll("[data-message-id]").length,nodes:document.querySelectorAll("*").length,scrollHeight:v.scrollHeight,scrollTop:v.scrollTop,draft:document.querySelector("#message-input").value};})()')
        assert window.grab().save(str(output/f'history-{count}.png'))
        return {'messages':count,'archive_load_ms':round((loaded-begun)*1000,3),'mount_render_ms':round((ready-loaded)*1000,3),
            'key_to_dom_ms':latencies,'median_key_to_dom_ms':round(statistics.median(latencies),3),
            'max_key_to_dom_ms':max(latencies),'geometry':geometry,'model_requests':0}
    finally:f.tearDown()


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--run',action='store_true')
    p.add_argument('--counts',nargs='+',type=int,default=[200,800,2000]);p.add_argument('--output',type=Path,default=ROOT/'artifacts/user-ux-20261105/history-initial')
    a=p.parse_args()
    if not a.run:print('{"status":"not_run","model_requests":0}');return 0
    if a.output.exists() or any(not 2<=n<=2000 for n in a.counts):p.error('Use new output and 2..2000 owned messages')
    a.output.mkdir(parents=True);before=source();rows=[];error=None
    try:
        for count in a.counts:
            row=case(count,a.output);rows.append(row)
            (a.output/'partial.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
    except Exception as exc:
        import traceback
        (a.output/'exception.log').write_text(traceback.format_exc(),encoding='utf-8');error=type(exc).__name__
    after=source();report={'cases':rows,'error_type':error,'model_requests':0,'production_profile_used':False,'source_before':before,'source_after':after,'source_unchanged':before==after,
        'scope':'Owned synthetic archive and real production Qt/WebEngine; direct QTest key events, no physical input. Timing includes renderer callback roundtrip, not pure browser event timing.'}
    (a.output/'verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'cases':[{k:r[k] for k in ('messages','archive_load_ms','mount_render_ms','median_key_to_dom_ms','max_key_to_dom_ms')} for r in rows],'error_type':error,'source_unchanged':before==after}))
    return 0 if error is None else 1


if __name__=='__main__':raise SystemExit(main())
