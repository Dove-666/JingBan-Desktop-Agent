"""Actual shipped Qt chat + FileReader + native inbox; event-local synthetic files."""
import base64
import hashlib
import json
from pathlib import Path
import socket
import sys
import threading
import traceback
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SOURCES = ('frontend/chat-ui/src/App.tsx', 'frontend/chat-ui/src/bridge.ts',
    'frontend/chat-ui/src/imageInput.ts', 'desktop_pet/ui/web_chat/index.html',
    'desktop_pet/chat_controller.py', 'desktop_pet/ui/chat_panel.py', 'desktop_pet/ui/assistant_panel.py')


def fence(): return {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in SOURCES}


def run(output):
    from PySide6.QtCore import QEvent, QThreadPool
    from PySide6.QtTest import QTest
    from desktop_pet.api_client import ChatReply
    from desktop_pet.settings_manager import ApiSettings
    from desktop_pet.ui.assistant_panel import AssistantChatPanel
    from tests.test_chat_images import ChatImageTests
    from tests.test_assistant_ui import click_dom, eval_js, set_dom_text, wait_js, wait_until
    ChatImageTests.setUpClass()
    fixture = ChatImageTests(); fixture.setUp()
    chat = fixture.chat
    class Replies:
        def __init__(self): self.calls = []
        def complete_context(self, api, key, messages, **kwargs):
            content = messages[-1]['content']
            images = [p['image_url']['url'] for p in content if p.get('type') == 'image_url'] if isinstance(content, list) else []
            self.calls.append({'images': len(images), 'sha256': [hashlib.sha256(base64.b64decode(p.split(',', 1)[1])).hexdigest() for p in images]})
            return ChatReply('独立合成回复：收到本轮图片。')
        def shutdown(self): pass
    client = Replies(); chat._client = client
    fixture.settings.save_api_settings(ApiSettings(base_url='http://127.0.0.1:1/v1', model='owned-image-fixture'))
    fixture.settings.save_api_key('owned-synthetic-key')
    panel = AssistantChatPanel(chat); panel.resize(980, 760)
    panel.setWindowTitle('AURA 图片粘贴与拖入 · 独立合成数据')
    checks, gestures, actions = {}, [], []
    original_dispatch = panel.dispatch_web_action
    def observed(action, payload, **kwargs):
        actions.append({'action': action, 'cid': payload.get('conversationId'), 'fields': sorted(payload)})
        return original_dispatch(action, payload, **kwargs)
    def check(name, value):
        checks[name] = bool(value)
        if not value: raise AssertionError(name)
    def count(action): return sum(row['action'] == action for row in actions)
    def value(): return eval_js(panel, "document.querySelector('#message-input').value")
    def capture(name):
        QTest.qWait(60)
        assert panel.grab().save(str(output / name), 'PNG')
    def stage_ready():
        wait_until(lambda: not panel._image_preparing and panel._current_staged_image() is not None)
        wait_js(panel, "Boolean(document.querySelector('[data-testid=composer-attachment] img')?.naturalWidth>0)")
    def gesture(kind, mode='valid', target='#message-input', immediate=False):
        script = """(() => {
          const d=new DataTransfer(), input=document.querySelector('#message-input');
          if(MODE==='text') d.setData('text/plain','普通文字');
          else {
            const bytes=Uint8Array.from(atob(DATA),c=>c.charCodeAt(0));
            const f=new File([MODE==='oversize'?new Uint8Array(4*1024*1024+1):bytes],
              MODE==='unsupported'?'拒绝.svg':MODE==='unknownExtension'?'unknown.bin':'可爱的鲸鱼娘.png',
              {type:MODE==='unsupported'?'image/svg+xml':MODE==='emptyMime'||MODE==='unknownExtension'?'':MODE==='genericMime'?'application/octet-stream':'image/png'});
            d.items.add(f); if(MODE==='multiple')d.items.add(new File([bytes],'第二张.png',{type:'image/png'}));
          }
          const event=KIND==='paste'?new ClipboardEvent('paste',{clipboardData:d,bubbles:true,cancelable:true})
            :new DragEvent('drop',{dataTransfer:d,bubbles:true,cancelable:true});
          const before=document.querySelectorAll('[data-message-id]').length;
          document.querySelector(TARGET).dispatchEvent(event);
          if(IMMEDIATE)document.querySelector('.composer').requestSubmit();
          return {prevented:event.defaultPrevented,text:input.value,before,
            after:document.querySelectorAll('[data-message-id]').length,readers:window.__readCount||0};
        })()"""
        for key, item in [('KIND',kind),('MODE',mode),('TARGET',target),('DATA',encoded),('IMMEDIATE',immediate)]:
            script = script.replace(key, json.dumps(item))
        result = eval_js(panel, script); gestures.append({'kind':kind,'mode':mode,'target':target,'immediate':immediate,**result})
        return result
    try:
        with patch.object(panel, 'dispatch_web_action', side_effect=observed):
            panel.show(); wait_until(lambda: panel._frontend_ready and not chat.history_loading, timeout=25)
            wait_js(panel, "Boolean(document.querySelector('#message-input'))")
            cid = chat.current_conversation_id
            _, encoded = fixture.upload()
            eval_js(panel, """(() => {
              window.__NativeReader=FileReader;window.__readCount=0;window.__held=[];window.__delayReaders=false;
              window.FileReader=class extends window.__NativeReader {
                readAsDataURL(file) {window.__readCount++; if(window.__delayReaders)window.__held.push({reader:this,file});else super.readAsDataURL(file);}
              };return true;
            })()""")
            draft = '这一段文字必须完整保留\n第二行也不能丢。'
            set_dom_text(panel, '#message-input', draft)
            first = gesture('paste', immediate=True)
            check('SAME_TURN_PASTE_PREVENTS_OPTIMISTIC_BUBBLE', first['before'] == first['after'])
            stage_ready()
            first_stage = panel._current_staged_image()['image_id']
            check('PASTE_USES_REAL_READER_AND_NATIVE_INBOX', eval_js(panel, 'window.__readCount') == 1 and count('stageImageData') == 1)
            check('PASTE_AND_SAME_TURN_SUBMIT_KEEP_DRAFT_WITHOUT_SEND', value() == draft and count('send') == 0 and not client.calls)
            check('PASTE_PREVIEW_PIXELS_MATCH_INBOX', hashlib.sha256(base64.b64decode(fixture.remote.images.get(first_stage)['data_base64'])).hexdigest() == hashlib.sha256(base64.b64decode(encoded)).hexdigest())
            capture('01-real-paste-preview.png')
            if '--mime-before' in sys.argv:
                stages = count('stageImageData'); readers = eval_js(panel, 'window.__readCount')
                empty = gesture('drop', 'emptyMime', target='.main-panel'); QTest.qWait(100)
                accepted = count('stageImageData') == stages + 1
                capture('02-empty-mime-before.png')
                return {'passed':accepted, 'checks':{'VALID_PNG_FILE_WITH_EMPTY_TYPE_ACCEPTED':accepted},
                    'scope':'Actual File([valid owned PNG bytes], png name, type empty) and current compiled main drop; synthetic event, not physical Windows drag-drop.',
                    'readers_before':readers,'readers_after':eval_js(panel,'window.__readCount'),
                    'stages_before':stages,'stages_after':count('stageImageData'),'gesture':empty,
                    'visible_error':eval_js(panel,"document.querySelector('[data-testid=composer-image-error]')?.textContent"),
                    'source_reference':'https://www.w3.org/TR/FileAPI/'}
            for mode in ('multiple', 'unsupported', 'oversize'):
                before = count('stageImageData'); readers = eval_js(panel, 'window.__readCount')
                result = gesture('paste', mode)
                wait_js(panel, "Boolean(document.querySelector('[data-testid=composer-image-error]'))")
                check(mode.upper()+'_REJECTS_WITHOUT_READ_OR_NATIVE_STAGE', result['prevented'] and count('stageImageData') == before and eval_js(panel, 'window.__readCount') == readers)
                check(mode.upper()+'_PRESERVES_REAL_DRAFT_AND_OLD_ATTACHMENT', value() == draft and panel._current_staged_image()['image_id'] == first_stage)
            text = gesture('paste', 'text')
            check('PLAIN_TEXT_PASTE_REMAINS_UNPREVENTED', not text['prevented'] and value() == draft)
            outside = gesture('drop', target='.sidebar')
            check('SIDEBAR_FILE_DROP_PREVENTS_NAVIGATION_WITHOUT_STAGE', outside['prevented'] and count('stageImageData') == 1)
            drop = gesture('drop', target='.main-panel')
            wait_until(lambda: count('stageImageData') == 2); stage_ready()
            check('MAIN_BLANK_DROP_STAGES_ONCE_WITH_REAL_READER', drop['prevented'] and count('stageImageData') == 2 and eval_js(panel, 'window.__readCount') == 2)
            check('DROP_KEEPS_DRAFT_AND_DOES_NOT_SEND', value() == draft and not client.calls)
            capture('02-main-drop-preview.png')
            for mode in ('emptyMime', 'genericMime'):
                old_stages, old_reads = count('stageImageData'), eval_js(panel, 'window.__readCount')
                result = gesture('drop', mode, target='.main-panel')
                wait_until(lambda: count('stageImageData') == old_stages + 1); stage_ready()
                check(mode.upper()+'_EXPLICIT_PNG_SUFFIX_USES_TYPED_BLOB_AND_ACTUAL_INBOX',
                      result['prevented'] and eval_js(panel, 'window.__readCount') == old_reads + 1
                      and panel._current_staged_image()['image_id'] == first_stage and value() == draft)
            old_stages, old_reads = count('stageImageData'), eval_js(panel, 'window.__readCount')
            gesture('drop', 'unknownExtension', target='.main-panel')
            wait_js(panel, "Boolean(document.querySelector('[data-testid=composer-image-error]'))")
            check('EMPTY_MIME_UNKNOWN_SUFFIX_STILL_REJECTS_WITHOUT_READ_OR_STAGE',
                  count('stageImageData') == old_stages and eval_js(panel, 'window.__readCount') == old_reads and value() == draft)
            # A controlled pause before native FileReader starts, never a fake
            # decoder or host importer. Releasing later still does real reading.
            eval_js(panel, 'window.__delayReaders=true')
            gesture('paste'); before = count('stageImageData')
            wait_js(panel, "document.querySelector('[data-testid=send-button]').disabled")
            chat.store.append_message(cid, 'assistant', '此消息只用于保留合成会话。', 'assistant')
            chat.new_conversation(); wait_until(lambda: not chat.history_loading)
            wait_js(panel, "document.querySelector('#message-input').value === ''")
            eval_js(panel, "(() => {window.__held.splice(0).forEach(x=>window.__NativeReader.prototype.readAsDataURL.call(x.reader,x.file));return true;})()")
            QTest.qWait(100)
            check('OLD_CID_DELAYED_READER_NEVER_STAGES_NEW_CID', count('stageImageData') == before and panel._current_staged_image() is None)
            chat.open_conversation(cid); wait_until(lambda: not chat.history_loading)
            wait_js(panel, "Boolean(document.querySelector('[data-testid=composer-attachment]'))")
            check('RETURN_RESTORES_UNSENT_DRAFT_AND_OLD_STAGE', value() == draft and panel._current_staged_image()['image_id'] == first_stage)
            # A reload destroys the old renderer and its unadmitted reader;
            # the existing native-admitted attachment belongs to the host.
            gesture('paste'); before = count('stageImageData')
            old_document = panel._web_document_token
            panel._reload_web(); wait_until(lambda: panel._frontend_ready, timeout=20)
            wait_js(panel, "Boolean(document.querySelector('[data-testid=composer-attachment] img')?.naturalWidth>0)")
            check('RELOAD_DESTROYS_UNADMITTED_READER_AND_PRESERVES_ADMITTED_STAGE',
                  panel._web_document_token != old_document and count('stageImageData') == before
                  and panel._current_staged_image()['image_id'] == first_stage and value() == draft)
            eval_js(panel, """(() => {
              window.__NativeReader=FileReader;window.__readCount=0;window.__held=[];window.__delayReaders=true;
              window.FileReader=class extends window.__NativeReader {
                readAsDataURL(file) {window.__readCount++; if(window.__delayReaders)window.__held.push({reader:this,file});else super.readAsDataURL(file);}
              };return true;
            })()""")
            gesture('paste'); before = count('stageImageData')
            wait_js(panel, "document.querySelector('[data-testid=send-button]').disabled && document.querySelector('[data-testid=composer-image-picker]').disabled")
            old_epoch = panel._draft_owner.snapshot(cid).epoch
            from PySide6.QtWidgets import QMessageBox
            with patch('desktop_pet.ui.chat_panel.QMessageBox.question', return_value=QMessageBox.StandardButton.Yes):
                assert panel.dispatch_web_action('clear', {'conversationId': cid})
            wait_js(panel, "!document.querySelector('[data-testid=composer-attachment]') && document.querySelector('#message-input').value === ''")
            draft = '清空之后新写的草稿\n不能被旧图片回调覆盖。'
            set_dom_text(panel, '#message-input', draft)
            eval_js(panel, "(() => {window.__held.splice(0).forEach(x=>window.__NativeReader.prototype.readAsDataURL.call(x.reader,x.file));return true;})()")
            QTest.qWait(100)
            check('SAME_CID_CLEAR_ABORTS_READER_WITHOUT_TOUCHING_NEW_DRAFT',
                  panel._draft_owner.snapshot(cid).epoch != old_epoch and count('stageImageData') == before
                  and panel._current_staged_image() is None and value() == draft)
            eval_js(panel, 'window.__delayReaders=false')
            gesture('drop', target='.main-panel'); wait_until(lambda: count('stageImageData') == before + 1); stage_ready()
            restored_stage = panel._current_staged_image()['image_id']
            original_state = panel.web_state
            def legacy_state():
                state = original_state(); state.pop('canStageImageData', None); return state
            with patch.object(panel, 'web_state', side_effect=legacy_state):
                panel._publish_web_state(); QTest.qWait(80)
                old_reads = eval_js(panel, 'window.__readCount'); old_stages = count('stageImageData')
                gesture('paste')
                wait_js(panel, "document.querySelector('[data-testid=composer-image-error]')?.textContent.includes('当前电脑版本')")
                check('LEGACY_ABSENT_CAPABILITY_REJECTS_WITHOUT_READER_DRAFT_OR_ATTACHMENT_LOSS',
                      eval_js(panel, 'window.__readCount') == old_reads and count('stageImageData') == old_stages
                      and value() == draft and panel._current_staged_image()['image_id'] == restored_stage)
            panel._publish_web_state(); QTest.qWait(60)
            text_drop = gesture('drop', 'text', target='.main-panel')
            check('PLAIN_TEXT_MAIN_DROP_REMAINS_UNPREVENTED', not text_drop['prevented'])
            release = threading.Event(); entered = threading.Event(); actual_import = chat.import_image_data
            def delayed_import(*args):
                entered.set()
                if not release.wait(5): raise AssertionError('Owned native import was not released')
                return actual_import(*args)
            try:
                with patch.object(chat, 'import_image_data', side_effect=delayed_import):
                    gesture('paste'); wait_until(entered.is_set)
                    wait_js(panel, "document.querySelector('[data-testid=send-button]').disabled && document.querySelector('[data-testid=composer-image-picker]').disabled")
                    check('NATIVE_ACCEPTED_ACK_HANDOFF_KEEPS_SEND_AND_PICKER_LOCKED', panel._image_preparing and value() == draft and not client.calls)
                    release.set(); stage_ready()
            finally: release.set()
            capture('03-boundary-restored-preview.png')
            click_dom(panel, '[data-testid=send-button]')
            wait_until(lambda: not chat.busy and len(client.calls) == 1, timeout=10)
            wait_js(panel, "!document.querySelector('[data-testid=composer-attachment]')")
            check('EXPLICIT_SEND_USES_IMAGE_ONCE_AND_CLEARS_STAGE', client.calls[0]['images'] == 1 and panel._current_staged_image() is None and value() == '')
            capture('04-explicit-send.png')
            return {'passed': all(checks.values()), 'checks': checks, 'gestures': gestures,
                'stage_calls': count('stageImageData'), 'send_calls': count('send'), 'synthetic_reply_calls': client.calls,
                'scope': 'Actual compiled React / WebEngine event handlers, real FileReader except explicitly paused native-reader start, production stage/controller/inbox. Clipboard events and File objects are fixture-supplied; no OS clipboard. Plain-text default insertion not exercised.'}
    except Exception:
        capture('failed-visible-state.png'); raise
    finally:
        wait_until(lambda: not panel._image_stage_runner._tasks and QThreadPool.globalInstance().activeThreadCount() == 0)
        for timer in (panel._web_publish_timer, panel._web_stream_timer, panel._web_start_timer, panel._web_timeout): timer.stop()
        panel.hide(); panel._discard_partial_web(); panel.deleteLater()
        fixture.app.sendPostedEvents(None, QEvent.Type.DeferredDelete); fixture.app.processEvents()
        fixture.tearDown()


def main():
    label = 'actual-empty-mime-before' if '--mime-before' in sys.argv else 'actual-ui-final-mime-boundaries'
    output = ROOT / 'artifacts/task-audit/image-input-frontend-20261106' / label
    output.mkdir(parents=True, exist_ok=False)
    before = fence(); blocked = []
    def deny(*a, **k): blocked.append('network'); raise AssertionError('NETWORK_FORBIDDEN')
    try:
        with patch.object(socket, 'getaddrinfo', deny), patch.object(socket.socket, 'connect', deny), patch.object(socket.socket, 'connect_ex', deny):
            result = run(output)
    except Exception as error:
        (output/'exception.log').write_text(traceback.format_exc(), encoding='utf8')
        result = {'passed':False, 'error_type': type(error).__name__}
    after = fence(); result.update(source_before=before,source_after=after,source_unchanged=before==after,
        blocked_network_attempts=blocked, real_model_requests=0, system_clipboard_used=False, user_profile_used=False)
    result['passed'] = result['passed'] and before == after and not blocked
    (output/'verification.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps(result,ensure_ascii=True,indent=2)); return 0 if result['passed'] else 1


if __name__ == '__main__': raise SystemExit(main())
