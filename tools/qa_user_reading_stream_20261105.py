"""Owned production Qt/WebEngine reading/draft/stream journey; no real API or profile."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from queue import Empty, Queue
import sys
import threading
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SOURCES = ("frontend/chat-ui/src/App.tsx", "frontend/chat-ui/src/styles.css",
    "desktop_pet/ui/web_chat/index.html", "desktop_pet/ui/chat_panel.py",
    "desktop_pet/ui/assistant_panel.py", "desktop_pet/chat_controller.py")
POSITION = r"""(() => {
 const vp=document.querySelector('.thread-viewport'),box=vp.getBoundingClientRect();
 const first=[...vp.querySelectorAll('[data-message-id]')].find(n=>n.getBoundingClientRect().bottom>box.top+4);
 const r=first?.getBoundingClientRect(),input=document.querySelector('#message-input');
 return {top:vp.scrollTop,height:vp.scrollHeight,client:vp.clientHeight,gap:vp.scrollHeight-vp.scrollTop-vp.clientHeight,
  anchor:first?.dataset.messageId,offset:r?r.top-box.top:null,anchorText:first?.textContent.slice(0,100),
  draft:input.value,selection:[input.selectionStart,input.selectionEnd],focused:document.activeElement===input,
  imageButtonDisabled:document.querySelector('[data-testid=composer-image-picker]').disabled,
  sendDisabled:document.querySelector('[data-testid=send-button]').disabled,
  stopVisible:Boolean(document.querySelector('[data-testid=stop-button]'))};
})()"""


def fingerprints():
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCES}


def run(output):
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QImage, QColor, QWheelEvent
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from desktop_pet.api_client import ApiClient, ApiError, ChatReply
    from tests.test_startup_settings_ui import StartupSettingsUiTests
    from tests.test_application import spin_until
    from tests.test_assistant_ui import eval_js, wait_js, set_dom_text, click_dom

    class SyntheticStream(ApiClient):
        def __init__(self):
            super().__init__()
            self.controls, self.requests = [], []
            self.lock = threading.Lock()

        def complete_context(self, settings, key, messages, *, on_delta=None, cancellation_event=None, **options):
            queue = Queue()
            with self.lock:
                self.controls.append(queue)
                last = messages[-1]["content"]
                self.requests.append({"last_kind": type(last).__name__, "image_parts":
                    sum(part.get("type") == "image_url" for part in last) if isinstance(last, list) else 0})
            text = "合成流式回复开始。"
            on_delta(text)
            while True:
                if cancellation_event.is_set():
                    raise ApiError("cancelled", "cancelled", partial_reply=text)
                try: kind, value = queue.get(timeout=.03)
                except Empty: continue
                if kind == "finish": return ChatReply(value)
                text = value; on_delta(text)

        def chunk(self, text): self.controls[-1].put(("chunk", text))
        def finish(self, text): self.controls[-1].put(("finish", text))
        def shutdown(self):
            for queue in self.controls: queue.put(("finish", "合成线程退出。"))
            super().shutdown()

    StartupSettingsUiTests.setUpClass()
    fixture = StartupSettingsUiTests(); fixture.setUp()
    chat = fixture.session.chat
    client = SyntheticStream(); chat._client = client; chat._metadata_factory = None
    checks, records = {}, {}

    def position(label):
        QTest.qWait(100)
        records[label] = eval_js(panel, POSITION)
        return records[label]

    def screenshot(name):
        QTest.qWait(80); assert window.grab().save(str(output / name))

    def same_anchor(a, b):
        return a["anchor"] == b["anchor"] and abs(a["offset"] - b["offset"]) <= 2

    def scroll_up():
        # Trigger genuine renderer wheel intent before selecting a deterministic
        # reading point; no physical cursor or global OS input is used.
        point=eval_js(panel,"(() => {const r=document.querySelector('.thread-viewport').getBoundingClientRect();return {x:r.left+r.width/2,y:r.top+r.height/2};})()")
        target=panel.web_view.focusProxy() or panel.web_view
        panel.web_view.setFocus();target.setFocus();fixture.app.processEvents()
        local=target.mapFrom(panel.web_view,QPoint(round(point['x']),round(point['y'])))
        event=QWheelEvent(QPointF(local),QPointF(target.mapToGlobal(local)),QPoint(),QPoint(0,960),
            Qt.MouseButton.NoButton,Qt.KeyboardModifier.NoModifier,Qt.ScrollPhase.NoScrollPhase,False)
        QApplication.sendEvent(target,event);QTest.qWait(80)
        eval_js(panel, "(() => {const n=document.querySelector('.thread-viewport'); n.scrollTop=(n.scrollHeight-n.clientHeight)*.32;return true;})()")
        QTest.qWait(100)
        assert eval_js(panel,"(() => {const v=document.querySelector('.thread-viewport');return v.scrollTop>100&&v.scrollHeight-v.scrollTop-v.clientHeight>v.clientHeight;})()"), 'The wheel fixture must actually leave the latest messages'

    def open_conversation(cid):
        click_dom(panel, '[data-testid="history-toggle"]')
        selector = '[data-conversation-id=' + json.dumps(cid) + '] .conversation-link'
        wait_js(panel, "Boolean(document.querySelector(" + json.dumps(selector) + "))")
        click_dom(panel, selector)
        spin_until(lambda: chat.current_conversation_id == cid and not chat.history_loading)
        wait_js(panel, "!document.querySelector('#message-input').disabled")
        QTest.qWait(180)

    try:
        a, b = chat.current_conversation_id, str(uuid4())
        for cid, prefix in ((a, "A"), (b, "B")):
            for index in range(40):
                content = f"{prefix} 合成记录 {index:02d}。仅在临时测试配置中显示。"
                if cid == a and index == 9: content += "唯一检索定位词：蓝鲸纸飞机。"
                chat.store.append_message(cid, "user", content, "user")
                chat.store.append_message(cid, "assistant", "回复内容保持简短，用来观察阅读位置。\n\n" + "这是不会发送到真实API的合成文字。" * 3, "assistant")
        chat.open_conversation(a); spin_until(lambda: not chat.history_loading)
        fixture.session.open_chat_window(); window = fixture.session.chat_window
        window.resize(880,680); window.setWindowTitle("AURA 阅读与流式交互 · 独立合成数据")
        panel = window.chat_panel
        spin_until(lambda: panel._frontend_ready, timeout=20)
        wait_js(panel, "document.querySelectorAll('[data-message-id]').length>=80")
        draft_a = "A 会话保留的草稿。\n第二行内容不会自动发送。"
        set_dom_text(panel, "#message-input", draft_a)
        scroll_up(); saved_a = position("a_before_switch")
        screenshot("a-before-switch.png")
        open_conversation(b)
        draft_b = "B 会话独立草稿。"
        set_dom_text(panel, "#message-input", draft_b)
        scroll_up(); saved_b = position("b_before_switch")
        open_conversation(a); returned_a = position("a_after_return")
        checks["CONVERSATION_A_DRAFT_RESTORED"] = returned_a["draft"] == draft_a
        checks["CONVERSATION_A_READING_POSITION_RESTORED"] = same_anchor(saved_a, returned_a)
        screenshot("a-after-return.png")
        open_conversation(b); returned_b = position("b_after_return")
        checks["CONVERSATION_B_DRAFT_RESTORED"] = returned_b["draft"] == draft_b
        checks["CONVERSATION_B_READING_POSITION_RESTORED"] = same_anchor(saved_b, returned_b)

        # Real host body search must focus its matching message, even above the latest content.
        click_dom(panel, '[aria-label="搜索会话 · Ctrl+F"]')
        set_dom_text(panel, '[data-testid="conversation-search"]', "蓝鲸纸飞机")
        wait_js(panel, f"document.querySelectorAll('.conversation-row').length===1 && Boolean(document.querySelector('[data-conversation-id=\"{a}\"]'))")
        open_conversation(a)
        target_id=next(row['id'] for row in chat.messages if '蓝鲸纸飞机' in row['content'])
        records['history_search_focus']=panel.web_state().get('focusRequest')
        visible_hit="""(() => {const n=document.querySelector(SELECTOR);if(!n)return false;const r=n.getBoundingClientRect(),v=document.querySelector('.thread-viewport').getBoundingClientRect();return r.bottom>v.top&&r.top<v.bottom;})()""".replace('SELECTOR',json.dumps('[data-message-id='+json.dumps(target_id)+']'))
        try:wait_js(panel,visible_hit)
        except AssertionError:
            records['history_search_failed_position']=position('history_search_failed_view')
            records['history_search_failed_focus']=panel.web_state().get('focusRequest')
            records['history_search_failed_target']=eval_js(panel,"""(() => {const n=document.querySelector(SELECTOR);return n?{text:n.textContent,top:n.getBoundingClientRect().top,bottom:n.getBoundingClientRect().bottom}:null;})()""".replace('SELECTOR',json.dumps('[data-message-id='+json.dumps(target_id)+']')))
            screenshot('history-search-failure.png')
            raise
        search = position("after_history_body_search")
        checks["HISTORY_BODY_SEARCH_LANDS_ON_HIT"] = eval_js(panel,visible_hit)
        checks["HISTORY_SEARCH_PRESERVES_DRAFT"] = search["draft"] == draft_a
        click_dom(panel, '[aria-label="关闭搜索"]')
        position("after_history_search_close")
        checks["CLOSING_HISTORY_SEARCH_KEEPS_MATCH_VISIBLE"] = same_anchor(search, records["after_history_search_close"])
        eval_js(panel, "(() => {window.dispatchEvent(new KeyboardEvent('keydown',{key:'f',ctrlKey:true,shiftKey:true,bubbles:true}));return true;})()")
        wait_js(panel, "Boolean(document.querySelector('[data-testid=message-search]'))")
        set_dom_text(panel, '[data-testid="message-search"]', "合成记录 09")
        wait_js(panel, "document.querySelector('.message-find-count').textContent==='1 / 1'")
        position("after_in_conversation_find")
        click_dom(panel, '[aria-label="关闭对话内查找"]')
        checks["IN_CONVERSATION_FIND_CLOSE_PRESERVES_DRAFT"] = position("after_in_conversation_find_close")["draft"] == draft_a
        screenshot("find-closed-keeps-reading.png")

        # Start an actual controller worker, with every delta supplied in this process.
        set_dom_text(panel, "#message-input", "合成流式显示测试")
        eval_js(panel, """(() => {
          window.qaReadingTrace=[];
          const v=document.querySelector('.thread-viewport'),column=document.querySelector('.reading-column');
          const record=event=>window.qaReadingTrace.push({event,at:performance.now(),top:v.scrollTop,
            height:v.scrollHeight,client:v.clientHeight,messages:v.querySelectorAll('[data-message-id]').length,
            stream:Boolean(v.querySelector('[data-stream-state]')),draft:document.querySelector('#message-input').value.length});
          v.addEventListener('scroll',()=>record('scroll'));
          window.qaReadingResize=new ResizeObserver(()=>record('resize'));window.qaReadingResize.observe(column);window.qaReadingResize.observe(v);
          record('before-send');return true;
        })()""")
        click_dom(panel, '[data-testid="send-button"]')
        spin_until(lambda: len(client.requests)==1 and chat.streaming_reply is not None)
        wait_js(panel, "Boolean(document.querySelector('[data-stream-state=streaming]'))")
        checks["EXPLICIT_SEND_RETURNS_TO_LATEST"] = position("stream_started")["gap"] <= 3
        screenshot("stream-started.png")
        growing = "合成流式回复开始。\n\n" + "流式段落用于验证向上阅读时不被打断。\n\n" * 12
        client.chunk(growing)
        wait_js(panel, "document.querySelector('[data-stream-state=streaming]').textContent.includes('流式段落')")
        checks["STREAM_FOLLOWS_LATEST_BY_DEFAULT"] = position("stream_grown_at_bottom")["gap"] <= 3
        screenshot("stream-grew-at-latest.png")
        records['send_scroll_trace']=eval_js(panel,"window.qaReadingTrace")
        scroll_up(); reading = position("stream_manual_reading")
        draft_next = "\n".join(f"第 {i} 行：下一轮待发送草稿。" for i in range(1,13))
        set_dom_text(panel, "#message-input", draft_next)
        eval_js(panel, "(() => {const i=document.querySelector('#message-input');i.setSelectionRange(4,11);return true;})()")
        typed = position("stream_multiline_typed")
        for index in range(3):
            client.chunk(growing + ("追加合成段落。\n\n" * (index+1)*8))
            QTest.qWait(130)
        streaming = position("stream_while_reading")
        checks["STREAM_DOES_NOT_PULL_MANUAL_READER"] = same_anchor(reading, streaming)
        checks["STREAM_PRESERVES_TYPED_MULTILINE_DRAFT"] = streaming["draft"] == draft_next
        checks["STREAM_PRESERVES_EDITOR_SELECTION"] = streaming["selection"] == typed["selection"]
        checks["BUSY_IMAGE_PICKER_IS_DISABLED"] = streaming["imageButtonDisabled"]
        image = QImage(80,60,QImage.Format.Format_RGB32); image.fill(QColor("#5b8fbd"))
        image_path = fixture.base/"owned-next-message.png"; assert image.save(str(image_path))
        with patch('desktop_pet.ui.chat_panel.QFileDialog.getOpenFileName', return_value=(str(image_path),"")) as picker:
            click_dom(panel, '[data-testid="composer-image-picker"]')
            checks["DISABLED_IMAGE_PICKER_DOES_NOT_OPEN_DIALOG"] = picker.call_count == 0
        screenshot("stream-manual-reading-multiline.png")
        click_dom(panel, '[data-testid="stop-button"]')
        spin_until(lambda: not chat.busy)
        wait_js(panel, "Boolean(document.querySelector('[data-stream-state=interrupted]'))")
        stopped = position("after_stop")
        checks["STOP_PRESERVES_DRAFT_AND_PARTIAL"] = stopped["draft"] == draft_next and not stopped["imageButtonDisabled"]
        checks["STOP_PRESERVES_READING_POSITION"] = same_anchor(streaming, stopped)
        with patch('desktop_pet.ui.chat_panel.QFileDialog.getOpenFileName', return_value=(str(image_path),"")) as picker:
            click_dom(panel, '[data-testid="composer-image-picker"]')
            checks["IMAGE_PICKER_RESTORED_AFTER_STOP"] = picker.call_count == 1
        wait_js(panel, "Boolean(document.querySelector('[data-testid=composer-attachment] img')?.naturalWidth>0)")
        checks["POST_STOP_IMAGE_SELECTION_PRESERVES_DRAFT"] = position("after_stop_image_staged")["draft"] == draft_next
        screenshot("after-stop-image-draft.png")
        click_dom(panel, '[data-testid="send-button"]')
        spin_until(lambda: len(client.requests)==2 and chat.streaming_reply is not None)
        latest_draft = "下一轮新的未发送草稿。\n完成回复后也应保留。"
        set_dom_text(panel, "#message-input", latest_draft)
        client.finish("合成最终回复完成。")
        spin_until(lambda: not chat.busy)
        wait_js(panel, "!document.querySelector('[data-testid=composer-attachment]')")
        final = position("after_followup_completed")
        checks["COMPLETION_PRESERVES_NEWER_DRAFT"] = final["draft"] == latest_draft
        checks["FOLLOWUP_SEND_HAS_ONE_EXPLICIT_IMAGE"] = client.requests[1]["image_parts"] == 1
        checks["NO_EXTRA_SYNTHETIC_SUBMISSIONS"] = len(client.requests)==2
        screenshot("after-followup-completed.png")
        return {"passed": all(checks.values()), "checks": checks, "positions": records,
            "synthetic_submissions": client.requests, "real_model_requests":0,
            "production_profile_used":False, "physical_input":False}
    finally:
        (output/'partial-progress.json').write_text(json.dumps({'checks':checks,'positions':records,
            'real_model_requests':0},ensure_ascii=False,indent=2),encoding='utf-8')
        client.shutdown()
        fixture.tearDown()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',action='store_true')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if not args.run:
        print('{"status":"not_run","real_model_requests":0}'); return 0
    output=args.output.resolve()
    assert output.is_relative_to((ROOT/'artifacts/user-ux-20261105').resolve())
    output.mkdir(parents=True,exist_ok=False)
    before=fingerprints()
    try: result=run(output)
    except Exception as error:
        import traceback
        (output/'exception.log').write_text(traceback.format_exc(),encoding='utf-8')
        result={"passed":False,"error_type":type(error).__name__,"real_model_requests":0}
    result.update(source_before=before,source_after=fingerprints())
    result['source_unchanged']=before==result['source_after']
    result['passed']=result['passed'] and result['source_unchanged']
    (output/'verification.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({key:result.get(key) for key in ('passed','checks','error_type','source_unchanged','real_model_requests')},ensure_ascii=False))
    return 0 if result['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
