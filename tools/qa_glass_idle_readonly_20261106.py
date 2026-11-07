"""One owned shipped panel, existing draw-call counters and process CPU only."""
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import time
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
SOURCES=('frontend/chat-ui/src/components/GlassSurface.tsx','frontend/chat-ui/src/components/glass-surface.css',
    'frontend/chat-ui/src/App.tsx','frontend/chat-ui/src/styles.css','desktop_pet/ui/web_chat/index.html',
    'desktop_pet/ui/assistant_panel.py')
COUNTERS=r'''(() => {
  const count=window.__ownedDrawCounters={rafScheduled:0,rafFired:0,rafCancelled:0,
    canvas2dContexts:0,canvas2dPut:0,canvas2dDraw:0,canvas2dClear:0,canvas2dFill:0,
    canvasExports:0,webglContexts:0,webglDrawArrays:0,webglDrawElements:0};
  const raf=window.requestAnimationFrame,cancel=window.cancelAnimationFrame;
  window.requestAnimationFrame=function(callback){count.rafScheduled++;return raf.call(this,function(timestamp){count.rafFired++;return callback.call(this,timestamp);});};
  window.cancelAnimationFrame=function(id){count.rafCancelled++;return cancel.call(this,id);};
  const context=HTMLCanvasElement.prototype.getContext;
  HTMLCanvasElement.prototype.getContext=function(kind,...args){
    if(kind==='2d')count.canvas2dContexts++;else if(['webgl','webgl2','experimental-webgl'].includes(kind))count.webglContexts++;
    return context.call(this,kind,...args);
  };
  const wrap=(prototype,key,counter)=>{
    if(!prototype||typeof prototype[key]!=='function'||prototype[key].__ownedMeasured)return;
    const original=prototype[key];const measured=function(...args){count[counter]++;return original.apply(this,args);};
    measured.__ownedMeasured=true;prototype[key]=measured;
  };
  wrap(HTMLCanvasElement.prototype,'toDataURL','canvasExports');
  wrap(CanvasRenderingContext2D.prototype,'putImageData','canvas2dPut');
  wrap(CanvasRenderingContext2D.prototype,'drawImage','canvas2dDraw');
  wrap(CanvasRenderingContext2D.prototype,'clearRect','canvas2dClear');
  wrap(CanvasRenderingContext2D.prototype,'fillRect','canvas2dFill');
  for(const type of [window.WebGLRenderingContext,window.WebGL2RenderingContext]){
    wrap(type?.prototype,'drawArrays','webglDrawArrays');wrap(type?.prototype,'drawElements','webglDrawElements');
  }
})();'''


def fence():return {p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in SOURCES}


def cpu_seconds(pid):
    kernel=ctypes.windll.kernel32
    kernel.OpenProcess.argtypes=(wintypes.DWORD,wintypes.BOOL,wintypes.DWORD)
    kernel.OpenProcess.restype=wintypes.HANDLE
    kernel.GetProcessTimes.argtypes=(wintypes.HANDLE,ctypes.POINTER(wintypes.FILETIME),ctypes.POINTER(wintypes.FILETIME),ctypes.POINTER(wintypes.FILETIME),ctypes.POINTER(wintypes.FILETIME))
    handle=kernel.OpenProcess(0x1000,False,pid)
    if not handle:raise ctypes.WinError()
    try:
        created,exited,system,user=(wintypes.FILETIME() for _ in range(4))
        if not kernel.GetProcessTimes(handle,ctypes.byref(created),ctypes.byref(exited),ctypes.byref(system),ctypes.byref(user)):raise ctypes.WinError()
        return ((system.dwHighDateTime<<32)+system.dwLowDateTime+(user.dwHighDateTime<<32)+user.dwLowDateTime)/10_000_000
    finally:
        kernel.CloseHandle.argtypes=(wintypes.HANDLE,);kernel.CloseHandle(handle)


def run(output):
    from PySide6.QtCore import QEvent
    from PySide6.QtTest import QTest
    from PySide6.QtWebEngineCore import QWebEngineScript
    from PySide6.QtWidgets import QApplication
    from desktop_pet.ui.assistant_panel import AssistantChatPanel
    from tests.test_assistant_ui import make_panel,eval_js,wait_js,wait_until
    app=QApplication.instance() or QApplication([]);app.setQuitOnLastWindowClosed(False)
    original=AssistantChatPanel._install_theme_bootstrap
    def install(panel,page):
        original(panel,page)
        script=QWebEngineScript();script.setName('owned-readonly-existing-draw-counters')
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld);script.setRunsOnSubFrames(False)
        script.setSourceCode(COUNTERS);page.scripts().insert(script)
    with patch.object(AssistantChatPanel,'_install_theme_bootstrap',install):panel=make_panel()
    panel.setWindowTitle('AURA 静置绘制观察 · 独立合成数据')
    def snapshot():
        return eval_js(panel,"""({counts:{...window.__ownedDrawCounters},visibility:document.visibilityState,
          active:document.activeElement?.tagName,animations:document.getAnimations().map(a=>({state:a.playState,type:a.constructor.name})),
          glass:[...document.querySelectorAll('[data-glass-renderer]')].map(n=>({renderer:n.dataset.glassRenderer,
            backdrop:getComputedStyle(n).backdropFilter,definitions:n.querySelectorAll('.glass-surface-definitions').length}))})""")
    def phase(name):
        start=snapshot();main_before=cpu_seconds(os.getpid());renderer_before=cpu_seconds(renderer)
        began=time.perf_counter();QTest.qWait(12_000);elapsed=time.perf_counter()-began
        main_after=cpu_seconds(os.getpid());renderer_after=cpu_seconds(renderer);end=snapshot()
        return {'name':name,'wall_seconds':elapsed,'before':start,'after':end,
            'draw_deltas':{key:end['counts'][key]-value for key,value in start['counts'].items()},
            'parent_cpu_seconds':main_after-main_before,'parent_percent_one_logical_cpu':(main_after-main_before)/elapsed*100,
            'known_renderer_cpu_seconds':renderer_after-renderer_before,'known_renderer_percent_one_logical_cpu':(renderer_after-renderer_before)/elapsed*100}
    try:
        wait_js(panel,"Boolean(window.__ownedDrawCounters && document.querySelector('[data-glass-renderer]'))")
        wait_until(lambda:panel.web_view.page().renderProcessPid()>0)
        renderer=int(panel.web_view.page().renderProcessPid())
        eval_js(panel,"(() => {document.activeElement?.blur();return true;})()")
        QTest.qWait(1000)
        assert panel.grab().save(str(output/'visible.png'),'PNG')
        phases=[phase('visible-still')]
        panel.hide();wait_js(panel,"document.visibilityState==='hidden'")
        QTest.qWait(100)
        phases.append(phase('qt-hidden'))
        return {'parent_pid':os.getpid(),'known_renderer_pid':renderer,'phases':phases,
            'scope':'One idle actual shipped chat/composer. Wrappers delegate existing RAF/Canvas/WebGL calls only; no sampling RAF/animation/periodic JS timer created. CPU GetProcessTimes covers owned parent and known renderer, not GPU process or system power. Idle caret deliberately blurred. Opt-in refraction renderer not activated by this main-chat fixture.'}
    finally:
        for timer in (panel._web_publish_timer,panel._web_stream_timer,panel._web_start_timer,panel._web_timeout):timer.stop()
        panel.hide();panel._discard_partial_web();panel.deleteLater()
        app.sendPostedEvents(None,QEvent.Type.DeferredDelete);app.processEvents()


def main():
    output=ROOT/'artifacts/task-audit/glass-idle-readonly-20261106';output.mkdir(parents=True,exist_ok=False)
    before=fence();blocked=[]
    def deny(*a,**k):blocked.append('network');raise AssertionError('NETWORK_FORBIDDEN')
    try:
        with patch.object(socket,'getaddrinfo',deny),patch.object(socket.socket,'connect',deny),patch.object(socket.socket,'connect_ex',deny):result=run(output)
    except Exception as error:
        import traceback
        (output/'exception.log').write_text(traceback.format_exc(),encoding='utf8');result={'error_type':type(error).__name__}
    after=fence();result.update(source_before=before,source_after=after,source_unchanged=before==after,
        blocked_network_attempts=blocked,real_model_requests=0,system_clipboard_used=False,user_profile_used=False)
    result['measurement_completed']='phases' in result and before==after and not blocked
    (output/'verification.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    print(json.dumps(result,indent=2));return 0 if result['measurement_completed'] else 1


if __name__=='__main__':raise SystemExit(main())
