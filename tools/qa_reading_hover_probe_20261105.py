"""Owned rail pointer delivery diagnostics; no API, profile, clipboard, production edits."""
import ctypes
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from tests.test_conversation_rail_ui import ConversationRailUiTests, RAIL, PREVIEW
from tests.test_assistant_ui import eval_js

focus='--focus' in sys.argv
direct='--direct' in sys.argv
suffix='-direct-trusted' if '--trusted' in sys.argv else '-direct' if direct else '-focus' if focus else ''
out=ROOT/('artifacts/user-ux-20261105/reading-hover-probe'+suffix)
out.mkdir(exist_ok=False)
ConversationRailUiTests.setUpClass()
case=ConversationRailUiTests('test_empty_and_loading_histories_do_not_leave_stale_ticks_or_previews')
case.setUp()
try:
    p=case.panel
    eval_js(p,"""(() => {window.qaPointerEvents=[];for(const type of ['pointermove','mousemove','pointerenter','pointerleave'])window.addEventListener(type,e=>window.qaPointerEvents.push({type,x:e.clientX,y:e.clientY,trusted:e.isTrusted,target:e.target?.getAttribute?.('data-testid')||e.target?.className}),true);return true;})()""")
    point=case.point_on_track(10)
    target=case.keyboard_target(p)
    if focus:
        p.web_view.setFocus();target.setFocus();case.app.processEvents();QTest.qWait(80)
    def move(local):
        if direct:
            event=QMouseEvent(QEvent.Type.MouseMove,QPointF(local),QPointF(target.mapToGlobal(local)),
                Qt.MouseButton.NoButton,Qt.MouseButton.NoButton,Qt.KeyboardModifier.NoModifier)
            QApplication.sendEvent(target,event);case.app.processEvents();QTest.qWait(40)
        else:QTest.mouseMove(target,local,delay=20)
    move(QPoint(max(40,target.width()//2),12))
    move(target.mapFrom(p.web_view,point))
    QTest.qWait(250)
    result=eval_js(p,"""(() => {const rail=document.querySelector('[data-testid=conversation-rail]'),r=rail.getBoundingClientRect();return {events:window.qaPointerEvents,preview:document.querySelector('[data-testid=conversation-rail-preview]')?.textContent,rail:{x:r.x,y:r.y,width:r.width,height:r.height},hit:document.elementFromPoint(r.left+r.width/2,r.top+r.height*.2)?.getAttribute('data-testid'),focus:document.hasFocus()};})()""")
    result['qt_active']=p.isActiveWindow()
    result['target_focus']=target.hasFocus()
    result['web_size']=[p.web_view.width(),p.web_view.height()]
    result['physical_input']='QTest targeted widget events only'
    assert p.grab().save(str(out/'after-qt-hover.png'))
    # DOM pointer delivery isolates application behavior from native test injection.
    eval_js(p,"""(() => {const rail=document.querySelector('[data-testid=conversation-rail]'),r=rail.getBoundingClientRect();rail.dispatchEvent(new PointerEvent('pointermove',{clientX:r.left+r.width/2,clientY:r.top+r.height*10/47,bubbles:true,pointerType:'mouse'}));return true;})()""")
    QTest.qWait(160)
    result['dom_pointer_preview']=eval_js(p,"document.querySelector('[data-testid=conversation-rail-preview]')?.dataset.previewMessageId")
    (out/'verification.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))
finally:
    case.tearDown()
