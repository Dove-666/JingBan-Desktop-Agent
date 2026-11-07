"""User-flow audit over owned synthetic production Qt/WebEngine components.

Explicit --run only. No production profile, network, model, camera, physical
input, or existing app process is used. Screenshots contain synthetic messages.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))

SOURCES = ("frontend/chat-ui/src/App.tsx", "frontend/chat-ui/src/styles.css",
    "desktop_pet/ui/web_chat/index.html", "desktop_pet/ui/assistant_panel.py",
    "desktop_pet/chat_controller.py", "desktop_pet/ui/window_chrome.py")


def fingerprints():
    return {name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in SOURCES}


METRICS = r"""(() => {
 const rect = n => { const r=n.getBoundingClientRect(); return {x:r.x,y:r.y,width:r.width,height:r.height,right:r.right,bottom:r.bottom}; };
 const visible = n => { const s=getComputedStyle(n),r=n.getBoundingClientRect();return s.visibility!=='hidden'&&s.display!=='none'&&r.width>0&&r.height>0; };
 const controls = [...document.querySelectorAll('button,input,textarea')].filter(visible)
  .map(n=>({label:n.getAttribute('aria-label')||n.textContent.trim(),rect:rect(n),disabled:n.disabled}));
 const outside=controls.filter(n=>n.rect.x<-.5||n.rect.y<-.5||n.rect.right>innerWidth+.5||n.rect.bottom>innerHeight+.5);
 const composer=document.querySelector('.composer'), viewport=document.querySelector('.thread-viewport'),input=document.querySelector('#message-input');
 const measure=document.querySelector('.composer-measure');
 return {width:innerWidth,height:innerHeight,bodyWidth:document.body.scrollWidth,documentWidth:document.documentElement.scrollWidth,
   composer:rect(composer),viewport:rect(viewport),outsideControls:outside,
   input:{rect:rect(input),scroll:input.scrollHeight,client:input.clientHeight,scrollTop:input.scrollTop,
    font:getComputedStyle(input).fontSize,lineHeight:getComputedStyle(input).lineHeight,measureScroll:measure.scrollHeight,measureWidth:measure.clientWidth},
   focused:document.activeElement?.getAttribute('aria-label')||document.activeElement?.id,
   draft:document.querySelector('#message-input').value};
})()"""


def run(output):
    from PySide6.QtCore import QEvent
    from PySide6.QtTest import QTest
    from tests.test_startup_settings_ui import StartupSettingsUiTests
    from tests.test_application import spin_until
    from tests.test_assistant_ui import eval_js,wait_js,set_dom_text,click_dom

    StartupSettingsUiTests.setUpClass()
    fixture=StartupSettingsUiTests();fixture.setUp()
    app=fixture.app
    records=[];checks={}
    chat=fixture.session.chat
    class NoNetwork:
        calls=0
        def complete_context(self,*args,**kwargs):
            self.calls+=1;raise AssertionError('Unexpected send during local UX audit')
        def cancel_request(self,*args): pass
        def shutdown(self): pass
    inert=NoNetwork();chat._client=inert;chat._metadata_factory=None
    try:
        current=chat.current_conversation_id
        for i in range(18):
            cid=str(uuid4())
            chat.store.append_message(cid,'user',f'合成历史 {i+1:02d}：整理工作与今天的待办事项','user')
            chat.store.append_message(cid,'assistant','这是一条只供界面检查的历史回复。','assistant')
        for i in range(12):
            chat.store.append_message(current,'user',f'界面检查问题 {i+1:02d}：这段记录只在独立临时配置中。','user')
            chat.store.append_message(current,'assistant',
                '鲸鱼娘已经把事情分成几步。\n\n'
                '| 项目 | 状态 |\n| --- | --- |\n| 任务一 | 已记录 |\n| 任务二 | 等待处理 |\n\n'
                '```python\nprint("这只是显示用代码，不执行")\n```\n\n'
                '可以先处理眼前的一件事，然后继续下一步。','assistant')
        chat.open_conversation(current);spin_until(lambda:not chat.history_loading)
        fixture.session.open_chat_window();window=fixture.session.chat_window
        window.setWindowTitle('AURA 用户体验检查 · 独立合成数据')
        panel=window.chat_panel
        spin_until(lambda:panel._frontend_ready,timeout=20)
        wait_js(panel,"Boolean(document.querySelector('#message-input'))")
        draft='尚未发送的合成草稿，切换设置和查找后仍要保留。\n第二行继续记录。'
        set_dom_text(panel,'#message-input',draft)
        for width,height,font in ((1100,800,16),(800,650,16),(620,500,16),(420,500,16),(620,500,22),(420,500,22)):
            window.resize(width,height);chat.set_reading_font_size(font)
            QTest.qWait(180)
            wait_js(panel,f"Math.abs(innerWidth-{panel.web_view.width()})<=2")
            metrics=eval_js(panel,METRICS)
            name=f'chat-{width}x{height}-font{font}.png'
            assert window.grab().save(str(output/name))
            records.append({'image':name,'requested':[width,height],'actual':[window.width(),window.height()],
                'font':font,'metrics':metrics})
        checks['NO_HORIZONTAL_DOCUMENT_OVERFLOW']=all(row['metrics']['documentWidth']<=row['metrics']['width']+1 for row in records)
        checks['DRAFT_PRESERVED_ACROSS_RESIZE_AND_FONT']=all(row['metrics']['draft']==draft for row in records)
        # A history menu near the bottom must remain completely reachable.
        window.resize(620,550);QTest.qWait(100)
        click_dom(panel,'[data-testid=history-toggle]')
        wait_js(panel,"document.querySelector('.sidebar').getBoundingClientRect().left>=-1")
        eval_js(panel,"(() => {const n=document.querySelector('.conversation-list');n.scrollTop=n.scrollHeight;return true;})()")
        last=eval_js(panel,"document.querySelector('.conversation-row:last-child').dataset.conversationId")
        selector='[data-conversation-id='+json.dumps(last)+'] .row-actions button'
        click_dom(panel,selector);wait_js(panel,"Boolean(document.querySelector('.row-popover'))")
        QTest.qWait(100)
        menu=eval_js(panel,"(() => {let r=document.querySelector('.row-popover').getBoundingClientRect();return {top:r.top,bottom:r.bottom,left:r.left,right:r.right,width:innerWidth,height:innerHeight};})()")
        checks['BOTTOM_HISTORY_MENU_INSIDE_VIEWPORT']=menu['top']>=0 and menu['bottom']<=menu['height'] and menu['left']>=0 and menu['right']<=menu['width']
        assert window.grab().save(str(output/'history-last-menu.png'))
        eval_js(panel,"(() => {document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));return true;})()")
        QTest.qWait(80)
        checks['HISTORY_ESCAPE_PRESERVES_DRAFT']=eval_js(panel,"document.querySelector('#message-input').value")==draft
        # Ctrl+Shift+F must operate a find field without submitting a draft.
        eval_js(panel,"(() => {window.dispatchEvent(new KeyboardEvent('keydown',{key:'f',ctrlKey:true,shiftKey:true,bubbles:true}));return true;})()")
        wait_js(panel,"Boolean(document.querySelector('[data-testid=message-search]'))")
        set_dom_text(panel,'[data-testid=message-search]','界面检查问题')
        QTest.qWait(80)
        before=eval_js(panel,"document.querySelector('.message-find-count').textContent")
        eval_js(panel,"document.querySelector('[data-testid=message-search]').dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}))")
        QTest.qWait(80)
        checks['FIND_ENTER_NAVIGATES_MATCHES']=before!=eval_js(panel,"document.querySelector('.message-find-count').textContent")
        checks['FIND_ENTER_DOES_NOT_SEND']=inert.calls==0
        assert window.grab().save(str(output/'find-at-620.png'))
        return {'passed':all(checks.values()),'checks':checks,'screenshots':records,'bottom_menu_metrics':menu,
            'model_requests':inert.calls,'production_profile_used':False,'physical_input':False}
    finally:
        fixture.tearDown()
        app.sendPostedEvents(None,QEvent.Type.DeferredDelete);app.processEvents()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',action='store_true')
    parser.add_argument('--output',type=Path,default=ROOT/'artifacts/user-ux-20261105/initial')
    args=parser.parse_args()
    if not args.run: print('{"status":"not_run","model_requests":0}');return 0
    if args.output.exists():parser.error('Use a new output directory')
    args.output.mkdir(parents=True)
    before=fingerprints()
    try:value=run(args.output)
    except Exception as error:
        import traceback
        (args.output/'exception.log').write_text(traceback.format_exc(),encoding='utf-8')
        value={'passed':False,'error_type':type(error).__name__,'model_requests':0}
    after=fingerprints();value.update(source_before=before,source_after=after,source_unchanged=before==after)
    value['passed']=value['passed'] and before==after
    (args.output/'verification.json').write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({key:value.get(key) for key in ('passed','error_type','model_requests','source_unchanged')}))
    return 0 if value['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
