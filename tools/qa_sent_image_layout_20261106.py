"""Actual shipped renderer before/after, bounded owned PNGs and native previews."""
import base64
import hashlib
import json
from pathlib import Path
import socket
import sys
import traceback
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SOURCES = ('frontend/chat-ui/src/App.tsx', 'frontend/chat-ui/src/styles.css',
    'desktop_pet/ui/web_chat/index.html', 'desktop_pet/ui/assistant_panel.py')


def fence(): return {p: hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in SOURCES}


def run(output):
    from PySide6.QtCore import QByteArray, QBuffer, QEvent, QIODevice, QThreadPool
    from PySide6.QtGui import QColor, QImage
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QLabel
    from tests.test_chat_image_web import WebImageChat
    from tests.test_assistant_ui import make_panel, eval_js, wait_js, click_dom, wait_until
    app=QApplication.instance() or QApplication([]); app.setQuitOnLastWindowClosed(False)
    cases=[]
    for width,font in ((1000,16),(420,22)):
        for image_width,image_height in ((24,16),(640,430)):
            image=QImage(image_width,image_height,QImage.Format.Format_RGB32); image.fill(QColor('#69a9d9'))
            raw=QByteArray(); buffer=QBuffer(raw); assert buffer.open(QIODevice.OpenModeFlag.WriteOnly)
            assert image.save(buffer,'PNG'); buffer.close()
            chat=WebImageChat(); chat.image={'filename':'owned.png','mime_type':'image/png',
                'data_base64':base64.b64encode(bytes(raw)).decode('ascii')}
            chat.reading_font_size=font
            panel=make_panel(chat); panel.setWindowTitle('AURA 已发送图片布局 · 独立合成数据'); panel.resize(width,740)
            label=f'{width}-font{font}-{image_width}x{image_height}'
            try:
                wait_js(panel,f'innerWidth==={width}')
                wait_js(panel,"Boolean(document.querySelector('[data-testid=image-thumbnail]')?.complete && document.querySelector('[data-testid=image-thumbnail]')?.naturalWidth>0)")
                QTest.qWait(80)
                metrics=eval_js(panel,"""(() => {
                  const root=document.querySelector('[data-testid=image-attachment]'),button=root.querySelector('button'),image=button.querySelector('img'),caption=button.querySelector('.image-thumbnail-caption'),bubble=root.closest('.message-body');
                  const rect=node=>{const r=node.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height,right:r.right,bottom:r.bottom,client:node.clientWidth,scroll:node.scrollWidth}};
                  const bounds=button.getBoundingClientRect(), im=image.getBoundingClientRect();
                  const cs=getComputedStyle(caption),canvas=document.createElement('canvas'),ctx=canvas.getContext('2d');ctx.font=cs.font;
                  const fullTextWidth=ctx.measureText(caption.textContent).width+parseFloat(cs.paddingLeft)+parseFloat(cs.paddingRight);
                  return {viewport:{width:innerWidth,height:innerHeight,documentWidth:document.documentElement.scrollWidth},
                    loaded:image.complete&&image.naturalWidth>0,natural:{width:image.naturalWidth,height:image.naturalHeight},
                    root:rect(root),button:rect(button),image:rect(image),caption:rect(caption),bubble:rect(bubble),
                    captionText:caption.textContent,captionFont:cs.font,fullTextWidth,
                    footerFullyVisible:caption.clientWidth>=fullTextWidth-1&&caption.scrollWidth<=caption.clientWidth+1,
                    noOverflow:document.documentElement.scrollWidth<=innerWidth+1&&bounds.right<=innerWidth+1,
                    imageAspectPreserved:Math.abs(im.width/im.height-image.naturalWidth/image.naturalHeight)<.01,
                    noUpscale:im.width<=image.naturalWidth+.1&&im.height<=image.naturalHeight+.1,
                    clickHit:document.elementFromPoint(bounds.x+bounds.width/2,bounds.y+bounds.height/2)?.closest('button')===button,
                    lazy:root.dataset.nearViewport};
                })()""")
                assert panel.grab().save(str(output/f'{label}.png'),'PNG')
                click_dom(panel,'[data-testid=image-attachment] button')
                wait_until(lambda: panel._image_preview_dialog is not None)
                dialog=panel._image_preview_dialog
                status=dialog.findChild(QLabel,'chatImagePreviewStatus')
                wait_until(lambda:status.text()=='图片附件')
                metrics['nativeOwnPreviewOpened']=not dialog.findChild(QLabel,'chatImagePreviewImage').pixmap().isNull() and chat.image_calls[-1][:3]==('current','picture','a'*64)
                dialog.reject()
                metrics['acceptancePassed']=all(metrics[key] for key in ('loaded','footerFullyVisible','noOverflow','imageAspectPreserved','noUpscale','clickHit','nativeOwnPreviewOpened'))
                cases.append({'label':label,**metrics})
            finally:
                if panel._image_preview_dialog is not None: panel._image_preview_dialog.reject()
                wait_until(lambda:QThreadPool.globalInstance().activeThreadCount()==0)
                for timer in (panel._web_publish_timer,panel._web_stream_timer,panel._web_start_timer,panel._web_timeout):timer.stop()
                panel.hide();panel._discard_partial_web();panel.deleteLater()
                app.sendPostedEvents(None,QEvent.Type.DeferredDelete);app.processEvents()
    return {'measurement_completed':True,'acceptance_passed':all(row['acceptancePassed'] for row in cases),'cases':cases,
        'scope':'Actual shipped React/Qt thumbnail loader, synthetic controller supplies bounded QImage PNG bytes; actual lazy thumbnail worker and native preview. No model, OS clipboard, user data or native desktop input.'}


def main():
    phase=sys.argv[1]
    output=ROOT/'artifacts/task-audit/sent-image-layout-20261106'/phase
    output.mkdir(parents=True,exist_ok=False)
    if phase=='before':
        for relative in SOURCES[:3]:
            (output/Path(relative).name).write_bytes((ROOT/relative).read_bytes())
    before=fence();blocked=[]
    def deny(*args,**kwargs):blocked.append('network');raise AssertionError('NETWORK_FORBIDDEN')
    try:
        with patch.object(socket,'getaddrinfo',deny),patch.object(socket.socket,'connect',deny),patch.object(socket.socket,'connect_ex',deny):result=run(output)
    except Exception as error:
        (output/'exception.log').write_text(traceback.format_exc(),encoding='utf8')
        result={'measurement_completed':False,'acceptance_passed':False,'error_type':type(error).__name__}
    after=fence();result.update(source_before=before,source_after=after,source_unchanged=before==after,
        blocked_network_attempts=blocked,real_model_requests=0,system_clipboard_used=False,user_profile_used=False)
    (output/'verification.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps({'report':str(output/'verification.json'),
        'measurement_completed':result.get('measurement_completed'),'acceptance_passed':result.get('acceptance_passed'),
        'source_unchanged':result['source_unchanged'],'blocked_network_attempts':blocked,
        'cases':[{'label':row['label'],'card_width':row['button']['width'],'image_width':row['image']['width'],
          'footer_full':row['footerFullyVisible'],'no_overflow':row['noOverflow'],'no_upscale':row['noUpscale'],
          'native_preview':row['nativeOwnPreviewOpened'],'passed':row['acceptancePassed']} for row in result.get('cases',[])]},ensure_ascii=True,indent=2))
    accepted = phase == 'before' or result.get('acceptance_passed') is True
    return 0 if result.get('measurement_completed') and accepted and before==after and not blocked else 1


if __name__=='__main__':raise SystemExit(main())
