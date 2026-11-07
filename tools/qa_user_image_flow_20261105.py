"""Explicit owned image/draft UI journey; fake in-process replies, zero API."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sys
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
SOURCES=('frontend/chat-ui/src/App.tsx','frontend/chat-ui/src/styles.css','frontend/chat-ui/src/liquid-theme.css',
    'desktop_pet/ui/web_chat/index.html','desktop_pet/ui/chat_panel.py','desktop_pet/ui/assistant_panel.py','desktop_pet/chat_controller.py')

def source():return {name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in SOURCES}


def run(out):
    from PySide6.QtGui import QImage,QColor
    from PySide6.QtTest import QTest
    from desktop_pet.api_client import ChatReply
    from tests.test_startup_settings_ui import StartupSettingsUiTests
    from tests.test_application import spin_until
    from tests.test_assistant_ui import eval_js,wait_js,set_dom_text,click_dom
    StartupSettingsUiTests.setUpClass();f=StartupSettingsUiTests();f.setUp()
    class SyntheticReplies:
        calls=[]
        def complete_context(self,settings,key,messages,**options):
            self.calls.append({'last_content_kind':type(messages[-1]['content']).__name__,
                'last_image_parts':sum(row.get('type')=='image_url' for row in messages[-1]['content']) if isinstance(messages[-1]['content'],list) else 0})
            return ChatReply('合成回复：已收到本轮输入。')
        def shutdown(self):pass
    client=SyntheticReplies();chat=f.session.chat;chat._client=client;chat._metadata_factory=None
    checks={};metrics={}
    def capture(name):
        QTest.qWait(80);assert window.grab().save(str(out/name))
    try:
        current=chat.current_conversation_id
        chat.store.append_message(current,'assistant','此会话仅用于独立图片流程测试。','assistant')
        chat.open_conversation(current);spin_until(lambda:not chat.history_loading)
        f.session.open_chat_window();window=f.session.chat_window;window.resize(420,500)
        window.setWindowTitle('AURA 图片流程 · 独立合成数据')
        panel=window.chat_panel;spin_until(lambda:panel._frontend_ready,timeout=20)
        long_name='鲸鱼娘拍摄的桌面截图'*12+'.png';path=f.base/long_name
        image=QImage(320,240,QImage.Format.Format_RGB32);image.fill(QColor('#638ec8'));assert image.save(str(path),'PNG')
        with patch('desktop_pet.ui.chat_panel.QFileDialog.getOpenFileName',return_value=(str(path),'')):
            click_dom(panel,'[data-testid=composer-image-picker]')
        wait_js(panel,"Boolean(document.querySelector('[data-testid=composer-attachment] img')?.naturalWidth>0)")
        stage=panel._current_staged_image();stage_id=stage['image_id']
        checks['SELECTING_IMAGE_DOES_NOT_SEND']=not client.calls
        set_dom_text(panel,'#message-input','一条尚未发送的合成问题。')
        chat.new_conversation();spin_until(lambda:not chat.history_loading)
        wait_js(panel,"!document.querySelector('[data-testid=composer-attachment]')")
        checks['OTHER_CONVERSATION_HAS_NO_OLD_STAGE']=panel._current_staged_image() is None
        chat.open_conversation(current);spin_until(lambda:not chat.history_loading)
        wait_js(panel,"Boolean(document.querySelector('[data-testid=composer-attachment]'))")
        checks['RETURN_RESTORES_OWN_STAGE']=panel._current_staged_image()['image_id']==stage_id
        checks['RETURN_RESTORES_OWN_DRAFT']=eval_js(panel,"document.querySelector('#message-input').value")=='一条尚未发送的合成问题。'
        panel._activate_native();checks['NATIVE_FALLBACK_RETAINS_STAGE']=panel._current_staged_image()['image_id']==stage_id
        panel._reload_web();spin_until(lambda:panel._frontend_ready,timeout=20)
        wait_js(panel,"Boolean(document.querySelector('[data-testid=composer-attachment]'))")
        chat.set_reading_font_size(22)
        set_dom_text(panel,'#message-input','\n'.join('第%d行：带着图片继续写一段较长的问题。'%i for i in range(20)))
        QTest.qWait(100)
        metrics=eval_js(panel,"""(() => {
          const e=document.querySelector('[data-testid=send-button]'), r=e.getBoundingClientRect();
          const a=document.querySelector('[data-testid=composer-attachment]').getBoundingClientRect();
          const c=document.querySelector('.composer').getBoundingClientRect();
          return {width:innerWidth,height:innerHeight,send:{left:r.left,right:r.right,top:r.top,bottom:r.bottom},
             composer:{top:c.top,bottom:c.bottom,height:c.height},attachment:{top:a.top,bottom:a.bottom,height:a.height},
             sendReachable:document.elementFromPoint(r.left+r.width/2,r.top+r.height/2)?.closest('[data-testid=send-button]')===e};
        })()""")
        checks['LONG_FILENAME_AND_DRAFT_KEEP_SEND_REACHABLE']=metrics['sendReachable'] and metrics['send']['bottom']<=metrics['height']
        capture('long-filename-long-draft.png')
        # Return to a normal-sized draft and finish a synthetic image turn.
        set_dom_text(panel,'#message-input','这张测试图片是什么颜色？')
        click_dom(panel,'[data-testid=send-button]');spin_until(lambda:not chat.busy,timeout=10)
        spin_until(lambda:len(client.calls)==1,timeout=10)
        wait_js(panel,"!document.querySelector('[data-testid=composer-attachment]')")
        checks['ACCEPTED_IMAGE_SEND_CLEARS_STAGE']=panel._current_staged_image() is None
        checks['EXPLICIT_IMAGE_TURN_HAS_ONE_IMAGE']=client.calls[0]['last_image_parts']==1
        set_dom_text(panel,'#message-input','?');click_dom(panel,'[data-testid=send-button]')
        spin_until(lambda:len(client.calls)==2 and not chat.busy,timeout=10)
        checks['FOLLOWING_TEXT_TURN_DOES_NOT_RESEND_OLD_IMAGE']=client.calls[1]['last_content_kind']=='str' and client.calls[1]['last_image_parts']==0
        users=[row for row in chat.store.load_messages(current) if row['role']=='user']
        checks['FOLLOWING_USER_BUBBLE_HAS_NO_OLD_IMAGE']=len(users)==2 and 'image_id' not in users[-1].get('metadata',{})
        capture('after-image-then-text.png')
        return {'passed':all(checks.values()),'checks':checks,'layout':metrics,'synthetic_client_calls':client.calls,'real_model_requests':0}
    finally:f.tearDown()


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--run',action='store_true')
    p.add_argument('--output',type=Path,default=ROOT/'artifacts/user-ux-20261105/image-flow-initial');a=p.parse_args()
    if not a.run:print('{"status":"not_run","real_model_requests":0}');return 0
    if a.output.exists():p.error('Use new output directory')
    a.output.mkdir(parents=True);before=source()
    try:r=run(a.output)
    except Exception as e:
        import traceback
        (a.output/'exception.log').write_text(traceback.format_exc(),encoding='utf-8');r={'passed':False,'error_type':type(e).__name__,'real_model_requests':0}
    after=source();r.update(source_before=before,source_after=after,source_unchanged=before==after,production_profile_used=False)
    r['passed']=r['passed'] and before==after
    (a.output/'verification.json').write_text(json.dumps(r,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:r.get(k) for k in ('passed','checks','error_type','source_unchanged')},ensure_ascii=True))
    return 0 if r['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
