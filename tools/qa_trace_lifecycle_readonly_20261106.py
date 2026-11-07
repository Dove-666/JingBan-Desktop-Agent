"""Read-only actual compiled process-card journey with owned synthetic state."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import socket
import sys
from types import SimpleNamespace
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
SOURCES=('frontend/chat-ui/src/components/ExecutionDetails.tsx','frontend/chat-ui/src/App.tsx',
    'frontend/chat-ui/src/bridge.ts','desktop_pet/ui/web_chat/index.html',
    'desktop_pet/execution_trace.py','desktop_pet/chat_controller.py','desktop_pet/ui/assistant_panel.py')
DOM="""(() => ({rows:[...document.querySelectorAll('[data-message-id]')].map(n=>({id:n.dataset.messageId,
  visible:n.getClientRects().length>0,text:n.querySelector('.markdown')?.textContent,
  request:n.querySelector('[data-testid=message-request-state]')?.textContent||'',
  records:[...n.querySelectorAll('[data-testid=execution-record]')].map(r=>({state:r.dataset.executionState,
    open:r.querySelector('[data-testid=execution-toggle]').getAttribute('aria-expanded'),
    text:r.textContent,spinners:r.querySelectorAll('.spin').length}))})),
  stream:[...document.querySelectorAll('[data-stream-state]')].map(n=>({id:n.dataset.messageId,state:n.dataset.streamState,text:n.textContent}))}))()"""


def fence():return {p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in SOURCES}


def run(output):
    from PySide6.QtCore import QEvent
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from desktop_pet.execution_trace import ExecutionTrace
    from desktop_pet.chat_controller import ChatController
    from tests.test_assistant_ui import AssistantUiChat,make_panel,eval_js,wait_js,click_dom
    app=QApplication.instance() or QApplication([]);app.setQuitOnLastWindowClosed(False)
    checks={};snapshots={}
    def check(name,value):
        checks[name]=bool(value)
        if not value:
            (output/'partial.json').write_text(json.dumps({'checks':checks,'snapshots':snapshots},ensure_ascii=False,indent=2),encoding='utf8')
            panel.grab().save(str(output/'failed-visible-state.png'),'PNG')
            raise AssertionError(name)
    def new_trace(label):
        trace=ExecutionTrace('owned-no-provider');trace.event('owned-operation','tool',label)
        return trace
    def user(mid,label,trace,status='pending',cid='current',**metadata):
        return {'id':mid,'conversation_id':cid,'role':'user','source':'user','content':label,
            'metadata':{'execution_mode':'agent','request_status':status,'execution':trace.snapshot(),**metadata}}
    def snap(name):
        QTest.qWait(40);value=eval_js(panel,DOM);snapshots[name]=value
        return {r['id']:r for r in value['rows']}
    def poke():chat.messages_changed.emit();panel._queue_web_state()
    def capture(name):assert panel.grab().save(str(output/name),'PNG')
    failed=new_trace('A_FAILED_ONLY');failed.finish('failed')
    a=user('owned-a','合成任务A失败',failed,'failed',agent_error_code='backend_failed')
    chat=AssistantUiChat();chat.messages=[a];chat.streaming_reply=None;chat.busy=False
    panel=make_panel(chat);panel.resize(1000,780);panel.setWindowTitle('AURA 任务过程归属 · 独立合成数据')
    try:
        wait_js(panel,"document.querySelector('[data-testid=execution-record]')?.dataset.executionState==='failed'")
        rows=snap('A-failed')
        check('FAILED_A_COLLAPSED_NO_SPINNER',rows['owned-a']['records'][0]['open']=='false' and rows['owned-a']['records'][0]['spinners']==0)
        b_trace=new_trace('B_RUNNING_ONLY');b=user('owned-b','合成任务B开始',b_trace)
        chat.messages.append(b);chat.busy=True;chat.request_conversation_id='current';poke()
        wait_js(panel,"document.querySelector('[data-message-id=owned-b] [data-testid=execution-toggle]')?.getAttribute('aria-expanded')==='true'")
        rows=snap('B-running')
        check('NEW_B_RUNNING_BELONGS_TO_B_WITH_NO_A_ERROR',len(rows['owned-b']['records'])==1 and rows['owned-b']['records'][0]['state']=='running' and 'A_FAILED_ONLY' not in rows['owned-b']['records'][0]['text'] and not rows['owned-b']['request'])
        check('OLD_A_REMAINS_FAILED_ONLY_ON_A',rows['owned-a']['records'][0]['state']=='failed' and len(rows['owned-a']['records'])==1)
        b_trace.finish('cancelled');b['metadata'].update(execution=b_trace.snapshot(),request_status='cancelled')
        chat.busy=False;poke()
        wait_js(panel,"document.querySelector('[data-message-id=owned-b] [data-testid=execution-record]')?.dataset.executionState==='cancelled'")
        wait_js(panel,"document.querySelector('[data-message-id=owned-b] [data-testid=execution-toggle]')?.getAttribute('aria-expanded')==='false'")
        rows=snap('B-stopped')
        check('STOPPED_B_COLLAPSES_WITHOUT_RUNNING_SPINNER',rows['owned-b']['records'][0]['spinners']==0)
        capture('01-failed-A-stopped-B.png')
        c_trace=new_trace('C_RUNNING_ONLY');c=user('owned-c','合成任务C流式回复',c_trace)
        chat.messages.append(c);chat.busy=True
        chat.streaming_reply={'conversationId':'current','requestId':'owned-stream-c','text':'C_PARTIAL_ONLY','state':'streaming'};poke()
        wait_js(panel,"Boolean(document.querySelector('[data-stream-state=streaming]'))")
        rows=snap('C-streaming')
        check('C_STREAM_CARD_IS_ON_C_USER_NOT_OLD_FAILED_OR_CANCELLED',rows['owned-c']['records'][0]['state']=='running' and 'C_RUNNING_ONLY' in rows['owned-c']['records'][0]['text'] and all(not row['records'] for key,row in rows.items() if key.startswith('stream-')))
        check('C_STREAM_DOES_NOT_REVIVE_B_RUNNING',rows['owned-b']['records'][0]['state']=='cancelled' and rows['owned-b']['records'][0]['spinners']==0)
        c_trace.finish('completed');c['metadata'].update(execution=c_trace.snapshot(),request_status='complete')
        chat.messages.append({'id':'owned-c-reply','conversation_id':'current','role':'assistant','source':'assistant','content':'C_FINAL_ONLY','metadata':{'execution':c_trace.snapshot()}})
        chat.streaming_reply=None;chat.busy=False;poke()
        wait_js(panel,"Boolean(document.querySelector('[data-message-id=owned-c-reply] [data-testid=execution-record]')) && !document.querySelector('[data-stream-state]')")
        rows=snap('C-final')
        check('FINAL_C_HAS_EXACTLY_ONE_COMPLETED_CARD_ON_ITS_OWN_REPLY',not rows['owned-c']['records'] and len(rows['owned-c-reply']['records'])==1 and rows['owned-c-reply']['records'][0]['state']=='completed')
        check('FINAL_C_COLLAPSED_NO_STALE_RUNNING_OR_ERRORS',rows['owned-c-reply']['records'][0]['open']=='false' and rows['owned-c-reply']['records'][0]['spinners']==0 and not rows['owned-c-reply']['request'])
        click_dom(panel,'[data-message-id=owned-c-reply] [data-testid=execution-toggle]')
        wait_js(panel,"document.querySelector('[data-message-id=owned-c-reply] [data-testid=execution-toggle]')?.getAttribute('aria-expanded')==='true'")
        rows=snap('C-final-expanded')
        check('FINAL_C_DETAILS_HAVE_NO_A_OR_B_EVENTS','A_FAILED_ONLY' not in rows['owned-c-reply']['records'][0]['text'] and 'B_RUNNING_ONLY' not in rows['owned-c-reply']['records'][0]['text'] and 'C_RUNNING_ONLY' in rows['owned-c-reply']['records'][0]['text'])
        capture('02-C-final-owner.png')
        current=deepcopy(chat.messages)
        d_trace=new_trace('D_OTHER_CID_ONLY');chat.current_conversation_id='old';chat.current_title='独立会话D'
        chat.messages=[user('owned-d','合成D',d_trace,cid='old')];chat.request_conversation_id='old';chat.busy=True;chat.conversation_changed.emit();poke()
        wait_js(panel,"Boolean(document.querySelector('[data-message-id=owned-d]')) && !document.querySelector('[data-message-id=owned-c-reply]')")
        rows=snap('D-other-CID')
        check('CID_SWITCH_SHOWS_ONLY_D_CARD',
              {mid for mid,row in rows.items() if row['records']}=={'owned-d'}
              and rows['owned-d']['records'][0]['state']=='running'
              and all(not row['records'] and not row['request'] and not row['text'] for mid,row in rows.items() if mid!='owned-d'))
        chat.current_conversation_id='current';chat.current_title='返回合成任务';chat.messages=current;chat.request_conversation_id='current';chat.busy=False;chat.conversation_changed.emit();poke()
        wait_js(panel,"Boolean(document.querySelector('[data-message-id=owned-c-reply]')) && !document.querySelector('[data-message-id=owned-d]')")
        rows=snap('returned-current')
        check('RETURN_RESTORES_ONLY_OWN_TERMINAL_STATES',[rows[mid]['records'][0]['state'] for mid in ('owned-a','owned-b','owned-c-reply')]==['failed','cancelled','completed'])
        retry=new_trace('A_RETRY_ONLY');chat.messages[0]['metadata'].update(execution=retry.snapshot(),request_status='pending')
        chat.busy=True;poke()
        wait_js(panel,"document.querySelector('[data-message-id=owned-a] [data-testid=execution-toggle]')?.getAttribute('aria-expanded')==='true'")
        rows=snap('same-MID-retry-A')
        check('SAME_MESSAGE_NEW_ATTEMPT_DROPS_OLD_FAILURE_AND_OPENS_RUNNING',rows['owned-a']['records'][0]['state']=='running' and 'A_RETRY_ONLY' in rows['owned-a']['records'][0]['text'] and 'A_FAILED_ONLY' not in rows['owned-a']['records'][0]['text'] and not rows['owned-a']['request'])
        # Exercise the actual controller acceptance method without constructing
        # providers/store/UI: receiver supplies only its owned mutable state.
        receiver=SimpleNamespace(_closed=False,_current_id='current',_messages=deepcopy(chat.messages),
            _recoverable_messages={},_retry_messages={},messages_changed=SimpleNamespace(emit=lambda:None))
        latest=deepcopy(receiver._messages)
        ChatController._accept_execution(receiver,'old','owned-a',failed.snapshot())
        check('ACTUAL_ACCEPT_EXECUTION_REJECTS_FOREIGN_CID',receiver._messages==latest)
        ChatController._accept_execution(receiver,'current','owned-a',failed.snapshot())
        check('ACTUAL_ACCEPT_EXECUTION_REJECTS_OLDER_ATTEMPT',receiver._messages==latest)
        late=deepcopy(b_trace.snapshot());late.update(state='running',duration_ms=late['duration_ms']+99999)
        ChatController._accept_execution(receiver,'current','owned-b',late)
        check('ACTUAL_ACCEPT_EXECUTION_KEEPS_STOPPED_TRACE_TERMINAL',receiver._messages[1]['metadata']['execution']['state']=='cancelled')
        capture('03-retry-A-own-state.png')
        return {'passed':all(checks.values()),'checks':checks,'snapshots':snapshots,
            'scope':'One actual shipped React/Qt panel and host projection over synthetic messages using actual ExecutionTrace. Stream/state transitions are controlled snapshots, not real provider/task execution. Actual ChatController._accept_execution is separately called with a minimal owned receiver; worker/DB settlement is source-reviewed only.'}
    finally:
        for timer in (panel._web_publish_timer,panel._web_stream_timer,panel._web_start_timer,panel._web_timeout):timer.stop()
        panel.hide();panel._discard_partial_web();panel.deleteLater();app.sendPostedEvents(None,QEvent.Type.DeferredDelete);app.processEvents()


def main():
    output=ROOT/'artifacts/task-audit/trace-lifecycle-readonly-20261106-v3';output.mkdir(parents=True,exist_ok=False)
    before=fence();blocked=[]
    def deny(*a,**k):blocked.append('network');raise AssertionError('NETWORK_FORBIDDEN')
    try:
        with patch.object(socket,'getaddrinfo',deny),patch.object(socket.socket,'connect',deny),patch.object(socket.socket,'connect_ex',deny):result=run(output)
    except Exception as error:
        import traceback
        (output/'exception.log').write_text(traceback.format_exc(),encoding='utf8');result={'passed':False,'error_type':type(error).__name__}
    after=fence();result.update(source_before=before,source_after=after,source_unchanged=before==after,blocked_network_attempts=blocked,real_model_requests=0,system_clipboard_used=False,user_profile_used=False)
    result['passed']=result['passed'] and before==after and not blocked
    (output/'verification.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps({key:value for key,value in result.items() if key!='snapshots'},ensure_ascii=True,indent=2));return 0 if result['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
